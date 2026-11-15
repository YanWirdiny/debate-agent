"""Ingest (P1): clip registry, timeline lookup, transcript replay at 1x, audio pump to Deepgram.

All timestamps are seconds from clip start. Speaker and role come from the hand-corrected
timeline, never from live diarization (CONTEXT.md 5.1).
"""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
import wave
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal
from urllib.parse import urlencode

import websockets

from app.interfaces import DeepgramStream
from app.models import Clip, Segment, TimelineEntry, Word

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CLIPS_DIR = DATA_DIR / "clips"

OnWords = Callable[[list[Word]], Awaitable[None]]
OnUtteranceEnd = Callable[[], Awaitable[None]]

SAMPLE_RATE = 16_000  # CONTEXT.md: audio.wav is 16 kHz mono, 16-bit PCM
FFMPEG_HINT = "ffmpeg -i clip.mp4 -ac 1 -ar 16000 -sample_fmt s16 audio.wav"


# --- clips -------------------------------------------------------------------


def list_clips(clips_dir: Path = CLIPS_DIR) -> list[Clip]:
    return [load_clip(p.parent.name, clips_dir) for p in sorted(clips_dir.glob("*/clip.json"))]


def load_clip(clip_id: str, clips_dir: Path = CLIPS_DIR) -> Clip:
    """`data/clips/<clip_id>/clip.json`. Relative paths inside it resolve against that folder."""
    folder = clips_dir / clip_id
    clip = Clip.model_validate_json((folder / "clip.json").read_text())
    if clip.clip_id != clip_id:
        raise ValueError(f"clip.json says clip_id={clip.clip_id!r}, folder is {clip_id!r}")

    def resolve(p: str | None) -> str | None:
        return str(folder / p) if p and not Path(p).is_absolute() else p

    timeline = clip.timeline
    if (folder / "timeline.json").exists():  # hand-fixed timeline (label_clip.py) wins
        timeline = load_timeline_file(folder / "timeline.json")
    return clip.model_copy(
        update={
            "audio_path": resolve(clip.audio_path) or "",
            "video_path": resolve(clip.video_path) or "",
            "transcript_path": resolve(clip.transcript_path),
            "timeline": timeline,
        }
    )


def load_timeline_file(path: str | Path) -> list[TimelineEntry]:
    return [TimelineEntry.model_validate(e) for e in json.loads(Path(path).read_text())]


def validate_timeline(timeline: list[TimelineEntry]) -> list[str]:
    """Problems a human should fix before the timeline is used live. Empty list = OK."""
    problems: list[str] = []
    if not timeline:
        return ["timeline is empty"]
    if not any(e.role == "moderator" for e in timeline):
        problems.append("no moderator entry")
    for i, e in enumerate(timeline):
        if e.t_end <= e.t_start:
            problems.append(f"entry {i}: t_end {e.t_end} <= t_start {e.t_start}")
        if i and e.t_start < timeline[i - 1].t_end - 1e-6:
            problems.append(f"entry {i}: starts at {e.t_start} before entry {i - 1} ends at {timeline[i - 1].t_end}")
    return problems


# --- timeline ----------------------------------------------------------------


class TimelineLookup:
    """Speaker and role at a clip time, from the hand-fixed timeline.

    Inside an entry -> that entry. In a gap or outside the clip -> the nearest entry.
    """

    def __init__(self, timeline: list[TimelineEntry]) -> None:
        if not timeline:
            raise ValueError("timeline is empty")
        self._entries = sorted(timeline, key=lambda e: e.t_start)
        self._starts = [e.t_start for e in self._entries]

    def who(self, t_mid: float) -> tuple[str, Literal["speaker", "moderator"]]:
        i = bisect.bisect_right(self._starts, t_mid) - 1
        candidates = [e for e in self._entries[max(i, 0) : i + 2]]
        for e in candidates:
            if e.t_start <= t_mid <= e.t_end:
                return e.speaker_id, e.role
        e = min(candidates, key=lambda e: min(abs(t_mid - e.t_start), abs(t_mid - e.t_end)))
        return e.speaker_id, e.role

    def tag(self, w: Word) -> tuple[str, Literal["speaker", "moderator"]]:
        return self.who((w.t_start + w.t_end) / 2)


