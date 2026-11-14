"""Sync 1 claims: extraction and reviewed bank matches only. No retrieval verdicts."""
from __future__ import annotations
import asyncio
from pydantic import Field, model_validator
from app.interfaces import Emit, KnowledgeBase
from app.models import Annotation, Claim, ClaimRecord, ContractModel, LaneContext, Segment, SlowJob, annotation_id
from app.llm.client import groq_json
from app.verify import stt_safe, transcript_span


class ExtractedClaim(ContractModel):
    text: str = Field(min_length=1)
    decontextualized: str = Field(min_length=1)
    entities: list[str]
    numbers: list[str]
    topic: str | None


class Extraction(ContractModel):
    checkworthy: bool
    claims: list[ExtractedClaim] = Field(max_length=3)

    @model_validator(mode="after")
    def consistent(self):
        if self.checkworthy != bool(self.claims):
            raise ValueError("checkworthy must agree with claims")
        return self


async def run_claims(seg: Segment, view: LaneContext, kb: KnowledgeBase, emit: Emit, slow_q: asyncio.Queue[SlowJob]) -> None:
    if seg.role == "moderator":
        return
    if kb is None:
        raise ValueError("claims lane requires a real KB")
    result = await groq_json("claim_extract", {
        "sentence": seg.text,
        "recent_segments": [s.text for s in view.recent_segments[-3:]],
        "last_question": view.last_question.text if view.last_question else None,
        "rolling_summary": view.rolling_summary,
        "speaker_digest": view.speaker_digest.model_dump(mode="json") if view.speaker_digest else None,
    }, Extraction)
    extracted = Extraction.model_validate(result.data)
    claims = [Claim.model_validate(c.model_dump()) for c in extracted.claims]
    # Invalid extracted spans invalidate the result; never silently repair an invented span.
    if any(c.text not in seg.text for c in claims):
        raise ValueError("extracted claim span is absent from transcript")

    def match_all():
        embed = getattr(kb, "embed_claims", None)
        embedded = embed(claims) if embed else claims
        return [kb.match_bank(seg.speaker_id, c) for c in embedded]

    hits = await asyncio.to_thread(match_all)
    for n, (claim, hit) in enumerate(zip(claims, hits)):
        aid = annotation_id(seg.segment_id, "claims", n) if hit else None
        # Until P1 dedupe checks semantic equivalence, omit embedding so it uses text.
        emit(ClaimRecord(claim=claim, speaker_id=seg.speaker_id, segment_id=seg.segment_id, annotation_id=aid))
        if hit is None:
            continue  # Unknown is not a verified verdict. Retrieval and slow jobs are Sync 2+.
        start = seg.text.index(claim.text)
        if not transcript_span(seg, claim.text, start, start + len(claim.text)):
            raise ValueError("invalid transcript span")
        emit(Annotation(
            annotation_id=aid, version=1, segment_id=seg.segment_id, speaker_id=seg.speaker_id,
            span_text=claim.text, span_char_start=start, span_char_end=start + len(claim.text),
            category=hit.verdict_category, subtype=None, explanation=hit.explanation,
            evidence=hit.evidence, opponent_span=None,
            # This is reviewed-bank provenance confidence, never embedding similarity.
            confidence=.9, status="verified" if stt_safe(seg, claim.text) else "candidate",
            path="bank", lane="claims", latency_ms=0, model_id=result.model_id, prompt_hash=result.prompt_hash,
        ))
