from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, Statement, Word)
from app.interfaces import KnowledgeBase, DeepgramStream

class Aggregator:
    async def run(self) -> None:
        raise NotImplementedError

    def is_public(self, a: Annotation) -> bool:
        raise NotImplementedError

    def kill(self, annotation_id: str, by: str) -> None:
        raise NotImplementedError
