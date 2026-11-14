from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, Statement, Word)
from app.interfaces import KnowledgeBase, DeepgramStream

async def run_claims(seg: Segment, view: LaneContext, kb: KnowledgeBase, emit: Callable[[Annotation], None], slow_q: asyncio.Queue[SlowJob]) -> None:
    raise NotImplementedError

async def run_fallacy(seg: Segment, view: LaneContext, emit: Callable[[Annotation], None]) -> None:
    raise NotImplementedError

async def run_consistency(claim: Claim, seg: Segment, kb: KnowledgeBase, ledger: list[ClaimRecord], emit: Callable[[Annotation], None]) -> None:
    raise NotImplementedError
