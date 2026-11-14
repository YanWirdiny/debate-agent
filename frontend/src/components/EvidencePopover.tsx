import { CATEGORIES, categoryLabel } from "../categories";
import type { Annotation } from "../types";

/** Explanation, quote, source and date (CONTEXT.md section 10). Neutral wording comes from the backend. */
export function EvidencePopover({ a, struck, align = "left" }: { a: Annotation; struck: boolean; align?: "left" | "right" }) {
  const { color } = CATEGORIES[a.category];
  return (
    <div
      role="dialog"
      className={`absolute bottom-full z-50 mb-2 w-[min(24rem,85vw)] rounded-lg border border-zinc-700 bg-zinc-900 p-3 text-left text-sm text-zinc-100 shadow-2xl ${align === "right" ? "right-0" : "left-0"}`}
    >
      <div className="mb-1 flex items-center gap-2">
        <span className="rounded px-1.5 py-0.5 text-xs font-bold text-white" style={{ backgroundColor: color }}>
          {a.category}
        </span>
        <span className="font-semibold">{categoryLabel(a)}</span>
        {struck && <span className="ml-auto text-xs text-zinc-400">removed</span>}
        {!struck && a.status !== "verified" && <span className="ml-auto text-xs text-zinc-400">{a.status}</span>}
      </div>
      <p className="mb-2 text-zinc-200">{a.explanation || "No explanation."}</p>
      {a.opponent_span && (
        <p className="mb-2 text-xs text-zinc-400">
          Opponent said: <q className="text-zinc-200">{a.opponent_span}</q>
        </p>
      )}
      {a.evidence.map((e) => (
        <figure key={e.source_id + e.quote} className="mb-2 border-l-2 border-zinc-600 pl-2 last:mb-0">
          {e.quote && <blockquote className="text-zinc-300 italic">“{e.quote}”</blockquote>}
          <figcaption className="mt-1 text-xs text-zinc-400">
            {e.url ? (
              <a href={e.url} target="_blank" rel="noreferrer noopener" className="underline hover:text-zinc-200">
                {e.publisher || e.title || "Source"}
              </a>
            ) : (
              e.publisher || e.title
            )}
            {e.published && <> · {e.published}</>}
            {e.reliability !== "media" && <> · {e.reliability === "primary" ? "official source" : "fact-checker"}</>}
          </figcaption>
        </figure>
      ))}
    </div>
  );
}
