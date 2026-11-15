import { useEffect } from "react";
import { Link, NavLink } from "react-router-dom";
import { Operator } from "../views/Operator";
import { Nav } from "./Nav";

/** The recorded run on YouTube (privacy-enhanced embed: no cookies until the viewer presses play). */
const YOUTUBE_ID = "QroA1SkaJTg";

const TABS = [
  { to: "/demo", label: "Recorded demo", end: true },
  { to: "/demo/live", label: "Live", end: false },
];

/** Segmented switch under the site nav: Recorded demo | Live. */
function DemoTabs() {
  return (
    <div className="sticky top-12 z-40 border-b border-ink/8 bg-cream/85 backdrop-blur-xl">
      <div className="mx-auto flex h-11 max-w-6xl items-center gap-4 px-5">
        <span className="text-sm font-semibold">Demo</span>
        <div className="flex rounded-full bg-almond p-0.5 text-[13px]">
          {TABS.map((t) => (
            <NavLink
              key={t.to}
              to={t.to}
              end={t.end}
              className={({ isActive }) =>
                `rounded-full px-3.5 py-1 transition-colors ${isActive ? "bg-cream font-medium text-ink shadow-sm" : "text-ink/65 hover:text-ink"}`
              }
            >
              {t.label}
            </NavLink>
          ))}
        </div>
      </div>
    </div>
  );
}

export function RecordedDemo() {
  useEffect(() => {
    document.title = "Recorded demo · BS Subtitles";
  }, []);
  return (
    <div className="min-h-screen bg-cream text-ink">
      <Nav />
      <DemoTabs />
      <main className="mx-auto max-w-6xl px-6 pt-14 pb-24">
        <p className="eyebrow text-mahogany">Recorded demo</p>
        <h1 className="display mt-3 text-5xl md:text-7xl lg:text-8xl">Watch a full run.</h1>
        <p className="lead mt-6 max-w-3xl">
          A recorded session of the agent checking a real debate: captions, flags as they land, and the operator view behind them.
        </p>

        <div className="mt-10 overflow-hidden rounded-3xl bg-almond shadow-xl shadow-ink/10">
          <div className="relative aspect-video w-full">
            <iframe
              className="absolute inset-0 h-full w-full"
              src={`https://www.youtube-nocookie.com/embed/${YOUTUBE_ID}?rel=0&modestbranding=1&playsinline=1`}
              title="BS Subtitles: recorded demo"
              allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
              referrerPolicy="strict-origin-when-cross-origin"
              allowFullScreen
              loading="lazy"
            />
          </div>
        </div>

        <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-3 text-sm text-ink/65">
          <span className="rounded-full border border-ink/20 px-2.5 py-0.5 text-xs font-semibold tracking-wide uppercase">Recording</span>
          <span>This is a replay of an earlier run, not a live session.</span>
          <a href={`https://youtu.be/${YOUTUBE_ID}`} target="_blank" rel="noreferrer" className="underline underline-offset-4 hover:text-ink">
            Open on YouTube
          </a>
          <Link to="/demo/live" className="pill-yale ml-auto">
            Run it live
          </Link>
        </div>
      </main>
    </div>
  );
}

/** The operator console under the site nav and the demo tabs. */
export function LiveDemo() {
  return (
    <div style={{ "--chrome": "calc(5.75rem + 1px)" } as React.CSSProperties}>
      <Nav />
      <DemoTabs />
      <Operator />
    </div>
  );
}
