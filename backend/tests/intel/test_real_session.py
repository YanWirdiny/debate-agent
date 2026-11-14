"""INTEL=real end to end on the economy_v2 transcript: real runner, aggregator, gate, log and report;
only the LLM providers and page fetches are mocked (per sentence), KB is synthetic and in memory."""

import asyncio
from types import SimpleNamespace

from app import report as reports
from app.eventlog import read_jsonl, verify_chain
from app.hubs import Hub, RecordingClient
from app.intel import async_jobs, warmup, web
from app.kb.store import KnowledgeBase
from app.lanes import claims, consistency, fallacy
from app.models import Evidence, Passage
from app.session import Intel, SessionRunner
from tests.intel.test_sync1 import Encoder
from tests.pipeline.test_phase23 import msgs

DEFICIT = "provide a tax cut for billionaires and big corporations, which will result in $5,000,000,000,000 to America's deficit"
TARIFF = "I took in billions and billions of dollars, as you know, from China"
SRC = Evidence(source_id="cbo", title="Synthetic CBO estimate", url="https://example.invalid/cbo", publisher="Synthetic CBO",
               reliability="primary", quote="", published="2024-06-01", retrieved_at="2026-11-14")
PASSAGE = Passage(passage_id="cbo1", topic="economy_deficit", source=SRC,
                  text="Synthetic data only. Extending the 2017 tax cuts would add about 4.6 trillion dollars to deficits over ten years.")


def answers(prompt_id, v):
    if prompt_id == "claim_extract":
        s = v["sentence"]
        if "$5,000,000,000,000" in s:
            return {"checkworthy": True, "claims": [{"text": DEFICIT, "decontextualized": "Trump's tax cut would add 5 trillion dollars to the deficit",
                                                     "entities": ["America"], "numbers": ["$5,000,000,000,000"], "topic": "economy_deficit"}]}
        if "from China" in s and "took in" in s:
            return {"checkworthy": True, "claims": [{"text": TARIFF, "decontextualized": "The US took in billions of dollars in tariffs from China",
                                                     "entities": ["China"], "numbers": ["billions"], "topic": "trade"}]}
        return {"checkworthy": False, "claims": []}
    if prompt_id == "stance":
        if any(p["passage_id"] == "cbo1" for p in v["passages"]) and "5 trillion" in v["claim"]:
            return {"verdict": "misleading", "passage_id": "cbo1", "explanation": "CBO (2024) estimates about $4.6 trillion.",
                    "quote": "Extending the 2017 tax cuts would add about 4.6 trillion dollars to deficits over ten years.", "confidence": .86}
        return {"verdict": "insufficient", "passage_id": None, "quote": "", "explanation": "", "confidence": 0}
    if prompt_id == "fallacy":
        if "pay us back for all that we've done" in v["sentence"]:
            return {"has_fallacy": True, "type": "hasty_generalization", "span": "pay us back for all that we've done for the world",
                    "opponent_span": None, "explanation": "Sweeping conclusion without support.", "confidence": .6}
        return {"has_fallacy": False, "type": None, "span": None, "opponent_span": None, "explanation": "", "confidence": 0}
    if prompt_id == "nli":
        return {"relation": "unrelated", "statement_id": None, "explanation": "", "confidence": 0}
    raise AssertionError(prompt_id)


async def test_real_intel_session_on_economy_clip(tmp_path, monkeypatch):
    async def groq(prompt_id, variables, schema, timeout=1.2):
        await asyncio.sleep(0.01)
        return SimpleNamespace(data=answers(prompt_id, variables), model_id="groq-test", prompt_hash="h-" + prompt_id)
    for mod in (claims, consistency, fallacy, warmup):
        monkeypatch.setattr(mod, "groq_json", groq)

    async def gemini(prompt_id, variables, schema, grounded=False, cache_name=None, timeout=20.0):
        if prompt_id == "grounded_verdict":
            return SimpleNamespace(model_id="gemini-test", prompt_hash="h-gv", data={
                "verdict": "misleading", "passage_id": None, "quote": "Importers paid nearly all of the cost of the tariffs on Chinese goods.",
                "explanation": "Synthetic study (2024): US importers bore the tariff cost.", "confidence": .84,
                "source_url": "https://example.invalid/study", "source_title": "Study", "publisher": "Synthetic Fed",
                "published": "2024-01-01", "reliability": "primary"})
        if prompt_id == "prefetch":
            return SimpleNamespace(model_id="gemini-test", prompt_hash="h-pf", data={"topic": "economy_deficit", "passages": []})
        return SimpleNamespace(model_id="gemini-test", prompt_hash="h-s", data={"summary": "s"})
    monkeypatch.setattr(async_jobs, "gemini_json", gemini)

    async def fetch(url, client=None):
        return url, "Synthetic page. Importers paid nearly all of the cost of the tariffs on Chinese goods. End."
    monkeypatch.setattr(web, "fetch_text", fetch)

    kb = KnowledgeBase(encoder=Encoder(), passages=[PASSAGE])
    kb._prepare()
    op, au = Hub("operator"), Hub("audience")
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc), au.add(auc)
    (tmp_path / "real").mkdir()
    r = SessionRunner("real", "economy_v2", operator=op, audience=au, intel=Intel.real(), kb=kb, speed=40,
                      archive_path=tmp_path / "real" / "log.jsonl")
    await r.warm_up()
    await r.start(lead_s=0.05)
    await asyncio.wait_for(r.wait(), 30)

    final = {a.annotation_id: a for a in r.aggregator.state.values()}
    by_cat = {a.category: a for a in final.values()}
    deficit = next(a for a in final.values() if a.span_text == DEFICIT)
    assert (deficit.category, deficit.path, deficit.status, deficit.speaker_id) == ("M", "retrieval", "verified", "harris")
    tariff = next(a for a in final.values() if a.span_text == TARIFF)
    assert (tariff.category, tariff.path, tariff.status) == ("M", "web", "verified")  # U upgraded by the async worker
    assert by_cat["L"].status == "candidate"  # .6 confidence: operator only
    assert not any(a.speaker_id == "moderator" for a in final.values())

    public = {m["data"]["annotation_id"] for m in msgs(auc) if m["type"].startswith("annotation")}
    assert deficit.annotation_id in public and tariff.annotation_id in public
    assert by_cat["L"].annotation_id not in public
    assert not msgs(opc, "pending") and not msgs(auc, "pending")

    events = read_jsonl(tmp_path / "real" / "log.jsonl")
    assert verify_chain(events)
    rep = reports.build_report(events)
    assert rep["header"]["participants"] == ["harris", "trump"] and "groq-test" in rep["header"]["model_ids"]
    assert rep["unchecked"]["by_lane"] == {}
