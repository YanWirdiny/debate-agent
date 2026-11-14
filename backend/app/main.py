"""FastAPI app (P1): routes and WebSocket endpoints. Run: uvicorn app.main:app --workers 1

Operator protocol: send `load` (warm-up, server replies `ready`), then `start` (server replies
`started` with t0_wall to both views, frontend calls video.play()), `kill` at any time.
Audience is receive-only. One live session per process (state is in memory).

Startup reads Settings (backend/.env):
- source: REPLAY_EVENTS=<session_id> replays a recorded run; REPLAY_TRANSCRIPT=1 plays the
  pre-made transcript; otherwise clip audio streams to Deepgram.
- INTEL=fake|real: fake lanes and async plane, or P2's real ones.
MongoDB holds the event log and session records. Every event is also archived locally in
data/runs/<session_id>/log.jsonl, so reports and REPLAY_EVENTS work with the network off.

Routes: GET /health, /clips, /sessions, /report/{session_id} (JSON), /report/{session_id}.html
(print to PDF); WS /ws/operator, /ws/audience.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import TypeAdapter, ValidationError

from app import db as dbm
from app import ingest
from app import report as reports
from app.aggregator import DEFAULT_AUDIENCE_MIN_CONF
from app.config import Settings
from app.eventlog import ARCHIVE_NAME, FALLBACK_NAME, load_session_events
from app.hubs import Hub
from app.models import BannerMessage, ClientMessage, KillMessage, LoadMessage, StartMessage
from app.session import Intel, SessionRunner, Source

log = logging.getLogger(__name__)
_client_msg = TypeAdapter(ClientMessage)


RUNS_DIR = ingest.DATA_DIR / "runs"


class SessionManager:
    def __init__(
        self,
        *,
        source: Source = "replay",
        deepgram_key: str = "",
        db: Any = None,
        audience_min_conf: float = DEFAULT_AUDIENCE_MIN_CONF,
        runs_dir: Path | None = None,
        intel: str = "fake",
        replay_session_id: str = "",
    ) -> None:
        self.operator = Hub("operator")
        self.audience = Hub("audience")
        self.runner: SessionRunner | None = None
        self.speed = 1.0
        self.source: Source = source
        self.deepgram_key = deepgram_key
        self.db = db
        self.audience_min_conf = audience_min_conf
        self.runs_dir = runs_dir
        self.intel = intel
        self.replay_session_id = replay_session_id

    @classmethod
    async def from_settings(cls, settings: Settings | None = None) -> SessionManager:
        s = settings or Settings()  # raises MissingRuntimeConfig unless a replay mode is set
        db = dbm.connect(s.mongodb_uri.get_secret_value(), s.mongodb_db)
        if await dbm.ping(db):
            await dbm.ensure_indexes(db)
        else:
            log.warning("MongoDB unreachable at startup; event log goes to %s until it is back", RUNS_DIR)
        replay_events = s.replay_events.strip()
        source: Source = "events" if replay_events else "replay" if s.replay_transcript else "deepgram"
        log.info("source=%s intel=%s mongo=%s", source, s.intel, s.mongodb_db)
        return cls(
            source=source,
            intel=s.intel,
            replay_session_id=replay_events,
            deepgram_key=s.deepgram_api_key.get_secret_value(),
            db=db,
            audience_min_conf=s.audience_min_conf,
            runs_dir=RUNS_DIR,
        )

    async def load(self, clip_id: str) -> None:
        if self.runner is not None:
            await self.runner.stop()
            self.runner = None
        session_id = f"{clip_id}-{uuid.uuid4().hex[:8]}"
        fallback = archive = None
        if self.runs_dir is not None:
            (self.runs_dir / session_id).mkdir(parents=True, exist_ok=True)
            fallback = self.runs_dir / session_id / FALLBACK_NAME
            archive = self.runs_dir / session_id / ARCHIVE_NAME
        runner = SessionRunner(
            session_id, clip_id, operator=self.operator, audience=self.audience, speed=self.speed,
            source=self.source, deepgram_key=self.deepgram_key, db=self.db, intel=Intel.by_name(self.intel),
            sink=dbm.MongoSink(self.db) if self.db is not None else None, fallback_path=fallback,
            archive_path=archive, audience_min_conf=self.audience_min_conf,
            replay_session_id=self.replay_session_id, runs_dir=self.runs_dir,
        )
        await runner.warm_up()
        self.runner = runner

    async def report(self, session_id: str) -> dict:
        events = await load_session_events(session_id, self.db, self.runs_dir)
        if not events:
            raise KeyError(session_id)
        eval_results = None
        if self.db is not None:  # P2's run_eval.py writes eval_results; newest doc wins
            try:
                eval_results = await self.db["eval_results"].find_one({}, {"_id": 0}, sort=[("_id", -1)])
            except Exception as e:
                log.warning("eval_results unavailable: %s", e)
        return reports.build_report(events, eval_results)

    async def sessions(self) -> list[dict]:
        """Recorded sessions, newest first: Mongo records plus local runs (network off)."""
        found: dict[str, dict] = {}
        if self.db is not None:
            try:
                async for d in self.db["sessions"].find({}, {"_id": 0}).sort("t0_wall", -1).limit(100):
                    found[d["session_id"]] = d
            except Exception as e:
                log.warning("sessions unavailable from MongoDB: %s", e)
        if self.runs_dir is not None and self.runs_dir.exists():
            for d in self.runs_dir.iterdir():
                if (d / ARCHIVE_NAME).exists() and d.name not in found:
                    found[d.name] = {"session_id": d.name, "t0_wall": (d / ARCHIVE_NAME).stat().st_mtime}
        return sorted(found.values(), key=lambda d: d.get("t0_wall") or 0, reverse=True)

    async def shutdown(self) -> None:
        if self.runner is not None:
            await self.runner.stop()

    async def start(self, clip_id: str) -> None:
        if self.runner is None or self.runner.clip_id != clip_id:
            await self.load(clip_id)
        assert self.runner is not None
        if self.runner.t0_wall is not None:
            raise ValueError("session already started; send load to begin a new one")
        await self.runner.start()

    def kill(self, annotation_id: str) -> None:
        if self.runner is None:
            raise ValueError("no active session")
        self.runner.aggregator.kill(annotation_id, by="operator")

    async def handle(self, raw: str) -> None:
        msg = _client_msg.validate_json(raw)
        if isinstance(msg, LoadMessage):
            await self.load(msg.clip_id)
        elif isinstance(msg, StartMessage):
            await self.start(msg.clip_id)
        elif isinstance(msg, KillMessage):
            self.kill(msg.annotation_id)


def create_app(manager: SessionManager | None = None) -> FastAPI:
    """With no manager (uvicorn), the manager is built from Settings at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal sessions
        if sessions is None:
            sessions = await SessionManager.from_settings()
        yield
        await sessions.shutdown()

    app = FastAPI(title="BS Subtitles", lifespan=lifespan)
    sessions: SessionManager | None = manager

    def mgr() -> SessionManager:
        assert sessions is not None, "app not started"
        return sessions

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/clips")
    async def clips() -> list[dict]:
        return [{"clip_id": c.clip_id, "title": c.title, "duration": c.duration} for c in ingest.list_clips()]

    @app.get("/sessions")
    async def sessions_list() -> list[dict]:
        return await mgr().sessions()

    @app.get("/report/{session_id}.html", response_class=HTMLResponse)
    async def report_html(session_id: str) -> str:
        return reports.render_html(await _report(session_id))

    @app.get("/report/{session_id}")
    async def report(session_id: str) -> dict:
        return await _report(session_id)

    async def _report(session_id: str) -> dict:
        try:
            return await mgr().report(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no event log for session {session_id}")

    @app.websocket("/ws/audience")
    async def ws_audience(ws: WebSocket) -> None:
        await ws.accept()
        mgr().audience.add(ws)
        try:
            while True:
                await ws.receive_text()  # receive-only view; keeps the socket open
        except WebSocketDisconnect:
            pass
        finally:
            mgr().audience.remove(ws)

    @app.websocket("/ws/operator")
    async def ws_operator(ws: WebSocket) -> None:
        await ws.accept()
        mgr().operator.add(ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    await mgr().handle(raw)
                except (ValidationError, ValueError, OSError) as e:  # OSError: missing file, Deepgram unreachable
                    await mgr().operator.send(ws, BannerMessage(level="error", text=str(e)[:300]))
        except WebSocketDisconnect:
            pass
        finally:
            mgr().operator.remove(ws)

    return app


app = create_app()
