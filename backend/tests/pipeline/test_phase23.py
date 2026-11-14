"""P1 Phases 2-3: metrics, dedupe, async plane (verdict workers, prefetch, summarizer),
REPLAY_EVENTS, report, HTTP routes. Fake intel, no network."""

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import report as reports
from app.aggregator import Aggregator
from app.eventlog import EventWriter, load_session_events, read_jsonl, verify_chain
from app.hubs import Hub, RecordingClient
from app.lanes import fake
from app.main import SessionManager, create_app
from app.metrics import Metrics, percentile
from app.models import Annotation, Claim, ClaimRecord, Evidence, Segment
from app.session import Intel, SessionRunner

FIXTURES = Path(__file__).parents[1] / "fixtures" / "segments.jsonl"
SEGMENTS = [Segment.model_validate_json(l) for l in FIXTURES.read_text().splitlines() if l.strip()]
EV = Evidence(source_id="s", title="t", url="https://example.org", publisher="p", reliability="primary",
              quote="q", published=None, retrieved_at="2026-11-14")


def ann(aid, seg="s:1", spk="a", **kw):
    base = dict(annotation_id=aid, version=1, segment_id=seg, speaker_id=spk, span_text="x", span_char_start=0,
                span_char_end=1, category="F", subtype=None, explanation="e", evidence=[EV], opponent_span=None,
                confidence=0.9, status="verified", path="bank", lane="claims", latency_ms=0, model_id="m1",
                prompt_hash="h1")
    base.update(kw)
    return Annotation(**base)


def rec(aid, text, spk="a", emb=None):
    return ClaimRecord(claim=Claim(text=text, decontextualized=text, embedding=emb or []), speaker_id=spk,
                       segment_id="s:1", annotation_id=aid)


def msgs(client, type_=None):
    out = [json.loads(m) for m in client.messages]
    return [m for m in out if type_ is None or m["type"] == type_]


# --- metrics ---------------------------------------------------------------------------


async def test_metrics_percentiles_bank_rate_and_ticker():
    assert percentile([], 95) == 0 and percentile([100, 200, 300, 400], 50) == 200
    assert percentile(list(range(1, 101)), 95) == 95
    m = Metrics()
    for i, (path, lat) in enumerate([("bank", 400), ("bank", 600), ("retrieval", 1200), ("web", 9000)]):
        m.record_annotation(ann(f"a{i}", path=path, latency_ms=lat))
    m.record_annotation(ann("a0", latency_ms=99999))  # later versions do not count
    for _ in range(4):
        m.record_claim()
    snap = m.snapshot()
    assert (snap.p50_ms, snap.p95_ms, snap.bank_hit_rate) == (600, 1200, 0.5)  # web excluded from latency
    assert m.p95_bank_ms == 600

    op = Hub("operator")
    c = RecordingClient()
    op.add(c)
    task = asyncio.create_task(m.ticker(op, interval=0.05, probe=0.01))
    await asyncio.sleep(0.18)
    task.cancel()
    ticks = msgs(c, "metrics")
    assert len(ticks) >= 2 and ticks[-1]["data"]["bank_hit_rate"] == 0.5 and ticks[-1]["data"]["loop_lag_ms"] >= 0


# --- dedupe ----------------------------------------------------------------------------


async def test_aggregator_dedupes_repeated_claims():
    op, au = Hub("operator"), Hub("audience")
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc), au.add(auc)
    agg = Aggregator(operator=op, audience=au, writer=EventWriter("s"))
    task = asyncio.create_task(agg.run())
    agg.register_claim(rec("c1", "Unemployment is 0%."))
    agg.submit(ann("c1"))
    agg.register_claim(rec("c2", "unemployment is 0 %"))  # same claim, normalized text
    agg.submit(ann("c2"))
    agg.register_claim(rec("c3", "Unemployment is 0%.", spk="b"))  # other speaker: not a repeat
    agg.submit(ann("c3", spk="b"))
    agg.register_claim(rec("e1", "x", emb=[1.0, 0.0]))
    agg.submit(ann("e1"))
    agg.register_claim(rec("e2", "different words", emb=[0.95, 0.1]))  # cosine 0.99
    agg.submit(ann("e2"))
    agg.submit(ann("c2", status="withdrawn"))  # later versions of a duplicate are dropped
    await agg.stop()
    await task

    assert set(agg.state) == {"c1", "c3", "e1"} and agg.duplicates == {"c2": "c1", "e2": "e1"}
    assert agg.state["c1"].repeat_count == 2 and agg.state["c1"].version == 2
    au_types = [(m["type"], m.get("data", {}).get("annotation_id")) for m in msgs(auc)]
    assert au_types == [("annotation", "c1"), ("annotation_update", "c1"), ("annotation", "c3"),
                        ("annotation", "e1"), ("annotation_update", "e1")]
    assert agg.metrics.claims_extracted == 5


