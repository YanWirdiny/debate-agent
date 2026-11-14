import { useView } from "../store";

const STYLE = {
  info: "border-sky-700 bg-sky-950 text-sky-100",
  warn: "border-amber-700 bg-amber-950 text-amber-100",
  error: "border-red-700 bg-red-950 text-red-100",
} as const;

/** Operator alerts (Deepgram reconnecting, replay mode, bank-only mode). */
export function Banners() {
  const banners = useView((s) => s.banners);
  const dismiss = useView((s) => s.dismissBanner);
  if (!banners.length) return null;
  return (
    <div className="space-y-2">
      {banners.map((b) => (
        <div key={b.id} role="status" className={`flex items-center gap-3 rounded-lg border px-3 py-2 text-sm ${STYLE[b.level]}`}>
          <span className="font-semibold uppercase">{b.level}</span>
          <span className="flex-1">{b.text}</span>
          <button type="button" onClick={() => dismiss(b.id)} className="opacity-70 hover:opacity-100" aria-label="Dismiss">
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
