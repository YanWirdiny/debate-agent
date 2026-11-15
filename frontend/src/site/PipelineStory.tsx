/**
 * "How it works": the live pipeline (ARCHITECTURE.md sections 1-3), drawn as one SVG and played by a
 * GSAP timeline scrubbed by scroll (ScrollTrigger, section pinned) with packets travelling the real
 * edges (MotionPathPlugin). Seven steps; ← / → jump between them while the section is on screen.
 *
 * Node shapes say what each box is: pill = model call (badge names the provider), square = code,
 * cylinder = data store, the tall red-edged box = the Gate (code decides what is shown, never the model).
 */
import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { MotionPathPlugin } from "gsap/MotionPathPlugin";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { useEffect, useRef, useState } from "react";

gsap.registerPlugin(useGSAP, ScrollTrigger, MotionPathPlugin);

const INK = "#02091E";
const YALE = "#0D3C64";
const RED = "#B20716";
const ALMOND = "#E8DDD2";
const CREAM = "#F1E5D5";

type Kind = "input" | "model" | "code" | "gate" | "store" | "output";
interface N {
  x: number;
  y: number;
  w?: number;
  h?: number;
  kind: Kind;
  label: string;
  sub?: string;
  badge?: string;
}

const NODES = {
  audio: { x: 80, y: 205, kind: "input", label: "Clip audio", sub: "streamed at 1×" },
  stt: { x: 235, y: 205, kind: "model", label: "Deepgram", sub: "live speech-to-text", badge: "Deepgram" },
  seg: { x: 395, y: 205, kind: "code", label: "Segmenter", sub: "sentence + speaker" },
  extract: { x: 560, y: 135, kind: "model", label: "Extract claims", badge: "Groq" },
  bank: { x: 735, y: 48, kind: "code", label: "Bank match", sub: "local embeddings" },
  stance: { x: 735, y: 145, kind: "model", label: "Retrieve + stance", badge: "Groq" },
  consist: { x: 735, y: 242, kind: "model", label: "Consistency", sub: "vs. past statements", badge: "Groq" },
  fallacy: { x: 560, y: 330, kind: "model", label: "Fallacy", sub: "8 types", badge: "Groq" },
  gate: { x: 945, y: 195, w: 156, h: 280, kind: "gate", label: "Gate" },
  operator: { x: 1100, y: 100, w: 120, kind: "output", label: "Operator", sub: "every flag" },
  audience: { x: 1100, y: 195, w: 120, kind: "output", label: "Audience", sub: "verified only" },
  log: { x: 1100, y: 290, w: 120, kind: "store", label: "Event log", sub: "hash chain" },
  report: { x: 1100, y: 372, w: 100, h: 40, kind: "output", label: "Report" },
  topic: { x: 395, y: 455, kind: "code", label: "Topic", sub: "moderator question" },
  prefetch: { x: 560, y: 455, kind: "model", label: "Prefetch", sub: "+ Google Search", badge: "Gemini" },
  verdict: { x: 860, y: 455, w: 170, kind: "model", label: "Grounded verdict", sub: "+ Google Search", badge: "Gemini" },
  passages: { x: 580, y: 565, kind: "store", label: "Topic passages" },
  stmts: { x: 735, y: 565, kind: "store", label: "Past statements" },
  bankdb: { x: 890, y: 565, kind: "store", label: "Claim bank" },
} satisfies Record<string, N>;
type Id = keyof typeof NODES;

const DIMS = (n: N) => ({ w: n.w ?? (n.kind === "store" ? 124 : 140), h: n.h ?? 54 });

