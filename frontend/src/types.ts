/** Wire types mirrored from backend/app/models.py. Field names are checked in tests/contracts. */

export type Role = "speaker" | "moderator";
export type Category = "F" | "M" | "D" | "L" | "C" | "U" | "V";
export type Lane = "claims" | "fallacy" | "consistency";
export type AnnotationStatus = "candidate" | "verified" | "withdrawn" | "killed";
export type EvidenceReliability = "primary" | "factchecker" | "media";
export type AnnotationPath = "bank" | "retrieval" | "llm" | "web";
export type RemovalStatus = "withdrawn" | "killed";

export interface Segment {
  segment_id: string;
  session_id: string;
  speaker_id: string;
  role: Role;
  text: string;
  t_start: number;
  t_end: number;
  stt_confidence: number;
  low_conf_words: string[];
}

export interface Evidence {
  source_id: string;
  title: string;
  url: string;
  publisher: string;
  reliability: EvidenceReliability;
  quote: string;
  published: string | null;
  retrieved_at: string;
}

export interface Annotation {
  annotation_id: string;
  version: number;
  segment_id: string;
  speaker_id: string;
  span_text: string;
  span_char_start: number;
  span_char_end: number;
  category: Category;
  subtype: string | null;
  explanation: string;
  evidence: Evidence[];
  opponent_span: string | null;
  confidence: number;
  status: AnnotationStatus;
  path: AnnotationPath;
  lane: Lane;
  latency_ms: number;
  model_id: string | null;
  prompt_hash: string | null;
  repeat_count: number;
}

export interface MetricsSnapshot {
  p50_ms: number;
  p95_ms: number;
  bank_hit_rate: number;
  cache_tokens: number;
  loop_lag_ms: number;
}

export interface SegmentMessage {
  type: "segment";
  data: Segment;
}

export interface AnnotationMessage {
  type: "annotation";
  data: Annotation;
}

export interface AnnotationUpdateMessage {
  type: "annotation_update";
  data: Annotation;
}

export interface MetricsMessage {
  type: "metrics";
  data: MetricsSnapshot;
}

export interface ReadyMessage {
  type: "ready";
  session_id: string;
}

export interface StartedMessage {
  type: "started";
  t0_wall: number;
}

export interface RemovalMessage {
  type: "removal";
  annotation_id: string;
  status: RemovalStatus;
}

export interface BannerMessage {
  type: "banner";
  level: "info" | "warn" | "error";
  text: string;
}

/** Operator only: a lane did not check this segment. Final (no late result follows). */
export interface PendingMessage {
  type: "pending";
  segment_id: string;
  lane: Lane;
  reason: "timeout" | "error";
}

export type ServerMessage =
  | SegmentMessage
  | AnnotationMessage
  | AnnotationUpdateMessage
  | MetricsMessage
  | ReadyMessage
  | StartedMessage
  | RemovalMessage
  | BannerMessage
  | PendingMessage;

export interface LoadMessage {
  type: "load";
  clip_id: string;
}

export interface StartMessage {
  type: "start";
  clip_id: string;
}

export interface KillMessage {
  type: "kill";
  annotation_id: string;
}

export type ClientMessage = LoadMessage | StartMessage | KillMessage;
