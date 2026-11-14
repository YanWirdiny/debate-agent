"""Operator "pending" indicator (CONTEXT.md rule 7): a lane that times out or fails ships a final
PendingMessage to the operator only, is logged, replayed by REPLAY_EVENTS, and counted in the report."""

import asyncio
import dataclasses
import json

from app import report as reports
from app.eventlog import read_jsonl, verify_chain
from app.hubs import Hub, RecordingClient
from app.lanes import fake
from app.models import PendingMessage
from app.session import Intel, SessionRunner

from tests.pipeline.test_phase23 import SEGMENTS, msgs


def slow_intel():
    """fallacy: always slower than the budget (timeout). claims: raises on 'caused' (error)."""

    async def fallacy(seg, view, emit):
        await asyncio.sleep(5)

    async def claims(seg, view, kb, emit, slow_q):
        if "caused" in seg.text:
            raise RuntimeError("invalid LLM JSON after retry")
        await fake.run_claims(seg, view, kb, emit, slow_q)

    return dataclasses.replace(Intel.fake(), run_fallacy=fallacy, run_claims=claims)


def hubs():
    op, au = Hub("operator"), Hub("audience")
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc), au.add(auc)
    return op, au, opc, auc


async def run(runner):
    await runner.warm_up()
    await runner.start(lead_s=0.05)
    await asyncio.wait_for(runner.wait(), 30)


async def test_pending_on_timeout_and_error_operator_only(tmp_path):
    op, au, opc, auc = hubs()
    (tmp_path / "rec").mkdir()
    r = SessionRunner("rec", "fixture", operator=op, audience=au, intel=slow_intel(), speed=40,
                      lane_timeout=0.5, archive_path=tmp_path / "rec" / "log.jsonl")
    await run(r)

    speaker = [s.segment_id.replace("fixture", "rec") for s in SEGMENTS if s.role == "speaker"]
    caused = next(s for s in SEGMENTS if "caused" in s.text).segment_id.replace("fixture", "rec")
    pending = [PendingMessage.model_validate(m) for m in msgs(opc, "pending")]

    timeouts = {p.segment_id for p in pending if p.lane == "fallacy"}
    assert timeouts == set(speaker) and all(p.reason == "timeout" for p in pending if p.lane == "fallacy")
    assert [(p.segment_id, p.reason) for p in pending if p.lane == "claims"] == [(caused, "error")]
    assert not msgs(auc, "pending")  # never on the audience screen
    assert r.lane_timeouts == len(speaker)
    assert msgs(opc, "segment") and len(msgs(opc, "segment")) == len(SEGMENTS)  # captions never blocked

    events = read_jsonl(tmp_path / "rec" / "log.jsonl")
    assert verify_chain(events)
    assert sum(e.type == "pending" for e in events) == len(pending)

    rep = reports.build_report(events)
    assert rep["unchecked"] == {"speaker_segments": len(speaker), "by_lane": {"claims": 1, "fallacy": len(speaker)}}
    html = reports.render_html(rep)
    assert "Not checked" in html and "not necessarily accurate" in html

    # REPLAY_EVENTS re-sends the recorded pendings to the operator, still never to the audience.
    op2, au2, opc2, auc2 = hubs()
    r2 = SessionRunner("replay", "fixture", operator=op2, audience=au2, source="events",
                       replay_session_id="rec", runs_dir=tmp_path, speed=40)
    await run(r2)
    assert sorted(json.dumps(m, sort_keys=True) for m in msgs(opc2, "pending")) == \
        sorted(json.dumps(m, sort_keys=True) for m in msgs(opc, "pending"))
    assert not msgs(auc2, "pending")


async def test_no_pending_when_lanes_finish(tmp_path):
    op, au, opc, _ = hubs()
    (tmp_path / "ok").mkdir()
    r = SessionRunner("ok", "fixture", operator=op, audience=au, intel=Intel.fake(), speed=40,
                      archive_path=tmp_path / "ok" / "log.jsonl")
    await run(r)
    assert not msgs(opc, "pending")
    rep = reports.build_report(read_jsonl(tmp_path / "ok" / "log.jsonl"))
    assert rep["unchecked"]["by_lane"] == {}
    assert "was checked by every lane" in reports.render_html(rep)
