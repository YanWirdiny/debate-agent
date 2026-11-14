from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, Statement, Word)
from app.interfaces import KnowledgeBase, DeepgramStream

async def grounded_verdict(job: SlowJob, kb: KnowledgeBase) -> Annotation | None:
    raise NotImplementedError

async def prefetch(job: SlowJob) -> tuple[str, list[Passage]]:
    raise NotImplementedError

async def summarize(prev: str, turns: list[Segment]) -> str:
    raise NotImplementedError
