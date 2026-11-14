"""Fake lanes never flag moderator segments."""

import asyncio

from app.lanes import fake
from app.models import Annotation, LaneContext, Role, Segment, SlowJob

# Matches both the claims number pattern and the fallacy absolute-word pattern.
_TRIGGER = "Every plan costs 10 percent and never works."


def _segment(role: Role) -> Segment:
    return Segment(
        segment_id="fixture:mod",
        session_id="fixture",
        speaker_id="moderator" if role == "moderator" else "speaker_a",
        role=role,
        text=_TRIGGER,
        t_start=0.0,
        t_end=1.0,
        stt_confidence=0.99,
    )


async def _emit(seg: Segment) -> list[Annotation]:
    out: list[Annotation] = []
    slow_q: asyncio.Queue[SlowJob] = asyncio.Queue()
    await fake.run_claims(seg, LaneContext(), None, out.append, slow_q)  # type: ignore[arg-type]
    await fake.run_fallacy(seg, LaneContext(), out.append)
    return out


async def test_fake_lanes_emit_nothing_for_moderator():
    assert await _emit(_segment("moderator")) == []
    lanes = {annotation.lane for annotation in await _emit(_segment("speaker"))}
    assert lanes == {"claims", "fallacy"}
