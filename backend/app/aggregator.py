"""Aggregator (P1): sole owner of annotation state; gates, versions, routing. Never calls LLMs or DB.

- every change is a new `version` and an event (`annotation` first, then `annotation_update`)
- operator gets every version; audience gets a flag the first time `is_public()` is true,
  updates while it stays public, and a removal when it stops being public
- killed is final: later lane or worker updates for that id are ignored
- dedupe (CONTEXT.md 5.6): a new claims-lane flag for the same speaker and same claim (same
  quantities and negation, AND cosine >= 0.9 on claim embeddings or same normalized claim text) as an earlier live flag of the same category is
  not shown again; the earlier flag gets `repeat_count + 1`. Needs the lane's ClaimRecord (with
  `annotation_id`) to be emitted no later than the annotation itself.
- replay mode (`preserve=True`): versions and latencies come from the recorded log unchanged
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from typing import Any, Protocol

from pydantic import BaseModel

from app.config import Settings
from app.metrics import Metrics
from app.models import Annotation, AnnotationMessage, AnnotationUpdateMessage, ClaimRecord, RemovalMessage, Segment

log = logging.getLogger(__name__)

# Default only; SessionRunner passes settings.audience_min_conf. Read from the field so we never
# build Settings() here (it raises when API keys are missing, e.g. in tests or replay).
DEFAULT_AUDIENCE_MIN_CONF: float = Settings.model_fields["audience_min_conf"].default


class _Broadcaster(Protocol):
    async def broadcast(self, message: BaseModel) -> None: ...


class _EventSink(Protocol):
    def put(self, type: str, payload: BaseModel | dict[str, Any]) -> None: ...


class _Null:
    async def broadcast(self, message: BaseModel) -> None:
        return None

    def put(self, type: str, payload: BaseModel | dict[str, Any]) -> None:
        return None


_STOP = object()
DEDUPE_COSINE = 0.9


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _norm(text: str) -> str:
    return re.sub(r"\s+%", "%", re.sub(r"[^a-z0-9%]+", " ", text.lower())).strip()


_NEGATION = re.compile(r"\b(?:not|no|never|none|nobody|nothing|neither|nor|without|n't)\b|n't\b")
_QUANTITY = re.compile(r"\d+(?:[.,]\d+)*\s*(?:%|percent(?:age points?)?|trillion|billion|million|thousand)?")


def _guards(text: str) -> tuple[list[str], int]:
    """Quantities (with units) and negation count: embeddings place '3%' near '5%' and 'did' near
    'did not', so cosine alone must never merge them (P2_SYNC1.md)."""
    t = text.lower().replace("’", "'")
    quantities = [re.sub(r"[\s,]+", "", q) for q in _QUANTITY.findall(t)]
    return quantities, len(_NEGATION.findall(t))


def same_claim(a: ClaimRecord, b: ClaimRecord) -> bool:
    if a.speaker_id != b.speaker_id:
        return False
    ta, tb = a.claim.decontextualized or a.claim.text, b.claim.decontextualized or b.claim.text
    if _guards(ta) != _guards(tb):
        return False
    if a.claim.embedding and b.claim.embedding:
        return _cosine(a.claim.embedding, b.claim.embedding) >= DEDUPE_COSINE
    return _norm(ta) == _norm(tb)


class Aggregator:
    def __init__(
        self,
        audience_min_conf: float = DEFAULT_AUDIENCE_MIN_CONF,
        *,
        operator: _Broadcaster | None = None,
        audience: _Broadcaster | None = None,
        writer: _EventSink | None = None,
        metrics: Metrics | None = None,
        preserve: bool = False,
    ) -> None:
        self.audience_min_conf = audience_min_conf
        self.operator = operator or _Null()
        self.audience = audience or _Null()
        self.writer = writer or _Null()
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.state: dict[str, Annotation] = {}
        self.public: set[str] = set()
        self.t0_wall: float | None = None
        self.speed = 1.0  # replay speed; wall time of clip t = t0_wall + t / speed
        self.metrics = metrics or Metrics()
        self.preserve = preserve
        self.duplicates: dict[str, str] = {}  # duplicate annotation_id -> original annotation_id
        self._seg_end: dict[str, float] = {}
        self._claims: dict[str, ClaimRecord] = {}  # annotation_id -> claim behind it

    # --- inputs (all non-blocking) ---

    def register_segment(self, seg: Segment) -> None:
        self._seg_end[seg.segment_id] = seg.t_end

    def register_claim(self, record: ClaimRecord) -> None:
        """Synchronous (not queued) so the claim is known before its annotation is applied."""
        self.metrics.record_claim()
        if record.annotation_id:
            self._claims[record.annotation_id] = record

    def submit(self, a: Annotation) -> None:
        """P1's `emit` for annotations."""
        self.queue.put_nowait(a)

    def kill(self, annotation_id: str, by: str) -> None:
        self.queue.put_nowait(("kill", annotation_id, by))

    async def stop(self) -> None:
        self.queue.put_nowait(_STOP)

    # --- loop ---

    async def run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is _STOP:
                return
            try:
                if isinstance(item, tuple):
                    await self._kill(item[1], item[2])
                else:
                    await self._apply(item)
            except Exception:
                log.exception("aggregator failed on %r", item)

    async def _kill(self, annotation_id: str, by: str) -> None:
        prev = self.state.get(annotation_id)
        if prev is None or prev.status == "killed":
            return
        self.writer.put("kill", {"annotation_id": annotation_id, "by": by})
        await self._apply(prev.model_copy(update={"status": "killed"}), from_kill=True)

    async def _apply(self, a: Annotation, from_kill: bool = False) -> None:
        if a.annotation_id in self.duplicates:
            return  # later versions of a deduped flag (e.g. an async verdict) are dropped
        prev = self.state.get(a.annotation_id)
        if prev is not None and prev.status == "killed":
            return
        if prev is None and not self.preserve and (orig := self._duplicate_of(a)) is not None:
            self.duplicates[a.annotation_id] = orig.annotation_id
            log.info("dedupe: %s repeats %s", a.annotation_id, orig.annotation_id)
            await self._apply(orig.model_copy(update={"repeat_count": orig.repeat_count + 1}), from_kill=True)
            return
        if not self.preserve:
            a = a.model_copy(
                update={
                    "version": prev.version + 1 if prev else 1,
                    "latency_ms": a.latency_ms if from_kill else self._latency_ms(a.segment_id),
                }
            )
        self.state[a.annotation_id] = a
        self.metrics.record_annotation(a)
        if prev is None:
            self.writer.put("annotation", a)
            await self.operator.broadcast(AnnotationMessage(data=a))
        else:
            self.writer.put("annotation_update", a)
            await self.operator.broadcast(AnnotationUpdateMessage(data=a))
        await self._route_audience(a)

    async def _route_audience(self, a: Annotation) -> None:
        was_public = a.annotation_id in self.public
        now_public = self.is_public(a)
        if now_public and not was_public:
            self.public.add(a.annotation_id)
            await self.audience.broadcast(AnnotationMessage(data=a))
        elif now_public and was_public:
            await self.audience.broadcast(AnnotationUpdateMessage(data=a))
        elif was_public:
            self.public.discard(a.annotation_id)
            status = "killed" if a.status == "killed" else "withdrawn"
            await self.audience.broadcast(RemovalMessage(annotation_id=a.annotation_id, status=status))

    def _duplicate_of(self, a: Annotation) -> Annotation | None:
        claim = self._claims.get(a.annotation_id)
        if a.lane != "claims" or claim is None:
            return None
        for aid, other in self._claims.items():
            prev = self.state.get(aid)
            if (aid != a.annotation_id and prev is not None and prev.category == a.category
                    and prev.status in ("candidate", "verified") and same_claim(claim, other)):
                return prev
        return None

    def _latency_ms(self, segment_id: str) -> int:
        t_end = self._seg_end.get(segment_id)
        if self.t0_wall is None or t_end is None:
            return 0
        return max(0, int((time.time() - (self.t0_wall + t_end / self.speed)) * 1000))

    # --- gate ---

    def is_public(self, a: Annotation) -> bool:
        """Audience gate. CONTEXT.md section 2 rule 3, ARCHITECTURE.md section 6."""
        if a.status != "verified" or a.confidence < self.audience_min_conf:
            return False
        if a.category == "U":
            return False
        if a.category == "L":
            return bool(a.span_text) and (a.subtype != "strawman" or bool(a.opponent_span))
        return len(a.evidence) > 0 and all(e.quote for e in a.evidence)
