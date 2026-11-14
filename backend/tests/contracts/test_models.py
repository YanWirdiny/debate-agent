import inspect
import json
from pathlib import Path
import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from app import models as m

SEG = json.loads((Path(__file__).parents[1] / "fixtures/segments.jsonl").read_text().splitlines()[1])
EVIDENCE = dict(source_id="source", title="Reference", url="https://example.org", publisher="Example", reliability="primary", quote="one moon", published=None, retrieved_at="2026-11-14")
CLAIM = dict(text="one moon", decontextualized="Earth has one moon")
ANN = dict(annotation_id="fixture:1:claims:0", version=1, segment_id="fixture:1", speaker_id="speaker_a", span_text=SEG["text"], span_char_start=0, span_char_end=len(SEG["text"]), category="F", subtype=None, explanation="Differs from the cited reference.", evidence=[EVIDENCE], opponent_span=None, confidence=.95, status="candidate", path="bank", lane="claims", latency_ms=100, model_id=None, prompt_hash=None)
SAMPLES = {
    "Segment": SEG, "Evidence": EVIDENCE, "Annotation": ANN,
    "Claim": CLAIM, "ClaimRecord": dict(claim=CLAIM, speaker_id="speaker_a", segment_id="fixture:1"),
    "Passage": dict(passage_id="p", text="one moon", source=EVIDENCE),
    "Statement": dict(statement_id="s", speaker_id="speaker_a", text="prior", date="2025-01-01", source_url="https://example.org"),
    "BankHit": dict(claim=CLAIM, verdict_category="F", explanation="Reference", evidence=[EVIDENCE], similarity=.9),
    "SpeakerDigest": dict(speaker_id="speaker_a", name="Speaker A"),
    "LaneContext": dict(recent_segments=[SEG]),
    "VerdictJob": dict(claim=CLAIM, segment=SEG, annotation=ANN),
    "PrefetchJob": dict(question=SEG, topic=None),
    "LLMResult": dict(data={"claims": [CLAIM]}, model_id="fixture", prompt_hash="abc", provider="fake"),
    "Word": dict(text="Hello", t_start=0, t_end=1, confidence=.9),
    "TimelineEntry": dict(t_start=0, t_end=1, speaker_id="speaker_a", role="speaker"),
    "Clip": dict(clip_id="clip", title="Fixture", video_path="clip.mp4", audio_path="audio.wav"),
    "Event": dict(session_id="fixture", seq=0, ts=0, type="segment", payload_json="{}", prev_hash="", hash="abc"),
    "MetricsSnapshot": dict(p50_ms=100, p95_ms=200, bank_hit_rate=.5),
    "SegmentMessage": dict(data=SEG), "AnnotationMessage": dict(data=ANN),
    "AnnotationUpdateMessage": dict(data=ANN),
    "MetricsMessage": dict(data=dict(p50_ms=100, p95_ms=200, bank_hit_rate=.5)),
    "ReadyMessage": dict(session_id="fixture"), "StartedMessage": dict(t0_wall=123.),
    "RemovalMessage": dict(annotation_id=ANN["annotation_id"], status="killed"),
    "StartMessage": dict(clip_id="clip"), "KillMessage": dict(annotation_id=ANN["annotation_id"]),
    "LoadMessage": dict(clip_id="clip"), "BannerMessage": dict(level="warn", text="Deepgram reconnecting"),
    "PendingMessage": dict(segment_id="fixture:1", lane="claims", reason="timeout"),
}
MODELS = {name: cls for name, cls in vars(m).items() if inspect.isclass(cls) and issubclass(cls, BaseModel) and cls.__module__ == m.__name__ and cls is not m.ContractModel}

def test_every_model_has_sample():
    assert MODELS.keys() == SAMPLES.keys()

@pytest.mark.parametrize("name", SAMPLES)
def test_json_roundtrip(name):
    obj = MODELS[name].model_validate(SAMPLES[name])
    assert MODELS[name].model_validate_json(obj.model_dump_json()) == obj

@pytest.mark.parametrize("alias,names", [(m.SlowJob, ["VerdictJob", "PrefetchJob"]), (m.ClientMessage, ["LoadMessage", "StartMessage", "KillMessage"]), (m.ServerMessage, ["SegmentMessage", "AnnotationMessage", "AnnotationUpdateMessage", "MetricsMessage", "ReadyMessage", "StartedMessage", "RemovalMessage", "BannerMessage", "PendingMessage"])])
def test_discriminated_unions(alias, names):
    adapter = TypeAdapter(alias)
    for name in names:
        obj = MODELS[name].model_validate(SAMPLES[name])
        assert adapter.validate_json(obj.model_dump_json()) == obj
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "invalid", "kind": "invalid"})

def test_fixture_and_ids():
    rows = [m.Segment.model_validate_json(line) for line in (Path(__file__).parents[1] / "fixtures/segments.jsonl").read_text().splitlines()]
    assert len(rows) == 20
    assert {s.speaker_id for s in rows} == {"speaker_a", "speaker_b", "moderator"}
    for i, seg in enumerate(rows):
        assert seg.segment_id == m.segment_id(seg.session_id, i)
    assert m.annotation_id("fixture:1", "claims", 0) == ANN["annotation_id"]

def test_annotation_span_end_at_least_start():
    assert m.Annotation.model_validate(dict(ANN, span_char_start=4, span_char_end=4)).span_char_end == 4
    with pytest.raises(ValidationError, match="span_char_end"):
        m.Annotation.model_validate(dict(ANN, span_char_start=4, span_char_end=3))

def test_independent_defaults():
    first = m.Claim.model_validate(CLAIM)
    second = m.Claim.model_validate(CLAIM)
    first.entities.append("Earth")
    assert second.entities == []

def test_fixture_labels_align():
    root = Path(__file__).parents[1] / "fixtures"
    segs = [m.Segment.model_validate_json(l) for l in (root / "segments.jsonl").read_text().splitlines()]
    labels = [json.loads(l) for l in (root / "segments_labels.jsonl").read_text().splitlines()]
    assert [l["segment_id"] for l in labels] == [s.segment_id for s in segs]
    for seg, label in zip(segs, labels):
        if seg.role == "moderator":
            assert label["expected_category"] is None
