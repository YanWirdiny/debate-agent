from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, Statement, Word)
from app.interfaces import KnowledgeBase, DeepgramStream

from app.models import SpeakerDigest
from collections import deque

class SessionContext:
    session_id: str
    participants: dict[str, SpeakerDigest]
    active_topic: str | None
    last_question: Segment | None
    turns: dict[str, deque[Segment]]
    current_turn_speaker: str | None
    rolling_summary: str
    claim_ledger: dict[str, list[ClaimRecord]]

    def view_for(self, seg: Segment, lane: str) -> LaneContext:
        raise NotImplementedError

class SessionRunner:
    def __init__(self, session_id: str, clip_id: str) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        raise NotImplementedError
