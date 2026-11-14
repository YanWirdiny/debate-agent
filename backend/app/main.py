"""FastAPI app (P1): routes and WebSocket endpoints. Run: uvicorn app.main:app --workers 1

Operator protocol: send `load` (warm-up, server replies `ready`), then `start` (server replies
`started` with t0_wall to both views, frontend calls video.play()), `kill` at any time.
Audience is receive-only. One live session per process (state is in memory).
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app import ingest
from app.hubs import Hub
from app.models import BannerMessage, ClientMessage, KillMessage, LoadMessage, StartMessage
from app.session import SessionRunner

log = logging.getLogger(__name__)
_client_msg = TypeAdapter(ClientMessage)


class SessionManager:
    def __init__(self) -> None:
        self.operator = Hub("operator")
        self.audience = Hub("audience")
        self.runner: SessionRunner | None = None
        self.speed = 1.0

    async def load(self, clip_id: str) -> None:
        if self.runner is not None:
            await self.runner.stop()
        session_id = f"{clip_id}-{uuid.uuid4().hex[:8]}"
        self.runner = SessionRunner(
            session_id, clip_id, operator=self.operator, audience=self.audience, speed=self.speed
        )
        await self.runner.warm_up()

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
    app = FastAPI(title="BS Subtitles")
    app.state.sessions = sessions = manager or SessionManager()

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/clips")
    async def clips() -> list[dict]:
        return [{"clip_id": c.clip_id, "title": c.title, "duration": c.duration} for c in ingest.list_clips()]

    @app.get("/report/{session_id}")
    async def report(session_id: str) -> dict:
        raise HTTPException(status_code=501, detail="not implemented")

    @app.websocket("/ws/audience")
    async def ws_audience(ws: WebSocket) -> None:
        await ws.accept()
        sessions.audience.add(ws)
        try:
            while True:
                await ws.receive_text()  # receive-only view; keeps the socket open
        except WebSocketDisconnect:
            pass
        finally:
            sessions.audience.remove(ws)

    @app.websocket("/ws/operator")
    async def ws_operator(ws: WebSocket) -> None:
        await ws.accept()
        sessions.operator.add(ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    await sessions.handle(raw)
                except (ValidationError, ValueError, FileNotFoundError) as e:
                    await sessions.operator.send(ws, BannerMessage(level="error", text=str(e)[:300]))
        except WebSocketDisconnect:
            pass
        finally:
            sessions.operator.remove(ws)

    return app


app = create_app()
