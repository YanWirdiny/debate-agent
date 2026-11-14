"""WebSocket hubs (P1): fan-out to audience and operator clients.

A slow or dead client never blocks the others: each send has a timeout and failing clients
are dropped.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from pydantic import BaseModel

log = logging.getLogger(__name__)


class Client(Protocol):
    async def send_text(self, data: str) -> None: ...


class Hub:
    def __init__(self, name: str, send_timeout: float = 1.0) -> None:
        self.name = name
        self.send_timeout = send_timeout
        self.clients: set[Client] = set()

    def add(self, client: Client) -> None:
        self.clients.add(client)

    def remove(self, client: Client) -> None:
        self.clients.discard(client)

    async def send(self, client: Client, message: BaseModel) -> None:
        await client.send_text(message.model_dump_json())

    async def broadcast(self, message: BaseModel) -> None:
        if not self.clients:
            return
        data = message.model_dump_json()
        clients = list(self.clients)
        results = await asyncio.gather(
            *(asyncio.wait_for(c.send_text(data), self.send_timeout) for c in clients),
            return_exceptions=True,
        )
        for c, r in zip(clients, results):
            if isinstance(r, BaseException):
                log.warning("%s hub: dropping client after send error: %r", self.name, r)
                self.clients.discard(c)


class RecordingClient:
    """Test / mock-stream client that keeps every message it receives."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, data: str) -> None:
        self.messages.append(data)
