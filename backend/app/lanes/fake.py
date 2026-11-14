"""Fake lanes for pipeline testing (P1). Same signatures as the real lanes, no LLM, no KB.

- run_claims: flags the first number in a sentence as V (verified, public).
- run_fallacy: flags "every"/"never"/"always"/"either" as an L candidate (operator only).
- run_consistency: never flags.
- Moderator segments (role == "moderator") are never flagged.
"""

from __future__ import annotations

import asyncio
import re
from typing import Callable

from app.interfaces import Emit, KnowledgeBase
from app.models import Annotation, Claim, ClaimRecord, Evidence, LaneContext, Segment, SlowJob, annotation_id

_NUMBER = re.compile(r"\b(?:\d[\d,.]*|zero|one|two|three|four|five|six|seven|eight|nine|ten)\b(?:\s+(?:percent|percentage points))?", re.IGNORECASE)
_ABSOLUTE = re.compile(r"\b(every|never|always|either)\b", re.IGNORECASE)


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
    if seg.role == "moderator":
        return
    await asyncio.sleep(0.2)
    m = _NUMBER.search(seg.text)
    if not m:
        return
    emit(
        _annotation(
            seg,
            m,
            annotation_id=annotation_id(seg.segment_id, "claims", 0),
            category="V",
            explanation="Fake lane: matched a number.",
            evidence=[
                Evidence(
                    source_id="fake",
                    title="Fake source",
                    url="https://example.org/fake",
                    publisher="Fake",
                    reliability="primary",
                    quote=m.group(0),
                    published=None,
                    retrieved_at="1970-01-01",
                )
            ],
            confidence=0.9,
            status="verified",
            path="bank",
            lane="claims",
        )
    )


async def run_fallacy(seg: Segment, view: LaneContext, emit: Emit) -> None:
    if seg.role == "moderator":
        return
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
