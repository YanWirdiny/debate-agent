import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { CATEGORIES, FALLACY_LABELS, fmtTime, speakerName } from "../categories";
import type { Category, Evidence } from "../types";

interface Item {
  annotation_id: string;
  speaker_id: string;
  category: Category;
  subtype: string | null;
  t_start: number | null;
  segment_text: string;
  span_text: string;
  opponent_span: string | null;
  explanation: string;
  evidence: Evidence[];
  status: string;
  path: string;
  confidence: number;
  repeat_count: number;
  latency_ms: number;
}

interface ReportData {
  header: Record<string, unknown> & { session_id: string; clip_id: string; participants: string[]; chain_valid: boolean; complete: boolean };
  highlights: { speaker_id: string; categories: { category: Category; label: string; items: Item[] }[] }[];
  differs_from_prior: Item[];
  unverifiable: Item[];
  operator_actions: { killed: number; kill_events: number; withdrawn: number };
  latency: { p50_ms: number; p95_ms: number; p95_bank_ms: number; n: number };
  unchecked?: { speaker_segments: number; by_lane: Record<string, number> };
  methodology: { eval: { per_category?: Record<string, { precision: number | null; recall: number | null; n: number }> } | null; notes: string[] };
}

/** Post-debate report from the event log (no ranking: speakers alphabetical, flags in time order). */
export function Report() {
  const { id = "" } = useParams();
  const [r, setR] = useState<ReportData | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    fetch(`/report/${encodeURIComponent(id)}`)
      .then(async (res) => (res.ok ? setR((await res.json()) as ReportData) : setErr(`No event log for session ${id}.`)))
      .catch(() => setErr("Backend unreachable."));
  }, [id]);

  if (err) return <Shell id={id}><p className="text-zinc-400">{err}</p></Shell>;
  if (!r) return <Shell id={id}><p className="text-zinc-500">Loading…</p></Shell>;
  const h = r.header;
  return (
    <Shell id={id}>
      <dl className="mb-8 grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
        <Meta k="Clip" v={String(h.clip_id)} />
        <Meta k="Participants" v={h.participants.map(speakerName).join(", ")} />
        <Meta k="Duration" v={fmtTime(Number(h.duration_s))} />
        <Meta k="Mode" v={`${String(h.source)} · ${String(h.intel)}${h.replay_of ? ` · replay of ${String(h.replay_of)}` : ""}`} />
        <Meta k="Event log" v={`${String(h.event_count)} events · chain ${h.chain_valid ? "valid" : "INVALID"}${h.complete ? "" : " · incomplete"}`} />
        <Meta k="Head hash" v={String(h.log_head_hash).slice(0, 16) + "…"} />
        <Meta k="Models" v={(h.model_ids as string[]).join(", ") || "–"} />
        <Meta k="Started" v={String(h.started_at ?? "–")} />
      </dl>

      <h2 className="section">Highlights</h2>
      {r.highlights.map((sp) => (
        <div key={sp.speaker_id} className="mb-6">
          <h3 className="mb-2 text-lg font-semibold">{speakerName(sp.speaker_id)}</h3>
          {sp.categories.length === 0 && <p className="text-sm text-zinc-500">No verified flags.</p>}
          {sp.categories.map((c) => (
            <div key={c.category} className="mb-3">
              <h4 className="mb-1 text-sm font-semibold" style={{ color: CATEGORIES[c.category].color }}>
                {c.category} · {c.label}
              </h4>
              {c.items.map((i) => <ItemRow key={i.annotation_id} i={i} />)}
            </div>
          ))}
        </div>
      ))}

      <h2 className="section">Differs from prior statement</h2>
      {r.differs_from_prior.length ? r.differs_from_prior.map((i) => <ItemRow key={i.annotation_id} i={i} showSpeaker />) : <None />}

      <h2 className="section">Unverifiable</h2>
      {r.unverifiable.length ? r.unverifiable.map((i) => <ItemRow key={i.annotation_id} i={i} showSpeaker />) : <None />}

      <h2 className="section">Methodology</h2>
      <ul className="list-disc space-y-1 pl-5 text-sm text-zinc-300">
        {r.methodology.notes.map((n) => <li key={n}>{n}</li>)}
        <li>
          Latency, end of sentence to flag: p50 {Math.round(r.latency.p50_ms)} ms, p95 {Math.round(r.latency.p95_ms)} ms (bank hits p95{" "}
          {Math.round(r.latency.p95_bank_ms)} ms, n={r.latency.n}).
        </li>
        <li>
          Operator removed {r.operator_actions.killed} flag(s); {r.operator_actions.withdrawn} withdrawn by later evidence.
        </li>
        {r.unchecked && (
          <li>
            {Object.keys(r.unchecked.by_lane).length
              ? `Not checked (lane timed out or failed), out of ${r.unchecked.speaker_segments} speaker sentences: ${Object.entries(r.unchecked.by_lane).map(([l, n]) => `${l} ${n}`).join(", ")}. An unflagged sentence is not necessarily accurate.`
              : `Every one of the ${r.unchecked.speaker_segments} speaker sentences was checked by every lane.`}
          </li>
        )}
      </ul>
      {r.methodology.eval?.per_category && (
        <table className="mt-3 text-sm">
          <thead className="text-left text-zinc-400">
            <tr><th className="pr-6">Category</th><th className="pr-6">Precision</th><th className="pr-6">Recall</th><th>n</th></tr>
          </thead>
          <tbody>
            {Object.entries(r.methodology.eval.per_category).map(([c, m]) => (
              <tr key={c}><td className="pr-6">{c}</td><td className="pr-6">{m.precision ?? "–"}</td><td className="pr-6">{m.recall ?? "–"}</td><td>{m.n}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </Shell>
  );
}

function Shell({ id, children }: { id: string; children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="mx-auto max-w-4xl px-4 py-8">
        <header className="mb-6 flex flex-wrap items-baseline gap-3">
          <Link to="/" className="text-sm text-zinc-400 underline">Home</Link>
          <h1 className="text-2xl font-bold">Debate report</h1>
          <span className="text-sm text-zinc-500">{id}</span>
          <a href={`/report/${encodeURIComponent(id)}.html`} target="_blank" rel="noreferrer" className="ml-auto text-sm underline">
            Printable version
          </a>
        </header>
        {children}
      </div>
    </div>
  );
}

function Meta({ k, v }: { k: string; v: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-zinc-500 uppercase">{k}</dt>
      <dd className="truncate" title={v}>{v}</dd>
    </div>
  );
}

function None() {
  return <p className="mb-4 text-sm text-zinc-500">None.</p>;
}

function ItemRow({ i, showSpeaker = false }: { i: Item; showSpeaker?: boolean }) {
  const label = i.category === "L" && i.subtype ? FALLACY_LABELS[i.subtype] ?? i.subtype : CATEGORIES[i.category].label;
  return (
    <div className="mb-3 border-l-2 pl-3 text-sm" style={{ borderColor: CATEGORIES[i.category].color }}>
      <p>
        <span className="mr-2 text-zinc-500 tabular-nums">{fmtTime(i.t_start)}</span>
        {showSpeaker && <span className="mr-2 font-semibold">{speakerName(i.speaker_id)}</span>}
        <q className="text-zinc-100">{i.span_text}</q>
        {i.repeat_count > 1 && <span className="ml-2 text-xs text-zinc-500">said {i.repeat_count}×</span>}
      </p>
      <p className="text-zinc-400">
        <span className="font-medium text-zinc-300">{label}.</span> {i.explanation}
      </p>
      {i.evidence.map((e) => (
        <p key={e.source_id + e.quote} className="text-xs text-zinc-500">
          {e.quote && <>“{e.quote}” </>}—{" "}
          {e.url ? <a href={e.url} target="_blank" rel="noreferrer noopener" className="underline">{e.publisher || e.title}</a> : e.publisher || e.title}
          {e.published && `, ${e.published}`}
        </p>
      ))}
    </div>
  );
}
