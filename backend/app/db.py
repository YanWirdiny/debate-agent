"""MongoDB (P1): motor client, collections, indexes, and the event-log sink.

Collections owned by P1: `events`, `sessions`, `timelines`. P2 owns the KB collections and asks
P1 for their indexes (add them to `KB_INDEXES`).

Nothing on the hot path awaits Mongo: the event writer batches in its own task, and session
records are written from background tasks. Every call has a short server-selection timeout so an
unreachable Mongo fails fast and the writer falls back to `events.jsonl`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, IndexModel
from pymongo.errors import BulkWriteError

from pydantic_settings import BaseSettings

from app.config import Settings
from app.models import Event, TimelineEntry

log = logging.getLogger(__name__)

DUPLICATE_KEY = 11000
SERVER_SELECTION_TIMEOUT_MS = 2000

INDEXES: dict[str, list[IndexModel]] = {
    "events": [
        IndexModel([("session_id", ASCENDING), ("seq", ASCENDING)], unique=True, name="session_seq"),
        IndexModel([("session_id", ASCENDING), ("type", ASCENDING)], name="session_type"),
    ],
    "sessions": [IndexModel([("session_id", ASCENDING)], unique=True, name="session_id")],
    "timelines": [IndexModel([("clip_id", ASCENDING)], unique=True, name="clip_id")],
}
# KB collections (shapes in tests/fixtures/kb_small/README.md); loaded by scripts/embed_kb.py.
KB_INDEXES: dict[str, list[IndexModel]] = {
    "claims": [IndexModel([("speaker_id", ASCENDING), ("reviewed", ASCENDING)], name="speaker_reviewed")],
    "statements": [IndexModel([("statement_id", ASCENDING)], unique=True, name="statement_id"),
                   IndexModel([("speaker_id", ASCENDING)], name="speaker_id")],
    "passages": [IndexModel([("passage_id", ASCENDING)], unique=True, name="passage_id"),
                 IndexModel([("topic", ASCENDING)], name="topic")],
    "speaker_digests": [IndexModel([("speaker_id", ASCENDING)], unique=True, name="speaker_id")],
}


class _LenientSettings(Settings):
    def __init__(self, **kwargs: object) -> None:
        BaseSettings.__init__(self, **kwargs)  # skip Settings' missing-key check, keep every value as set


def lenient_settings() -> Settings:
    """Settings for P1 scripts and the Mongo connection: reads .env and the environment but skips
    the startup check for LLM keys (label_clip.py only needs DEEPGRAM_API_KEY and MONGODB_URI)."""
    return _LenientSettings()


def connect(uri: str | None = None, db_name: str | None = None) -> AsyncIOMotorDatabase:
    """Client creation does no I/O; the first operation connects. Use `ping()` to check."""
    if uri is None or db_name is None:
        s = lenient_settings()
        uri = uri or s.mongodb_uri.get_secret_value()
        db_name = db_name or s.mongodb_db
    client: AsyncIOMotorClient = AsyncIOMotorClient(
        uri, serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS, tz_aware=True
    )
    return client[db_name]


async def ping(db: AsyncIOMotorDatabase) -> bool:
    try:
        await db.command("ping")
        return True
    except Exception as e:
        log.warning("MongoDB ping failed: %s", e)
        return False


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    for name, indexes in {**INDEXES, **KB_INDEXES}.items():
        await db[name].create_indexes(indexes)


# --- events ------------------------------------------------------------------


class MongoSink:
    """EventSink for EventWriter. Idempotent: the writer retries a failed batch together with
    later ones, so events already stored (duplicate session_id+seq) are skipped, not errors."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.coll = db["events"]

    async def insert(self, events: list[Event]) -> None:
        if not events:
            return
        try:
            await self.coll.insert_many([e.model_dump() for e in events], ordered=False)
        except BulkWriteError as e:
            other = [w for w in e.details.get("writeErrors", []) if w.get("code") != DUPLICATE_KEY]
            if other:
                raise


async def load_events(db: AsyncIOMotorDatabase, session_id: str) -> list[Event]:
    cursor = db["events"].find({"session_id": session_id}, {"_id": 0}).sort("seq", ASCENDING)
    return [Event.model_validate(d) async for d in cursor]


async def import_events_jsonl(db: AsyncIOMotorDatabase, path: str | Path) -> int:
    """Flush a local events.jsonl (written while Mongo was down) into Mongo. Safe to rerun."""
    events = [Event.model_validate_json(l) for l in Path(path).read_text().splitlines() if l.strip()]
    await MongoSink(db).insert(events)
    return len(events)


# --- sessions ----------------------------------------------------------------


async def save_session(db: AsyncIOMotorDatabase, session_id: str, **fields: Any) -> None:
    """Upsert the session record (clip_id, mode, t0_wall at start; ended_at, event_count at end)."""
    await db["sessions"].update_one(
        {"session_id": session_id}, {"$set": {"session_id": session_id, **fields}}, upsert=True
    )


async def get_session(db: AsyncIOMotorDatabase, session_id: str) -> dict[str, Any] | None:
    return await db["sessions"].find_one({"session_id": session_id}, {"_id": 0})


# --- timelines ---------------------------------------------------------------


async def save_timeline(db: AsyncIOMotorDatabase, clip_id: str, timeline: list[TimelineEntry]) -> None:
    await db["timelines"].replace_one(
        {"clip_id": clip_id},
        {"clip_id": clip_id, "timeline": [json.loads(e.model_dump_json()) for e in timeline]},
        upsert=True,
    )


async def load_timeline(db: AsyncIOMotorDatabase, clip_id: str) -> list[TimelineEntry]:
    doc = await db["timelines"].find_one({"clip_id": clip_id})
    return [TimelineEntry.model_validate(e) for e in doc["timeline"]] if doc else []
