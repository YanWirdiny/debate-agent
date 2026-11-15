"""P1 Phase 1: timeline, segmenter, event log, aggregator, session context, end-to-end replay, WS."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ingest
from app.aggregator import Aggregator
from app.eventlog import EventWriter, MemorySink, verify_chain
from app.hubs import Hub, RecordingClient
from app.ingest import TimelineLookup
from app.main import SessionManager, create_app
from app.models import Annotation, ClaimRecord, Claim, Evidence, Segment, TimelineEntry, Word
from app.segmenter import Segmenter
from app.session import SessionContext, SessionRunner

FIXTURES = Path(__file__).parents[1] / "fixtures" / "segments.jsonl"
SEGMENTS = [Segment.model_validate_json(l) for l in FIXTURES.read_text().splitlines() if l.strip()]


def tl(*rows):
    return [TimelineEntry(t_start=a, t_end=b, speaker_id=s, role="moderator" if s == "mod" else "speaker") for a, b, s in rows]


def w(text, t, conf=0.95, spk=None, role=None):
    return Word(text=text, t_start=t, t_end=t + 0.3, confidence=conf, speaker_id=spk, role=role)


def ann(seg_id="s:1", aid="s:1:claims:0", **kw):
    ev = Evidence(source_id="s", title="t", url="https://example.org", publisher="p",
                  reliability="primary", quote="q", published=None, retrieved_at="2026-11-14")
    base = dict(annotation_id=aid, version=1, segment_id=seg_id, speaker_id="a", span_text="x",
                span_char_start=0, span_char_end=1, category="F", subtype=None, explanation="e",
                evidence=[ev], opponent_span=None, confidence=0.9, status="verified", path="bank",
                lane="claims", latency_ms=0, model_id=None, prompt_hash=None)
    base.update(kw)
    return Annotation(**base)


# --- timeline ------------------------------------------------------------------


def test_timeline_inside_gap_and_edges():
    t = TimelineLookup(tl((0, 5, "mod"), (6, 10, "a"), (12, 20, "b")))
    assert t.who(2) == ("mod", "moderator")
    assert t.who(7) == ("a", "speaker")
    assert t.who(10.8) == ("a", "speaker")  # gap, nearer to a
    assert t.who(11.6) == ("b", "speaker")  # gap, nearer to b
    assert t.who(-3) == ("mod", "moderator")
    assert t.who(99) == ("b", "speaker")
    with pytest.raises(ValueError):
        TimelineLookup([])


# --- segmenter -----------------------------------------------------------------


def test_segmenter_punctuation_speaker_change_and_ids():
    seg = Segmenter("s", TimelineLookup(tl((0, 5, "a"), (5, 10, "b"))))
    out = seg.on_final([w("Hello", 0), w("world.", 0.5), w("Still", 1), w("talking", 1.5)])
    assert [s.text for s in out] == ["Hello world."]
    out = seg.on_final([w("now", 5.5), w("b", 6)])  # speaker change forces a split
    assert [(s.speaker_id, s.text) for s in out] == [("a", "Still talking")]
    assert [s.text for s in seg.flush()] == ["now b"]
    assert seg.flush() == []
    ids = [s.segment_id for s in [*out]]
    assert ids == ["s:1"]


def test_segmenter_abbreviation_quote_and_low_conf():
    seg = Segmenter("s")
    words = [w("The", 0, spk="a", role="speaker"), w("U.S.", 0.3, spk="a", role="speaker"),
             w("grew", 0.6, spk="a", role="speaker"), w("50", 0.9, conf=0.6, spk="a", role="speaker"),
             w('percent."', 1.2, spk="a", role="speaker")]
    [s] = seg.on_final(words)
    assert s.text == 'The U.S. grew 50 percent."'
    assert s.low_conf_words == ["50"]
    assert s.stt_confidence == pytest.approx((0.95 * 4 + 0.6) / 5, abs=1e-3)


def test_replay_words_round_trip_fixture():
    words = ingest.load_transcript_words(FIXTURES)
    seg = Segmenter("fixture")
    out = seg.on_final(words) + seg.flush()
    assert [s.text for s in out] == [s.text for s in SEGMENTS]
    assert [s.speaker_id for s in out] == [s.speaker_id for s in SEGMENTS]


# --- event log -----------------------------------------------------------------


class FlakySink(MemorySink):
    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self.fail_times = fail_times

    async def insert(self, events):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("mongo down")
        await super().insert(events)


async def test_event_writer_chain_and_fallback(tmp_path):
    sink = FlakySink(fail_times=1)
    fallback = tmp_path / "events.jsonl"
    wr = EventWriter("s", sink=sink, fallback_path=fallback, batch_interval=0.01)
    task = asyncio.create_task(wr.run())
    wr.put("session_start", {"a": 1})
    wr.put("segment", SEGMENTS[0])
    await asyncio.sleep(0.05)  # first batch fails -> buffered + jsonl
    assert sink.events == [] and len(fallback.read_text().splitlines()) == 2
    wr.put("session_end", {"a": 2})
    await wr.stop()
    await task
    assert [e.seq for e in sink.events] == [0, 1, 2]
    assert verify_chain(sink.events)
    assert len(fallback.read_text().splitlines()) == 2  # buffered events written once only
    tampered = [sink.events[0], sink.events[1].model_copy(update={"payload_json": "{}"}), sink.events[2]]
    assert not verify_chain(tampered)


# --- aggregator ----------------------------------------------------------------


async def test_aggregator_versions_routing_and_kill():
    op, au = Hub("operator"), Hub("audience")
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc), au.add(auc)
    wr = EventWriter("s", batch_interval=0.01)
    agg = Aggregator(operator=op, audience=au, writer=wr)
    task = asyncio.create_task(agg.run())

    agg.submit(ann(aid="c", status="candidate"))  # operator only
    agg.submit(ann(aid="c", status="verified"))  # becomes public
    agg.submit(ann(aid="m", category="U"))  # never public
    agg.kill("c", by="operator")
    agg.submit(ann(aid="c", status="verified"))  # ignored: killed is final
    await agg.stop()
    await task

    assert agg.state["c"].status == "killed" and agg.state["c"].version == 3
    op_types = [json.loads(m)["type"] for m in opc.messages]
    assert op_types == ["annotation", "annotation_update", "annotation", "annotation_update"]
    au_msgs = [json.loads(m) for m in auc.messages]
    assert [m["type"] for m in au_msgs] == ["annotation", "removal"]
    assert au_msgs[1] == {"type": "removal", "annotation_id": "c", "status": "killed"}


# --- session context -----------------------------------------------------------


def test_session_context_views():
    ctx = SessionContext("fixture")
    for s in SEGMENTS[:6]:  # mod?, a, b, a, mod?, b
        ctx.on_segment(s)
    b = SEGMENTS[5]
    assert ctx.last_question == SEGMENTS[4]
    claims = ctx.view_for(b, "claims")
    assert [s.segment_id for s in claims.recent_segments] == [SEGMENTS[2].segment_id]
    fallacy = ctx.view_for(b, "fallacy")
    assert fallacy.speaker_turn == [b]
    assert fallacy.opponent_turn == [SEGMENTS[3]]
    ctx.add_claim(ClaimRecord(claim=Claim(text="x", decontextualized="x"), speaker_id=b.speaker_id, segment_id=b.segment_id))
    assert len(ctx.view_for(b, "consistency").claim_ledger) == 1
    with pytest.raises(Exception):
        claims.active_topic = "x"  # type: ignore[misc]  views are frozen
    claims.recent_segments.clear()  # a copy: clearing it must not touch the context
    assert [s.segment_id for s in ctx.view_for(b, "claims").recent_segments] == [SEGMENTS[2].segment_id]


# --- end to end ----------------------------------------------------------------


async def test_end_to_end_replay_with_fake_lanes():
    op, au = Hub("operator"), Hub("audience")
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc), au.add(auc)
    sink = MemorySink()
    runner = SessionRunner("e2e", "fixture", operator=op, audience=au, sink=sink, speed=40)
    await runner.warm_up()
    await runner.start(lead_s=0.05)
    await asyncio.wait_for(runner.wait(), 15)

    op_msgs = [json.loads(m) for m in opc.messages]
    au_msgs = [json.loads(m) for m in auc.messages]
    segs = [m["data"] for m in op_msgs if m["type"] == "segment"]
    assert [s["text"] for s in segs] == [s.text for s in SEGMENTS]
    anns = [m["data"] for m in op_msgs if m["type"] == "annotation"]
    assert anns and {a["lane"] for a in anns} == {"claims", "fallacy"}
    moderator_ids = {s["segment_id"] for s in segs if s["role"] == "moderator"}
    assert not any(a["segment_id"] in moderator_ids for a in anns)
    au_anns = [m["data"] for m in au_msgs if m["type"] == "annotation"]
    assert au_anns and all(a["status"] == "verified" for a in au_anns)  # candidates never reach audience
    assert verify_chain(sink.events)
    types = [e.type for e in sink.events]
    assert types[0] == "session_start" and types[-1] == "session_end"
    assert types.count("segment") == len(SEGMENTS)
    # Both views are told where the clip clock stopped (never past the clip). The notice goes out
    # before lane drain, so a late flag or metrics tick can still follow it.
    for msgs in (op_msgs, au_msgs):
        ended = [m for m in msgs if m["type"] == "ended"]
        assert len(ended) == 1
        assert ended[0]["session_id"] == "e2e" and ended[0]["clip_time"] >= SEGMENTS[-1].t_end - 1
        assert runner.clip.duration is None or ended[0]["clip_time"] <= runner.clip.duration
    assert runner.ended_clip_time == ended[0]["clip_time"]


async def _ended_while_cleanup_is_blocked(clip, *, rewind_s: float = 0.0):
    """Start a session, hold cleanup on a lane that never finishes, and return once `ended` is out."""
    op, au = Hub("operator"), Hub("audience")
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc)
    au.add(auc)
    runner = SessionRunner("end", "fixture", clip=clip, operator=op, audience=au, sink=MemorySink(), speed=1)
    await runner.warm_up()
    await runner.start(lead_s=0)
    if rewind_s:
        runner.t0_wall = time.time() - rewind_s
    release = asyncio.Event()

    async def stuck() -> None:
        await release.wait()

    runner._lane_tasks.add(asyncio.create_task(stuck()))
    stopping = asyncio.create_task(runner.stop())

    async def wait_ended() -> None:
        while not any(json.loads(m)["type"] == "ended" for m in opc.messages):
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_ended(), 1)
        frozen = runner.ended_clip_time
        assert not stopping.done()  # views were told before lane drain and the rest of shutdown
        await asyncio.sleep(0.3)  # cleanup is still blocked; the frozen clock must not move with it
        assert runner.ended_clip_time == frozen and not stopping.done()
    finally:
        release.set()
        await asyncio.wait_for(stopping, 2)
    ended = [json.loads(m) for m in opc.messages if json.loads(m)["type"] == "ended"]
    audience = [json.loads(m) for m in auc.messages if json.loads(m)["type"] == "ended"]
    assert len(ended) == 1 and ended == audience
    assert runner.ended_clip_time == ended[0]["clip_time"]
    return runner


async def test_stop_freezes_clip_time_before_cleanup():
    clip = ingest.load_clip("fixture").model_copy(update={"duration": None})
    runner = await _ended_while_cleanup_is_blocked(clip)
    # No duration to cap it, and cleanup was held well past this. The clock is the moment of the stop.
    assert runner.ended_clip_time is not None and runner.ended_clip_time < 1


async def test_ended_clip_time_stays_capped_when_cleanup_is_slow():
    clip = ingest.load_clip("fixture").model_copy(update={"duration": 5.0})
    runner = await _ended_while_cleanup_is_blocked(clip, rewind_s=100)
    assert runner.ended_clip_time == 5.0


def test_websocket_load_start_and_bad_message():
    mgr = SessionManager()
    mgr.speed = 40
    client = TestClient(create_app(mgr))
    assert any(c["clip_id"] == "fixture" for c in client.get("/clips").json())
    with client.websocket_connect("/ws/operator") as ws:
        ws.send_text('{"type": "nope"}')
        assert ws.receive_json()["type"] == "banner"
        ws.send_text('{"type": "load", "clip_id": "fixture"}')
        assert ws.receive_json()["type"] == "ready"
        ws.send_text('{"type": "start", "clip_id": "fixture"}')
        assert ws.receive_json()["type"] == "started"
        seen = []
        while "segment" not in seen and len(seen) < 200:
            seen.append(ws.receive_json()["type"])
        assert "segment" in seen and "partial" in seen  # live words arrive before the final sentence


def test_segmenter_partial_carries_the_next_segment_id_and_word_times():
    from app.models import Word
    from app.segmenter import Segmenter

    sg = Segmenter("s")
    w = lambda t, a, spk="a": Word(text=t, t_start=a, t_end=a + 0.3, confidence=0.99, speaker_id=spk, role="speaker")
    assert sg.partial() is None
    assert sg.on_final([w("Prices", 1.0), w("rose", 1.4)]) == []
    p = sg.partial()
    assert p.segment_id == "s:0" and [x.text for x in p.words] == ["Prices", "rose"] and p.words[1].t_start == 1.4
    [seg] = sg.on_final([w("fast.", 1.8), w("Next", 2.4)])
    assert seg.segment_id == "s:0" and seg.text == "Prices rose fast."  # final replaces the partial (same id)
    p = sg.partial()
    assert p.segment_id == "s:1" and [x.text for x in p.words] == ["Next"]
