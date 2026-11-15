"""SessionContext and SessionRunner (P1). ARCHITECTURE.md sections 2, 4 and 8.

Single-writer rule: the segmenter path (`on_segment`) and the summarizer mutate the context;
lanes only read frozen `view_for()` snapshots. Claim records arrive through `emit`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import websockets

from app import db as dbm
from app import ingest
from app.aggregator import DEFAULT_AUDIENCE_MIN_CONF, Aggregator
from app.eventlog import EventSink, EventWriter, load_session_events, verify_chain
from app.hubs import Hub
from app.interfaces import KnowledgeBase
from app.lanes import fake
from app.metrics import Metrics
from app.models import (
    Annotation,
    BannerMessage,
    Event,
    PrefetchJob,
    ClaimRecord,
    Clip,
    Lane,
    LaneContext,
    PendingMessage,
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
WARMUP_SILENCE_S = 1.0
MAX_RECONNECTS = 3
FINAL_RESULTS_TIMEOUT_S = 10.0
SUMMARY_EVERY_TURNS = 6
ASYNC_JOB_TIMEOUT_S = 20.0  # Gemini grounded verdict / prefetch / summarize
ASYNC_WORKERS = 2
# _lane cancels a lane that exceeds this. Every lane budget derives from it. 1.2 s is the design
# target; on Groq's free-tier reasoning models (~0.5 s a call) run with LANE_TIMEOUT_S=2.5 in the
# environment. The audience view is delayed 2.5 s, so a flag still lands with its line.
LANE_TIMEOUT_S = float(os.environ.get("LANE_TIMEOUT_S", "1.2"))

Source = Literal["replay", "deepgram", "events"]


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
        self.speaker_turns_done = 0  # completed non-moderator turns (summarizer cadence)
        self._current_turn: list[Segment] = []
        self._completed_turns: deque[tuple[str, list[Segment]]] = deque(maxlen=TURN_HISTORY)

    # --- writes (segmenter path) ---

    def on_segment(self, seg: Segment) -> None:
        if seg.speaker_id != self.current_turn_speaker:
            if self.current_turn_speaker is not None and self._current_turn:
                self._completed_turns.append((self.current_turn_speaker, self._current_turn))
                if self._current_turn[0].role == "speaker":
                    self.speaker_turns_done += 1
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

    def recent_speaker_turns(self, n: int) -> list[Segment]:
        """Segments of the last n completed non-moderator turns, oldest first (summarizer input)."""
        turns = [t for _, t in self._completed_turns if t and t[0].role == "speaker"][-n:]
        return [s for t in turns for s in t]

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
                claim_ledger=list(self.claim_ledger.get(seg.speaker_id, [])),  # consistency runs inside claims
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


@dataclass
class Intel:
    """P2's functions, as the runner calls them. `fake()` runs the pipeline with no keys or KB;
    `real()` is P2's implementation; `bank()` is the Groq-only reduced mode."""

    name: str
    run_claims: Callable[..., Awaitable[None]]
    run_fallacy: Callable[..., Awaitable[None]]
    grounded_verdict: Callable[..., Awaitable[Annotation | None]]
    prefetch: Callable[..., Awaitable[tuple[str, list[Any]]]]
    summarize: Callable[..., Awaitable[str]]
    warm_up: Callable[..., Awaitable[None]]
    load_kb: Callable[..., Awaitable[KnowledgeBase | None]]

    @classmethod
    def fake(cls) -> Intel:
        return cls("fake", fake.run_claims, fake.run_fallacy, fake.grounded_verdict, fake.prefetch,
                   fake.summarize, fake.warm_up, fake.load_kb)

    @classmethod
    def real(cls) -> Intel:
        from app.intel import async_jobs, warmup
        from app.kb.store import KnowledgeBase as KB
        from app.lanes import claims, fallacy

        return cls("real", claims.run_claims, fallacy.run_fallacy, async_jobs.grounded_verdict,
                   async_jobs.prefetch, async_jobs.summarize, warmup.warm_up, KB.load)

    @classmethod
    def bank(cls) -> Intel:
        """Explicit reduced mode (Groq only, no Gemini): real KB, claims lane (bank, retrieval + stance,
        consistency) and warm-up. No fallacy lane, no grounded verdicts (U stays U), no prefetch,
        no rolling summary. The operator sees a banner saying so; nothing is faked."""
        real = cls.real()

        async def no_fallacy(seg, view, emit):
            return None

        async def no_verdict(job, kb):
            return None

        async def no_prefetch(job):
            return (getattr(job, "topic", None) or "general", [])

        async def keep_summary(prev, turns):
            return prev

        return cls("bank", real.run_claims, no_fallacy, no_verdict, no_prefetch, keep_summary, real.warm_up, real.load_kb)

    @classmethod
    def by_name(cls, name: str) -> Intel:
        modes = {"fake": cls.fake, "real": cls.real, "bank": cls.bank}
        if name not in modes:
            raise ValueError(f"INTEL must be fake, real or bank, got {name!r}")
        return modes[name]()