# --- async plane end to end --------------------------------------------------------------


def counting_intel():
    calls = {"verdict": 0, "prefetch": [], "summarize": 0, "warm_up": 0}
    base = Intel.fake()

    async def gv(job, kb):
        calls["verdict"] += 1
        return await fake.grounded_verdict(job, kb)

    async def pf(job):
        calls["prefetch"].append(job.question.text)
        return await fake.prefetch(job)

    async def sm(prev, turns):
        calls["summarize"] += 1
        assert all(t.role == "speaker" for t in turns)
        return await fake.summarize(prev, turns)

    async def wu(kb, speakers):
        calls["warm_up"] += 1
        assert speakers == ["speaker_a", "speaker_b"]

    return dataclasses.replace(base, grounded_verdict=gv, prefetch=pf, summarize=sm, warm_up=wu), calls


async def record_run(tmp_path, session_id="rec", kill_first_v=True):
    op, au = Hub("operator"), Hub("audience")
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc), au.add(auc)
    run_dir = tmp_path / session_id
    run_dir.mkdir(parents=True)
    intel, calls = counting_intel()
    r = SessionRunner(session_id, "fixture", operator=op, audience=au, intel=intel, speed=40,
                      archive_path=run_dir / "log.jsonl", metrics_interval=0.05)
    await r.warm_up()
    await r.start(lead_s=0.05)
    if kill_first_v:
        async def killer():
            while not any(a.category == "V" for a in r.aggregator.state.values()):
                await asyncio.sleep(0.01)
            r.aggregator.kill(next(a.annotation_id for a in r.aggregator.state.values() if a.category == "V"), by="operator")
        asyncio.create_task(killer())
    await asyncio.wait_for(r.wait(), 20)
    return r, opc, auc, calls


async def test_async_plane_end_to_end(tmp_path):
    r, opc, auc, calls = await record_run(tmp_path, kill_first_v=False)

    assert calls["warm_up"] == 1
    questions = [s.text for s in SEGMENTS if s.role == "moderator" and s.text.endswith("?")]
    assert calls["prefetch"] == questions  # one prefetch per moderator question
    assert calls["summarize"] >= 1 and r.ctx.rolling_summary
    caused = next(s for s in SEGMENTS if "caused" in s.text)
    aid = f"{caused.segment_id.replace('fixture', 'rec')}:claims:1"
    op_versions = [m["data"] for m in msgs(opc) if m["type"].startswith("annotation") and m["data"]["annotation_id"] == aid]
    assert [(a["category"], a["status"], a["path"]) for a in op_versions] == [
        ("U", "candidate", "retrieval"), ("F", "verified", "web")]  # async worker upgraded it
    au_ids = [m["data"]["annotation_id"] for m in msgs(auc, "annotation")]
    assert aid in au_ids  # public only after the upgrade
    assert msgs(opc, "metrics") and not msgs(auc, "metrics")  # metrics are operator-only
    events = read_jsonl(tmp_path / "rec" / "log.jsonl")
    end = json.loads(events[-1].payload_json)
    assert verify_chain(events) and end["metrics"]["bank_hit_rate"] > 0 and "p95_bank_ms" in end


async def test_real_intel_stubs_fail_warm_up_clearly():
    r = SessionRunner("x", "fixture", intel=Intel.real())
    with pytest.raises(ValueError, match="INTEL=fake"):
        await r.warm_up()


# --- REPLAY_EVENTS ---------------------------------------------------------------------------


async def test_replay_events_reproduces_recorded_run(tmp_path):
    rec_runner, rec_op, rec_au, _ = await record_run(tmp_path)
    killed = [a for a in rec_runner.aggregator.state.values() if a.status == "killed"]
    assert len(killed) == 1

    op, au = Hub("operator"), Hub("audience")
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc), au.add(auc)
    r = SessionRunner("replay", "fixture", operator=op, audience=au, source="events", replay_session_id="rec",
                      runs_dir=tmp_path, speed=40)
    await r.warm_up()
    await r.start(lead_s=0.05)
    await asyncio.wait_for(r.wait(), 20)

    def strip(ms):
        return [(m["type"], m.get("data", {}).get("segment_id") or m.get("data", {}).get("annotation_id"),
                 m.get("data", {}).get("version")) for m in ms if m["type"] in ("segment", "annotation", "annotation_update")]

    assert sorted(strip(msgs(opc)), key=str) == sorted(strip(msgs(rec_op)), key=str)
    removal = msgs(auc, "removal")
    assert removal == [{"type": "removal", "annotation_id": killed[0].annotation_id, "status": "killed"}]
    assert any("replayed from its event log" in b["text"] for b in msgs(opc, "banner"))
    assert r.aggregator.state[killed[0].annotation_id].version == killed[0].version  # versions preserved

    with pytest.raises(ValueError, match="no recorded event log"):
        await SessionRunner("y", "fixture", source="events", replay_session_id="nope", runs_dir=tmp_path).warm_up()


