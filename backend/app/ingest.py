from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, Statement, Word)
from app.interfaces import KnowledgeBase, DeepgramStream

class AudioPump:
    async def run(self, clip: Clip, dg: DeepgramStream, t0_wall: float) -> None:
        raise NotImplementedError

class TimelineLookup:
    def who(self, t_mid: float) -> tuple[str, Literal["speaker", "moderator"]]:
        raise NotImplementedError

async def replay_transcript(clip: Clip, t0_wall: float) -> None:
    raise NotImplementedError
