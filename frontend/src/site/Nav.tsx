import { NavLink } from "react-router-dom";

const LINKS = [
  { to: "/", label: "Overview", end: true },
  { to: "/demo", label: "Demo", end: false },
  { to: "/help", label: "Help", end: false },
];

/** The wordmark (transparent PNG, original blackletter in ink) with the seven-finding line under it. */
export function Logo({ className = "h-[22px]" }: { className?: string }) {
  return (
    <span className="inline-flex flex-col items-stretch gap-[5px]">
      <img src="/img/logo.png" alt="The BS Subtitle" className={`${className} w-auto select-none`} draggable={false} />
      <span className="finding-strip h-[3px] w-full rounded-full" aria-hidden />
    </span>
  );
}

/** Thin translucent bar, like apple.com's global nav. Light only: cream behind, ink text. */
export function Nav() {
  return (
    <nav
      className="sticky top-0 z-50 h-12 border-b border-ink/8 bg-cream/80 text-ink backdrop-blur-xl backdrop-saturate-150"
      style={{ paddingTop: "env(safe-area-inset-top, 0px)" }}
    >
      <div className="mx-auto flex h-full max-w-6xl items-center gap-6 px-5">
        <NavLink to="/" aria-label="Home" className="opacity-90 transition-opacity hover:opacity-100">
          <Logo />
        </NavLink>
        <div className="ml-auto flex items-center gap-1 text-[13px]">
          {LINKS.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.end}
              className={({ isActive }) =>
                `rounded-full px-3 py-1.5 transition-colors ${isActive ? "bg-almond text-ink" : "text-ink/65 hover:text-ink"}`
              }
            >
              {l.label}
            </NavLink>
          ))}
        </div>
      </div>
    </nav>
  );
}
