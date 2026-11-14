"""Aggregator (P1): sole owner of annotation state; gates, versions, routing. Never calls LLMs or DB."""

from __future__ import annotations

from app.config import Settings
from app.models import Annotation

# Default only; SessionRunner passes settings.audience_min_conf. Read from the field so we never
# build Settings() here (it raises when API keys are missing, e.g. in tests or replay).
DEFAULT_AUDIENCE_MIN_CONF: float = Settings.model_fields["audience_min_conf"].default


class Aggregator:
    def __init__(self, audience_min_conf: float = DEFAULT_AUDIENCE_MIN_CONF) -> None:
        self.audience_min_conf = audience_min_conf

    async def run(self) -> None:
        raise NotImplementedError

    def is_public(self, a: Annotation) -> bool:
        """Audience gate. CONTEXT.md section 2 rule 3, ARCHITECTURE.md section 6."""
        if a.status != "verified" or a.confidence < self.audience_min_conf:
            return False
        if a.category == "U":
            return False
        if a.category == "L":
            return bool(a.span_text) and (a.subtype != "strawman" or bool(a.opponent_span))
        return len(a.evidence) > 0 and all(e.quote for e in a.evidence)

    def kill(self, annotation_id: str, by: str) -> None:
        raise NotImplementedError