class LiveSpeakers:
    """Speaker and role in a live room, where there is no timeline.

    Deepgram diarization labels each word with a voice (`speaker_id="voice0"`, ...). Precedence:
    the operator's override (keys 1 / 2 / M: "this is X talking now"), then the operator's voice map,
    then auto-assign: a voice heard for the first time gets the next participant with no voice yet,
    the moderator first (a debate opens with the moderator), then the debaters in order. A voice
    heard once everyone is taken goes to the last debater until the operator fixes it.
    """

    def __init__(self, participants: list[tuple[str, Literal["speaker", "moderator"]]]) -> None:
        if not participants:
            raise ValueError("a room needs at least one participant")
        self.roles = dict(participants)
        self.order = [sid for sid, _ in participants]
        self.voices: dict[int, str] = {}
        self.override: str | None = None
        self.changed = False  # set when the map changes, so the session can tell the operator

    def _voice(self, w: Word) -> int | None:
        if w.speaker_id and w.speaker_id.startswith("voice") and w.speaker_id[5:].isdigit():
            return int(w.speaker_id[5:])
        return None

    def tag(self, w: Word) -> tuple[str, Literal["speaker", "moderator"]]:
        voice = self._voice(w)
        if voice is not None and voice not in self.voices:
            debaters = [s for s in self.order if self.roles[s] == "speaker"] or self.order
            queue = [s for s in self.order if self.roles[s] == "moderator"] + debaters
            free = [s for s in queue if s not in self.voices.values()]
            self.voices[voice] = free[0] if free else debaters[-1]
            self.changed = True
        sid = self.override or (self.voices.get(voice) if voice is not None else None) or self.order[0]
        return sid, self.roles.get(sid, "speaker")

    def assign(self, voice: int, speaker_id: str) -> None:
        if speaker_id not in self.roles:
            raise ValueError(f"unknown participant {speaker_id!r}")
        self.voices[voice] = speaker_id
        self.changed = True

    def set_override(self, speaker_id: str | None) -> None:
        if speaker_id is not None and speaker_id not in self.roles:
            raise ValueError(f"unknown participant {speaker_id!r}")
        self.override = speaker_id
        self.changed = True


# --- transcript replay (REPLAY_TRANSCRIPT=1, and the mock stream) -------------


def load_transcript_words(path: str | Path) -> list[Word]:
    """Transcript = JSONL of Segment rows. Each segment is split into words spread evenly over
    [t_start, t_end], carrying the segment's speaker and role and its STT confidence."""
    words: list[Word] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        seg = Segment.model_validate_json(line)
        tokens = seg.text.split()
        step = (seg.t_end - seg.t_start) / max(len(tokens), 1)
        low = {w.lower() for w in seg.low_conf_words}
        for i, tok in enumerate(tokens):
            bare = tok.strip(".,!?;:\"'()").lower()
            words.append(
                Word(
                    text=tok,
                    t_start=round(seg.t_start + i * step, 3),
                    t_end=round(seg.t_start + (i + 1) * step, 3),
                    confidence=min(seg.stt_confidence, 0.8) if bare in low else seg.stt_confidence,
                    speaker_id=seg.speaker_id,
                    role=seg.role,
                )
            )
    return words


async def sleep_until(target_wall: float) -> None:
    delay = target_wall - time.time()
    if delay > 0:
        await asyncio.sleep(delay)


async def replay_words(words: list[Word], t0_wall: float, on_words: OnWords, speed: float = 1.0) -> None:
    """Deliver each word when the clip reaches its t_end (wall = t0 + t_end / speed).
    Sleeps to target time, never fixed intervals, so it does not drift."""
    for w in words:
        await sleep_until(t0_wall + w.t_end / speed)
        await on_words([w])


# --- audio ---------------------------------------------------------------------


def open_pcm(path: str | Path) -> wave.Wave_read:
    """Open clip audio for streaming. Must be 16 kHz mono 16-bit PCM WAV."""
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"clip audio not found: {path!r} (make it with: {FFMPEG_HINT})")
    w = wave.open(str(path), "rb")
    fmt = (w.getframerate(), w.getnchannels(), w.getsampwidth())
    if fmt != (SAMPLE_RATE, 1, 2):
        w.close()
        raise ValueError(f"{path}: need 16 kHz mono 16-bit WAV, got rate/channels/width={fmt}. Convert: {FFMPEG_HINT}")
    return w


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


class AudioPump:
    """Streams clip audio to Deepgram as if it were live: 100 ms chunks, each sent when the clip
    reaches the chunk's end (wall = t0_wall + t / speed). Sleeps to target time, so it never drifts.

    `start_s` resumes mid-clip (after a Deepgram reconnect); `position` is how far it has sent.
    """

    def __init__(self, chunk_ms: int = 100, speed: float = 1.0, start_s: float = 0.0) -> None:
        self.chunk_ms = chunk_ms
        self.speed = speed
        self.start_s = start_s
        self.position = start_s
        self.done = False

    async def run(self, clip: Clip, dg: DeepgramStream, t0_wall: float) -> None:
        frames_per_chunk = SAMPLE_RATE * self.chunk_ms // 1000
        with open_pcm(clip.audio_path) as w:
            w.setpos(min(int(self.start_s * SAMPLE_RATE), w.getnframes()))
            frame = w.tell()
            while True:
                data = w.readframes(frames_per_chunk)
                if not data:
                    break
                frame += len(data) // 2
                t_end = frame / SAMPLE_RATE
                await sleep_until(t0_wall + t_end / self.speed)
                await dg.send(data)
                self.position = t_end
        self.done = True


