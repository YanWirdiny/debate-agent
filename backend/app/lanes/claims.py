"""Claims lane (CONTEXT.md 5.3, ARCHITECTURE.md section 3).

Per sentence: one extraction call. Per claim, in parallel:
- reviewed bank match -> verdict from the bank, no LLM call (Sync 1, unchanged)
- no match -> top-5 passages (active topic first) -> one `stance` call. The quote must be found
  verbatim in the cited passage, or the result degrades to U.
- no passages, insufficient, or failed verification -> U candidate (operator only) + VerdictJob on
  slow_q; the async worker may upgrade it later with a grounded verdict.
- consistency (Lane D) against prior statements and this session's ledger, alongside stance.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from pydantic import Field, model_validator

from app.interfaces import Emit, KnowledgeBase
from app.lanes.consistency import run_consistency
from app.llm.client import LLMError, groq_json
from app.models import (Annotation, Category, Claim, ClaimRecord, ContractModel, Evidence, LaneContext, Passage,
                        Segment, SlowJob, VerdictJob, annotation_id)
from app.session import LANE_TIMEOUT_S
from app.verify import evidence_span, stt_safe, transcript_span

log = logging.getLogger(__name__)

# Embed + match_bank run only after extraction returns, and _lane cancels the whole
# lane at LANE_TIMEOUT_S. Leave that tail free so a slow model call cannot eat it.
CLAIMS_LLM_RESERVE_S = 0.4
CLAIMS_LLM_TIMEOUT_S = LANE_TIMEOUT_S - CLAIMS_LLM_RESERVE_S
# Stance and NLI share what is left of the lane after extraction. Below MIN_CALL_S a call is
# not started: the claim ships as U at once (async worker) instead of the lane being cancelled.
LANE_TAIL_S = 0.08
MIN_CALL_S = 0.25

STANCE_K = 5
VERDICT_CATEGORY: dict[str, Category] = {
    "supported": "V", "contradicted": "F", "misleading": "M", "data_misuse": "D", "insufficient": "U",
}
U_CONFIDENCE = 0.3

log = logging.getLogger(__name__)

# Embed + match_bank run only after extraction returns, and _lane cancels the whole
# lane at LANE_TIMEOUT_S. Leave that tail free so a slow model call cannot eat it.
CLAIMS_LLM_RESERVE_S = 0.4
CLAIMS_LLM_TIMEOUT_S = LANE_TIMEOUT_S - CLAIMS_LLM_RESERVE_S


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


class Stance(ContractModel):
    verdict: Literal["supported", "contradicted", "misleading", "data_misuse", "insufficient"]
    passage_id: str | None
    quote: str
    explanation: str
    confidence: float = Field(ge=0, le=1)


def stance_evidence(stance: Stance, passages: list[Passage]) -> Evidence | None:
    """Code-side verification (never trust the model): the cited passage exists and contains the
    quote verbatim (whitespace/quote-mark normalized). Returns the evidence or None."""
    if stance.verdict == "insufficient" or not stance.passage_id or not stance.quote.strip():
        return None
    passage = next((p for p in passages if p.passage_id == stance.passage_id), None)
    if passage is None:
        return None
    evidence = passage.source.model_copy(update={"quote": stance.quote.strip()})
    return evidence if evidence_span(evidence, passage.text) else None


async def run_claims(seg: Segment, view: LaneContext, kb: KnowledgeBase, emit: Emit, slow_q: asyncio.Queue[SlowJob]) -> None:
    if seg.role == "moderator":
        return
    if kb is None:
        raise ValueError("claims lane requires a real KB")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LANE_TIMEOUT_S - LANE_TAIL_S
    result = await groq_json("claim_extract", {
        "sentence": seg.text,
        "recent_segments": [s.text for s in view.recent_segments[-3:]],
        "last_question": view.last_question.text if view.last_question else None,
        "rolling_summary": view.rolling_summary,
        "speaker_digest": view.speaker_digest.model_dump(mode="json") if view.speaker_digest else None,
    }, Extraction, timeout=CLAIMS_LLM_TIMEOUT_S)
    extracted = Extraction.model_validate(result.data)
    claims = [Claim.model_validate(c.model_dump()) for c in extracted.claims]
    # Drop an invented span; never rewrite it into the transcript. If every extracted
    # span is absent, fail the lane so the segment stays pending.
    kept: list[Claim] = []
    dropped = 0
    for claim in claims:
        if claim.text not in seg.text:
            dropped += 1
            continue
        kept.append(claim)
    if dropped:
        # Count only. Span text is transcript content and is not logged.
        log.warning("dropped %d extracted claim span(s) absent from the transcript", dropped)
    if claims and not kept:
        raise ValueError("extracted claim span is absent from transcript")
    claims = kept
    if not claims:
        return

    def match_all():
        embed = getattr(kb, "embed_claims", None)
        embedded = embed(claims) if embed else claims
        return embedded, [kb.match_bank(seg.speaker_id, c) for c in embedded]

    embedded, hits = await asyncio.to_thread(match_all)
    ledger = list(view.claim_ledger)  # before this sentence's claims
    jobs = []
    for n, (claim, hit) in enumerate(zip(embedded, hits)):
        aid = annotation_id(seg.segment_id, "claims", n)
        # P1 dedupe now guards quantities and negation, so the embedding can travel with the record.
        emit(ClaimRecord(claim=claim, speaker_id=seg.speaker_id, segment_id=seg.segment_id, annotation_id=aid))
        start = seg.text.index(claim.text)
        if not transcript_span(seg, claim.text, start, start + len(claim.text)):
            raise ValueError("invalid transcript span")
        base = dict(annotation_id=aid, version=1, segment_id=seg.segment_id, speaker_id=seg.speaker_id,
                    span_text=claim.text, span_char_start=start, span_char_end=start + len(claim.text),
                    subtype=None, opponent_span=None, lane="claims", latency_ms=0)
        if hit is not None:
            emit(Annotation(
                **base, category=hit.verdict_category, explanation=hit.explanation, evidence=hit.evidence,
                # This is reviewed-bank provenance confidence, never embedding similarity.
                confidence=.9, status="verified" if stt_safe(seg, claim.text) else "candidate",
                path="bank", model_id=result.model_id, prompt_hash=result.prompt_hash,
            ))
        else:
            jobs.append(_retrieve(seg, claim, base, view, kb, emit, slow_q, deadline))
        jobs.append(_consistency(claim, seg, kb, ledger, emit, deadline))
    await asyncio.gather(*jobs)


def _left(deadline: float) -> float:
    return deadline - asyncio.get_running_loop().time()


async def _consistency(claim: Claim, seg: Segment, kb: KnowledgeBase, ledger: list[ClaimRecord], emit: Emit,
                       deadline: float) -> None:
    if _left(deadline) < MIN_CALL_S:
        return
    try:
        async with asyncio.timeout(_left(deadline)):
            await run_consistency(claim, seg, kb, ledger, emit)
    except (LLMError, TimeoutError):
        log.warning("consistency check skipped or failed on %s", seg.segment_id)  # stance result still ships


async def _retrieve(seg: Segment, claim: Claim, base: dict, view: LaneContext, kb: KnowledgeBase,
                    emit: Emit, slow_q: asyncio.Queue[SlowJob], deadline: float) -> None:
    passages = await asyncio.to_thread(kb.search_passages, claim.decontextualized, view.active_topic, STANCE_K)
    stance, model_id, prompt_hash, evidence = None, None, None, None
    if passages and _left(deadline) >= MIN_CALL_S:
        try:
            r = await groq_json("stance", {
                "claim": claim.decontextualized,
                "passages": [{"passage_id": p.passage_id, "publisher": p.source.publisher,
                              "published": p.source.published, "text": p.text} for p in passages],
            }, Stance, timeout=_left(deadline))
            stance, model_id, prompt_hash = Stance.model_validate(r.data), r.model_id, r.prompt_hash
            evidence = stance_evidence(stance, passages)
        except LLMError:
            log.warning("stance call failed on %s; claim goes to the async worker as U", seg.segment_id)
        except ValueError:  # unusable stance output, or the deadline passed as the call started
            log.warning("stance unusable on %s; claim goes to the async worker as U", seg.segment_id)
    if stance is not None and evidence is not None:
        emit(Annotation(
            **base, category=VERDICT_CATEGORY[stance.verdict], explanation=stance.explanation.strip(),
            evidence=[evidence], confidence=stance.confidence,
            status="verified" if stt_safe(seg, claim.text) else "candidate",
            path="retrieval", model_id=model_id, prompt_hash=prompt_hash,
        ))
        return
    # No usable passage: U, operator only, and the async worker tries a grounded verdict.
    u = Annotation(**base, category="U", explanation="No supporting source found yet; checking further.",
                   evidence=[], confidence=U_CONFIDENCE, status="candidate", path="retrieval",
                   model_id=model_id, prompt_hash=prompt_hash)
    emit(u)
    slow_q.put_nowait(VerdictJob(claim=claim, segment=seg, annotation=u, speaker_digest=view.speaker_digest))
