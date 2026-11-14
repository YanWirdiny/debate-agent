from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol, Self
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, Statement, Word)

class KnowledgeBase:
    @classmethod
    async def load(cls, db: Any, speaker_ids: list[str], topics: list[str]) -> Self:
        raise NotImplementedError

    def match_bank(self, speaker_id: str, claim: Claim) -> BankHit | None:
        raise NotImplementedError

    def search_passages(self, text: str, topic: str | None, k: int = 5) -> list[Passage]:
        raise NotImplementedError

    def search_statements(self, speaker_id: str, text: str, k: int = 3) -> list[Statement]:
        raise NotImplementedError

    def add_passages(self, topic: str, passages: list[Passage]) -> None:
        raise NotImplementedError

    def topic_for(self, text: str) -> str | None:
        raise NotImplementedError
