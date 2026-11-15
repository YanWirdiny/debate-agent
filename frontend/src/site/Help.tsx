import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { FlagChip } from "../components/FlagChip";
import { CATEGORIES } from "../categories";
import type { Annotation, Category } from "../types";
import { Nav } from "./Nav";

interface SessionRow {
  session_id: string;
  clip_id?: string;
  source?: string;
  intel?: string;
  event_count?: number;
  t0_wall?: number;
}

function ann(category: Category, status: Annotation["status"]): Annotation {
  return {
    annotation_id: `${category}-${status}`, version: 1, segment_id: "help", speaker_id: "speaker_a", span_text: "x",
    span_char_start: 0, span_char_end: 1, category, subtype: null, explanation: "", evidence: [], opponent_span: null,
    confidence: 0.9, status, path: "bank", lane: "claims", latency_ms: 0, model_id: null, prompt_hash: null, repeat_count: 1,
  };
}

const STATES: { status: Annotation["status"]; struck?: boolean; title: string; body: string; underline: string }[] = [
  { status: "candidate", title: "Outlined: being checked", body: "Seen only by the operator. The model found something, but the evidence is not confirmed yet.", underline: "dashed" },
  { status: "verified", title: "Solid: verified", body: "A source quote was found word for word and confidence is at least 80 percent. This is what the audience sees.", underline: "solid" },
  { status: "withdrawn", struck: true, title: "Struck through: withdrawn", body: "A later check or the operator removed it. It stays visible, crossed out, so nothing disappears silently.", underline: "line-through" },
];

const STEPS = [
  ["Start the backend", "From backend/: REPLAY_TRANSCRIPT=1 INTEL=real .venv/bin/uvicorn app.main:app --workers 1 --reload"],
  ["Open the demo", "Pick the clip (economy is the full 8.5-minute exchange) and press Load. Warm-up takes a few seconds."],
  ["Press Start", "The video plays with sound and the transcript fills in as each sentence is spoken."],
  ["Watch the flags", "Outlined chips are still being checked. Click any chip for the explanation, quote and source."],
  ["Kill a wrong flag", "Use Kill on a flag before it is on air. It is struck through and logged, never deleted."],
  ["Open the report", "When the clip ends, Report shows every finding, built only from the event log."],
];

const FAQ = [
  ["Why does a line say “pending”?", "One of its checks ran out of time or failed. The sentence is shown without a flag rather than with a guess."],
  ["Why are some lines gray?", "Moderator lines. They set the topic but are never fact-checked."],
  ["Why did a flag appear late?", "Web verdicts (Gemini with Google Search) take a few seconds longer. A late verdict updates the chip in place and shows up in the side rail."],
  ["Does a replay cost tokens?", "No. Every answer is cached. Replaying the same transcript reuses the stored analysis; only new sentences call the models."],
  ["Do you score the candidates?", "Never. There are no scores, rankings or winner meters, by design."],
];

export function Help() {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  useEffect(() => {
    document.title = "Help · BS Subtitles";
    fetch("/sessions")
      .then((r) => (r.ok ? r.json() : []))
      .then(setSessions)
      .catch(() => setSessions([]));
  }, []);

  return (
    <div className="min-h-screen bg-cream text-ink">
      <Nav />
      <main className="mx-auto max-w-5xl px-6 pb-24">
        <header className="py-20">
          <p className="eyebrow text-yale">Help</p>
          <h1 className="display mt-3 text-6xl md:text-8xl lg:text-9xl">Reading the captions.</h1>
          <p className="lead mt-8 max-w-3xl">
            What each mark means, how to run a demo, and where to find the reports.
          </p>
        </header>

        <section className="border-t border-ink/15 py-14">
          <h2 className="display text-4xl md:text-6xl">Three states</h2>
          <div className="mt-8 grid gap-4 md:grid-cols-3">
            {STATES.map((s) => (
              <div key={s.title} className="rounded-2xl bg-almond p-6 text-ink">
                <p className="text-lg">
                  <span
                    style={{
                      textDecorationLine: s.underline === "line-through" ? "line-through" : "underline",
                      textDecorationStyle: s.underline === "dashed" ? "dashed" : "solid",
                      textDecorationColor: CATEGORIES.F.color,
                      textDecorationThickness: s.underline === "solid" ? "3px" : "2px",
                      textUnderlineOffset: "4px",
                      opacity: s.struck ? 0.6 : 1,
                    }}
                  >
                    exactly zero percent
                  </span>{" "}
                  <FlagChip a={ann("F", s.status)} struck={s.struck} popover={false} />
                </p>
                <p className="mt-5 font-semibold">{s.title}</p>
                <p className="mt-1 text-sm text-ink/65">{s.body}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="border-t border-ink/15 py-14">
          <h2 className="display text-4xl md:text-6xl">Seven findings</h2>
          <div className="mt-8 overflow-x-auto">
            <table className="w-full text-left">
              <tbody>
                {(Object.keys(CATEGORIES) as Category[]).map((c) => (
                  <tr key={c} className="border-b border-ink/10">
                    <td className="w-14 py-3">
                      <span className="inline-flex h-7 w-7 items-center justify-center rounded font-bold text-white" style={{ background: CATEGORIES[c].color }}>
                        {c}
                      </span>
                    </td>
                    <td className="py-3 font-medium">{CATEGORIES[c].label}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="border-t border-ink/15 py-14">
          <div className="flex flex-wrap items-baseline justify-between gap-4">
            <h2 className="display text-4xl md:text-6xl">Running a demo</h2>
            <Link to="/demo" className="pill-yale">
              Open the demo
            </Link>
          </div>
          <ol className="mt-8 grid gap-4 md:grid-cols-2">
            {STEPS.map(([t, d], i) => (
              <li key={t} className="rounded-2xl bg-almond p-5">
                <p className="font-mono text-sm text-ink/55">0{i + 1}</p>
                <p className="mt-1 font-semibold">{t}</p>
                <p className={`mt-1 text-sm text-ink/65 ${i === 0 ? "font-mono text-[12.5px] break-words" : ""}`}>{d}</p>
              </li>
            ))}
          </ol>
        </section>

        <section className="border-t border-ink/15 py-14">
          <h2 className="display text-4xl md:text-6xl">Questions</h2>
          <dl className="mt-8 divide-y divide-ink/10">
            {FAQ.map(([q, a]) => (
              <div key={q} className="grid gap-2 py-5 md:grid-cols-[1fr_1.4fr]">
                <dt className="font-semibold">{q}</dt>
                <dd className="text-ink/65">{a}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="border-t border-ink/15 py-14">
          <h2 className="display text-4xl md:text-6xl">Recorded runs</h2>
          {sessions.length === 0 && <p className="mt-6 text-ink/55">None yet, or the backend is not running.</p>}
          <ul className="mt-6 divide-y divide-ink/10">
            {sessions.map((s) => (
              <li key={s.session_id} className="flex flex-wrap items-center gap-3 py-3 text-sm">
                <Link to={`/reports/${encodeURIComponent(s.session_id)}`} className="font-mono text-yale underline underline-offset-4">
                  {s.session_id}
                </Link>
                <span className="text-ink/50">
                  {[s.source, s.intel && `INTEL=${s.intel}`, s.event_count != null && `${s.event_count} events`].filter(Boolean).join(" · ")}
                </span>
                {s.t0_wall && <span className="ml-auto text-ink/55">{new Date(s.t0_wall * 1000).toLocaleString()}</span>}
              </li>
            ))}
          </ul>
        </section>
      </main>
    </div>
  );
}
