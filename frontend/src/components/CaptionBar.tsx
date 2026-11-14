import { speakerName } from "../categories";
import type { Annotation, RemovalStatus, Segment } from "../types";
import { AnnotatedText } from "./AnnotatedText";

export const CAPTION_LINES = 2;

interface Props {
  segments: Segment[];
  bySegment: Record<string, Annotation[]>;
  removed: Record<string, RemovalStatus>;
}

/** Last two segments under the video. Speaker name always visible; moderator muted, never flagged. */
export function CaptionBar({ segments, bySegment, removed }: Props) {
  const shown = segments.slice(-CAPTION_LINES);
  return (
    <div className="flex min-h-[7.5rem] flex-col justify-end gap-2 rounded-xl bg-black/85 px-5 py-4 text-xl leading-relaxed sm:text-2xl" aria-live="polite">
      {shown.length === 0 && <p className="text-zinc-500">Captions appear here when the debate starts.</p>}
      {shown.map((s) => {
        const mod = s.role === "moderator";
        return (
          <p key={s.segment_id} className={mod ? "text-zinc-400" : "text-white"}>
            <span className={`mr-2 font-semibold ${mod ? "text-zinc-500" : "text-amber-200"}`}>{speakerName(s.speaker_id)}:</span>
            {mod ? s.text : <AnnotatedText text={s.text} annotations={bySegment[s.segment_id] ?? []} removed={removed} />}
          </p>
        );
      })}
    </div>
  );
}
