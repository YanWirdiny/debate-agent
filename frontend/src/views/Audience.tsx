import { useEffect, useMemo, useRef, useState } from "react";
import { useVideoSync } from "../clock";
import { CAPTION_LINES, CaptionBar } from "../components/CaptionBar";
import { SideRail } from "../components/SideRail";
import { annotationsBySegment, useView } from "../store";
import { fetchState, useViewSocket } from "../ws";
import type { Segment } from "../types";

/** Audience: video full width, two caption lines with flagged spans, side rail for late verdicts.
 *  Receives only public flags (the server applies the verification gate). */
export function Audience() {
  const setSession = useView((s) => s.setSession);
  const clipId = useView((s) => s.clipId);
  const segments = useView((s) => s.segments);
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
  useVideoSync(video, joined && videoOk);

  const bySegment = useMemo(() => annotationsBySegment(annotations), [annotations]);
  const segIndex = useMemo(() => Object.fromEntries(segments.map((s) => [s.segment_id, s])) as Record<string, Segment>, [segments]);
  const railFlags = useMemo(() => {
    const inBar = new Set(segments.slice(-CAPTION_LINES).map((s) => s.segment_id));
    return Object.values(annotations)
      .filter((a) => !inBar.has(a.segment_id))
      .sort((a, b) => (segIndex[b.segment_id]?.t_start ?? 0) - (segIndex[a.segment_id]?.t_start ?? 0));
  }, [annotations, segIndex, segments]);

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
        <CaptionBar segments={segments} bySegment={bySegment} removed={removed} />
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
