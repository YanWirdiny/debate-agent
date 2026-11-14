"""SessionContext and SessionRunner (P1). ARCHITECTURE.md sections 2 and 4.

Single-writer rule: the segmenter path (`on_segment`) and the summarizer mutate the context;
lanes only read frozen `view_for()` snapshots. Claim records arrive through `emit`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import ingest
from app.aggregator import DEFAULT_AUDIENCE_MIN_CONF, Aggregator
from app.eventlog import EventSink, EventWriter
from app.hubs import Hub
from app.interfaces import KnowledgeBase
from app.lanes import fake
from app.models import (
    Annotation,
    BannerMessage,
    ClaimRecord,
    Clip,
    LaneContext,
    ReadyMessage,
    Segment,
    SegmentMessage,
    SlowJob,
    SpeakerDigest,
    StartedMessage,
    Word,
)
from app.segmenter import Segmenter

log = logging.getLogger(__name__)

TURN_HISTORY = 8


class SessionContext:
    def __init__(self, session_id: str, participants: dict[str, SpeakerDigest] | None = None) -> None:
        self.session_id = session_id
        self.participants: dict[str, SpeakerDigest] = participants or {}
        self.active_topic: str | None = None
        self.last_question: Segment | None = None
        self.turns: dict[str, deque[Segment]] = {}
        self.current_turn_speaker: str | None = None
        self.rolling_summary = ""
        self.claim_ledger: dict[str, list[ClaimRecord]] = {}
        self._current_turn: list[Segment] = []
        self._completed_turns: deque[tuple[str, list[Segment]]] = deque(maxlen=TURN_HISTORY)

    # --- writes (segmenter path) ---

    def on_segment(self, seg: Segment) -> None:
        if seg.speaker_id != self.current_turn_speaker:
            if self.current_turn_speaker is not None and self._current_turn:
                self._completed_turns.append((self.current_turn_speaker, self._current_turn))
            self.current_turn_speaker = seg.speaker_id
            self._current_turn = []
        self._current_turn.append(seg)
        if seg.role == "moderator":
            if seg.text.rstrip().endswith("?"):
                self.last_question = seg
            return
        self.turns.setdefault(seg.speaker_id, deque(maxlen=TURN_HISTORY)).append(seg)

    def add_claim(self, record: ClaimRecord) -> None:
        self.claim_ledger.setdefault(record.speaker_id, []).append(record)

    # --- reads ---

    def opponent_turn(self, speaker_id: str) -> list[Segment]:
        for spk, turn in reversed(self._completed_turns):
            if spk != speaker_id and turn and turn[0].role == "speaker":
                return list(turn)
        return []

    def view_for(self, seg: Segment, lane: str) -> LaneContext:
        """Only what each lane's prompt needs (ARCHITECTURE.md section 4). Copies, never live refs."""
        digest = self.participants.get(seg.speaker_id)
        if lane == "claims":
            before = [s for s in self.turns.get(seg.speaker_id, ()) if s.segment_id != seg.segment_id]
            return LaneContext(
                active_topic=self.active_topic,
                last_question=self.last_question,
                recent_segments=before[-3:],
                rolling_summary=self.rolling_summary,
                speaker_digest=digest,
            )
        if lane == "fallacy":
            turn = self._current_turn if self.current_turn_speaker == seg.speaker_id else [seg]
            return LaneContext(
                active_topic=self.active_topic,
                last_question=self.last_question,
                speaker_turn=list(turn),
                opponent_turn=self.opponent_turn(seg.speaker_id),
                speaker_digest=digest,
            )
        if lane == "consistency":
            return LaneContext(claim_ledger=list(self.claim_ledger.get(seg.speaker_id, [])))
        raise ValueError(f"unknown lane {lane!r}")


ClaimsFn = Callable[..., Awaitable[None]]
FallacyFn = Callable[..., Awaitable[None]]


