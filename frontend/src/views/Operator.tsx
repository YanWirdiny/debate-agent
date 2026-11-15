import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { categoryLabel, fmtTime, isStruck, speakerName } from "../categories";
import { audienceDelay, useClipTime, useVideoSync } from "../clock";
import { fetchSource, toTrimmed, type ClipSource } from "../original";
import { AnnotatedText } from "../components/AnnotatedText";
import { Banners } from "../components/Banners";
import { FlagChip } from "../components/FlagChip";
import { LatencyMeter } from "../components/LatencyMeter";
import { listMics, MicStream } from "../mic";
import { annotationsBySegment, type PendingMark, useView } from "../store";
import type { Annotation, PartialMessage, Segment } from "../types";
import { type ClipInfo, fetchClips, fetchState, type ServerState, useViewSocket } from "../ws";
import { keyTarget, participantsFrom, RoomPanel, type RoomNames, RoomSetup } from "./Room";

const NAMES_KEY = "bs-room-names";

function loadNames(): RoomNames {
  try {
    const raw = localStorage.getItem(NAMES_KEY);
    if (raw) return { a: "", b: "", mod: "", topic: "", ...(JSON.parse(raw) as Partial<RoomNames>) };
  } catch {
    /* private mode: start empty */
  }
  return { a: "", b: "", mod: "", topic: "" };
}

type Phase = "idle" | "loading" | "ready" | "starting" | "live" | "ended";

/** Operator: load/start, full transcript with every flag (status, confidence, path, latency), kill,
 *  pending markers, latency + bank hit rate. `mode="room"`: two people live in front of a mic (no
 *  clip): names + mic instead of a clip picker, and a speaker panel instead of the video. */
