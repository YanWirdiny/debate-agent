import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { categoryLabel, fmtTime, isStruck, speakerName } from "../categories";
import { audienceDelay, useClipTime, useVideoSync } from "../clock";
import { AnnotatedText } from "../components/AnnotatedText";
import { Banners } from "../components/Banners";
import { FlagChip } from "../components/FlagChip";
import { LatencyMeter } from "../components/LatencyMeter";
import { annotationsBySegment, type PendingMark, useView } from "../store";
import type { Annotation, PartialMessage, Segment } from "../types";
import { type ClipInfo, fetchClips, fetchState, type ServerState, useViewSocket } from "../ws";

type Phase = "idle" | "loading" | "ready" | "starting" | "live";

/** Operator: load/start, full transcript with every flag (status, confidence, path, latency), kill,
 *  pending markers, latency + bank hit rate. */
export function Operator() {
  const [clips, setClips] = useState<ClipInfo[]>([]);
  const [clip, setClip] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [server, setServer] = useState<ServerState | null>(null);
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  const [videoOk, setVideoOk] = useState(true);
  const { segments, annotations, pending, metrics, sessionId, conn, t0Wall, partial } = useView();
  const setSession = useView((s) => s.setSession);
  const clipTime = useClipTime(500);
  const delay = useMemo(() => audienceDelay(), []);
  const onAir = useClipTime(500, delay); // where the audience is (broadcast delay)

  const sock = useViewSocket("operator", (m) => {
    if (m.type === "ready") {
      setPhase("ready");
      void fetchState().then(setServer);
    } else if (m.type === "started") {
      setPhase("live");
      void fetchState().then(setServer);
    } else if (m.type === "banner" && m.level === "error") setPhase((p) => (p === "loading" || p === "starting" ? "idle" : p));
  });

  useEffect(() => {
    void fetchClips().then((cs) => {
      setClips(cs);
      setClip((c) => c || cs.find((x) => x.clip_id !== "fixture")?.clip_id || cs[0]?.clip_id || "");
    });
    void fetchState().then((st) => {
      setServer(st);
      if (st?.clip_id) {
        setClip(st.clip_id);
        setSession({ sessionId: st.session_id, clipId: st.clip_id, t0Wall: st.t0_wall, speed: st.speed });
        setPhase(st.t0_wall ? "live" : "ready");
      }
    });
  }, [setSession]);
  useEffect(() => {
    if (server) setSession({ clipId: server.clip_id, speed: server.speed });
  }, [server, setSession]);
  useEffect(() => setVideoOk(true), [server?.clip_id]);
  useVideoSync(video, videoOk, delay); // preview = what the audience sees

  const bySegment = useMemo(() => annotationsBySegment(annotations), [annotations]);
  const pendingCount = useMemo(() => Object.values(pending).reduce((n, p) => n + p.length, 0), [pending]);
  const flagCount = Object.keys(annotations).length;
  const shown = flaggedOnly ? segments.filter((s) => bySegment[s.segment_id] || pending[s.segment_id]) : segments;

  const send = (type: "load" | "start") => {
    if (!clip) return;
    if (sock.send({ type, clip_id: clip })) setPhase(type === "load" ? "loading" : "starting");
  };
  const kill = (a: Annotation) => {
    if (window.confirm(`Remove this ${categoryLabel(a)} flag everywhere? Killed is final.`)) sock.send({ type: "kill", annotation_id: a.annotation_id });
  };

  return (
    <div className="flex min-h-screen flex-col bg-zinc-950 text-zinc-100 lg:h-screen">
      <header className="flex flex-wrap items-center gap-3 border-b border-zinc-800 px-4 py-3">
        <Link to="/" className="font-bold tracking-tight">
          BS Subtitles <span className="font-normal text-zinc-500">operator</span>
        </Link>
        <select
          value={clip}
          onChange={(e) => setClip(e.target.value)}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-sm"
          disabled={phase === "loading" || phase === "starting"}
          aria-label="Clip"
        >
          {clips.map((c) => (
            <option key={c.clip_id} value={c.clip_id}>
              {c.title} ({c.clip_id})
            </option>
          ))}
        </select>
        <button type="button" onClick={() => send("load")} disabled={!clip || conn !== "open" || phase === "loading" || phase === "starting"} className="btn">
          {phase === "loading" ? "Warming up…" : "Load"}
        </button>
        <button
          type="button"
          onClick={() => send("start")}
          disabled={!clip || conn !== "open" || phase !== "ready" || server?.clip_id !== clip}
          className="btn btn-primary"
          title={phase !== "ready" ? "Load the clip first; start when warm-up is done" : "Start playback everywhere"}
        >
          {phase === "starting" ? "Starting…" : "Start"}
        </button>
        <span className="text-sm text-zinc-400">
          {phase === "live" && <span className="mr-2 rounded bg-red-600 px-1.5 py-0.5 text-xs font-bold text-white">LIVE</span>}
          {sessionId ?? "no session"} · <span className="tabular-nums">{fmtTime(clipTime)}</span>
          {server && (
            <>
              {" "}
              · {server.source} · INTEL={server.intel}
            </>
          )}
        </span>
        <span className="ml-auto flex items-center gap-3 text-sm">
          {sessionId && (
            <Link to={`/reports/${encodeURIComponent(sessionId)}`} className="text-zinc-300 underline hover:text-white" target="_blank">
              Report
            </Link>
          )}
          <a href="/audience" target="_blank" className="text-zinc-300 underline hover:text-white">
            Audience view
          </a>
          <span className={`h-2.5 w-2.5 rounded-full ${conn === "open" ? "bg-emerald-500" : "bg-amber-500"}`} title={`socket ${conn}`} />
        </span>
      </header>

      <div className="space-y-3 border-b border-zinc-800 px-4 py-3">
        <Banners />
        <LatencyMeter m={metrics} pendingCount={pendingCount} />
      </div>

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <section className="flex min-h-0 flex-1 flex-col">
          <div className="flex items-center gap-4 px-4 py-2 text-sm text-zinc-400">
            <span>
              {segments.length} sentences · {flagCount} flags
            </span>
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={flaggedOnly} onChange={(e) => setFlaggedOnly(e.target.checked)} /> flagged only
            </label>
          </div>
          <Transcript segments={shown} bySegment={bySegment} pending={pending} onKill={kill} live={phase === "live"} onAir={onAir} partial={partial} />
        </section>
        <aside className="border-t border-zinc-800 p-4 lg:w-96 lg:border-t-0 lg:border-l">
          <div className="overflow-hidden rounded-lg bg-black">
            {server?.clip_id && videoOk ? (
              <video ref={setVideo} key={server.clip_id} src={`/clips/${encodeURIComponent(server.clip_id)}/video`} muted playsInline className="aspect-video w-full" onError={() => setVideoOk(false)} />
            ) : (
              <p className="aspect-video p-6 text-sm text-zinc-500">{server?.clip_id ? "No video for this clip." : "Load a clip."}</p>
            )}
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            Preview = audience picture, {delay} s behind live{t0Wall ? "" : " once started"} (muted). Flags on lines marked
            “not on air” can be killed before the audience sees them.
          </p>
          <Legend />
        </aside>
      </div>
    </div>
  );
}

