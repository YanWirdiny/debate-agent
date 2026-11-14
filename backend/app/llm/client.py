from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, Statement, Word)
from app.interfaces import KnowledgeBase, DeepgramStream

async def groq_json(prompt_id: str, variables: dict, schema: type[BaseModel], timeout: float = 1.2) -> LLMResult:
    raise NotImplementedError

async def gemini_json(prompt_id: str, variables: dict, schema: type[BaseModel], grounded: bool = False, cache_name: str | None = None, timeout: float = 20.0) -> LLMResult:
    raise NotImplementedError
