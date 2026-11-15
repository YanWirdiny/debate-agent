"""Gemini spend guard.

Every Gemini call on the paid key (GEMINI_API_KEY) is priced from its token usage and added to a
small ledger on disk. Once the ledger reaches GEMINI_BUDGET_USD, or Google answers 429 on the paid
key, calls go to the free-tier key (GEMINI_API_KEY_2) instead. With no second key, Gemini calls
fail like any provider failure (U stays U, prefetch is skipped, the summary is kept).

This is an estimate from published prices, not Google's bill. Set a hard spend cap in Google's
billing console as well. The ledger stores a key fingerprint, never the key.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LEDGER = Path(__file__).resolve().parents[3] / "data" / "runs" / "gemini_spend.json"
# After a 429 on the paid key, send calls to the free key for this long, then try paid again.
RATE_LIMIT_COOLDOWN_S = 60.0

# Defaults (paid tier, USD), overridable in .env. Thinking tokens are billed as output.
# Grounding is charged on every grounded call (free monthly allowance ignored: conservative).
DEFAULT_BUDGET_USD = 7.0
DEFAULT_PRICE_IN_PER_M = 0.75
DEFAULT_PRICE_OUT_PER_M = 3.75
DEFAULT_PRICE_GROUNDED_CALL = 0.014


@dataclass(frozen=True)
class GeminiKey:
    tier: str  # "paid" | "free"
    key: str


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _secret(settings: Any, name: str) -> str:
    value = getattr(settings, name, None)
    return value.get_secret_value().strip() if value is not None else ""


def _num(settings: Any, name: str, default: float) -> float:
    value = getattr(settings, name, None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


class Budget:
    def __init__(self) -> None:
        self._spent: dict[str, float] | None = None
        self._cooldown_until: dict[str, float] = {}
        self._warned: set[str] = set()

    # --- ledger ---
    def _load(self) -> dict[str, float]:
        if self._spent is None:
            try:
                raw = json.loads(LEDGER.read_text())
                self._spent = {k: float(v) for k, v in raw.get("spent_usd", {}).items()}
            except (OSError, ValueError, AttributeError):
                self._spent = {}
        return self._spent

    def spent(self, key: str) -> float:
        return self._load().get(fingerprint(key), 0.0)

    def add(self, key: str, usd: float) -> None:
        if usd <= 0:
            return
        spent = self._load()
        fp = fingerprint(key)
        spent[fp] = spent.get(fp, 0.0) + usd
        try:
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            tmp = LEDGER.with_suffix(".tmp")
            tmp.write_text(json.dumps({"spent_usd": spent}, indent=1))
            tmp.replace(LEDGER)
        except OSError:
            log.warning("gemini spend ledger not writable; the budget only holds for this process")

    def reset_cache(self) -> None:
        """Forget the in-memory copy (tests, or after editing the ledger by hand)."""
        self._spent = None
        self._cooldown_until.clear()
        self._warned.clear()

    # --- key choice ---
    def keys(self, settings: Any) -> list[GeminiKey]:
        """Keys to try, in order: the paid key while under budget and not cooling down, then the free key."""
        paid, free = _secret(settings, "gemini_api_key"), _secret(settings, "gemini_api_key_2")
        out: list[GeminiKey] = []
        if paid:
            budget = _num(settings, "gemini_budget_usd", DEFAULT_BUDGET_USD)
            if self.spent(paid) >= budget:
                self._warn_once("budget", "gemini paid key reached its %.2f USD budget; using GEMINI_API_KEY_2" if free
                                else "gemini paid key reached its %.2f USD budget and GEMINI_API_KEY_2 is empty", budget)
            elif self._cooldown_until.get(fingerprint(paid), 0.0) > time.monotonic():
                pass
            else:
                out.append(GeminiKey("paid", paid))
        if free and free != paid:
            out.append(GeminiKey("free", free))
        return out

    def rate_limited(self, key: str) -> None:
        self._cooldown_until[fingerprint(key)] = time.monotonic() + RATE_LIMIT_COOLDOWN_S
        log.warning("gemini paid key answered 429; using the free key for %.0fs", RATE_LIMIT_COOLDOWN_S)

    def _warn_once(self, tag: str, msg: str, *args: object) -> None:
        if tag not in self._warned:
            self._warned.add(tag)
            log.warning(msg, *args)


def cost_usd(settings: Any, usage: dict[str, int], grounded: bool) -> float:
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0) + usage.get("thinking_tokens", 0)
    usd = tokens_in * _num(settings, "gemini_price_in_per_m", DEFAULT_PRICE_IN_PER_M) / 1e6
    usd += tokens_out * _num(settings, "gemini_price_out_per_m", DEFAULT_PRICE_OUT_PER_M) / 1e6
    if grounded:
        usd += _num(settings, "gemini_price_grounded_call", DEFAULT_PRICE_GROUNDED_CALL)
    return usd


def is_rate_limited(exc: BaseException) -> bool:
    """429 / RESOURCE_EXHAUSTED from the google-genai SDK. Reads attributes only, never the message."""
    return getattr(exc, "code", None) == 429 or getattr(exc, "status", None) == "RESOURCE_EXHAUSTED"


BUDGET = Budget()
