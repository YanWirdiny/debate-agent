"""P1 MongoDB: indexes, idempotent event sink, sessions, timelines, writer -> Mongo end to end.

Runs against MONGODB_TEST_URI if set, else a throwaway local `mongod` (Homebrew or Docker image
binary on PATH). Skipped when neither is available.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import uuid

import pytest
from pymongo.errors import DuplicateKeyError

from app import db as dbm
from app.config import Settings
from app.eventlog import EventWriter, verify_chain
from app.main import SessionManager
from app.models import Event, TimelineEntry


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mongo_uri(tmp_path_factory):
    if uri := os.environ.get("MONGODB_TEST_URI"):
        yield uri
        return
    if not shutil.which("mongod"):
        pytest.skip("no MONGODB_TEST_URI and no mongod on PATH")
    port = _free_port()
    proc = subprocess.Popen(
        ["mongod", "--dbpath", str(tmp_path_factory.mktemp("mongo")), "--port", str(port), "--bind_ip", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            import time

            time.sleep(0.1)
    yield f"mongodb://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(10)


@pytest.fixture
async def db(mongo_uri):
    database = dbm.connect(mongo_uri, f"test_{uuid.uuid4().hex[:8]}")
    assert await dbm.ping(database)
    await dbm.ensure_indexes(database)
    yield database
    await database.client.drop_database(database.name)
    database.client.close()


def ev(seq: int, session_id: str = "s") -> Event:
    return Event(session_id=session_id, seq=seq, ts=1.0, type="segment", payload_json="{}", prev_hash="", hash=str(seq))


async def test_indexes_and_idempotent_sink(db):
    names = {i["name"] for i in await db["events"].list_indexes().to_list(None)}
    assert {"session_seq", "session_type"} <= names
    sink = dbm.MongoSink(db)
    await sink.insert([ev(0), ev(1)])
    await sink.insert([ev(1), ev(2)])  # writer retry overlapping a stored batch: no error, no duplicate
    assert [e.seq for e in await dbm.load_events(db, "s")] == [0, 1, 2]
    with pytest.raises(DuplicateKeyError):
        await db["events"].insert_one(ev(0).model_dump())  # the unique index is real


async def test_writer_to_mongo_chain_and_jsonl_import(db, tmp_path):
    wr = EventWriter("w", sink=dbm.MongoSink(db), batch_interval=0.01)
    task = asyncio.create_task(wr.run())
    for i in range(5):
        wr.put("segment", {"i": i})
    await wr.stop()
    await task
    events = await dbm.load_events(db, "w")
    assert len(events) == 5 and verify_chain(events)

    path = tmp_path / "events.jsonl"  # left behind by a run while Mongo was down
    path.write_text("".join(e.model_dump_json() + "\n" for e in [ev(0, "offline"), ev(1, "offline")]))
    assert await dbm.import_events_jsonl(db, path) == 2
    assert await dbm.import_events_jsonl(db, path) == 2  # rerun is safe
    assert len(await dbm.load_events(db, "offline")) == 2


async def test_sessions_and_timelines(db):
    await dbm.save_session(db, "x", clip_id="c", t0_wall=1.5)
    await dbm.save_session(db, "x", ended_at=9.0)
    assert await dbm.get_session(db, "x") == {"session_id": "x", "clip_id": "c", "t0_wall": 1.5, "ended_at": 9.0}
    tl = [TimelineEntry(t_start=0, t_end=2, speaker_id="moderator", role="moderator")]
    await dbm.save_timeline(db, "c", tl)
    await dbm.save_timeline(db, "c", tl + [TimelineEntry(t_start=2, t_end=4, speaker_id="a", role="speaker")])
    assert [e.speaker_id for e in await dbm.load_timeline(db, "c")] == ["moderator", "a"]
    assert await dbm.load_timeline(db, "missing") == []


async def test_manager_from_settings_writes_session_to_mongo(db, mongo_uri, tmp_path):
    settings = Settings(replay_transcript=True, mongodb_uri=mongo_uri, mongodb_db=db.name)
    mgr = await SessionManager.from_settings(settings)
    mgr.runs_dir = tmp_path
    mgr.speed = 40
    await mgr.load("fixture")
    await mgr.start("fixture")
    runner = mgr.runner
    await asyncio.wait_for(runner.wait(), 15)
    await asyncio.sleep(0.2)  # session-end record is fire-and-forget

    events = await dbm.load_events(mgr.db, runner.session_id)
    assert verify_chain(events) and events[0].type == "session_start" and events[-1].type == "session_end"
    doc = await dbm.get_session(mgr.db, runner.session_id)
    assert doc["clip_id"] == "fixture" and doc["source"] == "replay" and doc["event_count"] == len(events)
    assert not (tmp_path / runner.session_id / "events.jsonl").exists()  # Mongo was up: no fallback file
    assert json.loads(events[0].payload_json)["clip_id"] == "fixture"
