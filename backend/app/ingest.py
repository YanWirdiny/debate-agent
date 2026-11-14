"""Ingest (P1): clip registry, timeline lookup, transcript replay at 1x, audio pump to Deepgram.

All timestamps are seconds from clip start. Speaker and role come from the hand-corrected
timeline, never from live diarization (CONTEXT.md 5.1).
"""
from __future__ import annotations

import asyncio
import bisect
import json
import time
from pathlib import Path
from typing import Awaitable, Callable, Literal

from app.interfaces import DeepgramStream
from app.models import Clip, Segment, TimelineEntry, Word

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CLIPS_DIR = DATA_DIR / "clips"

OnWords = Callable[[list[Word]], Awaitable[None]]


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

    return clip.model_copy(
        update={
            "audio_path": resolve(clip.audio_path) or "",
            "video_path": resolve(clip.video_path) or "",
            "transcript_path": resolve(clip.transcript_path),
        }
    )


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


# --- audio pump (Deepgram) ---------------------------------------------------


class AudioPump:
    async def run(self, clip: Clip, dg: DeepgramStream, t0_wall: float) -> None:
        """Send 100 ms PCM chunks at 1x; sleep to target wall time. Next slice (Deepgram)."""
        raise NotImplementedError
