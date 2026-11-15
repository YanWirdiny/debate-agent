import { speakerName } from "../categories";
import type { Annotation, PartialMessage, PartialWord, RemovalStatus, Role, Segment } from "../types";
import { AnnotatedText } from "./AnnotatedText";

export const CAPTION_LINES = 2;

/** One caption line: a final sentence, or the sentence still being spoken (partial). */
export interface Line {
  segment_id: string;
  speaker_id: string;
  role: Role;
  text: string;
  t_start: number;
  t_end: number;
  words?: PartialWord[];
  final: boolean;
  /** Heard by the viewer but never analysed (original mode: a cut of the trimmed clip). */
  unchecked?: boolean;
}

/** Final sentences plus the live partial (if it is not final yet), in speaking order. */
export function buildLines(segments: Segment[], partial: PartialMessage | null, wordTimes: Record<string, PartialWord[]>): Line[] {
  const lines: Line[] = segments.map((s) => ({
    segment_id: s.segment_id, speaker_id: s.speaker_id, role: s.role, text: s.text,
    t_start: s.t_start, t_end: s.t_end, words: wordTimes[s.segment_id], final: true,
  }));
  if (partial && partial.words.length && !segments.some((s) => s.segment_id === partial.segment_id)) {
    lines.push({
      segment_id: partial.segment_id, speaker_id: partial.speaker_id, role: partial.role,
      text: partial.words.map((w) => w.text).join(" "), t_start: partial.words[0].t_start,
      t_end: partial.words[partial.words.length - 1].t_end, words: partial.words, final: false,
    });
  }
  return lines.sort((a, b) => a.t_start - b.t_start);
}

/**
 * Characters of a line spoken by time t, cut on a word boundary. With real word timings (from
 * partials) each word appears when it is said; otherwise words are spread over [t_start, t_end] by
 * character position (close to speaking rate).
 */
export function revealedChars(l: Pick<Line, "text" | "t_start" | "t_end" | "words" | "final">, t: number): number {
  if (t < l.t_start) return 0;
  if (l.final && t >= l.t_end) return l.text.length;
  if (l.words && l.words.map((w) => w.text).join(" ") === l.text) {
    let cut = 0;
    for (const w of l.words) {
      if (w.t_start > t) break;
      cut += (cut ? 1 : 0) + w.text.length;
    }
    return cut;
  }
  if (t >= l.t_end) return l.text.length;
  const target = ((t - l.t_start) / Math.max(0.05, l.t_end - l.t_start)) * l.text.length;
  let cut = 0;
  for (const m of l.text.matchAll(/\S+/g)) {
    if ((m.index ?? 0) > target) break;
    cut = (m.index ?? 0) + m[0].length;
  }
  return cut;
}

/** Lines the viewer has reached (not still ahead of the picture). */
export function reachedLines(lines: Line[], t: number | null): Line[] {
  if (t == null) return [];
  return lines.filter((l) => l.t_start <= t && revealedChars(l, t) > 0);
}

interface Props {
  lines: Line[];
  bySegment: Record<string, Annotation[]>;
  removed: Record<string, RemovalStatus>;
  /** What the viewer is watching (video time). null = not started: show nothing. */
  displayTime: number | null;
}

/**
 * Last two lines under the video, revealed word by word as they are spoken. A flag shows once its
 * whole span has been spoken. Speaker name always visible; moderator muted, never flagged.
 */
export function CaptionBar({ lines, bySegment, removed, displayTime }: Props) {
  const shown = reachedLines(lines, displayTime).slice(-CAPTION_LINES);
  return (
    <div className="flex min-h-[7.5rem] flex-col justify-end gap-2 rounded-xl bg-black/85 px-5 py-4 text-xl leading-relaxed sm:text-2xl" aria-live="off">
      {shown.length === 0 && <p className="text-zinc-600">{displayTime == null ? "Captions appear here when the debate starts." : " "}</p>}
      {shown.map((l, i) => {
        const mod = l.role === "moderator";
        const cut = displayTime == null ? l.text.length : revealedChars(l, displayTime);
        const text = l.text.slice(0, cut);
        const speaking = i === shown.length - 1 && (cut < l.text.length || !l.final);
        const anns = l.final ? (bySegment[l.segment_id] ?? []).filter((a) => a.span_char_end <= cut) : [];
        return (
          <p key={l.segment_id} className={mod ? "text-zinc-400" : "text-white"}>
            <span className={`mr-2 font-semibold ${mod ? "text-zinc-500" : "text-amber-200"}`}>{speakerName(l.speaker_id)}:</span>
            {l.unchecked && !mod && (
              <span className="mr-2 rounded border border-zinc-600 px-1.5 py-0.5 align-middle text-xs font-normal text-zinc-400" title="Outside the analysed excerpt: captions only, no fact-check">
                not fact-checked
              </span>
            )}
            {mod ? text : <AnnotatedText text={text} annotations={anns} removed={removed} />}
            {speaking && <span className="ml-1 inline-block h-[0.9em] w-[2px] translate-y-[2px] animate-pulse bg-current opacity-60" aria-hidden />}
          </p>
        );
      })}
    </div>
  );
}
