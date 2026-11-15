import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AnnotatedText } from "../components/AnnotatedText";
import { FlagChip } from "../components/FlagChip";
import { CATEGORIES } from "../categories";
import type { Annotation, Category } from "../types";
import { Nav } from "./Nav";
import { PipelineStory } from "./PipelineStory";
import { Problem } from "./Problem";

/* ------------------------------------------------------------------------------------------------
 * Presentation mode: every <section data-slide> is one screen. Arrow keys / PageUp/Down / Space move
 * between them, and the dots on the right show where you are (and jump on click).
 * ---------------------------------------------------------------------------------------------- */

const SLIDES = ["Intro", "Why", "How it works", "Findings", "Speed", "Rules", "Demo"];

function useSlides() {
  const [active, setActive] = useState(0);
  useEffect(() => {
    const els = () => Array.from(document.querySelectorAll<HTMLElement>("[data-slide]"));
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) setActive(Number((e.target as HTMLElement).dataset.slide));
      },
      { rootMargin: "-45% 0px -45% 0px" },
    );
    els().forEach((el) => io.observe(el));
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && e.target.closest("input, textarea, select, button")) return;
      const next = ["ArrowDown", "PageDown", " "].includes(e.key) ? 1 : ["ArrowUp", "PageUp"].includes(e.key) ? -1 : 0;
      if (!next) return;
      e.preventDefault();
      const all = els();
      const mid = window.innerHeight / 2;
      const cur = all.findIndex((el) => {
        const r = el.getBoundingClientRect();
        return r.top <= mid && r.bottom > mid;
      });
      all[Math.max(0, Math.min(all.length - 1, (cur < 0 ? 0 : cur) + next))]?.scrollIntoView({ behavior: "smooth" });
    };
    window.addEventListener("keydown", onKey);
    return () => {
      io.disconnect();
      window.removeEventListener("keydown", onKey);
    };
  }, []);
  return active;
}

