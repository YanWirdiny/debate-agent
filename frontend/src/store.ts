import { create } from "zustand";
import type {
  Annotation,
  BannerMessage,
  Lane,
  MetricsSnapshot,
  PartialMessage,
  PartialWord,
  PendingMessage,
  RemovalStatus,
  Segment,
  ServerMessage,
} from "./types";

export type ConnState = "connecting" | "open" | "closed";

export interface Banner extends Omit<BannerMessage, "type"> {
  id: number;
  at: number;
}

export interface PendingMark {
  lane: Lane;
  reason: PendingMessage["reason"];
}

export interface ViewState {
  conn: ConnState;
  sessionId: string | null;
  clipId: string | null;
  t0Wall: number | null;
  speed: number;
  segments: Segment[];
  /** The sentence being spoken right now (not final yet). */
  partial: PartialMessage | null;
  /** segment_id -> real word timings (from partials), to reveal final lines at speaking pace. */
  wordTimes: Record<string, PartialWord[]>;
  annotations: Record<string, Annotation>;
  /** annotation_id -> removal status (audience: the flag stopped being public). */
  removed: Record<string, RemovalStatus>;
  /** segment_id -> lanes that did not check it (operator only). */
  pending: Record<string, PendingMark[]>;
  metrics: MetricsSnapshot | null;
  banners: Banner[];
  setConn: (c: ConnState) => void;
  setSession: (s: { sessionId?: string | null; clipId?: string | null; t0Wall?: number | null; speed?: number }) => void;
  apply: (m: ServerMessage) => void;
  dismissBanner: (id: number) => void;
  reset: () => void;
}

const empty = {
  segments: [] as Segment[],
  partial: null as PartialMessage | null,
  wordTimes: {} as Record<string, PartialWord[]>,
  annotations: {} as Record<string, Annotation>,
  removed: {} as Record<string, RemovalStatus>,
  pending: {} as Record<string, PendingMark[]>,
  metrics: null as MetricsSnapshot | null,
};

let bannerSeq = 0;
const MAX_BANNERS = 4;

export const useView = create<ViewState>((set) => ({
  conn: "connecting",
  sessionId: null,
  clipId: null,
  t0Wall: null,
  speed: 1,
  banners: [],
  ...empty,
  setConn: (conn) => set({ conn }),
  setSession: (s) => set((st) => ({ ...st, ...Object.fromEntries(Object.entries(s).filter(([, v]) => v !== undefined)) })),
  dismissBanner: (id) => set((st) => ({ banners: st.banners.filter((b) => b.id !== id) })),
  reset: () => set({ ...empty, t0Wall: null }),
  apply: (m) =>
    set((st) => {
      switch (m.type) {
        case "ready":
          // A new session was loaded: clear the previous run.
          return { ...empty, sessionId: m.session_id, t0Wall: null, banners: [] };
        case "started":
          return { t0Wall: m.t0_wall };
        case "segment": {
          if (st.segments.some((s) => s.segment_id === m.data.segment_id)) return {};
          if (st.sessionId && m.data.session_id !== st.sessionId) {
            // First message of a session this view did not see `ready` for (joined late / reload).
            return { ...empty, sessionId: m.data.session_id, segments: [m.data] };
          }
          const partial = st.partial?.segment_id === m.data.segment_id ? null : st.partial;
          return { sessionId: st.sessionId ?? m.data.session_id, segments: [...st.segments, m.data], partial };
        }
        case "partial": {
          if (st.segments.some((s) => s.segment_id === m.segment_id)) return {}; // already final
          return { partial: m, wordTimes: { ...st.wordTimes, [m.segment_id]: m.words } };
        }
        case "annotation":
        case "annotation_update": {
          const prev = st.annotations[m.data.annotation_id];
          if (prev && prev.version > m.data.version) return {}; // out of order: keep the newest
          const removed = { ...st.removed };
          delete removed[m.data.annotation_id]; // public again (audience)
          return { annotations: { ...st.annotations, [m.data.annotation_id]: m.data }, removed };
        }
        case "removal":
          return { removed: { ...st.removed, [m.annotation_id]: m.status } };
        case "pending": {
          const marks = st.pending[m.segment_id] ?? [];
          if (marks.some((p) => p.lane === m.lane)) return {};
          return { pending: { ...st.pending, [m.segment_id]: [...marks, { lane: m.lane, reason: m.reason }] } };
        }
        case "metrics":
          return { metrics: m.data };
        case "banner": {
          const b: Banner = { id: ++bannerSeq, level: m.level, text: m.text, at: Date.now() };
          return { banners: [...st.banners.filter((x) => x.text !== m.text), b].slice(-MAX_BANNERS) };
        }
        default:
          return {};
      }
    }),
}));

/** Annotations per segment, oldest first (stable order for rendering). */
export function annotationsBySegment(annotations: Record<string, Annotation>): Record<string, Annotation[]> {
  const out: Record<string, Annotation[]> = {};
  for (const a of Object.values(annotations)) (out[a.segment_id] ??= []).push(a);
  for (const list of Object.values(out)) list.sort((a, b) => a.span_char_start - b.span_char_start || a.annotation_id.localeCompare(b.annotation_id));
  return out;
}