export function Operator({ mode = "clip" }: { mode?: "clip" | "room" }) {
  const room = mode === "room";
  const [clips, setClips] = useState<ClipInfo[]>([]);
  const [clip, setClip] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [server, setServer] = useState<ServerState | null>(null);
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  const [videoOk, setVideoOk] = useState(true);
  const { segments, annotations, pending, metrics, sessionId, conn, t0Wall, partial, voices } = useView();
  // live room
  const [names, setNames] = useState<RoomNames>(loadNames);
  const [mics, setMics] = useState<MediaDeviceInfo[]>([]);
  const [micId, setMicId] = useState("");
  const [level, setLevel] = useState(0);
  const [micError, setMicError] = useState<string | null>(null);
  const mic = useRef<MicStream | null>(null);
  const startWhenReady = useRef(false);
  const stopMic = () => {
    mic.current?.stop();
    mic.current = null;
  };
  const setSession = useView((s) => s.setSession);
  const clipTime = useClipTime(500);
  const delay = useMemo(() => audienceDelay(), []);
  const delayed = useClipTime(500, delay); // audience clock (broadcast delay), trimmed-clip seconds
  const [source, setSource] = useState<ClipSource | null>(null);

  const sock = useViewSocket("operator", (m) => {
    if (m.type === "ready") {
      setPhase("ready");
      void fetchState().then(setServer);
      if (room && startWhenReady.current) {
        // A room starts as soon as it is warm: the mic is already open (it needed the click).
        startWhenReady.current = false;
        if (sock.send({ type: "start", clip_id: "room" })) setPhase("starting");
      }
    } else if (m.type === "started") {
      setPhase("live");
      void fetchState().then(setServer);
    } else if (m.type === "ended") {
      setPhase((p) => (p === "live" ? "ended" : p));
      if (room) stopMic();
    } else if (m.type === "banner" && m.level === "error") {
      setPhase((p) => (p === "loading" || p === "starting" ? "idle" : p));
      if (room && !useView.getState().t0Wall) {
        startWhenReady.current = false;
        stopMic();
      }
    }
  });

  useEffect(() => {
    void fetchClips().then((cs) => {
      setClips(cs);
      setClip((c) => c || cs.find((x) => x.clip_id !== "fixture")?.clip_id || cs[0]?.clip_id || "");
    });
    void fetchState().then((st) => {
      setServer(st);
      // A room page only adopts a room session, a clip page only a clip session.
      if (st?.clip_id && (st.source === "mic") === room) {
        setClip(st.clip_id);
        const ended = st.ended_clip_time ?? null;
        setSession({ sessionId: st.session_id, clipId: st.clip_id, t0Wall: st.t0_wall, speed: st.speed, endedAt: ended, voices: st.voices ?? null });
        setPhase(ended != null ? "ended" : st.t0_wall ? "live" : "ready");
      }
    });
  }, [setSession]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!server) return;
    const duration = clips.find((c) => c.clip_id === server.clip_id)?.duration ?? null;
    setSession({ clipId: server.clip_id, speed: server.speed, duration });
  }, [server, clips, setSession]);
  useEffect(() => setVideoOk(true), [server?.clip_id]);
  // Re-asked on every state refresh (load, start, reconnect): one failed fetch while the backend was
  // restarting must not leave the preview on the trimmed clip for the rest of the page's life.
  useEffect(() => {
    if (!server?.clip_id || room) return setSource(null); // a room has no clip video
    let live = true;
    void fetchSource(server.clip_id).then((s) => live && setSource(s));
    return () => {
      live = false;
    };
  }, [server]);
  useEffect(() => {
    if (conn === "open") void fetchState().then(setServer);
  }, [conn]);
  // The operator is the only view now, so the preview carries the sound. A browser that blocks
  // autoplay with sound gets it muted by useVideoSync; the button turns it back on (a user gesture).
  const [sound, setSound] = useState(true);
  useEffect(() => {
    if (!video) return;
    video.muted = !sound;
    const onVolume = () => setSound(!video.muted);
    video.addEventListener("volumechange", onVolume);
    return () => video.removeEventListener("volumechange", onVolume);
  }, [video, sound]);
  // The audience watches the original when the clip has one (Audience.tsx): mirror it exactly.
  const orig = source && videoOk ? source : null;
  // Where the audience is, in trimmed-clip seconds (what the transcript uses). Original mode: the
  // viewer is at window.start + delayed clock in the original; map that back. In a cut the agent's
  // next sentence is not on air yet, so the kill window is the whole cut.
  const onAir = delayed == null ? null : orig ? toTrimmed(orig.window.start + delayed, orig.edl) : delayed;
  const viewerAt = delayed == null ? null : orig ? orig.window.start + delayed : delayed;
  useVideoSync(video, videoOk, delay, orig?.window.start ?? 0, orig?.window.end ?? Infinity); // preview = what the audience sees

  const bySegment = useMemo(() => annotationsBySegment(annotations), [annotations]);
  const pendingCount = useMemo(() => Object.values(pending).reduce((n, p) => n + p.length, 0), [pending]);
  const flagCount = Object.keys(annotations).length;
  const shown = flaggedOnly ? segments.filter((s) => bySegment[s.segment_id] || pending[s.segment_id]) : segments;

  // --- live room ---
  useEffect(() => {
    if (!room) return;
    void listMics().then(setMics);
    return () => stopMic(); // leaving the page closes the mic
  }, [room]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    try {
      localStorage.setItem(NAMES_KEY, JSON.stringify(names));
    } catch {
      /* ignore */
    }
  }, [names]);
  const startRoom = async () => {
    setMicError(null);
    const m = new MicStream(
      (rms) => setLevel((prev) => (Math.abs(prev - rms) > 0.002 ? rms : prev)),
      (err) => setMicError(err),
    );
    try {
      await m.start(micId || undefined); // in the click: browsers only open audio after a gesture
    } catch (e) {
      setMicError(e instanceof Error && e.name === "NotAllowedError" ? "Microphone access was refused. Allow it in the address bar, then try again." : String(e instanceof Error ? e.message : e));
      return;
    }
    mic.current = m;
    void listMics().then(setMics); // labels appear once access is granted
    startWhenReady.current = true;
    if (sock.send({ type: "load_room", participants: participantsFrom(names), topic: names.topic.trim() || null })) setPhase("loading");
    else {
      startWhenReady.current = false;
      stopMic();
      setMicError("Not connected to the backend.");
    }
  };
  const stopRoom = () => {
    sock.send({ type: "stop" });
    stopMic();
  };
  const assignVoice = (voice: number, speakerId: string) => sock.send({ type: "assign_voice", voice, speaker_id: speakerId });
  const override = (speakerId: string | null) => sock.send({ type: "speaker_override", speaker_id: speakerId });
  useEffect(() => {
    if (!room || phase !== "live") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.target instanceof HTMLElement && e.target.closest("input, textarea, select")) return;
      const target = keyTarget(e.key, voices?.participants ?? []);
      if (target === undefined) return;
      e.preventDefault();
      override(target);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [room, phase, voices]); // eslint-disable-line react-hooks/exhaustive-deps
  const nameOf = (id: string) => voices?.participants.find((p) => p.speaker_id === id)?.name ?? speakerName(id);
  const speaking = partial?.speaker_id || segments[segments.length - 1]?.speaker_id || null;
  const busy = phase === "loading" || phase === "starting";

  const send = (type: "load" | "start") => {
    if (!clip) return;
    if (sock.send({ type, clip_id: clip })) setPhase(type === "load" ? "loading" : "starting");
  };
  const kill = (a: Annotation) => {
    if (window.confirm(`Remove this ${categoryLabel(a)} flag everywhere? Killed is final.`)) sock.send({ type: "kill", annotation_id: a.annotation_id });
  };

  return (
    <div className="flex min-h-[calc(100dvh-var(--chrome,3rem))] flex-col bg-zinc-950 text-zinc-100 lg:h-[calc(100dvh-var(--chrome,3rem))]">
      <header className="flex flex-wrap items-center gap-3 border-b border-zinc-800 px-4 py-3">
        <Link to="/" className="font-bold tracking-tight">
          {room ? "Room" : "Operator"}
        </Link>
        {room ? (
          <>
            <RoomSetup names={names} setNames={setNames} mics={mics} micId={micId} setMicId={setMicId} level={level}
              disabled={busy || phase === "live"} />
            {phase === "live" ? (
              <button type="button" onClick={stopRoom} className="btn border-red-700 text-red-300 hover:bg-red-950">
                Stop
              </button>
            ) : (
              <button type="button" onClick={() => void startRoom()} disabled={conn !== "open" || busy} className="btn btn-primary"
                title="Opens the microphone, warms up, and starts listening">
                {phase === "loading" ? "Warming up…" : phase === "starting" ? "Starting…" : "Start"}
              </button>
            )}
          </>
        ) : (
          <>
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
          </>
        )}
        <span className="text-sm text-zinc-400">
          {phase === "live" && <span className="mr-2 rounded bg-red-600 px-1.5 py-0.5 text-xs font-bold text-white">LIVE</span>}
          {phase === "ended" && (
            <span className="mr-2 rounded border border-zinc-600 px-1.5 py-0.5 text-xs font-bold text-zinc-400" title="Load the clip again to run it again">
              ENDED
            </span>
          )}
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
            <Link to={`/reports/${encodeURIComponent(sessionId)}`} className="text-zinc-300 underline hover:text-zinc-100" target="_blank">
              Report
            </Link>
          )}
          <span className={`h-2.5 w-2.5 rounded-full ${conn === "open" ? "bg-emerald-500" : "bg-amber-500"}`} title={`socket ${conn}`} />
        </span>
      </header>

      <div className="space-y-3 border-b border-zinc-800 px-4 py-3">
        {micError && (
          <div className="rounded-md border border-red-700 bg-red-950 px-3 py-2 text-sm text-red-100" role="alert">
            {micError}
          </div>
        )}
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
          <Transcript segments={shown} bySegment={bySegment} pending={pending} onKill={kill} live={phase === "live"} onAir={room ? null : onAir} partial={partial}
            nameOf={nameOf} idle={room ? "Type the two names, then Start. The microphone opens and captions begin when someone speaks." : "Load a clip, then start."} />
        </section>
        <aside className="border-t border-zinc-800 p-4 lg:w-96 lg:border-t-0 lg:border-l">
          {room ? (
            <>
              <RoomPanel voices={voices} speaking={speaking} live={phase === "live"} onAssign={assignVoice} onOverride={override} />
              <Legend />
            </>
          ) : (
            <>
          <div className="relative overflow-hidden rounded-lg bg-zinc-900">
            {server?.clip_id && videoOk ? (
              <>
              <span className="absolute top-2 left-2 z-10 rounded bg-black/70 px-1.5 py-0.5 text-xs text-zinc-300">
                {orig ? "original (as the audience sees it)" : "trimmed clip"}
              </span>
              <button type="button" onClick={() => setSound((on) => !on)}
                className="absolute top-2 right-2 z-10 rounded bg-black/70 px-2 py-0.5 text-xs text-zinc-200 hover:bg-black/90">
                {sound ? "Sound on" : "Sound off: click to unmute"}
              </button>
              <video ref={setVideo} key={`${server.clip_id}:${orig ? "original" : "video"}`} src={`/clips/${encodeURIComponent(server.clip_id)}/${orig ? "original" : "video"}`} playsInline className="aspect-video w-full" onError={() => setVideoOk(false)} />
              </>
            ) : (
              <p className="aspect-video p-6 text-sm text-zinc-500">{server?.clip_id ? "No video for this clip." : "Load a clip."}</p>
            )}
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            Preview = audience picture{orig ? " (untrimmed original" + (viewerAt != null && viewerAt >= 0 ? `, at ${fmtTime(viewerAt)}` : "") + ")" : ""}, {delay} s behind live{t0Wall ? "" : " once started"}. Flags on lines marked
            “not on air” can be killed before the audience sees them.
          </p>
          <Legend />
            </>
          )}
        </aside>
      </div>
    </div>
  );
}

function Transcript({ segments, bySegment, pending, onKill, live, onAir, partial, nameOf, idle }: {
  segments: Segment[];
  bySegment: Record<string, Annotation[]>;
  pending: Record<string, PendingMark[]>;
  onKill: (a: Annotation) => void;
  live: boolean;
  onAir: number | null;
  partial: PartialMessage | null;
  nameOf: (speakerId: string) => string;
  idle: string;
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
      {segments.length === 0 && <p className="py-10 text-center text-zinc-500">{live ? "Waiting for the first sentence…" : idle}</p>}
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
                  <span className={`mr-1.5 font-semibold ${mod ? "text-zinc-500" : "text-amber-200"}`}>{nameOf(s.speaker_id)}</span>
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
            <span className="mr-1.5 font-semibold not-italic">{nameOf(partial.speaker_id)}</span>
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
