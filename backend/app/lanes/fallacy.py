"""Fallacy lane (CONTEXT.md 5.4). One Groq call per speaker sentence, conservative by prompt and
by code: the span must be verbatim in the sentence; strawman needs a verbatim opponent span;
a fallacy flag is verified only when confident and the transcript is clean (STT guard)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.interfaces import Emit
from app.llm.client import FALLBACK_SLACK_S, MIN_FALLBACK_BUDGET_S, groq_json
from app.models import Annotation, ContractModel, LaneContext, Segment, annotation_id
from app.session import LANE_BUDGET_S, LANE_TIMEOUT_S
from app.verify import stt_safe

# groq_json holds MIN_FALLBACK_BUDGET_S + FALLBACK_SLACK_S back from Groq so Gemini can
# still start. _lane cancels the whole lane at LANE_TIMEOUT_S, so the call itself has to
# end that much sooner — otherwise the fallback window is the lane deadline and never runs.
FALLACY_LLM_RESERVE_S = MIN_FALLBACK_BUDGET_S + FALLBACK_SLACK_S
FALLACY_LLM_TIMEOUT_S = LANE_TIMEOUT_S - FALLACY_LLM_RESERVE_S

FallacyType = Literal["ad_hominem", "strawman", "false_dilemma", "slippery_slope", "red_herring",
                      "hasty_generalization", "whataboutism", "post_hoc"]
VERIFY_MIN_CONF = 0.80


class FallacyResult(ContractModel):
    has_fallacy: bool
    type: FallacyType | None
    span: str | None
    opponent_span: str | None
    explanation: str
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def complete(self):
        if self.has_fallacy and (not self.type or not self.span or not self.explanation.strip()):
            raise ValueError("a fallacy needs type, span and explanation")
        return self


def _text(segs: list[Segment]) -> str:
    return " ".join(s.text for s in segs)


async def run_fallacy(seg: Segment, view: LaneContext, emit: Emit) -> None:
    if seg.role == "moderator":
        return
    opponent = _text(view.opponent_turn)
    r = await groq_json("fallacy", {
        "speaker_digest": view.speaker_digest.model_dump(mode="json") if view.speaker_digest else None,
        "last_question": view.last_question.text if view.last_question else None,
        "opponent_turn": opponent,
        "speaker_turn": _text([s for s in view.speaker_turn if s.segment_id != seg.segment_id]),
        "sentence": seg.text,
    }, FallacyResult, timeout=LANE_BUDGET_S.get() - FALLACY_LLM_RESERVE_S)  # this session's lane budget
    f = FallacyResult.model_validate(r.data)
    if not f.has_fallacy:
        return
    assert f.span is not None and f.type is not None
    start = seg.text.find(f.span)
    if start < 0:
        raise ValueError("fallacy span is absent from transcript")  # invented span: operator sees pending
    opponent_span = f.opponent_span if f.opponent_span and f.opponent_span in opponent else None
    if f.type == "strawman" and opponent_span is None:
        return  # strawman requires the opponent's real words (CONTEXT.md section 3)
    emit(Annotation(
        annotation_id=annotation_id(seg.segment_id, "fallacy", 0), version=1, segment_id=seg.segment_id,
        speaker_id=seg.speaker_id, span_text=f.span, span_char_start=start, span_char_end=start + len(f.span),
        category="L", subtype=f.type, explanation=f.explanation.strip(), evidence=[],
        opponent_span=opponent_span, confidence=f.confidence,
        status="verified" if f.confidence >= VERIFY_MIN_CONF and stt_safe(seg, f.span) else "candidate",
        path="llm", lane="fallacy", latency_ms=0, model_id=r.model_id, prompt_hash=r.prompt_hash,
    ))
