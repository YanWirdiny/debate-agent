"""Async plane (CONTEXT.md 5.3 step 4, ARCHITECTURE.md section 8). Gemini only, never on the hot path.
P1's workers call these with a 20 s timeout.

- grounded_verdict: Gemini + Google Search on a U claim. The quote must appear on the fetched source
  page, or the update is operator-only (candidate, capped confidence). Same annotation_id; the
  aggregator makes it a new version. None = no change (stays U).
- prefetch: on a moderator question, background passages for the topic. Only passages whose text is
  found on the fetched page are kept (stance later quotes from them).
- summarize: rolling summary for the lanes' context.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from typing import Literal

import httpx
from pydantic import Field

from app.intel import web
from app.interfaces import KnowledgeBase
from app.lanes.claims import VERDICT_CATEGORY
from app.llm.client import gemini_json
from app.models import Annotation, ContractModel, Evidence, Passage, PrefetchJob, Segment, SlowJob, VerdictJob
from app.verify import stt_safe

log = logging.getLogger(__name__)

GEMINI_TIMEOUT_S = 15.0  # leaves room for the page fetch inside P1's 20 s job budget
UNVERIFIED_MAX_CONF = 0.6
MAX_PREFETCH = 10
SUMMARY_MAX_CHARS = 1200

Reliability = Literal["primary", "factchecker", "media"]


class GroundedVerdict(ContractModel):
    verdict: Literal["supported", "contradicted", "misleading", "data_misuse", "insufficient"]
    passage_id: str | None = None
    quote: str
    explanation: str
    confidence: float = Field(ge=0, le=1)
    source_url: str | None
    source_title: str | None
    publisher: str | None
    published: str | None
    reliability: Reliability | None


class FoundPassage(ContractModel):
    text: str
    url: str
    title: str
    publisher: str
    published: str | None
    reliability: Reliability


class Prefetched(ContractModel):
    topic: str
    passages: list[FoundPassage] = Field(max_length=MAX_PREFETCH)


class Summary(ContractModel):
    summary: str


def _today() -> str:
    return dt.date.today().isoformat()


def _source_id(url: str, text: str) -> str:
    return "web:" + hashlib.sha256(f"{url}\n{text}".encode()).hexdigest()[:16]


async def grounded_verdict(job: SlowJob, kb: KnowledgeBase) -> Annotation | None:
    if not isinstance(job, VerdictJob) or job.segment.role == "moderator":
        return None
    r = await gemini_json("grounded_verdict", {
        "claim": job.claim.decontextualized,
        "speaker_digest": job.speaker_digest.model_dump(mode="json") if job.speaker_digest else None,
        "debate_date": job.debate_date,
    }, GroundedVerdict, grounded=True, timeout=GEMINI_TIMEOUT_S)
    g = GroundedVerdict.model_validate(r.data)
    if g.verdict == "insufficient" or not g.quote.strip() or not g.source_url:
        return None
    fetched = await web.fetch_text(g.source_url)
    on_page = fetched is not None and web.quote_on_page(g.quote, fetched[1])
    url = fetched[0] if fetched else g.source_url
    evidence = Evidence(
        source_id=_source_id(url, g.quote), title=g.source_title or url, url=url,
        publisher=g.publisher or "", reliability=g.reliability or "media", quote=g.quote.strip(),
        published=g.published, retrieved_at=_today())
    verified = on_page and stt_safe(job.segment, job.annotation.span_text)
    return job.annotation.model_copy(update={
        "category": VERDICT_CATEGORY[g.verdict], "explanation": g.explanation.strip(), "evidence": [evidence],
        # A quote we could not find on the page never reaches the audience.
        "confidence": g.confidence if on_page else min(g.confidence, UNVERIFIED_MAX_CONF),
        "status": "verified" if verified else "candidate", "path": "web",
        "model_id": r.model_id, "prompt_hash": r.prompt_hash,
    })


async def prefetch(job: SlowJob) -> tuple[str, list[Passage]]:
    if not isinstance(job, PrefetchJob):
        return ("general", [])
    r = await gemini_json("prefetch", {
        "question": job.question.text,
        "topic_hint": job.topic,
        "participants": [d.model_dump(mode="json") for d in job.participants],
    }, Prefetched, grounded=True, timeout=GEMINI_TIMEOUT_S)
    found = Prefetched.model_validate(r.data)
    topic = job.topic or found.topic or "general"
    async with httpx.AsyncClient(timeout=web.FETCH_TIMEOUT_S, follow_redirects=True, headers=web._HEADERS) as client:
        pages = await asyncio.gather(*(web.fetch_text(p.url, client) for p in found.passages))
    out = []
    for p, page in zip(found.passages, pages):
        if page is None or not web.quote_on_page(p.text, page[1]):
            continue  # stance quotes from passages; an unverified passage would launder a made-up quote
        url = page[0]
        out.append(Passage(
            passage_id=_source_id(url, p.text), text=p.text.strip(), topic=topic,
            source=Evidence(source_id=_source_id(url, p.text), title=p.title, url=url, publisher=p.publisher,
                            reliability=p.reliability, quote=p.text.strip(), published=p.published,
                            retrieved_at=_today())))
    log.info("prefetch kept %d of %d passages (quote found on page)", len(out), len(found.passages))
    return topic, out


async def summarize(prev: str, turns: list[Segment]) -> str:
    turns = [t for t in turns if t.role == "speaker"]
    if not turns:
        return prev
    r = await gemini_json("summarize", {
        "previous_summary": prev,
        "turns": [{"speaker_id": t.speaker_id, "text": t.text} for t in turns],
    }, Summary, timeout=GEMINI_TIMEOUT_S)
    return Summary.model_validate(r.data).summary.strip()[:SUMMARY_MAX_CHARS] or prev
