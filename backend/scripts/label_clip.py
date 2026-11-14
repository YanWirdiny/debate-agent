"""label_clip.py (P1): clip -> Deepgram prerecorded + diarize -> timeline draft -> hand fix -> Mongo.

Speaker and role during the live demo come from this timeline, never from live diarization
(CONTEXT.md 5.1), so a human checks it. Steps:

  1. Make 16 kHz mono WAV (what the live pump streams):
       ffmpeg -i clip.mp4 -ac 1 -ar 16000 -sample_fmt s16 data/clips/<id>/audio.wav
  2. Draft (one Deepgram call, cached in deepgram_prerecorded.json; --refresh to redo):
       python scripts/label_clip.py draft <id> --title "..." --video clip.mp4 \\
           --map 0=moderator:moderator --map 1=speaker_a --map 2=speaker_b
     Writes clip.json, timeline.json (draft), transcript.jsonl (for REPLAY_TRANSCRIPT and fallback).
     Run `draft` once without --map first to see who Deepgram's speaker 0/1/2 are.
  3. Review and hand-fix timeline.json:
       python scripts/label_clip.py show <id>        # every turn with its text
     Edit timeline.json (boundaries, speaker ids), then rebuild the transcript from it:
       python scripts/label_clip.py build <id>
  4. Push the fixed timeline to Mongo (shared through Atlas):
       python scripts/label_clip.py push <id>

speaker_id values must match P2's dossier ids (data/dossiers/<speaker_id>/).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app import db as dbm  # noqa: E402
from app import ingest  # noqa: E402
from app.models import Clip, Segment, TimelineEntry, Word  # noqa: E402
from app.segmenter import Segmenter  # noqa: E402

DEEPGRAM_PRERECORDED_URL = "https://api.deepgram.com/v1/listen"
PRERECORDED_PARAMS = {
    "model": "nova-3", "language": "en", "diarize": "true", "punctuate": "true", "smart_format": "true",
}
CACHE_NAME = "deepgram_prerecorded.json"
MIN_TURN_S = 0.8  # shorter diarization blips ("yeah", cross-talk) are folded into the previous turn
MIN_TURN_WORDS = 3


# --- pure functions (tested) ---------------------------------------------------


def parse_map(items: list[str]) -> dict[int, tuple[str, str]]:
    """["0=moderator:moderator", "1=speaker_a"] -> {0: ("moderator", "moderator"), 1: ("speaker_a", "speaker")}"""
    out: dict[int, tuple[str, str]] = {}
    for item in items:
        key, _, value = item.partition("=")
        speaker_id, _, role = value.partition(":")
        role = role or "speaker"
        if not key.strip().isdigit() or not speaker_id or role not in ("speaker", "moderator"):
            raise ValueError(f"bad --map {item!r}; expected N=speaker_id[:moderator]")
        out[int(key)] = (speaker_id, role)
    return out


def diarized_words(resp: dict[str, Any]) -> list[dict[str, Any]]:
    alts = resp["results"]["channels"][0]["alternatives"]
    return alts[0].get("words", []) if alts else []


def turns_from_words(words: list[dict[str, Any]], mapping: dict[int, tuple[str, str]]) -> list[TimelineEntry]:
    """Runs of the same diarized speaker -> timeline entries. Blips are folded into the previous turn,
    then neighbours with the same speaker are merged. Unmapped speakers become dg<N>."""
    runs: list[dict[str, Any]] = []
    for w in words:
        spk = int(w.get("speaker", 0))
        if runs and runs[-1]["spk"] == spk:
            runs[-1]["end"], runs[-1]["n"] = w["end"], runs[-1]["n"] + 1
        else:
            runs.append({"spk": spk, "start": w["start"], "end": w["end"], "n": 1})
    merged: list[dict[str, Any]] = []
    for i, r in enumerate(runs):
        nxt = runs[i + 1] if i + 1 < len(runs) else None
        short = r["end"] - r["start"] < MIN_TURN_S and r["n"] < MIN_TURN_WORDS
        blip = short and bool(merged) and nxt is not None and nxt["spk"] == merged[-1]["spk"]  # interjection
        if merged and (blip or merged[-1]["spk"] == r["spk"]):
            merged[-1]["end"], merged[-1]["n"] = r["end"], merged[-1]["n"] + r["n"]
        else:
            merged.append(dict(r))
    out: list[TimelineEntry] = []
    for r in merged:
        speaker_id, role = mapping.get(r["spk"], (f"dg{r['spk']}", "speaker"))
        if out and out[-1].speaker_id == speaker_id:  # two Deepgram ids mapped to one person
            out[-1] = out[-1].model_copy(update={"t_end": r["end"]})
            continue
        out.append(TimelineEntry(t_start=round(r["start"], 2), t_end=round(r["end"], 2), speaker_id=speaker_id, role=role))  # type: ignore[arg-type]
    return out


def transcript_from_words(clip_id: str, words: list[dict[str, Any]], timeline: list[TimelineEntry]) -> list[Segment]:
    """Sentences with speaker/role from the (hand-fixed) timeline, via the live segmenter."""
    seg = Segmenter(clip_id, ingest.TimelineLookup(timeline))
    ws = [
        Word(text=w.get("punctuated_word") or w["word"], t_start=w["start"], t_end=w["end"],
             confidence=max(0.0, min(1.0, float(w.get("confidence", 0.0)))))
        for w in words
    ]
    return seg.on_final(ws) + seg.flush()


def format_turns(timeline: list[TimelineEntry], segments: list[Segment], width: int = 110) -> str:
    lines = []
    for i, e in enumerate(timeline):
        text = " ".join(s.text for s in segments if e.t_start <= (s.t_start + s.t_end) / 2 <= e.t_end)
        tag = f"{i:>3} {e.t_start:>8.2f}-{e.t_end:<8.2f} {e.speaker_id:<14} {'MOD ' if e.role == 'moderator' else '    '}"
        lines.append(tag + (text[: width - len(tag) - 3] + "..." if len(tag) + len(text) > width else text))
    return "\n".join(lines)


# --- io -------------------------------------------------------------------------


def clip_dir(clip_id: str) -> Path:
    return ingest.CLIPS_DIR / clip_id


def rel(path: Path, folder: Path) -> str:
    path = path.resolve()
    return str(path.relative_to(folder.resolve())) if path.is_relative_to(folder.resolve()) else str(path)


def transcribe(audio: Path, api_key: str) -> dict[str, Any]:
    if not api_key:
        raise SystemExit("DEEPGRAM_API_KEY is not set (backend/.env)")
    ctype = mimetypes.guess_type(audio.name)[0] or "application/octet-stream"
    print(f"Deepgram prerecorded: {audio} ({audio.stat().st_size / 1e6:.1f} MB) ...", file=sys.stderr)
    r = httpx.post(DEEPGRAM_PRERECORDED_URL, params=PRERECORDED_PARAMS, content=audio.read_bytes(),
                   headers={"Authorization": f"Token {api_key}", "Content-Type": ctype}, timeout=600)
    r.raise_for_status()
    return r.json()


def load_cached_words(folder: Path) -> list[dict[str, Any]]:
    cache = folder / CACHE_NAME
    if not cache.exists():
        raise SystemExit(f"{cache} not found; run `draft` first")
    return diarized_words(json.loads(cache.read_text()))


def write_timeline(folder: Path, timeline: list[TimelineEntry]) -> None:
    data = [json.loads(e.model_dump_json()) for e in timeline]
    (folder / "timeline.json").write_text(json.dumps(data, indent=2) + "\n")


def write_transcript(folder: Path, segments: list[Segment]) -> None:
    (folder / "transcript.jsonl").write_text("".join(s.model_dump_json() + "\n" for s in segments))


def report(timeline: list[TimelineEntry]) -> None:
    problems = ingest.validate_timeline(timeline)
    speakers = sorted({(e.speaker_id, e.role) for e in timeline})
    print(f"{len(timeline)} turns; speakers: " + ", ".join(f"{s}({r})" for s, r in speakers))
    for p in problems:
        print(f"  PROBLEM: {p}")
    unmapped = [s for s, _ in speakers if s.startswith("dg")]
    if unmapped:
        print(f"  unmapped Deepgram speakers {unmapped}: rerun draft with --map N=speaker_id[:moderator]")


# --- commands --------------------------------------------------------------------


def cmd_draft(a: argparse.Namespace) -> None:
    folder = clip_dir(a.clip_id)
    folder.mkdir(parents=True, exist_ok=True)
    existing = Clip.model_validate_json((folder / "clip.json").read_text()) if (folder / "clip.json").exists() else None
    audio = Path(a.audio) if a.audio else (folder / "audio.wav")
    if existing and not a.audio and existing.audio_path:
        audio = folder / existing.audio_path
    try:
        ingest.open_pcm(audio).close()
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(str(e))

    cache = folder / CACHE_NAME
    if a.refresh or not cache.exists():
        cache.write_text(json.dumps(transcribe(audio, dbm.lenient_settings().deepgram_api_key.get_secret_value())))
    words = diarized_words(json.loads(cache.read_text()))
    if not words:
        raise SystemExit("Deepgram returned no words")

    tl_path = folder / "timeline.json"
    unmapped_draft = tl_path.exists() and all(e.speaker_id.startswith("dg") for e in ingest.load_timeline_file(tl_path))
    if tl_path.exists() and not (a.overwrite or unmapped_draft):
        raise SystemExit("timeline.json exists (maybe hand-fixed). Use --overwrite to replace it, or `build`.")
    timeline = turns_from_words(words, parse_map(a.map))
    segments = transcript_from_words(a.clip_id, words, timeline)
    clip = Clip(
        clip_id=a.clip_id,
        title=a.title or (existing.title if existing else a.clip_id),
        video_path=rel(Path(a.video), folder) if a.video else (existing.video_path if existing else ""),
        audio_path=rel(audio, folder),
        timeline=[],  # source of truth is timeline.json next to clip.json
        transcript_path="transcript.jsonl",
        duration=round(words[-1]["end"], 2),
    )
    (folder / "clip.json").write_text(clip.model_dump_json(indent=2) + "\n")
    write_timeline(folder, timeline)
    write_transcript(folder, segments)
    print(format_turns(timeline, segments))
    report(timeline)
    print(f"\nWrote {folder}/clip.json, timeline.json, transcript.jsonl. Hand-fix timeline.json, then `build` and `push`.")


def cmd_show(a: argparse.Namespace) -> None:
    folder = clip_dir(a.clip_id)
    timeline = ingest.load_timeline_file(folder / "timeline.json")
    segments = transcript_from_words(a.clip_id, load_cached_words(folder), timeline)
    print(format_turns(timeline, segments, width=a.width))
    report(timeline)


def cmd_build(a: argparse.Namespace) -> None:
    folder = clip_dir(a.clip_id)
    timeline = ingest.load_timeline_file(folder / "timeline.json")
    segments = transcript_from_words(a.clip_id, load_cached_words(folder), timeline)
    write_transcript(folder, segments)
    report(timeline)
    print(f"Wrote {len(segments)} sentences to {folder}/transcript.jsonl")


async def cmd_push(a: argparse.Namespace) -> None:
    timeline = ingest.load_timeline_file(clip_dir(a.clip_id) / "timeline.json")
    problems = ingest.validate_timeline(timeline)
    if problems and not a.force:
        raise SystemExit("timeline has problems (fix them or --force):\n  " + "\n  ".join(problems))
    db = dbm.connect()
    if not await dbm.ping(db):
        raise SystemExit("MongoDB unreachable (MONGODB_URI)")
    await dbm.ensure_indexes(db)
    await dbm.save_timeline(db, a.clip_id, timeline)
    print(f"Pushed {len(timeline)} turns for {a.clip_id} to {db.name}.timelines")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft", help="Deepgram diarization -> clip.json, timeline.json, transcript.jsonl")
    d.add_argument("clip_id")
    d.add_argument("--audio", help="16 kHz mono WAV (default: data/clips/<id>/audio.wav)")
    d.add_argument("--video", help="clip.mp4 the browser plays")
    d.add_argument("--title")
    d.add_argument("--map", action="append", default=[], metavar="N=speaker_id[:moderator]")
    d.add_argument("--refresh", action="store_true", help="call Deepgram again instead of using the cache")
    d.add_argument("--overwrite", action="store_true", help="replace an existing timeline.json")
    s = sub.add_parser("show", help="print each timeline turn with its text")
    s.add_argument("clip_id")
    s.add_argument("--width", type=int, default=160)
    b = sub.add_parser("build", help="rebuild transcript.jsonl from the hand-fixed timeline.json")
    b.add_argument("clip_id")
    u = sub.add_parser("push", help="save timeline.json to MongoDB")
    u.add_argument("clip_id")
    u.add_argument("--force", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "push":
        asyncio.run(cmd_push(a))
    else:
        {"draft": cmd_draft, "show": cmd_show, "build": cmd_build}[a.cmd](a)


if __name__ == "__main__":
    main()