function SlideDots({ active }: { active: number }) {
  return (
    <div className="fixed top-1/2 right-4 z-40 hidden -translate-y-1/2 flex-col gap-2.5 md:flex" aria-label="Slides">
      {SLIDES.map((name, i) => (
        <button
          key={name}
          type="button"
          title={name}
          aria-label={`Go to ${name}`}
          onClick={() => document.querySelector(`[data-slide="${i}"]`)?.scrollIntoView({ behavior: "smooth" })}
          className={`h-2 w-2 rounded-full transition-all ${i === active ? "scale-125" : "opacity-40 hover:opacity-80"} bg-ink`}
        />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------
 * Hero caption: the real caption components, fed a synthetic example (labelled as such).
 * ---------------------------------------------------------------------------------------------- */

function mock(id: string, text: string, span: string, category: Category, status: Annotation["status"], explanation: string, subtype: string | null = null): Annotation {
  const start = text.indexOf(span);
  return {
    annotation_id: id, version: 1, segment_id: id, speaker_id: "speaker_a", span_text: span, span_char_start: start,
    span_char_end: start + span.length, category, subtype, explanation, opponent_span: null, confidence: 0.92, status,
    path: "bank", lane: category === "L" ? "fallacy" : "claims", latency_ms: 840, model_id: null, prompt_hash: null, repeat_count: 1,
    evidence: [{ source_id: "demo", title: "Synthetic reference (example)", url: "", publisher: "Example data", reliability: "primary",
      quote: "Unemployment rate: 4.1 percent.", published: null, retrieved_at: "" }],
  };
}

const LINE_1 = "The unemployment rate is exactly zero percent.";
const LINE_2 = "My opponent wears expensive shoes, so their budget cannot work.";
const STEPS: { label: string; anns: (s: Annotation["status"]) => Annotation[][]; status: Annotation["status"] | null }[] = [
  { label: "Heard", status: null, anns: () => [[], []] },
  { label: "Checking", status: "candidate", anns: (s) => [[mock("h1", LINE_1, "exactly zero percent", "F", s, "Contradicted by the reference figure (4.1 percent).")], []] },
  { label: "Verified", status: "verified", anns: (s) => [[mock("h1", LINE_1, "exactly zero percent", "F", s, "Contradicted by the reference figure (4.1 percent).")], [mock("h2", LINE_2, "wears expensive shoes, so their budget cannot work", "L", s, "Attacks the person, not the budget.", "ad_hominem")]] },
];

function HeroCaption() {
  const [step, setStep] = useState(0);
  const [ms, setMs] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setStep((s) => (s + 1) % (STEPS.length + 1)), 2200);
    return () => window.clearInterval(id);
  }, []);
  const shown = Math.min(step, STEPS.length - 1);
  useEffect(() => {
    if (shown === 0) return setMs(0);
    const target = shown === 1 ? 620 : 1340;
    const id = window.setInterval(() => setMs((m) => (m >= target ? target : m + 40)), 16);
    return () => window.clearInterval(id);
  }, [shown]);
  const s = STEPS[shown];
  const [a1, a2] = s.anns(s.status ?? "candidate");
  return (
    <div className="w-full max-w-3xl">
      <div className="overflow-hidden rounded-2xl border border-ink/10 bg-almond shadow-xl shadow-ink/10">
        <div className="flex items-center gap-2 border-b border-ink/10 px-4 py-2 text-xs text-ink/60">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-mahogany" /> Caption bar
          <span className="ml-auto font-mono tabular-nums">{s.status ? `${(ms / 1000).toFixed(2)} s after the sentence` : "listening"}</span>
        </div>
        <div className="space-y-3 px-5 py-5 text-left text-lg leading-relaxed text-ink md:text-xl">
          <p>
            <span className="mr-2 text-sm font-semibold text-ink/60">SPEAKER A</span>
            <AnnotatedText text={LINE_1} annotations={a1} />
          </p>
          <p>
            <span className="mr-2 text-sm font-semibold text-ink/60">SPEAKER A</span>
            <AnnotatedText text={LINE_2} annotations={a2} />
          </p>
        </div>
      </div>
      <div className="mt-3 flex items-center justify-center gap-4 text-xs text-ink/55">
        {STEPS.map((x, i) => (
          <span key={x.label} className={i === shown ? "text-ink" : ""}>
            {i + 1}. {x.label}
          </span>
        ))}
        <span className="text-ink/50">· synthetic example</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ */

const EXAMPLES: Record<Category, string> = {
  F: "Contradicted by BLS data (2024)",
  M: "True number, wrong comparison",
  D: "Real statistic, misread",
  L: "Strawman: not what was said",
  C: "Differs from prior statement (2019)",
  U: "No reliable source found yet",
  V: "Matches the official figure",
};

function chip(c: Category): Annotation {
  return mock(`cat-${c}`, "x", "x", c, "verified", EXAMPLES[c]);
}

function Stat({ value, label, note }: { value: string; label: string; note: string }) {
  return (
    <div className="border-t border-ink/15 pt-5">
      <p className="display text-6xl whitespace-nowrap lg:text-[3.25rem] xl:text-[3.5rem]">{value}</p>
      <p className="mt-3 text-xl font-semibold text-ink">{label}</p>
      <p className="mt-1 text-sm text-ink/60">{note}</p>
    </div>
  );
}

export function Home() {
  const active = useSlides();
  useEffect(() => {
    document.title = "BS Subtitles · Live debate fact-checking";
  }, []);

  return (
    <div className="bg-cream text-ink">
      <Nav />
      <SlideDots active={active} />

      {/* 0 · Hero */}
      <section data-slide={0} className="relative flex min-h-[calc(100dvh-3rem)] flex-col items-center justify-center overflow-hidden px-6 py-16 text-center">
        <p className="eyebrow rise relative text-mahogany">Live debate fact-checking</p>
        <h1 className="display rise relative mt-4 max-w-6xl text-6xl md:text-8xl lg:text-[8.5rem]" style={{ animationDelay: "80ms" }}>
          Every claim, checked before the applause ends.
        </h1>
        <p className="lead rise relative mt-8 max-w-3xl" style={{ animationDelay: "160ms" }}>
          Captions that underline what a candidate gets wrong, seconds after they say it, with the source one tap away.
        </p>
        <div className="rise relative mt-8 flex flex-wrap justify-center gap-3" style={{ animationDelay: "240ms" }}>
          <Link to="/demo" className="pill-yale">
            Watch the demo
          </Link>
          <a href="#how" className="pill-ghost text-ink">
            How it works
          </a>
        </div>
        <div className="rise relative mt-14 w-full flex justify-center" style={{ animationDelay: "360ms" }}>
          <HeroCaption />
        </div>
      </section>

      {/* 1 · Why */}
      <Problem />

      {/* 2 · How it works */}
      <PipelineStory />

      {/* 3 · Findings */}
      <section data-slide={3} className="bg-almond text-ink">
        <div className="mx-auto flex min-h-[100dvh] max-w-6xl flex-col justify-center px-6 py-24">
          <p className="eyebrow text-yale">Seven findings</p>
          <h2 className="display mt-3 max-w-5xl text-5xl md:text-7xl lg:text-8xl">Color always means a finding. A letter always names it.</h2>
          <p className="lead mt-8 max-w-3xl">
            Every color comes with its letter, so the captions work for color-blind viewers too. A solid chip is verified, an outlined one is
            still being checked, and a struck-through one was withdrawn.
          </p>
          <div className="mt-12 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {(Object.keys(CATEGORIES) as Category[]).map((c) => (
              <div key={c} className="rounded-2xl bg-cream p-5 text-ink">
                <FlagChip a={chip(c)} popover={false} />
                <p className="mt-4 text-xl font-semibold tracking-tight">{CATEGORIES[c].label}</p>
                <p className="mt-1 text-sm text-ink/60">“{EXAMPLES[c]}”</p>
              </div>
            ))}
            <div className="flex flex-col justify-end rounded-2xl border border-ink/15 p-5">
              <p className="text-sm text-ink/60">Moderator lines are shown in gray and are never flagged.</p>
            </div>
          </div>
        </div>
      </section>

      {/* 4 · Speed */}
      <section data-slide={4} className="bg-cream text-ink">
        <div className="mx-auto flex min-h-[100dvh] max-w-6xl flex-col justify-center px-6 py-24">
          <p className="eyebrow text-ink/60">Speed is the feature</p>
          <h2 className="display mt-3 max-w-5xl text-5xl md:text-7xl lg:text-8xl">The check lands while the line still matters.</h2>
          <p className="lead mt-8 max-w-3xl">
            Each sentence runs through three checks at once, on the fastest models we could find. A flag appears seconds after the
            sentence ends, not the next morning.
          </p>
          <div className="mt-14 grid gap-10 md:grid-cols-2 lg:grid-cols-4">
            <Stat value="1.6–5.7 s" label="first flag" note="after the end of a sentence" />
            <Stat value="4–9 s" label="web verdict" note="Gemini with Google Search, off the hot path" />
            <Stat value="115" label="sentences checked" note="the full 8.5-minute economy exchange" />
            <Stat value="0 tokens" label="on a replayed debate" note="analysis is cached and reused" />
          </div>
          <p className="mt-10 text-sm text-ink/50">Measured on the economy debate clip, November 2026.</p>
        </div>
      </section>

      {/* 5 · Rules */}
      <section data-slide={5} className="bg-almond text-ink">
        <div className="mx-auto grid min-h-[100dvh] max-w-6xl items-center gap-12 px-6 py-24 lg:grid-cols-[1fr_1.1fr]">
          <div>
            <p className="eyebrow text-mahogany">Rules we don't break</p>
            <h2 className="display mt-3 text-6xl md:text-8xl">Neutral by design.</h2>
            <ul className="mt-10 space-y-5 text-lg">
              {[
                ["No scores, no winners.", "We mark claims. We never rank the people making them."],
                ["Verified, or not shown.", "The audience only sees findings with a source quote found on the page and 80 percent confidence."],
                ["Neutral words.", "“Contradicted by BLS data (2024)”, never “False” or “Liar”."],
                ["Everything on the record.", "A hash-chained log of every event; replays are labelled as replays."],
              ].map(([t, d]) => (
                <li key={t} className="border-t border-ink/15 pt-4">
                  <p className="font-semibold">{t}</p>
                  <p className="mt-1 text-ink/65">{d}</p>
                </li>
              ))}
            </ul>
          </div>
          <img src="/img/capitol.jpg" alt="The dome of the United States Capitol with a flag" className="w-full rounded-2xl object-cover shadow-xl shadow-ink/20" loading="lazy" />
        </div>
      </section>

      {/* 6 · Demo CTA */}
      <section data-slide={6} className="relative overflow-hidden bg-cream text-ink">
        {/* Background photo at 10% over the cream page; the content below sits on top (relative). */}
        <img src="/img/crowd-flags.jpg" alt="" aria-hidden className="pointer-events-none absolute inset-0 h-full w-full object-cover opacity-10" />
        <div className="relative mx-auto flex min-h-[100dvh] max-w-6xl flex-col items-center justify-center px-6 py-24 text-center">
          <h2 className="display text-6xl md:text-8xl lg:text-[8.5rem]">See it on a real debate.</h2>
          <p className="lead mt-8 max-w-2xl">Harris and Trump on the economy, September 2024. Eight and a half minutes, checked live.</p>
          <div className="mt-8 flex flex-wrap justify-center gap-3">
            <Link to="/demo" className="pill-yale">
              Open the demo
            </Link>
            <Link to="/help" className="pill-ghost text-ink">
              How to read the captions
            </Link>
          </div>
        </div>
        <footer className="relative flex flex-wrap items-center gap-4 bg-cream/85 px-[max(1.5rem,calc((100%-72rem)/2+1.5rem))] py-5 text-xs text-ink/65 backdrop-blur-md">
          <span className="finding-strip h-[3px] w-16 rounded-full" />
          <span>BS Subtitles · built for live political debates</span>
          <span className="ml-auto">Use ↑ ↓ to move between slides</span>
        </footer>
      </section>
    </div>
  );
}
