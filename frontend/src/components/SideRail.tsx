import { categoryLabel, fmtTime, isStruck, speakerName } from "../categories";
import type { Annotation, RemovalStatus, Segment } from "../types";
import { FlagChip } from "./FlagChip";

interface Props {
  flags: Annotation[];
  segments: Record<string, Segment>;
  removed: Record<string, RemovalStatus>;
}

/** Flags whose line already left the caption bar (late verdicts), newest first, with timestamp. */
export function SideRail({ flags, segments, removed }: Props) {
  return (
    <aside className="flex h-full flex-col overflow-hidden rounded-xl bg-zinc-900/80">
      <h2 className="border-b border-zinc-800 px-4 py-3 text-sm font-semibold tracking-wide text-zinc-400 uppercase">Flags</h2>
      <ol className="flex-1 space-y-3 overflow-y-auto p-4">
        {flags.length === 0 && <li className="text-sm text-zinc-500">No flags yet.</li>}
        {flags.map((a) => {
          const seg = segments[a.segment_id];
          const struck = isStruck(a, removed[a.annotation_id]);
          return (
            <li key={a.annotation_id} className={`text-sm ${struck ? "opacity-50" : ""}`}>
              <div className="mb-1 flex items-center gap-2 text-zinc-400">
                <FlagChip a={a} struck={struck} size="sm" align="right" />
                <span className="font-semibold text-zinc-200">{speakerName(a.speaker_id)}</span>
                <span className="tabular-nums">{fmtTime(seg?.t_start)}</span>
                <span className="ml-auto truncate text-xs">{categoryLabel(a)}</span>
              </div>
              <p className={`text-zinc-300 ${struck ? "line-through" : ""}`}>“{a.span_text}”</p>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
