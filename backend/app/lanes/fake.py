"""Fake lanes for pipeline testing (P1). Same signatures as the real lanes, no LLM, no KB.

- run_claims: flags the first number in a sentence as V (verified, public), with its ClaimRecord;
  a sentence with "caused" becomes a U candidate plus a VerdictJob on slow_q (exercises the async plane).
- run_fallacy: flags "every"/"never"/"always"/"either" as an L candidate (operator only).
- run_consistency: never flags.
- grounded_verdict: upgrades the U candidate to a verified F with web evidence.
- prefetch / summarize / warm_up / load_kb: trivial stand-ins for P2's async plane.
"""

from __future__ import annotations

import asyncio
import re
from typing import Callable

from app.interfaces import Emit, KnowledgeBase
from app.models import (Annotation, Claim, ClaimRecord, Evidence, LaneContext, Passage, Segment, SlowJob,
                        VerdictJob, annotation_id)

_NUMBER = re.compile(r"\b(?:\d[\d,.]*|zero|one|two|three|four|five|six|seven|eight|nine|ten)\b(?:\s+(?:percent|percentage points))?", re.IGNORECASE)
_ABSOLUTE = re.compile(r"\b(every|never|always|either)\b", re.IGNORECASE)
_CAUSAL = re.compile(r"\bcaused\b[^.?!]*", re.IGNORECASE)


def _evidence(quote: str, source: str = "fake") -> Evidence:
    return Evidence(source_id=source, title="Fake source", url=f"https://example.org/{source}", publisher="Fake",
                    reliability="primary", quote=quote, published=None, retrieved_at="1970-01-01")


def _annotation(seg: Segment, m: re.Match[str], **kw) -> Annotation:
    base = dict(
        version=1,
        segment_id=seg.segment_id,
        speaker_id=seg.speaker_id,
        span_text=m.group(0),
        span_char_start=m.start(),
        span_char_end=m.end(),
        subtype=None,
        evidence=[],
        opponent_span=None,
        latency_ms=0,
        model_id="fake",
        prompt_hash=None,
    )
    base.update(kw)
    return Annotation(**base)


async def run_claims(seg: Segment, view: LaneContext, kb: KnowledgeBase, emit: Emit, slow_q: asyncio.Queue[SlowJob]) -> None:
    await asyncio.sleep(0.2)
    if m := _CAUSAL.search(seg.text):
        aid = annotation_id(seg.segment_id, "claims", 1)
        claim = Claim(text=m.group(0), decontextualized=m.group(0))
        u = _annotation(seg, m, annotation_id=aid, category="U", explanation="Fake lane: needs a web check.",
                        confidence=0.5, status="candidate", path="retrieval", lane="claims")
        emit(ClaimRecord(claim=claim, speaker_id=seg.speaker_id, segment_id=seg.segment_id, annotation_id=aid))
        emit(u)
        slow_q.put_nowait(VerdictJob(claim=claim, segment=seg, annotation=u, speaker_digest=view.speaker_digest))
    m = _NUMBER.search(seg.text)
    if not m:
        return
    aid = annotation_id(seg.segment_id, "claims", 0)
    emit(ClaimRecord(claim=Claim(text=seg.text, decontextualized=seg.text, numbers=[m.group(0)]),
                     speaker_id=seg.speaker_id, segment_id=seg.segment_id, annotation_id=aid))
    emit(
        _annotation(
            seg,
            m,
            annotation_id=aid,
            category="V",
            explanation="Fake lane: matched a number.",
            evidence=[_evidence(m.group(0))],
            confidence=0.9,
            status="verified",
            path="bank",
            lane="claims",
        )
    )


async def run_fallacy(seg: Segment, view: LaneContext, emit: Emit) -> None:
    await asyncio.sleep(0.3)
    m = _ABSOLUTE.search(seg.text)
    if not m:
        return
    emit(
        _annotation(
            seg,
            m,
            annotation_id=annotation_id(seg.segment_id, "fallacy", 0),
            category="L",
            subtype="hasty_generalization",
            explanation="Fake lane: absolute word.",
            confidence=0.5,
            status="candidate",
            path="llm",
            lane="fallacy",
        )
    )


async def run_consistency(claim: Claim, seg: Segment, kb: KnowledgeBase, ledger: list[ClaimRecord], emit: Emit) -> None:
    return None


# --- async plane stand-ins (same signatures as P2's) ---------------------------------


async def grounded_verdict(job: SlowJob, kb: KnowledgeBase) -> Annotation | None:
    await asyncio.sleep(0.05)
    if not isinstance(job, VerdictJob):
        return None
    return job.annotation.model_copy(update={
        "category": "F", "status": "verified", "path": "web", "confidence": 0.85,
        "explanation": "Fake web verdict.", "evidence": [_evidence(job.claim.text, "fake-web")],
    })


async def prefetch(job: SlowJob) -> tuple[str, list[Passage]]:
    await asyncio.sleep(0.05)
    return (getattr(job, "topic", None) or "general", [])


async def summarize(prev: str, turns: list[Segment]) -> str:
    text = " ".join(f"{s.speaker_id}: {s.text}" for s in turns)
    return (prev + " " + text).strip()[-600:]


async def warm_up(kb: KnowledgeBase, speaker_ids: list[str]) -> None:
    return None


async def load_kb(db: object, speaker_ids: list[str], topics: list[str]) -> KnowledgeBase | None:
    return None