function Transcript({ segments, bySegment, pending, onKill, live, onAir, partial }: {
  segments: Segment[];
  bySegment: Record<string, Annotation[]>;
  pending: Record<string, PendingMark[]>;
  onKill: (a: Annotation) => void;
  live: boolean;
  onAir: number | null;
  partial: PartialMessage | null;
}) {
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useEffect(() => {
    if (stick.current) end.current?.scrollIntoView({ block: "end" });
  }, [segments.length, partial?.words.length]);
  return (
    <div
      ref={box}
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
      className="min-h-[50vh] flex-1 overflow-y-auto px-4 pb-6 lg:min-h-0"
    >
      {segments.length === 0 && <p className="py-10 text-center text-zinc-500">{live ? "Waiting for the first sentence…" : "Load a clip, then start."}</p>}
      <ol className="space-y-2">
        {segments.map((s) => {
          const anns = bySegment[s.segment_id] ?? [];
          const marks = pending[s.segment_id] ?? [];
          const mod = s.role === "moderator";
          return (
            <li key={s.segment_id} className={`rounded-lg px-3 py-2 ${anns.length ? "bg-zinc-900" : ""}`}>
              <div className="flex gap-3">
                <span className="w-12 shrink-0 pt-0.5 text-xs text-zinc-500 tabular-nums">
                  {fmtTime(s.t_start)}
                  {onAir != null && onAir < s.t_end && (
                    <span className="mt-1 block rounded bg-sky-950 px-1 text-[10px] leading-4 text-sky-300" title="The audience has not heard this line yet">
                      not on air
                    </span>
                  )}
                </span>
                <p className={`min-w-0 flex-1 leading-relaxed ${mod ? "text-zinc-500" : ""}`}>
                  <span className={`mr-1.5 font-semibold ${mod ? "text-zinc-500" : "text-amber-200"}`}>{speakerName(s.speaker_id)}</span>
                  {mod ? s.text : <AnnotatedText text={s.text} annotations={anns} chipSize="sm" />}
                  {s.low_conf_words.length > 0 && !mod && (
                    <span className="ml-2 text-[11px] text-zinc-500" title={`Low STT confidence: ${s.low_conf_words.join(", ")}`}>
                      STT {Math.round(s.stt_confidence * 100)}%
                    </span>
                  )}
                </p>
              </div>
              {marks.length > 0 && (
                <div className="mt-1 ml-15 flex flex-wrap gap-2">
                  {marks.map((p) => (
                    <span key={p.lane} className="rounded border border-zinc-600 px-1.5 py-0.5 text-[11px] text-zinc-400" title="This lane did not check the sentence; no late result will come">
                      {p.lane} not checked ({p.reason})
                    </span>
                  ))}
                </div>
              )}
              {anns.length > 0 && (
                <ul className="mt-2 ml-15 space-y-1.5">
                  {anns.map((a) => (
                    <FlagRow key={a.annotation_id} a={a} onKill={onKill} />
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ol>
      {partial && !segments.some((s) => s.segment_id === partial.segment_id) && (
        <p className="mt-2 flex gap-3 px-3 text-zinc-400 italic" aria-live="off">
          <span className="w-12 shrink-0 pt-0.5 text-xs not-italic tabular-nums">{fmtTime(partial.words[0]?.t_start)}</span>
          <span>
            <span className="mr-1.5 font-semibold not-italic">{speakerName(partial.speaker_id)}</span>
            {partial.words.map((w) => w.text).join(" ")}
            <span className="ml-1 inline-block h-[0.9em] w-[2px] translate-y-[2px] animate-pulse bg-current" aria-hidden />
          </span>
        </p>
      )}
      <div ref={end} />
    </div>
  );
}

function FlagRow({ a, onKill }: { a: Annotation; onKill: (a: Annotation) => void }) {
  const struck = isStruck(a);
  const pub = a.status === "verified" && a.category !== "U";
  return (
    <li className={`flex flex-wrap items-center gap-x-3 gap-y-1 text-sm ${struck ? "opacity-50" : ""}`}>
      <FlagChip a={a} struck={struck} size="sm" />
      <span className="font-medium">{categoryLabel(a)}</span>
      <span className={`rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase ${statusStyle(a.status)}`}>{a.status}</span>
      <span className="text-xs text-zinc-400 tabular-nums" title="confidence · path · latency (end of sentence to flag) · version">
        {Math.round(a.confidence * 100)}% · {a.path} · {a.latency_ms} ms · v{a.version}
        {pub && !struck ? " · on audience if ≥ threshold" : ""}
      </span>
      <span className={`min-w-0 basis-full text-zinc-400 ${struck ? "line-through" : ""}`}>{a.explanation}</span>
      {a.status !== "killed" && (
        <button type="button" onClick={() => onKill(a)} className="ml-auto rounded border border-red-800 px-2 py-0.5 text-xs text-red-300 hover:bg-red-950">
          Kill
        </button>
      )}
    </li>
  );
}

function statusStyle(s: Annotation["status"]): string {
  return {
    verified: "bg-emerald-900 text-emerald-200",
    candidate: "bg-zinc-800 text-zinc-300",
    withdrawn: "bg-zinc-800 text-zinc-500",
    killed: "bg-red-950 text-red-300",
  }[s];
}

function Legend() {
  return (
    <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-zinc-400">
      <dt>solid</dt>
      <dd>verified (audience sees it when confidence ≥ threshold)</dd>
      <dt>dashed</dt>
      <dd>candidate, operator only</dd>
      <dt>struck</dt>
      <dd>withdrawn or killed</dd>
      <dt>not checked</dt>
      <dd>lane timed out or failed; final</dd>
    </dl>
  );
}
