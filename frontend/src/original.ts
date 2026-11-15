import type { Line } from "./components/CaptionBar";
import type { PartialWord, Role } from "./types";

/**
 * Original mode: the agent analyses the trimmed clip, the viewer watches the untrimmed original.
 * source.json (backend/scripts/align_clip.py) says where each kept piece sits in the original.
 * Every caption line is moved onto original-video time, so one clock (the original's position) drives
 * captions, flags and the side rail. The cuts get captions only, marked as not fact-checked.
 */
export interface EdlPiece {
  trim_start: number;
  trim_end: number;
  orig_start: number;
}

export interface CutLine {
  t_start: number;
  t_end: number;
  speaker_id: string;
  role: Role;
  text: string;
  words: PartialWord[];
}

export interface ClipSource {
  original: { path: string; url: string; duration: number };
  window: { start: number; end: number };
  edl: EdlPiece[];
  cut_captions?: CutLine[];
}

export async function fetchSource(clipId: string): Promise<ClipSource | null> {
  try {
    const r = await fetch(`/clips/${encodeURIComponent(clipId)}/source`);
    return r.ok ? ((await r.json()) as ClipSource) : null;
  } catch {
    return null;
  }
}

/** Trimmed-clip time -> original time (a time before/after every piece uses the nearest piece). */
export function toOrig(t: number, edl: EdlPiece[]): number {
  if (!edl.length) return t;
  const p = edl.find((x) => t < x.trim_end) ?? edl[edl.length - 1];
  return p.orig_start + (t - p.trim_start);
}

/** Original time -> trimmed time the viewer has reached. In a cut: the end of the piece before it
 *  (everything analysed so far has been seen); before the first piece: -1 (nothing seen yet). */
export function toTrimmed(t: number, edl: EdlPiece[]): number {
  let reached = -1;
  for (const p of edl) {
    const len = p.trim_end - p.trim_start;
    if (t < p.orig_start) break;
    if (t < p.orig_start + len) return p.trim_start + (t - p.orig_start);
    reached = p.trim_end;
  }
  return reached;
}

/** Is original time `t` inside the viewer's window but in a cut (not analysed)? */
export function inCut(t: number, src: ClipSource): boolean {
  if (t < src.window.start || t > src.window.end) return false;
  return !src.edl.some((p) => t >= p.orig_start && t < p.orig_start + (p.trim_end - p.trim_start));
}

/** Offset (original - trimmed) of the piece holding the line's midpoint. A sentence never spans a
 *  cut, but its start can sit a few ms before the boundary (rounding), so the start alone misleads. */
function lineOffset(l: Pick<Line, "t_start" | "t_end">, edl: EdlPiece[]): number {
  const mid = (l.t_start + l.t_end) / 2;
  return toOrig(mid, edl) - mid;
}

/** Analysed lines on original time, plus the cut captions, in viewing order. */
export function originalLines(lines: Line[], src: ClipSource): Line[] {
  const analysed = lines.map((l) => {
    const d = lineOffset(l, src.edl);
    return {
      ...l,
      t_start: l.t_start + d,
      t_end: l.t_end + d,
      words: l.words?.map((w) => ({ ...w, t_start: w.t_start + d, t_end: w.t_end + d })),
    };
  });
  const cuts: Line[] = (src.cut_captions ?? []).map((c, i) => ({
    segment_id: `cut:${i}`, speaker_id: c.speaker_id, role: c.role, text: c.text,
    t_start: c.t_start, t_end: c.t_end, words: c.words, final: true, unchecked: true,
  }));
  return [...analysed, ...cuts].sort((a, b) => a.t_start - b.t_start);
}
