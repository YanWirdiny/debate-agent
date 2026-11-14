import { useEffect, useMemo, useRef, useState } from "react";
import { audienceDelay, useDisplayTime, useVideoSync } from "../clock";
import { buildLines, CAPTION_LINES, CaptionBar, reachedLines, revealedChars } from "../components/CaptionBar";
import { SideRail } from "../components/SideRail";
import { annotationsBySegment, useView } from "../store";
import { fetchState, useViewSocket } from "../ws";
import type { Segment } from "../types";

/** Audience: video full width, two caption lines revealed at the speaker's pace with flagged spans,
 *  side rail for older flags. Receives only public flags (the server applies the verification gate).
 *  Plays `audienceDelay()` seconds behind the live pipeline so captions and flags are never ahead of
 *  the picture and analysis latency is hidden (broadcast delay). */
export function Audience() {
  const setSession = useView((s) => s.setSession);
  const clipId = useView((s) => s.clipId);
  const segments = useView((s) => s.segments);
  const partial = useView((s) => s.partial);
  const wordTimes = useView((s) => s.wordTimes);
  const annotations = useView((s) => s.annotations);
  const removed = useView((s) => s.removed);
  const conn = useView((s) => s.conn);
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  const [videoOk, setVideoOk] = useState(true);
  const [joined, setJoined] = useState(false);
  const refresh = useRef(async () => {
    const st = await fetchState();
    if (st) setSession({ clipId: st.clip_id, sessionId: st.session_id, t0Wall: st.t0_wall, speed: st.speed });
  });

  useViewSocket("audience", (m) => {
    if (m.type === "started" || m.type === "ready") void refresh.current(); // learn the clip id
  });
  useEffect(() => {
    void refresh.current();
  }, []);
  useEffect(() => setVideoOk(true), [clipId]);
  const delay = useMemo(() => audienceDelay(), []);
  useVideoSync(video, joined && videoOk, delay);
  const displayTime = useDisplayTime(joined && videoOk ? video : null, delay);

  const bySegment = useMemo(() => annotationsBySegment(annotations), [annotations]);
  const segIndex = useMemo(() => Object.fromEntries(segments.map((s) => [s.segment_id, s])) as Record<string, Segment>, [segments]);
  // Only what the viewer has already heard: segments reached by the video, flags whose span was spoken.
  const lines = useMemo(() => buildLines(segments, partial, wordTimes), [segments, partial, wordTimes]);
  const reached = reachedLines(lines, displayTime);
  const reachedKey = reached.length ? reached[reached.length - 1].segment_id : "";
  const railFlags = useMemo(() => {
    const inBar = new Set(reached.slice(-CAPTION_LINES).map((l) => l.segment_id));
    const finalLine = Object.fromEntries(lines.filter((l) => l.final).map((l) => [l.segment_id, l]));
    return Object.values(annotations)
      .filter((a) => {
        const l = finalLine[a.segment_id];
        return l && !inBar.has(a.segment_id) && displayTime != null && revealedChars(l, displayTime) >= a.span_char_end;
      })
      .sort((a, b) => (segIndex[b.segment_id]?.t_start ?? 0) - (segIndex[a.segment_id]?.t_start ?? 0));
    // displayTime only matters for the bar boundary, captured by reachedKey
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [annotations, segIndex, reachedKey]);

  return (
    <div className="flex min-h-screen flex-col gap-4 bg-black p-4 text-white lg:h-screen lg:flex-row">
      <main className="flex min-w-0 flex-1 flex-col gap-4">
        <div className="relative flex flex-1 items-center justify-center overflow-hidden rounded-xl bg-zinc-950">
          {clipId && videoOk ? (
            <video
              ref={setVideo}
              key={clipId}
              src={`/clips/${encodeURIComponent(clipId)}/video`}
              className="h-full max-h-[70vh] w-full object-contain lg:max-h-none"
              playsInline
              muted={!joined}
              onError={() => setVideoOk(false)}
            />
          ) : (
            <p className="p-8 text-center text-zinc-500">{clipId ? "Video unavailable for this clip; captions continue below." : "Waiting for the operator to load a clip."}</p>
          )}
          {!joined && (
            <button
              type="button"
              onClick={() => setJoined(true)}
              className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/70 text-2xl font-semibold"
            >
              Click to join with sound
              <span className="text-sm font-normal text-zinc-400">Playback follows the operator.</span>
            </button>
          )}
          {conn !== "open" && <span className="absolute top-3 right-3 rounded bg-amber-600 px-2 py-1 text-xs font-semibold">Reconnecting…</span>}
        </div>
        <CaptionBar lines={lines} bySegment={bySegment} removed={removed} displayTime={displayTime} />
        <Legend />
      </main>
      <div className="h-72 shrink-0 lg:h-auto lg:w-80">
        <SideRail flags={railFlags} segments={segIndex} removed={removed} />
      </div>
    </div>
  );
}

function Legend() {
  return (
    <p className="text-center text-xs text-zinc-500">
      Flags appear only when backed by a cited source (or, for fallacies, a quoted span). Tap a letter for the evidence. F factual error ·
      M misleading · D data misuse · L logical fallacy · C differs from prior statement · V verified
    </p>
  );
}
