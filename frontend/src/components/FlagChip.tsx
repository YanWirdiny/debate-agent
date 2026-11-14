import { useEffect, useRef, useState } from "react";
import { CATEGORIES, categoryLabel } from "../categories";
import type { Annotation } from "../types";
import { EvidencePopover } from "./EvidencePopover";

interface Props {
  a: Annotation;
  struck?: boolean;
  size?: "sm" | "md";
  /** Open the evidence popover on click (audience + operator). */
  popover?: boolean;
  align?: "left" | "right";
}

/** Letter chip: solid = verified, outlined = candidate, struck = withdrawn/killed (CONTEXT.md section 3). */
export function FlagChip({ a, struck = false, size = "md", popover = true, align = "left" }: Props) {
  const { color } = CATEGORIES[a.category];
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const solid = a.status === "verified" && !struck;

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const dims = size === "sm" ? "h-5 min-w-5 text-[11px] px-1" : "h-6 min-w-6 text-xs px-1.5";
  return (
    <span ref={ref} className="relative inline-flex align-middle">
      <button
        type="button"
        onClick={() => popover && setOpen((o) => !o)}
        title={`${categoryLabel(a)} (${a.status})`}
        aria-label={`${categoryLabel(a)}, ${a.status}. Show evidence`}
        className={`inline-flex items-center justify-center rounded font-bold leading-none ${dims} ${struck ? "line-through opacity-50" : ""} ${popover ? "cursor-pointer" : "cursor-default"}`}
        style={{
          backgroundColor: solid ? color : "transparent",
          color: solid ? "#fff" : color,
          border: `1.5px ${a.status === "verified" ? "solid" : "dashed"} ${color}`,
        }}
      >
        {a.category}
        {a.repeat_count > 1 && <sup className="ml-0.5 text-[9px]">×{a.repeat_count}</sup>}
      </button>
      {open && <EvidencePopover a={a} struck={struck} align={align} />}
    </span>
  );
}