# --- Deepgram streaming ---------------------------------------------------------

DEEPGRAM_LISTEN_URL = "wss://api.deepgram.com/v1/listen"
DEEPGRAM_PARAMS: dict[str, str | int] = {
    "model": "nova-3",
    "language": "en",
    "encoding": "linear16",
    "sample_rate": SAMPLE_RATE,
    "channels": 1,
    "punctuate": "true",
    "smart_format": "true",  # digits for numbers: "6%" not "six percent"
    "interim_results": "true",  # required for utterance_end_ms; we only use is_final results
    "utterance_end_ms": 1000,
}
# Live room (one mic, two people): Deepgram tells the voices apart; LiveSpeakers names them.
ROOM_PARAMS: dict[str, str | int] = {"diarize": "true"}


class DeepgramLive:
    """Deepgram streaming client (implements DeepgramStream). Raw WebSocket, no SDK, so the
    message format is pinned by us and testable against a local fake server.

    Clip time of a word = Deepgram time + offset, where offset = clip time at which the current
    pump started minus audio already sent before it (warm-up silence).
    """

    def __init__(self, api_key: str, *, url: str = DEEPGRAM_LISTEN_URL, keepalive_s: float = 4.0,
                 open_timeout: float = 5.0, params: dict[str, str | int] | None = None) -> None:
        self.api_key = api_key
        self.url = url
        self.keepalive_s = keepalive_s
        self.open_timeout = open_timeout
        self.params = {**DEEPGRAM_PARAMS, **(params or {})}
        self.sent_bytes = 0
        self._ws: Any = None
        self._last_send = 0.0
        self._keepalive_task: asyncio.Task[None] | None = None

    @property
    def sent_s(self) -> float:
        return self.sent_bytes / (2 * SAMPLE_RATE)

    async def connect(self) -> None:
        if not self.api_key:
            raise ValueError("DEEPGRAM_API_KEY is not set")
        self._ws = await websockets.connect(
            f"{self.url}?{urlencode(self.params)}",
            additional_headers={"Authorization": f"Token {self.api_key}"},
            open_timeout=self.open_timeout,
            max_size=None,
        )
        self._last_send = time.monotonic()
        self._keepalive_task = asyncio.create_task(self._keepalive())

    async def send(self, data: bytes) -> None:
        await self._ws.send(data)
        self.sent_bytes += len(data)
        self._last_send = time.monotonic()

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """JSON messages until the server closes. Raises ConnectionClosedError on a drop."""
        async for raw in self._ws:
            if isinstance(raw, str):
                yield json.loads(raw)

    async def finish(self) -> None:
        """No more audio: Deepgram flushes final results, then closes the socket."""
        self._stop_keepalive()
        try:
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        except websockets.ConnectionClosed:
            pass

    async def close(self) -> None:
        self._stop_keepalive()
        if self._ws is not None:
            await self._ws.close()

    def _stop_keepalive(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    async def _keepalive(self) -> None:
        """Deepgram closes a stream after ~10 s without audio (warm-up -> operator presses start)."""
        try:
            while True:
                await asyncio.sleep(min(1.0, self.keepalive_s / 2))
                if time.monotonic() - self._last_send >= self.keepalive_s:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
                    self._last_send = time.monotonic()
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass


def dg_final_words(msg: dict[str, Any], offset: float = 0.0) -> list[Word] | None:
    """Words of a final `Results` message, in clip time. None for interim results and other types."""
    if msg.get("type") != "Results" or not msg.get("is_final"):
        return None
    alts = (msg.get("channel") or {}).get("alternatives") or []
    if not alts:
        return []
    return [
        Word(
            text=w.get("punctuated_word") or w["word"],
            t_start=round(w["start"] + offset, 3),
            t_end=round(w["end"] + offset, 3),
            confidence=max(0.0, min(1.0, float(w.get("confidence", 0.0)))),
            speaker_id=f"voice{int(w['speaker'])}" if w.get("speaker") is not None else None,
        )
        for w in alts[0].get("words") or []
    ]


async def stt_reader(dg: DeepgramLive, offset: float, on_words: OnWords, on_utterance_end: OnUtteranceEnd) -> None:
    """Pass finals to the segmenter path; UtteranceEnd ends the open sentence. Never blocks on lanes
    (on_words only segments and schedules). Returns when Deepgram closes the stream."""
    async for msg in dg.messages():
        words = dg_final_words(msg, offset)
        if words:
            await on_words(words)
        elif msg.get("type") == "UtteranceEnd":
            await on_utterance_end()
        elif msg.get("type") == "Error" or msg.get("err_code"):
            log.error("Deepgram error: %s", msg)
