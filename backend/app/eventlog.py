"""Event log writer (P1). One instance per session; the only writer of the event log.

- assigns `seq`, chains `hash = sha256(prev_hash + payload_json)` (genesis prev_hash = "")
- batches inserts every 250 ms
- if the sink fails: keeps events in memory, appends them once to a local `events.jsonl`,
  and retries the sink on the next batch (no loss, no reordering)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from app.models import Event

log = logging.getLogger(__name__)

EventType = str  # one of Event.type's literals; validated when the Event is built


class EventSink(Protocol):
    async def insert(self, events: list[Event]) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def insert(self, events: list[Event]) -> None:
        self.events.extend(events)


def payload_json(payload: BaseModel | dict[str, Any]) -> str:
    if isinstance(payload, BaseModel):
        return payload.model_dump_json()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def chain_hash(prev_hash: str, payload: str) -> str:
    return hashlib.sha256((prev_hash + payload).encode()).hexdigest()


def verify_chain(events: list[Event]) -> bool:
    prev = ""
    for i, e in enumerate(sorted(events, key=lambda e: e.seq)):
        if e.seq != i or e.prev_hash != prev or e.hash != chain_hash(prev, e.payload_json):
            return False
        prev = e.hash
    return True


_STOP = object()


class EventWriter:
    def __init__(
        self,
        session_id: str = "",
        sink: EventSink | None = None,
        fallback_path: Path | None = None,
        batch_interval: float = 0.25,
    ) -> None:
        self.session_id = session_id
        self.sink: EventSink = sink or MemorySink()
        self.fallback_path = fallback_path
        self.batch_interval = batch_interval
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._seq = 0
        self._prev_hash = ""
        self._unsent: list[Event] = []  # failed to reach the sink, already in events.jsonl

    def put(self, type: EventType, payload: BaseModel | dict[str, Any]) -> None:
        """Non-blocking; called from the aggregator and segmenter paths."""
        self._q.put_nowait((time.time(), type, payload_json(payload)))

    async def stop(self) -> None:
        self._q.put_nowait(_STOP)

    async def run(self) -> None:
        stopping = False
        while not stopping:
            first = await self._q.get()
            batch = [first]
            if first is not _STOP:
                await asyncio.sleep(self.batch_interval)
            while not self._q.empty():
                batch.append(self._q.get_nowait())
            stopping = any(item is _STOP for item in batch)
            await self._write([self._chain(*item) for item in batch if item is not _STOP])

    def _chain(self, ts: float, type: EventType, payload: str) -> Event:
        h = chain_hash(self._prev_hash, payload)
        e = Event(session_id=self.session_id, seq=self._seq, ts=ts, type=type,  # type: ignore[arg-type]
                  payload_json=payload, prev_hash=self._prev_hash, hash=h)
        self._seq += 1
        self._prev_hash = h
        return e

    async def _write(self, events: list[Event]) -> None:
        pending = self._unsent + events
        if not pending:
            return
        try:
            await self.sink.insert(pending)
            if self._unsent:
                log.warning("event sink recovered, flushed %d buffered events", len(self._unsent))
            self._unsent = []
        except Exception:
            log.exception("event sink failed; buffering %d events", len(events))
            self._append_fallback(events)
            self._unsent = pending

    def _append_fallback(self, events: list[Event]) -> None:
        if self.fallback_path is None or not events:
            return
        with self.fallback_path.open("a") as f:
            for e in events:
                f.write(e.model_dump_json() + "\n")
