"""Analysis cache: reuse model answers across runs of the same video instead of paying for them again.

Every successful structured-output call on the primary model (claim extraction, stance, NLI, fallacy,
grounded web verdict, prefetch, summary) is stored under a key made from what decides its answer:
the prompt text (via its hash, so editing a prompt invalidates its entries), the output schema,
grounding, the configured model and provider, and the input variables. A later call with the same
key returns the stored answer at once: no tokens, no rate limit, no latency. A miss calls the
models as usual and adds the answer.

The model and provider are part of the key because a different LLM_MODEL_* or LLM_PROVIDER is a
different answer. Fallback-provider answers are not stored (see app/llm/client.py): the first
write wins (INSERT OR IGNORE), so a weak backup answer must not occupy the primary model's key.
There is no time-to-live. Prompt edits and model changes already miss, and a demo reuses one clip.

Inputs that change from run to run without changing what is being judged are left out of the key:
`rolling_summary` (written by an async Gemini job; whether it has landed before a given sentence
depends on timing). Everything else must match exactly, so a different sentence, context or
opponent turn is a miss, never a wrong reuse.

Hits are exact only when the transcript is the same: run the clip with REPLAY_TRANSCRIPT=1 (the
saved transcript) rather than live Deepgram, whose text can differ slightly between runs.

Stored locally in SQLite (data/cache/llm_cache.sqlite, git-ignored) so it works offline on demo
day. Reads and writes on the call path touch an in-memory copy only. A background thread persists
new rows, so a slow disk never blocks the event loop or a lane's 1.2 s budget. LLM_CACHE=0 turns
the cache off; delete the file to clear it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "cache" / "llm_cache.sqlite"
VOLATILE_KEYS = frozenset({"rolling_summary"})
_Row = tuple[str, dict, str, str]  # prompt_id, data, model_id, provider


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def cache_key(prompt_id: str, prompt_hash: str, schema_name: str, grounded: bool, variables: dict, *, model: str, provider: str) -> str:
    """Key for one primary-model configuration. `model` is the configured model string (the whole
    LLM_MODEL_FAST list for Groq), not whichever backup happened to answer this call."""
    material = json.dumps(
        {"p": prompt_id, "h": prompt_hash, "s": schema_name, "g": grounded, "m": model, "r": provider, "v": _stable(variables)},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


class AnalysisCache:
    def __init__(self, path: Path = CACHE_PATH) -> None:
        self.path = path
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()  # memory only; never held across sqlite IO
        self._mem: dict[str, _Row] = {}
        self._broken = False
        self.hits = 0
        self.misses = 0
        self._pending: queue.Queue[tuple | None] = queue.Queue()
        self._writer: threading.Thread | None = None
        self._load()
        if not self._broken:
            self._writer = threading.Thread(target=self._persist, name="llm-cache", daemon=True)
            self._writer.start()

    def _load(self) -> None:
        """Read the on-disk rows into memory once, at construction (process start or a test), not
        on a lane call."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, prompt_id TEXT NOT NULL, "
                "data TEXT NOT NULL, model_id TEXT NOT NULL, provider TEXT NOT NULL, created REAL NOT NULL)"
            )
            rows = db.execute("SELECT key, prompt_id, data, model_id, provider FROM answers").fetchall()
        except (OSError, sqlite3.Error):
            self._broken = True
            log.warning("analysis cache not usable at %s; running without it", self.path)
            return
        mem: dict[str, _Row] = {}
        for key, prompt_id, raw, model_id, provider in rows:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                mem[key] = (prompt_id, data, model_id, provider)
        self._db = db
        self._mem = mem

    def _persist(self) -> None:
        """Background thread: the call path only enqueues. A stalled disk stays off the event loop."""
        while True:
            item = self._pending.get()
            if item is None:
                return
            key, prompt_id, data, model_id, provider = item
            db = self._db
            if db is None:
                continue
            try:
                db.execute(
                    "INSERT OR IGNORE INTO answers VALUES (?, ?, ?, ?, ?, ?)",
                    (key, prompt_id, json.dumps(data, ensure_ascii=False), model_id, provider, time.time()),
                )
            except sqlite3.Error:
                log.warning("analysis cache write failed; answer not stored")

    def get(self, key: str) -> tuple[dict, str, str] | None:
        """(data, model_id, provider) or None. Memory only."""
        with self._lock:
            row = self._mem.get(key)
            if row is None:
                self.misses += 1
                return None
            self.hits += 1
            _prompt_id, data, model_id, provider = row
        return data, model_id, provider

    def put(self, key: str, prompt_id: str, data: dict, model_id: str, provider: str) -> None:
        """Remember an answer if the key is not there yet (the first answer stays). Memory only;
        the sqlite row is written on the background thread. A cache failure never raises."""
        try:
            snapshot = json.loads(json.dumps(data, ensure_ascii=False))
        except (TypeError, ValueError):
            log.warning("analysis cache write failed; answer not stored")
            return
        with self._lock:
            if key in self._mem:
                return
            self._mem[key] = (prompt_id, snapshot, model_id, provider)
        if self._broken:
            return
        self._pending.put((key, prompt_id, snapshot, model_id, provider))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            stored: dict[str, int] = {}
            for prompt_id, *_rest in self._mem.values():
                stored[prompt_id] = stored.get(prompt_id, 0) + 1
            return {"hits": self.hits, "misses": self.misses, "stored": stored}

    def close(self) -> None:
        writer = self._writer
        if writer is not None and writer.is_alive():
            self._pending.put(None)
            writer.join(timeout=5)
            self._writer = None
        db = self._db
        self._db = None
        if db is not None:
            db.close()


CACHE = AnalysisCache()
