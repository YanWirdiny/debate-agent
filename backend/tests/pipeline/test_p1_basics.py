"""P1 tests: audience gate and fake lanes on the shared fixture segments."""

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.aggregator import Aggregator
from app.lanes import fake
from app.main import app
from app.models import Annotation, Evidence, LaneContext, Segment, SlowJob

FIXTURES = Path(__file__).parents[1] / "fixtures" / "segments.jsonl"
SEGMENTS = [Segment.model_validate_json(line) for line in FIXTURES.read_text().splitlines() if line.strip()]

EVIDENCE = Evidence(
    source_id="s", title="t", url="https://example.org", publisher="p",
    reliability="primary", quote="q", published=None, retrieved_at="2026-11-14",
)


def ann(**kw) -> Annotation:
    base = dict(
        annotation_id="fixture:1:claims:0", version=1, segment_id="fixture:1", speaker_id="speaker_a",
        span_text="zero percent", span_char_start=0, span_char_end=12, category="F", subtype=None,
        explanation="x", evidence=[EVIDENCE], opponent_span=None, confidence=0.9, status="verified",
        path="bank", lane="claims", latency_ms=0, model_id=None, prompt_hash=None,
    )
    base.update(kw)
    return Annotation(**base)


def test_is_public_gate():
    g = Aggregator().is_public
    assert g(ann())
    assert not g(ann(status="candidate"))
    assert not g(ann(confidence=0.79))
    assert not g(ann(category="U"))
    assert not g(ann(evidence=[]))
    assert not g(ann(evidence=[EVIDENCE.model_copy(update={"quote": ""})]))
    assert not g(ann(category="L", subtype="strawman", evidence=[]))
    assert g(ann(category="L", subtype="strawman", opponent_span="ban all vehicles", evidence=[]))


async def test_fake_lanes_emit_valid_spans():
    emitted: list = []
    q: asyncio.Queue[SlowJob] = asyncio.Queue()
    for seg in SEGMENTS:
        if seg.role == "moderator":
            continue
        await fake.run_claims(seg, LaneContext(), None, emitted.append, q)  # type: ignore[arg-type]
        await fake.run_fallacy(seg, LaneContext(), emitted.append)
    out: list[Annotation] = [a for a in emitted if isinstance(a, Annotation)]
    claim_ids = {r.annotation_id for r in emitted if not isinstance(r, Annotation)}
    assert claim_ids == {a.annotation_id for a in out if a.lane == "claims"}  # every claims flag has its ClaimRecord
    assert q.qsize() == 1  # the "caused" sentence went to the async plane
    assert {a.lane for a in out} == {"claims", "fallacy"}
    text = {s.segment_id: s.text for s in SEGMENTS}
    for a in out:
        assert text[a.segment_id][a.span_char_start : a.span_char_end] == a.span_text


def test_app_health():
    assert TestClient(app).get("/health").json() == {"ok": True}
