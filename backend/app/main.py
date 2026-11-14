"""FastAPI app (P1): routes and WebSocket endpoints. Run: uvicorn app.main:app --workers 1"""

from fastapi import FastAPI, HTTPException, WebSocket


def create_app() -> FastAPI:
    app = FastAPI(title="BS Subtitles")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/clips")
    async def clips() -> list[dict]:
        return []

    @app.get("/report/{session_id}")
    async def report(session_id: str) -> dict:
        raise HTTPException(status_code=501, detail="not implemented")

    @app.websocket("/ws/audience")
    async def ws_audience(ws: WebSocket) -> None:
        await ws.accept()
        await ws.close(code=1011)

    @app.websocket("/ws/operator")
    async def ws_operator(ws: WebSocket) -> None:
        await ws.accept()
        await ws.close(code=1011)

    return app


app = create_app()