class SessionRunner:
    """One live session: transcript source -> segmenter -> context -> lanes -> aggregator -> hubs/log.

    Sources: `replay` (REPLAY_TRANSCRIPT=1 and the mock stream: transcript words at 1x).
    Deepgram streaming is the next slice.
    """

    def __init__(
        self,
        session_id: str,
        clip_id: str,
        *,
        clip: Clip | None = None,
        operator: Hub | None = None,
        audience: Hub | None = None,
        kb: KnowledgeBase | None = None,
        run_claims: ClaimsFn = fake.run_claims,
        run_fallacy: FallacyFn = fake.run_fallacy,
        sink: EventSink | None = None,
        fallback_path: Path | None = None,
        audience_min_conf: float = DEFAULT_AUDIENCE_MIN_CONF,
        lane_concurrency: int = 8,
        lane_timeout: float = 1.2,
        speed: float = 1.0,
    ) -> None:
        self.session_id = session_id
        self.clip_id = clip_id
        self.clip = clip or ingest.load_clip(clip_id)
        self.operator = operator or Hub("operator")
        self.audience = audience or Hub("audience")
        self.kb = kb
        self.run_claims = run_claims
        self.run_fallacy = run_fallacy
        self.lane_timeout = lane_timeout
        self.speed = speed
        self.ctx = SessionContext(session_id)
        self.writer = EventWriter(session_id, sink=sink, fallback_path=fallback_path)
        self.aggregator = Aggregator(audience_min_conf, operator=self.operator, audience=self.audience, writer=self.writer)
        self.segmenter = Segmenter(session_id)  # replay words carry their own speaker/role
        self.slow_q: asyncio.Queue[SlowJob] = asyncio.Queue()
        self.lane_sem = asyncio.Semaphore(lane_concurrency)
        self.t0_wall: float | None = None
        self.ready = False
        self.lane_timeouts = 0
        self._tasks: list[asyncio.Task[Any]] = []
        self._lane_tasks: set[asyncio.Task[Any]] = set()
        self._done = asyncio.Event()
        self._finishing = False

    # --- lifecycle ---

    async def warm_up(self) -> None:
        """Phase 3 adds KB load, embedding model, P2 warm_up(). For now: validate the clip."""
        if self.clip.transcript_path is None:
            raise ValueError(f"clip {self.clip_id} has no transcript_path (needed for replay)")
        self._words = ingest.load_transcript_words(self.clip.transcript_path)
        self.ready = True
        await self.operator.broadcast(ReadyMessage(session_id=self.session_id))

    async def start(self, lead_s: float = 0.3) -> float:
        """Starts the source and long-lived tasks; returns t0_wall (clip t=0 in wall time)."""
        if not self.ready:
            await self.warm_up()
        self.t0_wall = time.time() + lead_s
        self.aggregator.t0_wall = self.t0_wall
        self.aggregator.speed = self.speed
        self.writer.put("session_start", {"session_id": self.session_id, "clip_id": self.clip_id, "t0_wall": self.t0_wall})
        started = StartedMessage(t0_wall=self.t0_wall)
        await asyncio.gather(self.operator.broadcast(started), self.audience.broadcast(started))
        await self.operator.broadcast(BannerMessage(level="info", text=f"Replay mode: pre-made transcript at {self.speed:g}x"))
        self._tasks = [
            asyncio.create_task(self.writer.run(), name="writer"),
            asyncio.create_task(self.aggregator.run(), name="aggregator"),
            asyncio.create_task(self._source(), name="source"),
        ]
        return self.t0_wall

    async def wait(self) -> None:
        await self._done.wait()

    async def stop(self) -> None:
        for t in self._tasks:
            if t.get_name() == "source" and not t.done():
                t.cancel()
        await self._finish()

    async def _finish(self) -> None:
        if self._finishing:
            await self._done.wait()
            return
        self._finishing = True
        if self._lane_tasks:
            await asyncio.gather(*self._lane_tasks, return_exceptions=True)
        await self.aggregator.stop()
        self.writer.put("session_end", {"session_id": self.session_id})
        await self.writer.stop()
        await asyncio.gather(*(t for t in self._tasks if t.get_name() != "source"), return_exceptions=True)
        self._done.set()

    # --- hot path ---

    async def _source(self) -> None:
        assert self.t0_wall is not None
        try:
            await ingest.replay_words(self._words, self.t0_wall, self.on_words, speed=self.speed)
            for seg in self.segmenter.flush():
                await self.on_segment(seg)
        finally:
            asyncio.get_running_loop().create_task(self._finish())

    async def on_words(self, words: list[Word]) -> None:
        for seg in self.segmenter.on_final(words):
            await self.on_segment(seg)

    async def on_segment(self, seg: Segment) -> None:
        self.ctx.on_segment(seg)
        self.aggregator.register_segment(seg)
        self.writer.put("segment", seg)
        msg = SegmentMessage(data=seg)
        await asyncio.gather(self.operator.broadcast(msg), self.audience.broadcast(msg))
        if seg.role == "moderator":
            if self.kb is not None and seg is self.ctx.last_question:
                self.ctx.active_topic = await asyncio.to_thread(self.kb.topic_for, seg.text)
            return  # moderator is context, never a subject
        task = asyncio.create_task(self._process(seg))
        self._lane_tasks.add(task)
        task.add_done_callback(self._lane_tasks.discard)

    def emit(self, item: Annotation | ClaimRecord) -> None:
        if isinstance(item, ClaimRecord):
            self.ctx.add_claim(item)
        else:
            self.aggregator.submit(item)

    async def _process(self, seg: Segment) -> None:
        async with self.lane_sem:
            await asyncio.gather(
                self._lane("claims", self.run_claims(seg, self.ctx.view_for(seg, "claims"), self.kb, self.emit, self.slow_q)),
                self._lane("fallacy", self.run_fallacy(seg, self.ctx.view_for(seg, "fallacy"), self.emit)),
            )

    async def _lane(self, name: str, coro: Awaitable[None]) -> None:
        try:
            await asyncio.wait_for(coro, self.lane_timeout)
        except TimeoutError:
            self.lane_timeouts += 1
            log.warning("%s lane timed out after %.1fs (pending)", name, self.lane_timeout)
        except Exception:
            log.exception("%s lane failed", name)