interface E {
  id: string;
  from: Id;
  to: Id;
  dir?: "h" | "down" | "up";
  async?: boolean;
  /** Hand-routed path where the automatic curve would cross a node. */
  d?: string;
}
const EDGES: E[] = [
  { id: "audio-stt", from: "audio", to: "stt" },
  { id: "stt-seg", from: "stt", to: "seg" },
  { id: "seg-extract", from: "seg", to: "extract" },
  { id: "seg-fallacy", from: "seg", to: "fallacy" },
  { id: "extract-bank", from: "extract", to: "bank" },
  { id: "extract-stance", from: "extract", to: "stance" },
  { id: "extract-consist", from: "extract", to: "consist" },
  { id: "bank-gate", from: "bank", to: "gate" },
  { id: "stance-gate", from: "stance", to: "gate" },
  { id: "consist-gate", from: "consist", to: "gate" },
  { id: "fallacy-gate", from: "fallacy", to: "gate" },
  { id: "gate-operator", from: "gate", to: "operator" },
  { id: "gate-audience", from: "gate", to: "audience" },
  { id: "gate-log", from: "gate", to: "log" },
  { id: "log-report", from: "log", to: "report", dir: "down" },
  { id: "seg-topic", from: "seg", to: "topic", dir: "down", async: true },
  { id: "topic-prefetch", from: "topic", to: "prefetch", async: true },
  { id: "prefetch-passages", from: "prefetch", to: "passages", dir: "down", async: true },
  // U claim: out of the stance node's right side, down the gap between the lanes and the Gate
  { id: "stance-verdict", from: "stance", to: "verdict", async: true, d: "M805,145 C824,145 824,330 795,428" },
  { id: "verdict-gate", from: "verdict", to: "gate", async: true, d: "M905,428 C905,385 945,380 945,335" },
  // the knowledge base feeds the claims lane (bank, passages, past statements)
  { id: "kb-claims", from: "stmts", to: "consist", dir: "up" },
];

function edgePath({ from, to, dir = "h", d }: E): string {
  if (d) return d;
  const a = NODES[from] as N;
  const b = NODES[to] as N;
  const A = DIMS(a);
  const B = DIMS(b);
  if (dir === "h") {
    const x1 = a.x + A.w / 2;
    const x2 = b.x - B.w / 2;
    const dx = Math.max(30, (x2 - x1) * 0.5);
    return `M${x1},${a.y} C${x1 + dx},${a.y} ${x2 - dx},${b.y} ${x2},${b.y}`;
  }
  const down = dir === "down";
  const y1 = a.y + (down ? A.h / 2 : -A.h / 2);
  const y2 = b.y + (down ? -B.h / 2 : B.h / 2);
  const dy = (y2 - y1) * 0.5;
  return `M${a.x},${y1} C${a.x},${y1 + dy} ${b.x},${y2 - dy} ${b.x},${y2}`;
}

export const STEPS: { title: string; body: string; clock: string; nodes: Id[]; edges: string[]; packets: string[][] }[] = [
  {
    title: "Hear",
    body: "The debate audio streams to Deepgram as it plays. Final words come back a few hundred milliseconds after they are spoken.",
    clock: "0.40", nodes: ["audio", "stt"], edges: ["audio-stt"], packets: [["audio-stt"]],
  },
  {
    title: "Split",
    body: "Words become sentences, and each gets its speaker from a hand-checked timeline. A moderator's question sets the topic and starts fetching background in parallel.",
    clock: "0.45", nodes: ["seg", "topic", "prefetch", "passages"], edges: ["stt-seg", "seg-topic", "topic-prefetch", "prefetch-passages"],
    packets: [["stt-seg"], ["seg-topic", "topic-prefetch", "prefetch-passages"]],
  },
  {
    title: "Check",
    body: "Two lanes run at once. Claims are extracted, matched against a reviewed claim bank or checked against retrieved passages, and compared with what the speaker said before. Fallacy runs on the raw sentence.",
    clock: "1.10", nodes: ["extract", "bank", "stance", "consist", "fallacy", "bankdb", "stmts"],
    edges: ["seg-extract", "seg-fallacy", "extract-bank", "extract-stance", "extract-consist", "kb-claims"],
    packets: [["seg-extract", "extract-bank"], ["seg-extract", "extract-stance"], ["seg-extract", "extract-consist"], ["seg-fallacy"]],
  },
  {
    title: "Gate",
    body: "Code, not the model, decides. The span must be in the transcript word for word, the quote must be in the source, the words must be heard clearly, and confidence must be at least 0.80.",
    clock: "1.25", nodes: ["gate"], edges: ["bank-gate", "stance-gate", "consist-gate", "fallacy-gate"],
    packets: [["bank-gate"], ["stance-gate"], ["consist-gate"], ["fallacy-gate"]],
  },
  {
    title: "Show",
    body: "The operator sees every finding, including those still being checked. The audience sees only verified ones: the words underlined in the finding's color, with its letter beside them.",
    clock: "1.35", nodes: ["operator", "audience"], edges: ["gate-operator", "gate-audience"], packets: [["gate-operator"], ["gate-audience"]],
  },
  {
    title: "Ground",
    body: "A claim nothing could settle goes to the slow lane: Gemini with Google Search finds a source, the quote is checked on the real page, and the flag is upgraded in place seconds later.",
    clock: "+4–9 s", nodes: ["verdict"], edges: ["stance-verdict", "verdict-gate"], packets: [["stance-verdict", "verdict-gate", "gate-audience"]],
  },
  {
    title: "Prove",
    body: "Every event is written to a hash-chained log. The report and every replay are built only from that log, so what was shown can always be checked afterwards.",
    clock: "1.35", nodes: ["log", "report"], edges: ["gate-log", "log-report"], packets: [["gate-log", "log-report"]],
  },
];

