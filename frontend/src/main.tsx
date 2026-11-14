import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import "./index.css";
import { Audience } from "./views/Audience";
import { Operator } from "./views/Operator";
import { Report } from "./views/Report";

interface SessionRow {
  session_id: string;
  clip_id?: string;
  source?: string;
  intel?: string;
  event_count?: number;
  t0_wall?: number;
}

function Home() {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  useEffect(() => {
    fetch("/sessions")
      .then((r) => (r.ok ? r.json() : []))
      .then(setSessions)
      .catch(() => setSessions([]));
  }, []);
  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="mx-auto max-w-3xl px-4 py-12">
        <h1 className="mb-1 text-3xl font-bold tracking-tight">BS Subtitles</h1>
        <p className="mb-8 text-zinc-400">Live fact-check and fallacy captions for political debates.</p>
        <div className="mb-10 grid gap-3 sm:grid-cols-2">
          <Link to="/operator" className="card">
            <span className="text-lg font-semibold">Operator</span>
            <span className="text-sm text-zinc-400">Load a clip, start it, review every flag, kill wrong ones.</span>
          </Link>
          <Link to="/audience" className="card">
            <span className="text-lg font-semibold">Audience</span>
            <span className="text-sm text-zinc-400">Video with captions and verified flags only.</span>
          </Link>
        </div>
        <h2 className="mb-3 text-sm font-semibold tracking-wide text-zinc-400 uppercase">Recorded sessions</h2>
        {sessions.length === 0 && <p className="text-sm text-zinc-500">None yet.</p>}
        <ul className="divide-y divide-zinc-800">
          {sessions.map((s) => (
            <li key={s.session_id} className="flex items-center gap-3 py-2 text-sm">
              <Link to={`/reports/${encodeURIComponent(s.session_id)}`} className="font-mono underline">
                {s.session_id}
              </Link>
              <span className="text-zinc-500">
                {[s.source, s.intel && `INTEL=${s.intel}`, s.event_count != null && `${s.event_count} events`].filter(Boolean).join(" · ")}
              </span>
              {s.t0_wall && <span className="ml-auto text-zinc-500">{new Date(s.t0_wall * 1000).toLocaleString()}</span>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/operator" element={<Operator />} />
        <Route path="/audience" element={<Audience />} />
        <Route path="/reports/:id" element={<Report />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