class SessionRunner:
    """One live session: transcript source -> segmenter -> context -> lanes -> aggregator -> hubs/log.

    Sources:
    - `deepgram`: clip audio pumped at 1x to Deepgram streaming; speaker/role from the clip timeline.
      On a drop it reconnects and resumes from the current clip time (up to MAX_RECONNECTS), then
      falls back to the pre-made transcript from that point, with an operator banner.
    - `replay`: pre-made transcript words at 1x (REPLAY_TRANSCRIPT=1, mock stream, tests).
    - `events`: REPLAY_EVENTS=<session_id>; re-broadcasts a recorded run from its event log at 1x
      (no STT, no lanes, no LLM; labelled on the operator view).

    Long-lived tasks (ARCHITECTURE.md section 2): writer, aggregator, source, 2 async verdict
    workers, summarizer, metrics ticker.
    """

    def __init__(
        self,
        session_id: str,
        clip_id: str,
        *,
        clip: Clip | None = None,
        operator: Hub | None = None,
        audience: Hub | None = None,
        intel: Intel | None = None,
        kb: KnowledgeBase | None = None,
        sink: EventSink | None = None,
        fallback_path: Path | None = None,
        archive_path: Path | None = None,
        audience_min_conf: float = DEFAULT_AUDIENCE_MIN_CONF,
        lane_concurrency: int = 8,
        lane_timeout: float = LANE_TIMEOUT_S,
        speed: float = 1.0,
        source: Source = "replay",
        deepgram_key: str = "",
        deepgram_url: str = ingest.DEEPGRAM_LISTEN_URL,
        db: Any = None,
        replay_session_id: str = "",
        runs_dir: Path | None = None,
        drain_timeout: float = ASYNC_JOB_TIMEOUT_S + 5,
        metrics_interval: float = 1.0,
    ) -> None:
        self.session_id = session_id
        self.clip_id = clip_id
        self.clip = clip or ingest.load_clip(clip_id)
        self.operator = operator or Hub("operator")
        self.audience = audience or Hub("audience")
        self.intel = intel or Intel.fake()
        self.kb = kb
        self.lane_timeout = lane_timeout
        self.speed = speed
        self.source = source
        self.deepgram_key = deepgram_key
        self.deepgram_url = deepgram_url
        self.db = db
        self.replay_session_id = replay_session_id
        self.runs_dir = runs_dir
        self.drain_timeout = drain_timeout
        self.metrics_interval = metrics_interval
        self.dg: ingest.DeepgramLive | None = None
        self.pump: ingest.AudioPump | None = None
        self.reconnects = 0
        self.ctx = SessionContext(session_id)
        self.metrics = Metrics()
        self.writer = EventWriter(session_id, sink=sink, fallback_path=fallback_path, archive_path=archive_path)
        self.aggregator = Aggregator(audience_min_conf, operator=self.operator, audience=self.audience,
                                     writer=self.writer, metrics=self.metrics, preserve=source == "events")
        self.segmenter = Segmenter(session_id)  # replay words carry their own speaker/role; deepgram: set in warm_up
        self._words: list[Word] = []
        self._recorded: list[Event] = []
        self.slow_q: asyncio.Queue[SlowJob] = asyncio.Queue()
        self.lane_sem = asyncio.Semaphore(lane_concurrency)
        self.t0_wall: float | None = None
        self.ready = False
        self.async_timeouts = 0
        self._summary_due = asyncio.Event()
        self._summarized_at = 0
        self._tasks: list[asyncio.Task[Any]] = []
        self._background: list[asyncio.Task[Any]] = []  # workers, summarizer, ticker: cancelled at the end
        self._lane_tasks: set[asyncio.Task[Any]] = set()
        self._done = asyncio.Event()
        self._finishing = False

    @property
    def lane_timeouts(self) -> int:
        return self.metrics.lane_timeouts

    # --- warm-up (ARCHITECTURE.md section 8) ---

    async def warm_up(self) -> None:
        """1-2, 4-5: KB + P2 warm-up; 3: Deepgram open + 1 s silence; 6: `ready`. Target < 10 s."""
        t = time.monotonic()
        if self.clip.transcript_path:
            self._words = ingest.load_transcript_words(self.clip.transcript_path)
        if self.source == "events":
            await self._warm_up_events()
        else:
            if self.source == "replay" and not self._words:
                raise ValueError(f"clip {self.clip_id} has no transcript_path (needed for replay)")
            await self._warm_up_intel()
            if self.source == "deepgram":
                await self._warm_up_deepgram()
        self.ready = True
        log.info("warm-up for %s done in %.1fs (intel=%s, source=%s)", self.session_id, time.monotonic() - t,
                 self.intel.name, self.source)
        msg = ReadyMessage(session_id=self.session_id)
        await asyncio.gather(self.operator.broadcast(msg), self.audience.broadcast(msg))

    def speaker_ids(self) -> list[str]:
        """Non-moderator speakers of the clip, from the timeline (or the transcript)."""
        ids = [e.speaker_id for e in self.clip.timeline if e.role == "speaker"]
        ids += [w.speaker_id for w in self._words if w.role == "speaker" and w.speaker_id]
        return sorted(set(ids))

    async def _warm_up_intel(self) -> None:
        speakers = self.speaker_ids()
        try:
            if self.kb is None:
                self.kb = await self.intel.load_kb(self.db, speakers, [])  # [] = all topic shards
            if self.kb is not None:
                for spk in speakers:
                    if digest := self.kb.digest(spk):
                        self.ctx.participants[spk] = digest
            await self.intel.warm_up(self.kb, speakers)  # type: ignore[arg-type]
        except NotImplementedError as e:
            raise ValueError(f"INTEL={self.intel.name}: an intel module is not implemented; use INTEL=fake") from e

    async def _warm_up_deepgram(self) -> None:
        ingest.open_pcm(self.clip.audio_path).close()  # fail now, not at start
        timeline = self.clip.timeline
        if not timeline and self.db is not None:
            timeline = await dbm.load_timeline(self.db, self.clip_id)
        if not timeline:
            raise ValueError(f"clip {self.clip_id} has no timeline (run scripts/label_clip.py)")
        self.segmenter = Segmenter(self.session_id, ingest.TimelineLookup(timeline))
        self.dg = await self._open_deepgram()

    async def _warm_up_events(self) -> None:
        events = await load_session_events(self.replay_session_id, self.db, self.runs_dir)
        if not events or events[0].type != "session_start":
            raise ValueError(f"no recorded event log for session {self.replay_session_id!r}")
        import json

        recorded_clip = json.loads(events[0].payload_json).get("clip_id")
        if recorded_clip != self.clip_id:
            raise ValueError(f"session {self.replay_session_id} recorded clip {recorded_clip!r}, not {self.clip_id!r}")
        if not verify_chain(events):
            await self.operator.broadcast(BannerMessage(level="warn", text="Recorded event log fails its hash chain check"))
        self._recorded = events

    async def _open_deepgram(self) -> ingest.DeepgramLive:
        dg = ingest.DeepgramLive(self.deepgram_key, url=self.deepgram_url)
        try:
            await dg.connect()
        except websockets.InvalidHandshake as e:  # e.g. HTTP 401 for a bad key
            raise ConnectionError(f"Deepgram rejected the stream: {e} (check DEEPGRAM_API_KEY)") from e
        await dg.send(ingest.silence(WARMUP_SILENCE_S))
        return dg

    # --- lifecycle ---

    async def start(self, lead_s: float = 0.3) -> float:
        """Starts the source and long-lived tasks; returns t0_wall (clip t=0 in wall time)."""
        if not self.ready:
            await self.warm_up()
        self.t0_wall = time.time() + lead_s
        self.aggregator.t0_wall = self.t0_wall
        self.aggregator.speed = self.speed
        self.writer.put("session_start", {"session_id": self.session_id, "clip_id": self.clip_id, "t0_wall": self.t0_wall,
                                          "source": self.source, "intel": self.intel.name,
                                          "participants": self.speaker_ids(), "replay_of": self.replay_session_id or None})
        started = StartedMessage(t0_wall=self.t0_wall)
        await asyncio.gather(self.operator.broadcast(started), self.audience.broadcast(started))
        if self.source == "replay":
            await self.operator.broadcast(BannerMessage(level="info", text=f"Replay mode: pre-made transcript at {self.speed:g}x"))
        elif self.source == "events":
            await self.operator.broadcast(BannerMessage(
                level="info", text=f"Recorded run {self.replay_session_id} replayed from its event log (not live)"))
        if self.intel.name == "bank" and self.source != "events":
            await self.operator.broadcast(BannerMessage(
                level="info", text="Bank-only mode: claims + consistency; no fallacy lane, no web verdicts (U stays U)"))
        self._db_task(dbm.save_session, self.session_id, clip_id=self.clip_id, source=self.source, intel=self.intel.name,
                      t0_wall=self.t0_wall, speed=self.speed, replay_of=self.replay_session_id or None)
        self._tasks = [
            asyncio.create_task(self.writer.run(), name="writer"),
            asyncio.create_task(self.aggregator.run(), name="aggregator"),
            asyncio.create_task(self._source(), name="source"),
        ]
        self._background = [asyncio.create_task(self.metrics.ticker(self.operator, self.metrics_interval), name="metrics")]
        if self.source != "events":
            self._background += [asyncio.create_task(self._verdict_worker(i), name=f"verdict-{i}") for i in range(ASYNC_WORKERS)]
            self._background.append(asyncio.create_task(self._summarizer(), name="summarizer"))
        return self.t0_wall

    async def wait(self) -> None:
        await self._done.wait()

    async def stop(self) -> None:
        for t in self._tasks:
            if t.get_name() == "source" and not t.done():
                t.cancel()
        self.drain_timeout = 0  # operator asked to stop: do not wait for pending web verdicts
        await self._finish()

    async def _finish(self) -> None:
        if self._finishing:
            await self._done.wait()
            return
        self._finishing = True
        if self._lane_tasks:
            await asyncio.gather(*self._lane_tasks, return_exceptions=True)
        if self.drain_timeout > 0 and self._background:
            try:  # late web verdicts still land in the log and the report
                await asyncio.wait_for(self.slow_q.join(), self.drain_timeout)
            except TimeoutError:
                log.warning("%d async jobs still pending at session end", self.slow_q.qsize())
        for t in self._background:
            t.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.aggregator.stop()
        self.writer.put("session_end", {"session_id": self.session_id, "metrics": self.metrics.snapshot().model_dump(),
                                        "p95_bank_ms": self.metrics.p95_bank_ms, "lane_timeouts": self.lane_timeouts,
                                        "async_timeouts": self.async_timeouts, "reconnects": self.reconnects})
        await self.writer.stop()
        await asyncio.gather(*(t for t in self._tasks if t.get_name() != "source"), return_exceptions=True)
        if self.dg is not None:
            await self.dg.close()
        if self.t0_wall is not None:
            self._db_task(dbm.save_session, self.session_id, ended_at=time.time(), event_count=self.writer.count,
                          lane_timeouts=self.lane_timeouts, reconnects=self.reconnects)
        self._done.set()

    def _db_task(self, fn: Callable[..., Awaitable[Any]], *args: Any, **kw: Any) -> None:
        """Fire-and-forget Mongo write (session records); never on the hot path, never raises."""
        if self.db is None:
            return

        async def go() -> None:
            try:
                await fn(self.db, *args, **kw)
            except Exception as e:
                log.warning("mongo %s failed: %s", fn.__name__, e)

        self._spawn(go())

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        self._lane_tasks.add(task)
        task.add_done_callback(self._lane_tasks.discard)

    # --- sources ---

    async def _source(self) -> None:
        assert self.t0_wall is not None
        try:
            if self.source == "deepgram":
                await self._source_deepgram()
            elif self.source == "events":
                await self._source_events()
            else:
                await self._replay_from(0.0)
            await self.on_utterance_end()
        finally:
            asyncio.get_running_loop().create_task(self._finish())

    async def _replay_from(self, t: float) -> None:
        assert self.t0_wall is not None
        words = [w for w in self._words if w.t_end > t]
        await ingest.replay_words(words, self.t0_wall, self.on_words, speed=self.speed)

    async def _source_events(self) -> None:
        """Same relative timing as the recorded run. Annotations go through the aggregator in
        preserve mode, so audience routing (gate, removals) is recomputed, versions are not."""
        import json

        assert self.t0_wall is not None
        t0_rec = json.loads(self._recorded[0].payload_json)["t0_wall"]
        for e in self._recorded[1:]:
            if e.type not in ("segment", "annotation", "annotation_update", "pending"):
                continue  # kill shows up as the killed annotation_update that follows it
            await ingest.sleep_until(self.t0_wall + max(0.0, e.ts - t0_rec) / self.speed)
            if e.type == "segment":
                await self.on_segment(Segment.model_validate_json(e.payload_json))
            elif e.type == "pending":
                await self._pending(PendingMessage.model_validate_json(e.payload_json))
            else:
                self.aggregator.submit(Annotation.model_validate_json(e.payload_json))

    async def _source_deepgram(self) -> None:
        assert self.t0_wall is not None
        start_s = 0.0
        while True:
            pump = self.pump = ingest.AudioPump(speed=self.speed, start_s=start_s)
            reader: asyncio.Task[None] | None = None
            try:
                dg = self.dg = self.dg or await self._open_deepgram()
                offset = start_s - dg.sent_s  # clip time = Deepgram time + offset
                reader = asyncio.create_task(ingest.stt_reader(dg, offset, self.on_words, self.on_utterance_end))
                await self._pump_until_drop(pump, dg, reader)
                await dg.finish()
                try:
                    await asyncio.wait_for(reader, FINAL_RESULTS_TIMEOUT_S)
                except TimeoutError:
                    log.warning("Deepgram did not close %.0fs after the clip ended", FINAL_RESULTS_TIMEOUT_S)
                return
            except (websockets.ConnectionClosed, OSError, TimeoutError) as e:
                if self.dg is not None:
                    await self.dg.close()
                self.dg = None
                start_s = pump.position
                await self.on_utterance_end()  # do not glue sentences across the gap
                self.reconnects += 1
                log.warning("Deepgram stream dropped at %.1fs: %r", start_s, e)
                if self.reconnects > MAX_RECONNECTS:
                    await self._fallback_to_transcript(start_s)
                    return
                await self.operator.broadcast(BannerMessage(
                    level="warn", text=f"Speech-to-text dropped at {start_s:.0f}s; reconnecting ({self.reconnects}/{MAX_RECONNECTS})"))
            finally:
                if reader is not None and not reader.done():
                    reader.cancel()

    async def _pump_until_drop(self, pump: ingest.AudioPump, dg: ingest.DeepgramLive, reader: asyncio.Task[None]) -> None:
        """Run the pump; if the reader ends first (Deepgram closed on us), treat it as a drop."""
        pump_task = asyncio.create_task(pump.run(self.clip, dg, self.t0_wall or 0.0))
        done, _ = await asyncio.wait({pump_task, reader}, return_when=asyncio.FIRST_COMPLETED)
        if pump_task in done:
            pump_task.result()
            return
        pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
        exc = reader.exception()
        raise exc if exc else websockets.ConnectionClosed(None, None)

    async def _fallback_to_transcript(self, from_t: float) -> None:
        if not self._words:
            await self.operator.broadcast(BannerMessage(level="error", text="Speech-to-text lost and no pre-made transcript; captions stopped"))
            return
        await self.operator.broadcast(BannerMessage(level="warn", text=f"Speech-to-text lost; switched to pre-made transcript at {from_t:.0f}s"))
        await self._replay_from(from_t)

    # --- hot path ---

    async def on_utterance_end(self) -> None:
        for seg in self.segmenter.flush():
            await self.on_segment(seg)

    async def on_words(self, words: list[Word]) -> None:
        for seg in self.segmenter.on_final(words):
            await self.on_segment(seg)
        partial = self.segmenter.partial()
        if partial is not None:  # live captions at speaking pace; not logged, not analysed
            await asyncio.gather(self.operator.broadcast(partial), self.audience.broadcast(partial))

    async def on_segment(self, seg: Segment) -> None:
        self.ctx.on_segment(seg)
        self.aggregator.register_segment(seg)
        self.writer.put("segment", seg)
        msg = SegmentMessage(data=seg)
        await asyncio.gather(self.operator.broadcast(msg), self.audience.broadcast(msg))
        if self.source == "events":
            return  # recorded run: annotations come from the log
        if self.ctx.speaker_turns_done - self._summarized_at >= SUMMARY_EVERY_TURNS:
            self._summary_due.set()
        if seg.role == "moderator":
            if seg is self.ctx.last_question:
                self._spawn(self._on_question(seg))
            return  # moderator is context, never a subject
        self._spawn(self._process(seg))

    def emit(self, item: Annotation | ClaimRecord) -> None:
        if isinstance(item, ClaimRecord):
            self.ctx.add_claim(item)
            self.aggregator.register_claim(item)
        else:
            self.aggregator.submit(item)

    async def _process(self, seg: Segment) -> None:
        async with self.lane_sem:
            await asyncio.gather(
                self._lane(seg, "claims", self.intel.run_claims(seg, self.ctx.view_for(seg, "claims"), self.kb, self.emit, self.slow_q)),
                self._lane(seg, "fallacy", self.intel.run_fallacy(seg, self.ctx.view_for(seg, "fallacy"), self.emit)),
            )

    async def _lane(self, seg: Segment, name: Lane, coro: Awaitable[None]) -> None:
        """Run one lane under its budget. A timeout cancels it (no late result); a timeout or a
        failure ships a final `pending` to the operator and the event log (CONTEXT.md rule 7)."""
        try:
            await asyncio.wait_for(coro, self.lane_timeout)
            return
        except TimeoutError:
            self.metrics.lane_timeouts += 1
            log.warning("%s lane timed out after %.1fs on %s (pending)", name, self.lane_timeout, seg.segment_id)
            reason = "timeout"
        except Exception:
            log.exception("%s lane failed on %s (pending)", name, seg.segment_id)
            reason = "error"
        await self._pending(PendingMessage(segment_id=seg.segment_id, lane=name, reason=reason))

    async def _pending(self, msg: PendingMessage) -> None:
        self.writer.put("pending", msg)
        await self.operator.broadcast(msg)  # operator only, never the audience

    # --- async plane (never on the hot path) ---

    async def _on_question(self, seg: Segment) -> None:
        """Per-question prefetch (ARCHITECTURE.md section 8): set active_topic, enqueue a PrefetchJob."""
        topic = None
        if self.kb is not None:
            try:
                topic = await asyncio.to_thread(self.kb.topic_for, seg.text)
            except Exception:
                log.exception("topic_for failed")
        self.ctx.active_topic = topic or self.ctx.active_topic
        self.slow_q.put_nowait(PrefetchJob(question=seg, topic=topic, participants=list(self.ctx.participants.values())))

    async def _verdict_worker(self, i: int) -> None:
        while True:
            job = await self.slow_q.get()
            try:
                if job.kind == "verdict":
                    a = await asyncio.wait_for(self.intel.grounded_verdict(job, self.kb), ASYNC_JOB_TIMEOUT_S)
                    if a is not None:
                        self.aggregator.submit(a)
                else:
                    topic, passages = await asyncio.wait_for(self.intel.prefetch(job), ASYNC_JOB_TIMEOUT_S)
                    if passages and self.kb is not None:
                        await asyncio.to_thread(self.kb.add_passages, topic, passages)
                    log.info("prefetch %s: %d passages", topic, len(passages))
            except TimeoutError:
                self.async_timeouts += 1
                log.warning("async %s job dropped after %.0fs", job.kind, ASYNC_JOB_TIMEOUT_S)
            except Exception:
                log.exception("async %s job failed", job.kind)
            finally:
                self.slow_q.task_done()

    async def _summarizer(self) -> None:
        """Every SUMMARY_EVERY_TURNS speaker turns, refresh ctx.rolling_summary (lanes read the last value)."""
        while True:
            await self._summary_due.wait()
            self._summary_due.clear()
            done = self.ctx.speaker_turns_done
            turns = self.ctx.recent_speaker_turns(SUMMARY_EVERY_TURNS)
            try:
                self.ctx.rolling_summary = await asyncio.wait_for(
                    self.intel.summarize(self.ctx.rolling_summary, turns), ASYNC_JOB_TIMEOUT_S)
                self._summarized_at = done
            except Exception:
                log.exception("summarizer failed; keeping the previous summary")
                self._summarized_at = done  # retry after the next 6 turns, not on every segment
