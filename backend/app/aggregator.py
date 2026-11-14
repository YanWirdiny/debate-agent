"""Aggregator (P1): sole owner of annotation state; gates, versions, routing. Never calls LLMs or DB."""

from __future__ import annotations

from functools import lru_cache

from app.config import Settings
from app.models import Annotation


@lru_cache
def _audience_min_conf() -> float:
    return Settings().audience_min_conf


class Aggregator:
    async def run(self) -> None:
        raise NotImplementedError

    def is_public(self, a: Annotation) -> bool:
        """Audience gate. CONTEXT.md section 2 rule 3, ARCHITECTURE.md section 6."""
        if a.status != "verified" or a.confidence < _audience_min_conf():
            return False
        if a.category == "U":
            return False
        if a.category == "L":
            return bool(a.span_text) and (a.subtype != "strawman" or bool(a.opponent_span))
        return len(a.evidence) > 0 and all(e.quote for e in a.evidence)

    def kill(self, annotation_id: str, by: str) -> None:
        raise NotImplementedError