const GATE_CHECKS = ["span in transcript", "quote in source", "heard clearly", "confidence ≥ 0.80"];

function Node({ id, n }: { id: Id; n: N }) {
  const { w, h } = DIMS(n);
  const x = n.x - w / 2;
  const y = n.y - h / 2;
  const rx = n.kind === "model" ? h / 2 : n.kind === "gate" ? 16 : 10;
  const stroke = n.kind === "gate" ? RED : INK;
  return (
    <g className={`pn pn-${id}`} data-id={id} opacity={0.28}>
      <g className="pn-ring" opacity={0}>
        <rect x={x - 6} y={y - 6} width={w + 12} height={h + 12} rx={rx + 6} fill="none" stroke={n.kind === "gate" ? RED : YALE} strokeWidth={2} />
      </g>
      {n.kind === "store" ? (
        <g className="pn-body">
          <path
            d={`M${x},${y + 8} a${w / 2},8 0 0,1 ${w},0 v${h - 16} a${w / 2},8 0 0,1 ${-w},0 z`}
            fill={ALMOND} stroke={stroke} strokeOpacity={0.35} strokeWidth={1.25}
          />
          <path d={`M${x},${y + 8} a${w / 2},8 0 0,0 ${w},0`} fill="none" stroke={stroke} strokeOpacity={0.35} strokeWidth={1.25} />
        </g>
      ) : (
        <rect className="pn-body" x={x} y={y} width={w} height={h} rx={rx} fill={n.kind === "gate" ? CREAM : ALMOND}
          stroke={stroke} strokeOpacity={n.kind === "gate" ? 0.9 : 0.35} strokeWidth={n.kind === "gate" ? 2 : 1.25} />
      )}
      {n.badge && (
        <text x={n.x} y={y - 7} textAnchor="middle" fontSize={10} fontWeight={700} letterSpacing="0.08em" fill={YALE}>
          {n.badge.toUpperCase()}
        </text>
      )}
      {n.kind === "gate" ? (
        <>
          <text x={n.x} y={y + 34} textAnchor="middle" fontSize={20} fontWeight={800} fill={RED}>
            Gate
          </text>
          <text x={n.x} y={y + 52} textAnchor="middle" fontSize={11} fill={INK} fillOpacity={0.6}>
            code checks
          </text>
          {GATE_CHECKS.map((c, i) => (
            <g key={c} className="gate-check" opacity={0.25}>
              <circle cx={x + 18} cy={y + 86 + i * 38} r={8} fill="none" stroke={INK} strokeOpacity={0.4} />
              <path className="gate-tick" d={`M${x + 14},${y + 86 + i * 38} l3,3 l6,-6`} fill="none" stroke={RED} strokeWidth={2} strokeLinecap="round" opacity={0} />
              <text x={x + 32} y={y + 90 + i * 38} fontSize={11.5} fill={INK}>
                {c}
              </text>
            </g>
          ))}
        </>
      ) : (
        <>
          <text x={n.x} y={n.sub ? n.y - 2 : n.y + 5} textAnchor="middle" fontSize={14} fontWeight={650} fill={INK}>
            {n.label}
          </text>
          {n.sub && (
            <text x={n.x} y={n.y + 15} textAnchor="middle" fontSize={11} fill={INK} fillOpacity={0.6}>
              {n.sub}
            </text>
          )}
        </>
      )}
    </g>
  );
}

