"""Live metrics (P1): latency percentiles, claim-bank hit rate, cache tokens, event-loop lag.

The aggregator records every annotation version it applies; the runner records claim records
(one per extracted claim). A ticker task pushes a `MetricsSnapshot` to the operator every second.

- p50/p95: latency_ms of the first version of each hot-path annotation (path bank/retrieval/llm);
  async web verdicts are excluded (they are seconds by design). `p95_bank_ms` is the
  definition-of-done number (bank-hit flags <= 1.5 s).
- bank_hit_rate: claims answered from the pre-verified bank / claims extracted.
- loop_lag_ms: how late a 100 ms sleep wakes up (max over the last second). > 100 ms = overloaded.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Protocol

from pydantic import BaseModel

from app.models import Annotation, MetricsMessage, MetricsSnapshot

HOT_PATHS = {"bank", "retrieval", "llm"}


class _Broadcaster(Protocol):
    async def broadcast(self, message: BaseModel) -> None: ...


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile; 0 for no data."""
    if not values:
        return 0.0
    s = sorted(values)
    return float(s[max(0, math.ceil(p / 100 * len(s)) - 1)])


class Metrics:
    def __init__(self) -> None:
        self.latencies: list[int] = []
        self.bank_latencies: list[int] = []
        self.claims_extracted = 0
        self.bank_hits = 0
        self.cache_tokens = 0
        self.lane_timeouts = 0
        self.loop_lag_ms = 0.0
        self._seen: set[str] = set()

    def record_annotation(self, a: Annotation) -> None:
        if a.annotation_id in self._seen or a.path not in HOT_PATHS:
            return
        self._seen.add(a.annotation_id)
        self.latencies.append(a.latency_ms)
        if a.path == "bank":
            self.bank_latencies.append(a.latency_ms)
            if a.lane == "claims":
                self.bank_hits += 1

    def record_claim(self) -> None:
        self.claims_extracted += 1

    def add_cache_tokens(self, n: int) -> None:
        """P2 hook: cached-token count from LLMResult.usage, if the client reports it."""
        self.cache_tokens += n

    def snapshot(self) -> MetricsSnapshot:
        rate = min(1.0, self.bank_hits / self.claims_extracted) if self.claims_extracted else 0.0
        return MetricsSnapshot(
            p50_ms=percentile(self.latencies, 50),
            p95_ms=percentile(self.latencies, 95),
            bank_hit_rate=round(rate, 3),
            cache_tokens=self.cache_tokens,
            loop_lag_ms=round(self.loop_lag_ms, 1),
        )

    @property
    def p95_bank_ms(self) -> float:
        return percentile(self.bank_latencies, 95)

    async def ticker(self, operator: _Broadcaster, interval: float = 1.0, probe: float = 0.1) -> None:
        """Runs until cancelled. Measures loop lag with short sleeps, pushes a snapshot each interval."""
        last_push = time.monotonic()
        worst = 0.0
        while True:
            t = time.monotonic()
            await asyncio.sleep(probe)
            worst = max(worst, (time.monotonic() - t - probe) * 1000)
            if time.monotonic() - last_push >= interval:
                self.loop_lag_ms = worst
                worst = 0.0
                last_push = time.monotonic()
                await operator.broadcast(MetricsMessage(data=self.snapshot()))


def snapshot(metrics: Metrics) -> MetricsSnapshot:
    return metrics.snapshot()