# --- report ------------------------------------------------------------------------------------


def test_report_rules_from_synthetic_log():
    w = EventWriter("rep")
    evs = []
    def put(t, p):
        payload = p.model_dump_json() if hasattr(p, "model_dump_json") else json.dumps(p)
        evs.append(w._chain(0.0, t, payload))
    mod = SEGMENTS[0]
    a_seg, b_seg = SEGMENTS[1], SEGMENTS[2]
    put("session_start", {"clip_id": "fixture", "t0_wall": 1.7e9, "source": "replay"})
    for s in (mod, a_seg, b_seg):
        put("segment", s)
    put("annotation", ann("x1", seg=b_seg.segment_id, spk=b_seg.speaker_id, category="V", latency_ms=500))
    put("annotation", ann("x2", seg=a_seg.segment_id, spk=a_seg.speaker_id, category="F", latency_ms=700,
                          explanation="<script>alert(1)</script>"))
    put("annotation", ann("x3", seg=a_seg.segment_id, spk=a_seg.speaker_id, category="U", status="candidate",
                          path="retrieval", latency_ms=900))
    put("annotation", ann("x4", seg=a_seg.segment_id, spk=a_seg.speaker_id, category="M", latency_ms=300))
    put("kill", {"annotation_id": "x4", "by": "operator"})
    put("annotation_update", ann("x4", seg=a_seg.segment_id, spk=a_seg.speaker_id, category="M", status="killed", version=2))
    put("annotation", ann("x5", seg=b_seg.segment_id, spk=b_seg.speaker_id, category="C", path="retrieval", latency_ms=800))
    put("session_end", {"metrics": {"p50_ms": 1}})

    r = reports.build_report(evs, {"per_category": {"F": {"precision": 0.9, "recall": 0.7, "n": 20}}})
    h = r["header"]
    assert h["chain_valid"] and h["complete"] and h["participants"] == ["speaker_a", "speaker_b"]
    assert h["log_head_hash"] == evs[-1].hash and h["model_ids"] == ["m1"]
    assert [sp["speaker_id"] for sp in r["highlights"]] == ["speaker_a", "speaker_b"]  # alphabetical, moderator absent
    a_cats = [c["category"] for c in r["highlights"][0]["categories"]]
    assert a_cats == ["F"]  # killed M hidden, U goes to its own section
    assert [c["category"] for c in r["highlights"][1]["categories"]] == ["V"]
    assert [i["annotation_id"] for i in r["unverifiable"]] == ["x3"]
    assert [i["annotation_id"] for i in r["differs_from_prior"]] == ["x5"]
    assert r["operator_actions"] == {"killed": 1, "kill_events": 1, "withdrawn": 0}
    assert r["latency"]["p95_bank_ms"] == 700 and r["latency"]["n"] == 5
    page = reports.render_html(r)
    assert "&lt;script&gt;" in page and "<script>" not in page and "0.9" in page
    with pytest.raises(ValueError):
        reports.build_report([])


def test_http_sessions_and_report_routes(tmp_path):
    asyncio.run(record_run(tmp_path))
    mgr = SessionManager(runs_dir=tmp_path)
    client = TestClient(create_app(mgr))
    assert [s["session_id"] for s in client.get("/sessions").json()] == ["rec"]
    r = client.get("/report/rec").json()
    assert r["header"]["clip_id"] == "fixture" and r["header"]["chain_valid"]
    assert r["operator_actions"]["killed"] == 1
    page = client.get("/report/rec.html")
    assert page.status_code == 200 and page.text.startswith("<!doctype html>")
    assert client.get("/report/missing").status_code == 404


async def test_load_session_events_from_local_archive(tmp_path):
    await record_run(tmp_path, kill_first_v=False)
    local = read_jsonl(tmp_path / "rec" / "log.jsonl")
    got = await load_session_events("rec", None, tmp_path)
    assert [e.seq for e in got] == [e.seq for e in local]
    assert await load_session_events("missing", None, tmp_path) == []
