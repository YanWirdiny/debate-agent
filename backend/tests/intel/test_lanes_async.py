"""P2 Sync 2-3: retrieval + stance, U -> slow job, consistency, fallacy, async jobs, warm-up.
Every provider and page fetch is mocked; synthetic data only."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.intel import async_jobs, warmup, web
from app.kb.store import KnowledgeBase
from app.lanes import claims, consistency, fallacy
from app.llm import client
from app.models import Annotation, Claim, ClaimRecord, Evidence, LaneContext, Passage, PrefetchJob, Segment, Statement, VerdictJob
from tests.intel.test_sync1 import Encoder

SRC = Evidence(source_id="bls-cpi", title="CPI release", url="https://example.invalid/cpi", publisher="Synthetic BLS",
               reliability="primary", quote="", published="2024-09-11", retrieved_at="2026-11-14")
PASSAGE = Passage(passage_id="cpi", text="Synthetic data only. Consumer prices rose 2.5 percent over the year to August 2024.",
                  source=SRC, topic="economy_inflation")
SEG = Segment(segment_id="s:4", session_id="s", speaker_id="speaker_a", role="speaker",
              text="Prices rose 2.5 percent last year.", t_start=10, t_end=12, stt_confidence=.97)
CLAIM = dict(text="Prices rose 2.5 percent last year", decontextualized="Consumer prices rose 2.5 percent in the year to August 2024",
             entities=[], numbers=["2.5 percent"], topic="economy_inflation")


def result(data, model="m-fast", h="h1"):
    return SimpleNamespace(data=data, model_id=model, prompt_hash=h)


def kb(passages=(PASSAGE,), statements=()):
    bank = KnowledgeBase(encoder=Encoder(), passages=list(passages), statements=list(statements))
    bank._prepare()
    return bank


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def blocked(*a, **k):
        raise AssertionError("unexpected HTTP request")
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)


def route(monkeypatch, **by_prompt):
    """Mock groq_json in every lane module; answers per prompt id (dict, or Exception to raise)."""
    calls = []

    async def fake(prompt_id, variables, schema, timeout=1.2):
        calls.append(prompt_id)
        answer = by_prompt.get(prompt_id)
        if answer is None:
            raise client.LLMError("no mock")
        if isinstance(answer, Exception):
            raise answer
        return result(answer)
    for mod in (claims, consistency, fallacy, warmup):
        monkeypatch.setattr(mod, "groq_json", fake)
    return calls


EXTRACT = {"checkworthy": True, "claims": [CLAIM]}


async def run(seg=SEG, bank=None, view=LaneContext()):
    out, q = [], asyncio.Queue()
    await claims.run_claims(seg, view, bank or kb(), out.append, q)
    return out, q


# --- claims: retrieval + stance ----------------------------------------------------------------


async def test_stance_supported_with_verbatim_quote_is_verified(monkeypatch):
    route(monkeypatch, claim_extract=EXTRACT, stance={
        "verdict": "supported", "passage_id": "cpi", "quote": "Consumer prices rose 2.5  percent over the year to August 2024.",
        "explanation": "BLS data (2024) shows prices rose 2.5%.", "confidence": .9})
    out, q = await run()
    rec, ann = out
    assert isinstance(rec, ClaimRecord) and rec.claim.embedding
    assert (ann.category, ann.status, ann.path, ann.annotation_id) == ("V", "verified", "retrieval", "s:4:claims:0")
    assert ann.evidence[0].source_id == "bls-cpi" and ann.span_text == CLAIM["text"]
    assert q.empty()


@pytest.mark.parametrize("stance", [
    {"verdict": "contradicted", "passage_id": "cpi", "quote": "Consumer prices fell 9 percent.", "explanation": "x", "confidence": .9},
    {"verdict": "contradicted", "passage_id": "nope", "quote": "Consumer prices rose 2.5 percent", "explanation": "x", "confidence": .9},
    {"verdict": "insufficient", "passage_id": None, "quote": "", "explanation": "", "confidence": 0},
    client.LLMError("timeout"),
])
async def test_unverifiable_stance_degrades_to_u_and_slow_job(monkeypatch, stance):
    route(monkeypatch, claim_extract=EXTRACT, stance=stance)
    out, q = await run()
    u = out[1]
    assert (u.category, u.status, u.evidence) == ("U", "candidate", [])
    job = q.get_nowait()
    assert isinstance(job, VerdictJob) and job.annotation.annotation_id == u.annotation_id


async def test_low_confidence_number_caps_stance_at_candidate(monkeypatch):
    route(monkeypatch, claim_extract=EXTRACT, stance={
        "verdict": "contradicted", "passage_id": "cpi", "quote": "Consumer prices rose 2.5 percent over the year to August 2024.",
        "explanation": "x", "confidence": .95})
    out, _ = await run(SEG.model_copy(update={"low_conf_words": ["2.5"]}))
    assert out[1].category == "F" and out[1].status == "candidate"


# --- consistency -------------------------------------------------------------------------------

PRIOR = Statement(statement_id="st1", speaker_id="speaker_a", text="Prices fell last year.", date="2024-03-01",
                  source_url="https://example.invalid/speech")


async def test_consistency_differs_from_dated_statement(monkeypatch):
    calls = route(monkeypatch, claim_extract=EXTRACT, stance={"verdict": "insufficient", "passage_id": None, "quote": "",
                  "explanation": "", "confidence": 0},
                  nli={"relation": "contradicts", "statement_id": "st1", "explanation": "x", "confidence": .85})
    out, _ = await run(bank=kb(statements=[PRIOR]))
    c = next(a for a in out if isinstance(a, Annotation) and a.category == "C")
    assert c.lane == "consistency" and c.status == "verified" and c.evidence[0].quote == PRIOR.text
    assert c.explanation.startswith("Differs from prior statement (2024-03-01)")
    assert "contradict" not in c.explanation.lower() and "nli" in calls


async def test_consistency_session_ledger_is_operator_only_and_unrelated_is_silent(monkeypatch):
    earlier = ClaimRecord(claim=Claim(text="Prices fell", decontextualized="Prices fell", embedding=[1.0, 0.0]),
                          speaker_id="speaker_a", segment_id="s:1")
    route(monkeypatch, claim_extract=EXTRACT, stance={"verdict": "insufficient", "passage_id": None, "quote": "",
          "explanation": "", "confidence": 0},
          nli={"relation": "contradicts", "statement_id": "session:s:1", "explanation": "x", "confidence": .9})
    out, _ = await run(bank=kb(), view=LaneContext(claim_ledger=[earlier]))
    c = next(a for a in out if isinstance(a, Annotation) and a.category == "C")
    assert c.status == "candidate" and not c.evidence and "earlier in this debate" in c.explanation

    route(monkeypatch, claim_extract=EXTRACT, stance={"verdict": "insufficient", "passage_id": None, "quote": "",
          "explanation": "", "confidence": 0},
          nli={"relation": "unrelated", "statement_id": None, "explanation": "", "confidence": .2})
    out, _ = await run(bank=kb(statements=[PRIOR]))
    assert not any(isinstance(a, Annotation) and a.category == "C" for a in out)


# --- fallacy -----------------------------------------------------------------------------------

FSEG = Segment(segment_id="s:7", session_id="s", speaker_id="speaker_a", role="speaker",
               text="Either we drill everywhere or gas hits ten dollars.", t_start=1, t_end=3, stt_confidence=.95)
OPP = Segment(segment_id="s:6", session_id="s", speaker_id="speaker_b", role="speaker",
              text="We should tighten border processing.", t_start=0, t_end=1, stt_confidence=.95)


def fres(**kw):
    base = {"has_fallacy": True, "type": "false_dilemma", "span": "Either we drill everywhere or gas hits ten dollars",
            "opponent_span": None, "explanation": "Presents two options as the only ones.", "confidence": .9}
    return base | kw


async def test_fallacy_verified_candidate_and_guards(monkeypatch):
    out = []
    route(monkeypatch, fallacy=fres())
    await fallacy.run_fallacy(FSEG, LaneContext(), out.append)
    a = out[0]
    assert (a.category, a.subtype, a.status, a.lane) == ("L", "false_dilemma", "verified", "fallacy")
    assert FSEG.text[a.span_char_start:a.span_char_end] == a.span_text

    out.clear()
    route(monkeypatch, fallacy=fres(confidence=.6))
    await fallacy.run_fallacy(FSEG, LaneContext(), out.append)
    assert out[0].status == "candidate"

    out.clear()
    route(monkeypatch, fallacy=fres(type="strawman", opponent_span="open borders"))  # not the opponent's words
    await fallacy.run_fallacy(FSEG, LaneContext(opponent_turn=[OPP]), out.append)
    assert out == []
    route(monkeypatch, fallacy=fres(type="strawman", opponent_span="tighten border processing"))
    await fallacy.run_fallacy(FSEG, LaneContext(opponent_turn=[OPP]), out.append)
    assert out[0].opponent_span == "tighten border processing"

    route(monkeypatch, fallacy=fres(span="invented words"))
    with pytest.raises(ValueError, match="absent"):
        await fallacy.run_fallacy(FSEG, LaneContext(), out.append)

    out.clear()
    route(monkeypatch, fallacy={"has_fallacy": False, "type": None, "span": None, "opponent_span": None,
                                "explanation": "", "confidence": 0})
    await fallacy.run_fallacy(FSEG, LaneContext(), out.append)
    calls = route(monkeypatch, fallacy=fres())
    await fallacy.run_fallacy(FSEG.model_copy(update={"role": "moderator"}), LaneContext(), out.append)
    assert out == [] and calls == []


async def test_fallacy_timeout_is_shorter_than_the_lane_budget(monkeypatch):
    from app.session import LANE_TIMEOUT_S

    mock = AsyncMock(return_value=result({
        "has_fallacy": False, "type": None, "span": None, "opponent_span": None,
        "explanation": "", "confidence": 0,
    }))
    monkeypatch.setattr(fallacy, "groq_json", mock)
    await fallacy.run_fallacy(FSEG, LaneContext(), lambda _item: None)
    timeout = mock.await_args.kwargs["timeout"]
    assert timeout == fallacy.FALLACY_LLM_TIMEOUT_S == LANE_TIMEOUT_S - fallacy.FALLACY_LLM_RESERVE_S
    assert timeout < LANE_TIMEOUT_S
    # groq_json only starts Gemini when the timeout still covers the fallback reserve.
    assert timeout > client.MIN_FALLBACK_BUDGET_S + client.FALLBACK_SLACK_S


# --- async jobs --------------------------------------------------------------------------------

U = Annotation(annotation_id="s:4:claims:0", version=1, segment_id="s:4", speaker_id="speaker_a",
               span_text=CLAIM["text"], span_char_start=0, span_char_end=len(CLAIM["text"]), category="U", subtype=None,
               explanation="", evidence=[], opponent_span=None, confidence=.3, status="candidate", path="retrieval",
               lane="claims", latency_ms=500, model_id=None, prompt_hash=None)
JOB = VerdictJob(claim=Claim(**CLAIM), segment=SEG, annotation=U)
GV = {"verdict": "contradicted", "passage_id": None, "quote": "Consumer prices rose 2.9 percent over the year to August 2024.",
      "explanation": "BLS data (2024) shows 2.9%.", "confidence": .88, "source_url": "https://example.invalid/cpi",
      "source_title": "CPI", "publisher": "Synthetic BLS", "published": "2024-09-11", "reliability": "primary"}
PAGE = "<html><script>x()</script><p>Consumer prices rose 2.9 percent</p><p>over the year to August 2024.</p></html>"


def gemini(monkeypatch, data):
    m = AsyncMock(return_value=result(data, "m-gemini", "h2"))
    monkeypatch.setattr(async_jobs, "gemini_json", m)
    return m


def page(monkeypatch, text):
    async def fetch(url, client=None):
        return None if text is None else (url, web.page_text(text))
    monkeypatch.setattr(web, "fetch_text", fetch)


async def test_grounded_verdict_upgrades_only_with_quote_on_page(monkeypatch):
    m = gemini(monkeypatch, GV)
    page(monkeypatch, PAGE)
    a = await async_jobs.grounded_verdict(JOB, None)
    assert (a.annotation_id, a.category, a.status, a.path, a.confidence) == (U.annotation_id, "F", "verified", "web", .88)
    assert m.call_args.kwargs["grounded"] is True and a.model_id == "m-gemini"

    page(monkeypatch, "<p>Something else entirely, nothing about prices.</p>")
    a = await async_jobs.grounded_verdict(JOB, None)
    assert a.status == "candidate" and a.confidence <= async_jobs.UNVERIFIED_MAX_CONF

    gemini(monkeypatch, GV | {"verdict": "insufficient", "quote": ""})
    assert await async_jobs.grounded_verdict(JOB, None) is None


async def test_prefetch_keeps_only_passages_found_on_page(monkeypatch):
    found = {"topic": "economy_inflation", "passages": [
        {"text": "Consumer prices rose 2.9 percent over the year to August 2024.", "url": "https://example.invalid/a",
         "title": "CPI", "publisher": "BLS", "published": None, "reliability": "primary"},
        {"text": "A sentence the model invented about inflation.", "url": "https://example.invalid/b",
         "title": "X", "publisher": "Y", "published": None, "reliability": "media"}]}
    gemini(monkeypatch, found)
    page(monkeypatch, PAGE)
    q = Segment(segment_id="s:0", session_id="s", speaker_id="moderator", role="moderator", text="What about inflation?",
                t_start=0, t_end=1, stt_confidence=.99)
    topic, passages = await async_jobs.prefetch(PrefetchJob(question=q, topic=None))
    assert topic == "economy_inflation" and [p.source.url for p in passages] == ["https://example.invalid/a"]
    bank = kb(passages=[])
    bank.add_passages(topic, passages)
    assert bank.search_passages("inflation", topic, 1)[0].passage_id == passages[0].passage_id


async def test_summarize(monkeypatch):
    m = gemini(monkeypatch, {"summary": "  speaker_a said prices rose.  "})
    assert await async_jobs.summarize("", [SEG]) == "speaker_a said prices rose."
    assert await async_jobs.summarize("prev", []) == "prev" and m.await_count == 1


def test_quote_on_page_rules():
    text = web.page_text(PAGE)
    assert web.quote_on_page("Consumer prices rose 2.9 percent over the year to August 2024.", text)
    assert not web.quote_on_page("2.9 percent", text)  # too short to count
    assert not web.quote_on_page("Consumer prices rose 9.2 percent over the year to August 2024.", text)


# --- warm-up -----------------------------------------------------------------------------------

async def test_warm_up_calls_each_hot_prompt_and_fails_clearly(monkeypatch):
    calls = route(monkeypatch, claim_extract={"checkworthy": False, "claims": []}, stance={}, fallacy={}, nli={})
    await warmup.warm_up(kb(), ["speaker_a"])
    assert sorted(calls) == ["claim_extract", "fallacy", "nli", "stance"]
    route(monkeypatch)  # every call fails
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        await warmup.warm_up(kb(), ["speaker_a"])
    with pytest.raises(ValueError, match="knowledge base"):
        await warmup.warm_up(None, [])

    class NoModel:
        def search_passages(self, *a):
            raise OSError("no cached weights")
    with pytest.raises(ValueError, match="download-model"):
        await warmup.warm_up(NoModel(), [])


async def test_slow_extraction_still_ships_u_inside_the_lane_budget(monkeypatch):
    """Extraction uses most of the 1.2 s lane: stance/NLI are skipped, the claim ships as U
    (and goes to the async worker) before P1's lane timeout would cancel everything."""
    calls = []

    async def slow(prompt_id, variables, schema, timeout=1.2):
        calls.append(prompt_id)
        if prompt_id == "claim_extract":
            await asyncio.sleep(1.0)
            return result(EXTRACT)
        raise AssertionError(f"{prompt_id} should be skipped: no budget left")
    for mod in (claims, consistency):
        monkeypatch.setattr(mod, "groq_json", slow)
    out, q = [], asyncio.Queue()
    await asyncio.wait_for(claims.run_claims(SEG, LaneContext(), kb(statements=[PRIOR]), out.append, q), 1.2)
    assert calls == ["claim_extract"]
    assert out[1].category == "U" and q.get_nowait().kind == "verdict"
