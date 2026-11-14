"""Shared Phase 0 contracts. IDs are session:sequence and segment:lane:index.

Internal records use JSON-safe embeddings; runtime indexes may convert to numpy.
Candidate annotations deliberately permit missing evidence; verification is an aggregator duty.
"""
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["speaker", "moderator"]
Category = Literal["F", "M", "D", "L", "C", "U", "V"]
Lane = Literal["claims", "fallacy", "consistency"]
Confidence = Annotated[float, Field(ge=0, le=1)]

class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Segment(ContractModel):
    segment_id: str
    session_id: str
    speaker_id: str
    role: Role
    text: str
    t_start: float
    t_end: float
    stt_confidence: Confidence
    low_conf_words: list[str] = Field(default_factory=list)

class Evidence(ContractModel):
    source_id: str
    title: str
    url: str
    publisher: str
    reliability: Literal["primary", "factchecker", "media"]
    quote: str
    published: str | None
    retrieved_at: str

class Annotation(ContractModel):
    annotation_id: str
    version: int = Field(ge=1)
    segment_id: str
    speaker_id: str
    span_text: str
    span_char_start: int = Field(ge=0)
    span_char_end: int = Field(ge=0)
    category: Category
    subtype: str | None
    explanation: str
    evidence: list[Evidence] = Field(default_factory=list)
    opponent_span: str | None
    confidence: Confidence
    status: Literal["candidate", "verified", "withdrawn", "killed"]
    path: Literal["bank", "retrieval", "llm", "web"]
    lane: Lane
    latency_ms: int
    model_id: str | None
    prompt_hash: str | None
    repeat_count: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def span_end_at_least_start(self) -> "Annotation":
        if self.span_char_end < self.span_char_start:
            raise ValueError("span_char_end must be >= span_char_start")
        return self

class Claim(ContractModel):
    text: str
    decontextualized: str
    entities: list[str] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)
    topic: str | None = None
    embedding: list[float] = Field(default_factory=list)

class ClaimRecord(ContractModel):
    claim: Claim
    speaker_id: str
    segment_id: str
    annotation_id: str | None = None

class Passage(ContractModel):
    passage_id: str
    text: str
    source: Evidence
    topic: str | None = None
    embedding: list[float] = Field(default_factory=list)

class Statement(ContractModel):
    statement_id: str
    speaker_id: str
    text: str
    date: str
    source_url: str
    topic: str | None = None
    embedding: list[float] = Field(default_factory=list)

class BankHit(ContractModel):
    claim: Claim
    verdict_category: Category
    explanation: str
    evidence: list[Evidence]
    similarity: Confidence

class SpeakerDigest(ContractModel):
    speaker_id: str
    name: str
    office: str = ""
    key_positions: list[str] = Field(default_factory=list)
    summary: str = ""

class LaneContext(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    active_topic: str | None = None
    last_question: Segment | None = None
    recent_segments: list[Segment] = Field(default_factory=list)
    speaker_turn: list[Segment] = Field(default_factory=list)
    opponent_turn: list[Segment] = Field(default_factory=list)
    rolling_summary: str = ""
    speaker_digest: SpeakerDigest | None = None
    claim: Claim | None = None
    prior_statements: list[Statement] = Field(default_factory=list)
    claim_ledger: list[ClaimRecord] = Field(default_factory=list)

class VerdictJob(ContractModel):
    kind: Literal["verdict"] = "verdict"
    claim: Claim
    segment: Segment
    annotation: Annotation
    speaker_digest: SpeakerDigest | None = None
    debate_date: str | None = None

class PrefetchJob(ContractModel):
    kind: Literal["prefetch"] = "prefetch"
    question: Segment
    topic: str | None
    participants: list[SpeakerDigest] = Field(default_factory=list)

SlowJob = Annotated[VerdictJob | PrefetchJob, Field(discriminator="kind")]

class LLMResult(ContractModel):
    data: dict[str, Any]
    model_id: str
    prompt_hash: str
    provider: str
    usage: dict[str, int] = Field(default_factory=dict)

class Word(ContractModel):
    text: str
    t_start: float
    t_end: float
    confidence: Confidence
    speaker_id: str | None = None
    role: Role | None = None

class TimelineEntry(ContractModel):
    t_start: float
    t_end: float
    speaker_id: str
    role: Role

class Clip(ContractModel):
    clip_id: str
    title: str
    video_path: str
    audio_path: str
    timeline: list[TimelineEntry] = Field(default_factory=list)
    transcript_path: str | None = None
    duration: float | None = None

class Event(ContractModel):
    session_id: str
    seq: int = Field(ge=0)
    ts: float
    type: Literal["session_start", "segment", "annotation", "annotation_update", "kill", "session_end"]
    payload_json: str
    prev_hash: str
    hash: str

class MetricsSnapshot(ContractModel):
    p50_ms: float
    p95_ms: float
    bank_hit_rate: Confidence
    cache_tokens: int = 0
    loop_lag_ms: float = 0

class SegmentMessage(ContractModel):
    type: Literal["segment"] = "segment"
    data: Segment

class AnnotationMessage(ContractModel):
    type: Literal["annotation"] = "annotation"
    data: Annotation

class AnnotationUpdateMessage(ContractModel):
    type: Literal["annotation_update"] = "annotation_update"
    data: Annotation

class MetricsMessage(ContractModel):
    type: Literal["metrics"] = "metrics"
    data: MetricsSnapshot

class ReadyMessage(ContractModel):
    type: Literal["ready"] = "ready"
    session_id: str

class StartedMessage(ContractModel):
    type: Literal["started"] = "started"
    t0_wall: float

class RemovalMessage(ContractModel):
    type: Literal["removal"] = "removal"
    annotation_id: str
    status: Literal["withdrawn", "killed"]

class StartMessage(ContractModel):
    type: Literal["start"] = "start"
    clip_id: str

class KillMessage(ContractModel):
    type: Literal["kill"] = "kill"
    annotation_id: str

ServerMessage = Annotated[SegmentMessage | AnnotationMessage | AnnotationUpdateMessage | MetricsMessage | ReadyMessage | StartedMessage | RemovalMessage, Field(discriminator="type")]
ClientMessage = Annotated[StartMessage | KillMessage, Field(discriminator="type")]

def segment_id(session_id: str, seq: int) -> str:
    return f"{session_id}:{seq}"

def annotation_id(segment_id: str, lane: Lane, n: int) -> str:
    return f"{segment_id}:{lane}:{n}"
