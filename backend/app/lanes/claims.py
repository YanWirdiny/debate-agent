from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, Statement, Word)
from app.interfaces import Emit, KnowledgeBase, DeepgramStream

async def run_claims(seg: Segment, view: LaneContext, kb: KnowledgeBase, emit: Emit, slow_q: asyncio.Queue[SlowJob]) -> None:
    raise NotImplementedError
