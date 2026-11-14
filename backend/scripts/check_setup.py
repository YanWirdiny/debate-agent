"""check_setup.py (P1): is this machine ready to run a session? Read-only except for creating
P1's Mongo indexes (idempotent).

    python scripts/check_setup.py            # everything
    python scripts/check_setup.py --no-net   # skip the Deepgram key check

Checks: backend/.env and mode, MongoDB (ping, database, indexes, P1/P2 collections),
Deepgram key, ffmpeg, every clip in data/clips (audio format, timeline, transcript), local runs.
Exit code 1 if anything required for the configured mode is missing.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app import db as dbm  # noqa: E402
from app import ingest  # noqa: E402
from app.config import ENV_FILE, MissingRuntimeConfig, Settings  # noqa: E402

OK, WARN, FAIL = "ok  ", "warn", "FAIL"
problems = 0


def line(status: str, what: str, detail: str = "") -> None:
    global problems
    problems += status == FAIL
    print(f"[{status}] {what}" + (f": {detail}" if detail else ""))


def redact(uri: str) -> str:
    if "@" in uri and "://" in uri:
        scheme, rest = uri.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return uri


async def check_mongo(s: Settings) -> None:
    uri = s.mongodb_uri.get_secret_value()
    db = dbm.connect(uri, s.mongodb_db)
    if not await dbm.ping(db):
        line(FAIL, "MongoDB", f"cannot reach {redact(uri)} (Atlas: add your IP under Network Access; check user/password)")
        return
    line(OK, "MongoDB", f"{redact(uri)} database={s.mongodb_db}")
    try:
        await dbm.ensure_indexes(db)
        line(OK, "P1 indexes", "events, sessions, timelines")
    except Exception as e:
        line(FAIL, "P1 indexes", f"{e} (the DB user needs readWrite on {s.mongodb_db})")
        return
    names = set(await db.list_collection_names())
    for coll in ("claims", "statements", "passages", "speaker_digests"):
        n = await db[coll].estimated_document_count() if coll in names else 0
        line(OK if n else WARN, f"KB collection {coll}", f"{n} docs" + ("" if n else " (scripts/embed_kb.py loads it)"))
    n_sessions = await db["sessions"].estimated_document_count() if "sessions" in names else 0
    timelines = [d["clip_id"] async for d in db["timelines"].find({}, {"clip_id": 1})] if "timelines" in names else []
    line(OK, "recorded sessions", str(n_sessions))
    line(OK if timelines else WARN, "timelines in Mongo", ", ".join(timelines) or "none (label_clip.py push)")


def check_deepgram(key: str) -> None:
    if not key:
        line(FAIL, "Deepgram key", "DEEPGRAM_API_KEY is empty")
        return
    try:
        r = httpx.get("https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {key}"}, timeout=10)
    except httpx.HTTPError as e:
        line(WARN, "Deepgram key", f"could not reach Deepgram: {e}")
        return
    line(OK if r.status_code == 200 else FAIL, "Deepgram key", "accepted" if r.status_code == 200 else f"HTTP {r.status_code}")


def check_model(required: bool) -> None:
    try:
        from sentence_transformers import SentenceTransformer

        SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", local_files_only=True)
        line(OK, "embedding model", "all-MiniLM-L6-v2 cached locally")
    except Exception:
        line(FAIL if required else WARN, "embedding model", "not cached: python scripts/embed_kb.py --download-model")


def check_dossiers() -> None:
    from scripts.kb_files import DOSSIERS, read_jsonl

    speakers = sorted({e.speaker_id for c in ingest.list_clips() if c.clip_id != "fixture" for e in c.timeline if e.role == "speaker"})
    for spk in speakers:
        d = DOSSIERS / spk
        rows = read_jsonl(d / "claims.jsonl")
        reviewed = sum(1 for r in rows if r.get("reviewed"))
        status = OK if reviewed else WARN
        line(status, f"dossier {spk}", f"{reviewed} reviewed / {len(rows)} claims" + ("" if d.exists() else
             " (none: scripts/fetch_factchecks.py + build_dossier.py; id must match the clip timeline)"))


def check_clips(live: bool) -> None:
    clips = ingest.list_clips()
    if not clips:
        line(FAIL, "clips", f"none in {ingest.CLIPS_DIR}")
    for c in clips:
        tag = f"clip {c.clip_id}"
        try:
            with ingest.open_pcm(c.audio_path) as w:
                line(OK, f"{tag} audio", f"{w.getnframes() / ingest.SAMPLE_RATE:.0f} s, 16 kHz mono")
        except (FileNotFoundError, ValueError) as e:
            line(FAIL if live and c.clip_id != "fixture" else WARN, f"{tag} audio", str(e).split(" (")[0])
        issues = ingest.validate_timeline(c.timeline)
        line(WARN if issues else OK, f"{tag} timeline", "; ".join(issues) if issues else f"{len(c.timeline)} turns, "
             + ", ".join(sorted({e.speaker_id for e in c.timeline})))
        has_t = bool(c.transcript_path and Path(c.transcript_path).exists())
        line(OK if has_t else WARN, f"{tag} transcript", "present (replay + STT fallback)" if has_t else "missing: no STT fallback")
        if not c.video_path or not Path(c.video_path).exists():
            line(WARN, f"{tag} video", "missing (the browser plays it)")


async def main(no_net: bool) -> int:
    print(f".env: {ENV_FILE} {'(found)' if ENV_FILE.exists() else '(MISSING: cp .env.example .env)'}")
    try:
        s = Settings()
    except MissingRuntimeConfig as e:  # only raised when no replay mode is set: the server will not start
        s = dbm.lenient_settings()
        line(FAIL, "live mode", f"missing {', '.join(e.missing)}; set them, or REPLAY_TRANSCRIPT=1 to run without keys")
    mode = ("REPLAY_EVENTS=" + s.replay_events) if s.replay_events.strip() else "REPLAY_TRANSCRIPT" if s.replay_transcript else "live (Deepgram)"
    line(OK, "mode", f"{mode}, INTEL={s.intel}")
    await check_mongo(s)
    live = mode.startswith("live")
    if not no_net and (live or s.deepgram_api_key.get_secret_value()):
        check_deepgram(s.deepgram_api_key.get_secret_value())
    check_model(required=s.intel in ("real", "bank"))
    check_dossiers()
    line(OK if shutil.which("ffmpeg") else WARN, "ffmpeg", "found" if shutil.which("ffmpeg") else "not found (needed to make audio.wav)")
    check_clips(live)
    print("\nready" if not problems else f"\n{problems} problem(s) to fix")
    return 1 if problems else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-net", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args().no_net)))
