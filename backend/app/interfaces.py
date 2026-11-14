from __future__ import annotations
import asyncio
from typing import Any, Callable, Literal, Protocol, Self
from pydantic import BaseModel
from app.models import (Annotation, BankHit, Claim, ClaimRecord, Clip, LaneContext,
                        LLMResult, Passage, Segment, SlowJob, SpeakerDigest, Statement, Word)

# Lanes push results through `emit`; it never blocks (P1: put_nowait).
#   Annotation  -> aggregator
#   ClaimRecord -> SessionContext.claim_ledger (read by the consistency lane via LaneContext)
Emit = Callable[[Annotation | ClaimRecord], None]

class DeepgramStream(Protocol):
    async def send(self, data: bytes) -> None:
        raise NotImplementedError

class KnowledgeBase(Protocol):
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

    def digest(self, speaker_id: str) -> SpeakerDigest | None:
        raise NotImplementedError

async def warm_up(kb: KnowledgeBase, speaker_ids: list[str]) -> None:
    """P2: load embedding model, one dummy call per hot prompt, create Gemini session cache.
    Called by P1's SessionRunner during warm-up, before `ready`."""
    raise NotImplementedError

async def run_claims(seg: Segment, view: LaneContext, kb: KnowledgeBase, emit: Emit, slow_q: asyncio.Queue[SlowJob]) -> None:
    raise NotImplementedError

async def run_fallacy(seg: Segment, view: LaneContext, emit: Emit) -> None:
    raise NotImplementedError

async def run_consistency(claim: Claim, seg: Segment, kb: KnowledgeBase, ledger: list[ClaimRecord], emit: Emit) -> None:
    raise NotImplementedError

async def grounded_verdict(job: SlowJob, kb: KnowledgeBase) -> Annotation | None:
    raise NotImplementedError

async def prefetch(job: SlowJob) -> tuple[str, list[Passage]]:
    raise NotImplementedError

async def summarize(prev: str, turns: list[Segment]) -> str:
    raise NotImplementedError

async def groq_json(prompt_id: str, variables: dict, schema: type[BaseModel], timeout: float = 1.2) -> LLMResult:
    raise NotImplementedError

async def gemini_json(prompt_id: str, variables: dict, schema: type[BaseModel], grounded: bool = False, cache_name: str | None = None, timeout: float = 20.0) -> LLMResult:
    raise NotImplementedError

class AudioPump(Protocol):
    async def run(self, clip: Clip, dg: DeepgramStream, t0_wall: float) -> None:
        raise NotImplementedError

class TimelineLookup(Protocol):
    def who(self, t_mid: float) -> tuple[str, Literal["speaker", "moderator"]]:
        raise NotImplementedError

class Segmenter(Protocol):
    def on_final(self, words: list[Word]) -> list[Segment]:
        raise NotImplementedError

class Aggregator(Protocol):
    async def run(self) -> None:
        raise NotImplementedError

    def is_public(self, a: Annotation) -> bool:
        raise NotImplementedError

    def kill(self, annotation_id: str, by: str) -> None:
        raise NotImplementedError

class EventWriter(Protocol):
    async def run(self) -> None:
        raise NotImplementedError
