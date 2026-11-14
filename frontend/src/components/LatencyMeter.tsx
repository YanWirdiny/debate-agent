import type { MetricsSnapshot } from "../types";

const BUDGET_MS = 1500; // flag clock: end of sentence -> caption flag (CONTEXT.md section 4)

function Stat({ label, value, warn, hint }: { label: string; value: string; warn?: boolean; hint?: string }) {
  return (
    <div className="min-w-0 rounded-lg bg-zinc-800 px-3 py-2" title={hint}>
      <div className="text-[11px] tracking-wide text-zinc-400 uppercase">{label}</div>
      <div className={`text-lg font-semibold tabular-nums ${warn ? "text-red-400" : ""}`}>{value}</div>
    </div>
  );
}

/** p50/p95 latency, bank hit rate, loop lag (+ unchecked count) for the operator. */
export function LatencyMeter({ m, pendingCount }: { m: MetricsSnapshot | null; pendingCount: number }) {
  const ms = (v: number | undefined) => (v == null || !Number.isFinite(v) ? "–" : `${Math.round(v)} ms`);
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
      <Stat label="p50 latency" value={ms(m?.p50_ms)} />
      <Stat label="p95 latency" value={ms(m?.p95_ms)} warn={(m?.p95_ms ?? 0) > BUDGET_MS} hint="Budget 1.5 s" />
      <Stat label="Bank hit rate" value={m ? `${Math.round(m.bank_hit_rate * 100)}%` : "–"} />
      <Stat label="Loop lag" value={ms(m?.loop_lag_ms)} warn={(m?.loop_lag_ms ?? 0) > 100} hint="> 100 ms: machine overloaded" />
      <Stat label="Not checked" value={String(pendingCount)} warn={pendingCount > 0} hint="Lane timed out or failed (pending)" />
    </div>
  );
}
