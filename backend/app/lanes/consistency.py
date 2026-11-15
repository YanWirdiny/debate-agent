"""Consistency lane (CONTEXT.md 5.5). Runs inside the claims lane, per extracted claim.

Candidates: the speaker's dated past statements (KB) plus earlier claims this session (ledger).
Only candidates with cosine >= 0.75 reach one `nli` call. `contradicts` -> category C,
"Differs from prior statement (YYYY-MM-DD)", with the prior statement as quoted evidence.
Never "contradiction" or "flip-flop" (CONTEXT.md rule 4).
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import math
from typing import Literal

from pydantic import Field

from app.interfaces import Emit, KnowledgeBase
from app.llm.client import groq_json
from app.models import Annotation, Claim, ClaimRecord, ContractModel, Evidence, Segment, Statement, annotation_id
from app.verify import stt_safe

COSINE_MIN = 0.75
MAX_CANDIDATES = 3
NLI_TIMEOUT_S = 5.0
SESSION_SOURCE = "this debate"
# The claims lane sets this to what is left of the lane, so the NLI call (and its Groq/Gemini
# fallback window) ends inside the lane instead of being cancelled with it. The signature of
# run_consistency is a frozen contract, hence a context variable rather than a parameter.
NLI_BUDGET_S: ContextVar[float] = ContextVar("NLI_BUDGET_S", default=NLI_TIMEOUT_S)


class NLI(ContractModel):
    relation: Literal["contradicts", "consistent", "unrelated"]
    statement_id: str | None
    explanation: str
    confidence: float = Field(ge=0, le=1)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def candidates(claim: Claim, seg: Segment, kb: KnowledgeBase, ledger: list[ClaimRecord]) -> list[Statement]:
    """Top prior statements by cosine, KB first then this session's earlier claims (other sentences)."""
    pool: list[tuple[float, Statement]] = []
    for s in kb.search_statements(seg.speaker_id, claim.decontextualized, MAX_CANDIDATES):
        pool.append((_cosine(claim.embedding, s.embedding), s))
    for r in ledger:
        if r.speaker_id != seg.speaker_id or r.segment_id == seg.segment_id:
            continue
        pool.append((_cosine(claim.embedding, r.claim.embedding), Statement(
            statement_id=f"session:{r.segment_id}", speaker_id=r.speaker_id, text=r.claim.text,
            date=SESSION_SOURCE, source_url="", topic=r.claim.topic)))
    pool = [(score, s) for score, s in pool if score >= COSINE_MIN]
    pool.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in pool[:MAX_CANDIDATES]]


async def run_consistency(claim: Claim, seg: Segment, kb: KnowledgeBase, ledger: list[ClaimRecord], emit: Emit) -> None:
    if seg.role == "moderator" or kb is None or not claim.embedding:
        return
    prior = await asyncio.to_thread(candidates, claim, seg, kb, ledger)
    if not prior:
        return
    r = await groq_json("nli", {
        "claim": claim.decontextualized,
        "statements": [{"statement_id": s.statement_id, "date": s.date, "text": s.text} for s in prior],
    }, NLI, timeout=min(NLI_TIMEOUT_S, NLI_BUDGET_S.get()))
    nli = NLI.model_validate(r.data)
    if nli.relation != "contradicts":
        return
    stmt = next((s for s in prior if s.statement_id == nli.statement_id), None)
    if stmt is None:
        return  # the model cited a statement it was not given
    start = seg.text.index(claim.text)
    in_session = stmt.statement_id.startswith("session:")
    when = "earlier in this debate" if in_session else stmt.date
    evidence = [] if in_session else [Evidence(
        source_id=stmt.statement_id, title=f"Statement by {seg.speaker_id} ({stmt.date})", url=stmt.source_url,
        publisher=seg.speaker_id, reliability="primary", quote=stmt.text, published=stmt.date,
        retrieved_at=stmt.date)]
    emit(Annotation(
        annotation_id=annotation_id(seg.segment_id, "consistency", start), version=1,
        segment_id=seg.segment_id, speaker_id=seg.speaker_id, span_text=claim.text,
        span_char_start=start, span_char_end=start + len(claim.text), category="C", subtype=None,
        explanation=f"Differs from prior statement ({when}): \"{stmt.text}\"",
        evidence=evidence, opponent_span=None, confidence=nli.confidence,
        # Same-session differences have no external source: operator only (candidate).
        status="verified" if evidence and stt_safe(seg, claim.text) else "candidate",
        path="llm", lane="consistency", latency_ms=0, model_id=r.model_id, prompt_hash=r.prompt_hash,
    ))
