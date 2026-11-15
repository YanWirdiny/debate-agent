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
    type: Literal["session_start", "segment", "annotation", "annotation_update", "kill", "session_end", "pending"]
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

class EndedMessage(ContractModel):
    """The session is over (clip played out, operator stopped it, or a new clip was loaded).
    `clip_time`: seconds of clip played, so views freeze their clock there instead of counting on."""
    type: Literal["ended"] = "ended"
    session_id: str
    clip_time: float

class RemovalMessage(ContractModel):
    type: Literal["removal"] = "removal"
    annotation_id: str
    status: Literal["withdrawn", "killed"]

class BannerMessage(ContractModel):
    """Operator alert, e.g. Deepgram reconnecting or replay mode active."""
    type: Literal["banner"] = "banner"
    level: Literal["info", "warn", "error"]
    text: str

class PartialWord(ContractModel):
    text: str
    t_start: float
    t_end: float

class PartialMessage(ContractModel):
    """Both views: the sentence being spoken, before it is final (live captions at speaking pace).
    `segment_id` is the id the final Segment will get; the final SegmentMessage replaces it.
    Ephemeral: not written to the event log, never analysed."""
    type: Literal["partial"] = "partial"
    segment_id: str
    speaker_id: str
    role: Role
    words: list[PartialWord]

class PendingMessage(ContractModel):
    """Operator only: a lane did not check this segment (CONTEXT.md rule 7). Final: the lane was
    cancelled, so no late result follows. "timeout" = over the lane budget; "error" = lane failed
    (e.g. invalid LLM output after retry). Never sent to the audience."""
    type: Literal["pending"] = "pending"
    segment_id: str
    lane: Lane
    reason: Literal["timeout", "error"]

class LoadMessage(ContractModel):
    """Operator picks a clip; server runs warm-up and replies `ready`. Then `start` plays it."""
    type: Literal["load"] = "load"
    clip_id: str

class StartMessage(ContractModel):
    type: Literal["start"] = "start"
    clip_id: str

class KillMessage(ContractModel):
    type: Literal["kill"] = "kill"
    annotation_id: str

class Participant(ContractModel):
    """Someone in a live room. speaker_id is a slug ("alice"); name is shown on captions."""
    speaker_id: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    name: str = Field(min_length=1, max_length=60)
    role: Role = "speaker"

class LoadRoomMessage(ContractModel):
    """Operator sets up a live room (mic in the browser); server warms up and replies `ready`."""
    type: Literal["load_room"] = "load_room"
    participants: list[Participant] = Field(min_length=1, max_length=4)
    topic: str | None = Field(default=None, max_length=200)  # typed by the operator: what the debate is about

class AssignVoiceMessage(ContractModel):
    """Operator: Deepgram's voice N is this participant."""
    type: Literal["assign_voice"] = "assign_voice"
    voice: int = Field(ge=0, le=15)
    speaker_id: str

class SpeakerOverrideMessage(ContractModel):
    """Operator keys 1 / 2 / M: everything heard from now on is this participant. null = automatic."""
    type: Literal["speaker_override"] = "speaker_override"
    speaker_id: str | None

class StopMessage(ContractModel):
    """Operator ends the session (a live room has no end of clip)."""
    type: Literal["stop"] = "stop"

class VoicesMessage(ContractModel):
    """Operator only: who each Deepgram voice is, and the manual override if one is set."""
    type: Literal["voices"] = "voices"
    participants: list[Participant]
    voices: dict[str, str]
    override: str | None
    topic: str | None = None

ServerMessage = Annotated[SegmentMessage | AnnotationMessage | AnnotationUpdateMessage | MetricsMessage | ReadyMessage | StartedMessage | EndedMessage | RemovalMessage | BannerMessage | PendingMessage | PartialMessage | VoicesMessage, Field(discriminator="type")]
ClientMessage = Annotated[LoadMessage | StartMessage | KillMessage | LoadRoomMessage | AssignVoiceMessage | SpeakerOverrideMessage | StopMessage, Field(discriminator="type")]

def segment_id(session_id: str, seq: int) -> str:
    return f"{session_id}:{seq}"

def annotation_id(segment_id: str, lane: Lane, n: int) -> str:
    return f"{segment_id}:{lane}:{n}"
