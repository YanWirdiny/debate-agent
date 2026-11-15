import { useEffect, useMemo, useRef, useState } from "react";
import { audienceDelay, clipTimeNow, useDisplayTime, useVideoSync } from "../clock";
import { fetchSource, inCut, originalLines, toOrig, type ClipSource } from "../original";
import { buildLines, CAPTION_LINES, CaptionBar, reachedLines, revealedChars } from "../components/CaptionBar";
import { SideRail } from "../components/SideRail";
import { annotationsBySegment, useView } from "../store";
import { fetchState, useViewSocket } from "../ws";
import type { Segment } from "../types";

/** Audience: video full width, two caption lines revealed at the speaker's pace with flagged spans,
 *  side rail for older flags. Receives only public flags (the server applies the verification gate).
 *  Plays `audienceDelay()` seconds behind the live pipeline so captions and flags are never ahead of
 *  the picture and analysis latency is hidden (broadcast delay).
 *
 *  Original mode (default when the clip has a source.json; `?trimmed` to opt out): the agent analyses
 *  the trimmed clip while the viewer watches the untrimmed original from the window start. Everything
 *  runs on original-video time; the cuts get captions only ("not fact-checked"), and the agent is
 *  ahead of the viewer by the length of the cuts. */
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
  const [source, setSource] = useState<ClipSource | null>(null);
  const wantOriginalRef = useRef(!new URLSearchParams(location.search).has("trimmed"));
  const [lost, setLost] = useState(false); // backend gone: stop playback instead of running on the local clock
  const wantOriginal = useMemo(() => !new URLSearchParams(location.search).has("trimmed"), []);
  const refresh = useRef(async () => {
    const st = await fetchState();
    if (st) {
      // If the session changed (e.g. operator loaded a new clip while we were connected or briefly
      // disconnected and missed the `ready` broadcast), reset before writing the new session into
      // the store so stale segments from the previous clip are not mixed with the new ones.
      const view = useView.getState();
      if (view.sessionId !== null && st.session_id !== null && view.sessionId !== st.session_id) {
        view.reset();
      }
      setSession({
        clipId: st.clip_id,
        sessionId: st.session_id,
        t0Wall: st.t0_wall,
        speed: st.speed,
        endedAt: st.ended_clip_time ?? null,
      });
    }
    if (st?.clip_id && wantOriginalRef.current) setSource(await fetchSource(st.clip_id)); // retried on every load/start
  });

  // Clear any state left by a previous view (e.g. navigating from /operator in the same SPA tab).
  useEffect(() => {
    useView.getState().reset();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // One audible audience tab per browser: joining here mutes every other audience tab (old tabs
  // left open otherwise play their own copy of the debate on top of this one).
  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const ch = new BroadcastChannel("bs-audience-audio");
    const me = Math.random().toString(36).slice(2);
    ch.onmessage = (e: MessageEvent<{ joined: string }>) => {
      if (e.data?.joined && e.data.joined !== me) setJoined(false);
    };
    if (joined) ch.postMessage({ joined: me });
    return () => ch.close();
  }, [joined]);

  useViewSocket("audience", (m) => {
    if (m.type === "started" || m.type === "ready") void refresh.current(); // learn the clip id
  });
  useEffect(() => {
    if (conn === "open") void refresh.current(); // first connect and every reconnect
  }, [conn]);
  useEffect(() => setVideoOk(true), [clipId]);
  useEffect(() => {
    setSource(null);
    if (clipId && wantOriginal) void fetchSource(clipId).then(setSource);
  }, [clipId, wantOriginal]);
  const orig = source && videoOk ? source : null;
  useEffect(() => {
    if (conn === "open") {
      setLost(false);
      return;
    }
    const id = window.setTimeout(() => setLost(true), 3000);
    return () => window.clearTimeout(id);
  }, [conn]);
  useEffect(() => {
    if (lost) video?.pause();
  }, [lost, video]);
  const delay = useMemo(() => audienceDelay(), []);
  useVideoSync(video, joined && videoOk && !lost, delay, orig?.window.start ?? 0, orig?.window.end ?? Infinity);
  const displayTime = useDisplayTime(joined && videoOk ? video : null, delay, 100, orig?.window.start ?? 0);

  const bySegment = useMemo(() => annotationsBySegment(annotations), [annotations]);
  const segIndex = useMemo(() => Object.fromEntries(segments.map((s) => [s.segment_id, s])) as Record<string, Segment>, [segments]);
  // Only what the viewer has already heard: segments reached by the video, flags whose span was spoken.
  const lines = useMemo(() => {
    const trimmed = buildLines(segments, partial, wordTimes);
    return orig ? originalLines(trimmed, orig) : trimmed;
  }, [segments, partial, wordTimes, orig]);
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
        <div className="relative flex flex-1 items-center justify-center overflow-hidden rounded-xl bg-black">
          {clipId && videoOk ? (
            <video
              ref={setVideo}
              key={clipId}
              src={`/clips/${encodeURIComponent(clipId)}/${orig ? "original" : "video"}`}
              className="h-full max-h-[70vh] w-full object-contain lg:max-h-none"
              playsInline
              muted={!joined}
              onError={() => setVideoOk(false)}
            />
          ) : (
            <p className="p-8 text-center text-white/60">{clipId ? "Video unavailable for this clip; captions continue below." : "Waiting for the operator to load a clip."}</p>
          )}
          {!joined && (
            <button
              type="button"
              onClick={() => setJoined(true)}
              className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/70 text-2xl font-semibold"
            >
              Click to join with sound
              <span className="text-sm font-normal text-white/60">Playback follows the operator.</span>
            </button>
          )}
          {conn !== "open" && (
            <span className="absolute top-3 right-3 rounded bg-amber-600 px-2 py-1 text-xs font-semibold">
              {lost ? "Connection lost · playback paused" : "Reconnecting…"}
            </span>
          )}
          {orig && joined && <AgentLead source={orig} displayTime={displayTime} />}
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

/** "Agent is N s ahead": how far the analysis (live on the trimmed clip) is ahead of the picture, and
 *  whether the viewer is in a cut that was never analysed. Shown in original mode only. */
function AgentLead({ source, displayTime }: { source: ClipSource; displayTime: number | null }) {
  const t0 = useView((s) => s.t0Wall);
  const speed = useView((s) => s.speed);
  if (displayTime == null) return null;
  const agent = clipTimeNow(t0, speed);
  const trimmedEnd = source.edl.length ? source.edl[source.edl.length - 1].trim_end : 0;
  const agentDone = agent != null && agent >= trimmedEnd;
  const lead = agent == null ? 0 : toOrig(Math.min(agent, trimmedEnd), source.edl) - displayTime;
  const cut = inCut(displayTime, source);
  return (
    <div className="absolute top-3 left-3 flex flex-col items-start gap-1 text-xs">
      <span className="rounded bg-black/70 px-2 py-1 font-mono text-green-300">
        {agentDone ? "Agent finished the excerpt" : lead > 0.5 ? `Agent is ${Math.round(lead)} s ahead` : "Agent is live"}
      </span>
      {cut && <span className="rounded bg-black/70 px-2 py-1 text-white/75">Outside the analysed excerpt · captions only</span>}
    </div>
  );
}

function Legend() {
  return (
    <p className="text-center text-xs text-white/50">
      Flags appear only when backed by a cited source (or, for fallacies, a quoted span). Tap a letter for the evidence. F factual error ·
      M misleading · D data misuse · L logical fallacy · C differs from prior statement · V verified
    </p>
  );
}