const MAX_PACKETS = 4;
/** Timeline units the last step holds before the section unpins (so → never lands on the edge). */
const HOLD = 0.6;

export function PipelineStory() {
  const root = useRef<HTMLElement>(null);
  const clockRef = useRef<HTMLSpanElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const [step, setStep] = useState(0);
  const stRef = useRef<ScrollTrigger | null>(null);
  const reduced = useRef(false);

  useGSAP(
    () => {
      const q = gsap.utils.selector(root);
      const mm = gsap.matchMedia();

      const build = (scrub: boolean) => {
        // Every highlight edge starts undrawn.
        q<SVGPathElement>(".pe-draw").forEach((p) => {
          const len = p.getTotalLength();
          gsap.set(p, { strokeDasharray: len, strokeDashoffset: len });
        });
        gsap.set(q(".packet"), { opacity: 0 });
        const clock = { t: 0 };
        const tl = gsap.timeline({ defaults: { ease: "power2.inOut" }, paused: !scrub });

        STEPS.forEach((s, i) => {
          const at = `s${i}`;
          tl.addLabel(at, i);
          // the previous step's rings fade; this step's nodes come on with a ring
          if (i > 0) tl.to(STEPS[i - 1].nodes.map((id) => `.pn-${id} .pn-ring`).join(","), { opacity: 0, duration: 0.2 }, at);
          // draw edges, then nodes pop, while packets run the paths
          s.edges.forEach((e, k) => tl.to(`#pe-${e}`, { strokeDashoffset: 0, duration: 0.45 }, `${at}+=${0.05 + k * 0.04}`));
          s.packets.forEach((route, p) => {
            if (p >= MAX_PACKETS) return;
            const dot = `#packet-${p}`;
            tl.set(dot, { opacity: 1 }, `${at}+=0.02`);
            route.forEach((e, r) => {
              tl.to(dot, { duration: 0.42 / route.length + 0.12, ease: "power1.inOut",
                motionPath: { path: `#pe-${e}`, align: `#pe-${e}`, alignOrigin: [0.5, 0.5] } }, `${at}+=${0.05 + r * (0.42 / route.length + 0.12)}`);
            });
            tl.to(dot, { opacity: 0, duration: 0.1 }, `${at}+=0.72`);
          });
          tl.to(s.nodes.map((id) => `.pn-${id}`).join(","), { opacity: 1, duration: 0.3, stagger: 0.04 }, `${at}+=0.3`);
          tl.fromTo(s.nodes.map((id) => `.pn-${id} .pn-ring`).join(","), { opacity: 0, scale: 0.94, transformOrigin: "50% 50%" },
            { opacity: 1, scale: 1, duration: 0.3 }, `${at}+=0.35`);
          // the hot-path clock (Ground is async: shown as text, the bar stays)
          const target = parseFloat(s.clock);
          if (!s.clock.startsWith("+")) {
            tl.to(clock, { t: target, duration: 0.6, ease: "none",
              onUpdate: () => {
                if (clockRef.current) clockRef.current.textContent = `${clock.t.toFixed(2)} s`;
                if (barRef.current) barRef.current.style.width = `${Math.min(100, (clock.t / 1.5) * 100)}%`;
              } }, `${at}+=0.1`);
          }
          // step-specific moments
          if (s.title === "Gate") tl.to(q(".gate-check"), { opacity: 1, duration: 0.15, stagger: 0.1 }, `${at}+=0.45`)
            .to(q(".gate-tick"), { opacity: 1, duration: 0.12, stagger: 0.1 }, `${at}+=0.5`);
          if (s.title === "Show") tl.fromTo(q(".chip-out"), { opacity: 0, scale: 0.5, transformOrigin: "50% 50%" },
            { opacity: 1, scale: 1, duration: 0.3, ease: "back.out(2)", stagger: 0.08 }, `${at}+=0.55`);
          if (s.title === "Ground") tl.to("#chip-audience", { scale: 1.35, duration: 0.15, yoyo: true, repeat: 1, transformOrigin: "50% 50%" }, `${at}+=0.75`)
            .fromTo(".verdict-note", { opacity: 0 }, { opacity: 1, duration: 0.2 }, `${at}+=0.6`);
          if (s.title === "Prove") tl.fromTo(q(".hash"), { opacity: 0, x: -6 }, { opacity: 1, x: 0, duration: 0.2, stagger: 0.08 }, `${at}+=0.5`);
        });
        tl.addLabel("end", STEPS.length);
        tl.to({}, { duration: HOLD }, "end"); // the last step holds a while before the pin releases

        if (!scrub) {
          tl.progress(1);
          return;
        }
        const st = ScrollTrigger.create({
          trigger: root.current,
          start: "top 48px", // under the sticky nav
          end: `+=${(STEPS.length + HOLD) * 85}%`,
          pin: true,
          scrub: 0.6,
          animation: tl,
          onUpdate: () => {
            const t = tl.time();
            let cur = 0;
            STEPS.forEach((_, i) => {
              if (t >= tl.labels[`s${i}`] - 0.02) cur = i;
            });
            setStep((prev) => (prev === cur ? prev : cur));
          },
        });
        stRef.current = st;
      };

      mm.add("(prefers-reduced-motion: no-preference)", () => {
        reduced.current = false;
        build(true);
      });
      mm.add("(prefers-reduced-motion: reduce)", () => {
        reduced.current = true;
        build(false);
      });
    },
    { scope: root },
  );

  // ← / →: jump to the end of the next / previous step's animation. Past either end, move to the
  // neighbouring slide. Only while this section is on screen.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      if (e.target instanceof HTMLElement && e.target.closest("input, textarea, select")) return;
      const r = root.current?.getBoundingClientRect();
      const mid = window.innerHeight / 2;
      if (!r || r.top > mid || r.bottom < mid) return;
      e.preventDefault();
      const to = step + (e.key === "ArrowRight" ? 1 : -1);
      const st = stRef.current;
      if (to < 0 || to >= STEPS.length) {
        document.querySelector(`[data-slide="${to < 0 ? 1 : 3}"]`)?.scrollIntoView({ behavior: "smooth" });
        return;
      }
      if (!st || reduced.current) return setStep(to);
      const y = st.start + (st.end - st.start) * ((to + 0.85) / (STEPS.length + HOLD));
      window.scrollTo({ top: y, behavior: "smooth" });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step]);

  const s = STEPS[step];
  const audience = NODES.audience as N;
  const operator = NODES.operator as N;
  const log = NODES.log as N;

  return (
    <section ref={root} data-slide={2} id="how" className="relative bg-cream text-ink">
      <div className="mx-auto flex h-[calc(100dvh-3rem)] max-w-6xl flex-col px-6 pt-8 pb-6">
        <div className="flex flex-wrap items-end justify-between gap-6">
          <div>
            <p className="eyebrow text-ink/60">How it works</p>
            <h2 className="display mt-2 text-4xl md:text-6xl">One sentence, start to flag.</h2>
          </div>
          <div className="w-56 text-right">
            <p className="font-mono text-xs text-ink/60">hot path, design budget 1.5 s</p>
            <p className="display mt-1 text-4xl tabular-nums">
              <span ref={clockRef}>0.00 s</span>
            </p>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-ink/10">
              <div ref={barRef} className="h-full rounded-full bg-yale" style={{ width: "0%" }} />
            </div>
          </div>
        </div>

        <div className="relative mt-4 min-h-0 flex-1">
          <svg viewBox="0 0 1200 620" className="h-full w-full" preserveAspectRatio="xMidYMid meet" role="img"
            aria-label={`Pipeline, step ${step + 1} of ${STEPS.length}: ${s.title}`}>
            {/* bands */}
            <g fontSize={10.5} fontWeight={700} letterSpacing="0.12em" fill={INK} fillOpacity={0.45}>
              <text x={8} y={22}>HOT PATH · EVERY SENTENCE · ≤ 1.5 S</text>
              <rect x={0} y={400} width={1200} height={110} rx={18} fill={ALMOND} fillOpacity={0.55} />
              <text x={16} y={424}>ASYNC · SECONDS · OFF THE HOT PATH</text>
              <rect x={0} y={525} width={1200} height={88} rx={18} fill={ALMOND} fillOpacity={0.9} />
              <text x={16} y={549}>KNOWLEDGE BASE · REVIEWED · LOADED AT WARM-UP</text>
            </g>
            {/* edges: a faint track always, a drawn highlight on top */}
            {EDGES.map((e) => {
              const d = edgePath(e);
              return (
                <g key={e.id}>
                  <path d={d} fill="none" stroke={INK} strokeOpacity={0.13} strokeWidth={1.5} strokeDasharray={e.async ? "4 5" : undefined} />
                  <path id={`pe-${e.id}`} className="pe-draw" d={d} fill="none" stroke={e.async ? RED : YALE} strokeWidth={2.25} strokeLinecap="round" />
                </g>
              );
            })}
            {(Object.keys(NODES) as Id[]).map((id) => (
              <Node key={id} id={id} n={NODES[id] as N} />
            ))}
            {/* outputs: flags appear at Show; the async verdict upgrades the audience chip */}
            <g className="chip-out" opacity={0}>
              <rect x={operator.x + 68} y={operator.y - 11} width={22} height={22} rx={5} fill="none" stroke="#CE2C31" strokeWidth={1.5} strokeDasharray="3 2" />
              <text x={operator.x + 79} y={operator.y + 5} textAnchor="middle" fontSize={12} fontWeight={800} fill="#CE2C31">F</text>
            </g>
            <g className="chip-out" opacity={0} id="chip-audience">
              <rect x={audience.x + 68} y={audience.y - 11} width={22} height={22} rx={5} fill="#CE2C31" />
              <text x={audience.x + 79} y={audience.y + 5} textAnchor="middle" fontSize={12} fontWeight={800} fill="#fff">F</text>
            </g>
            <text className="verdict-note" x={860} y={498} textAnchor="middle" fontSize={11} fill={RED} opacity={0}>
              upgrades the flag in place, seconds later
            </text>
            {[0, 1, 2].map((i) => (
              <rect key={i} className="hash" x={log.x + 68 + i * 15} y={log.y - 6} width={12} height={12} rx={2} fill={YALE} opacity={0} />
            ))}
            {/* packets */}
            {Array.from({ length: MAX_PACKETS }, (_, i) => (
              <g key={i} id={`packet-${i}`} className="packet" opacity={0}>
                <circle r={9} fill={RED} fillOpacity={0.18} />
                <circle r={5} fill={RED} stroke={CREAM} strokeWidth={1.5} />
              </g>
            ))}
          </svg>
        </div>

        <div className="mt-2 grid items-start gap-6 md:grid-cols-[auto_1fr]">
          <div className="flex items-center gap-2 pt-2">
            {STEPS.map((x, i) => (
              <span key={x.title} title={x.title}
                className={`h-1.5 rounded-full transition-all duration-300 ${i === step ? "w-8 bg-ink" : i < step ? "w-3 bg-ink/50" : "w-3 bg-ink/15"}`} />
            ))}
          </div>
          <div className="min-h-[5.5rem]">
            <p className="flex items-baseline gap-3">
              <span className="font-mono text-sm text-ink/55">0{step + 1}</span>
              <span className="text-2xl font-bold tracking-tight">{s.title}</span>
              <span className="ml-auto hidden font-mono text-xs text-ink/50 md:inline">← → to step · scroll to play</span>
            </p>
            <p className="mt-1 max-w-3xl text-lg leading-snug font-medium text-ink/70">{s.body}</p>
          </div>
        </div>
      </div>
    </section>
  );
}
