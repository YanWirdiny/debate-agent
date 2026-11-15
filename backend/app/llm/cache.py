"""Analysis cache: reuse model answers across runs of the same video instead of paying for them again.

Every successful structured-output call (claim extraction, stance, NLI, fallacy, grounded web
verdict, prefetch, summary) is stored under a key made from what decides its answer: the prompt
text (via its hash, so editing a prompt invalidates its entries), the output schema, grounding, and
the input variables. A later call with the same key returns the stored answer at once: no tokens,
no rate limit, no latency. A miss calls the models as usual and adds the answer.

Inputs that change from run to run without changing what is being judged are left out of the key:
`rolling_summary` (written by an async Gemini job; whether it has landed before a given sentence
depends on timing). Everything else must match exactly, so a different sentence, context or
opponent turn is a miss, never a wrong reuse.

Hits are exact only when the transcript is the same: run the clip with REPLAY_TRANSCRIPT=1 (the
saved transcript) rather than live Deepgram, whose text can differ slightly between runs.

Stored locally in SQLite (data/cache/llm_cache.sqlite, git-ignored): works offline on demo day and
adds well under a millisecond to the hot path. LLM_CACHE=0 turns it off; delete the file to clear it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "cache" / "llm_cache.sqlite"
VOLATILE_KEYS = frozenset({"rolling_summary"})


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def cache_key(prompt_id: str, prompt_hash: str, schema_name: str, grounded: bool, variables: dict) -> str:
    material = json.dumps({"p": prompt_id, "h": prompt_hash, "s": schema_name, "g": grounded, "v": _stable(variables)},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


class AnalysisCache:
    def __init__(self, path: Path = CACHE_PATH) -> None:
        self.path = path
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._broken = False
        self.hits = 0
        self.misses = 0

    def _conn(self) -> sqlite3.Connection | None:
        if self._broken:
            return None
        if self._db is None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, prompt_id TEXT NOT NULL, "
                           "data TEXT NOT NULL, model_id TEXT NOT NULL, provider TEXT NOT NULL, created REAL NOT NULL)")
                self._db = db
            except (OSError, sqlite3.Error):
                self._broken = True
                log.warning("analysis cache not usable at %s; running without it", self.path)
                return None
        return self._db

    def get(self, key: str) -> tuple[dict, str, str] | None:
        """(data, model_id, provider) or None."""
        with self._lock:
            db = self._conn()
            if db is None:
                return None
            try:
                row = db.execute("SELECT data, model_id, provider FROM answers WHERE key = ?", (key,)).fetchone()
            except sqlite3.Error:
                return None
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(row[0]), row[1], row[2]

    def put(self, key: str, prompt_id: str, data: dict, model_id: str, provider: str) -> None:
        """Add an answer if the key is not there yet (the first answer stays)."""
        with self._lock:
            db = self._conn()
            if db is None:
                return
            try:
                db.execute("INSERT OR IGNORE INTO answers VALUES (?, ?, ?, ?, ?, ?)",
                           (key, prompt_id, json.dumps(data, ensure_ascii=False), model_id, provider, time.time()))
            except sqlite3.Error:
                log.warning("analysis cache write failed; answer not stored")

    def stats(self) -> dict[str, Any]:
        with self._lock:
            db = self._conn()
            rows = db.execute("SELECT prompt_id, COUNT(*) FROM answers GROUP BY prompt_id").fetchall() if db else []
        return {"hits": self.hits, "misses": self.misses, "stored": dict(rows)}

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


CACHE = AnalysisCache()
