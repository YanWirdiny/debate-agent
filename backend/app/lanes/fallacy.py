from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, Statement, Word)
from app.interfaces import Emit, KnowledgeBase, DeepgramStream

async def run_fallacy(seg: Segment, view: LaneContext, emit: Emit) -> None:
    raise NotImplementedError
