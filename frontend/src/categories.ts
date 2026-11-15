import type { Annotation, Category } from "./types";

/** CONTEXT.md section 3. Color is always paired with the letter (color-blind safe). Radix step-11
 * hues: the light (cream) UI needs the darker step for 3:1 contrast and white chip letters. */
export const CATEGORIES: Record<Category, { label: string; color: string }> = {
  F: { label: "Factual error", color: "#CE2C31" },
  M: { label: "Misleading", color: "#CC4E00" },
  D: { label: "Data misuse", color: "#AB6400" },
  L: { label: "Logical fallacy", color: "#8145B5" },
  C: { label: "Differs from prior statement", color: "#0D74CE" },
  U: { label: "Unverifiable", color: "#60646C" },
  V: { label: "Verified", color: "#218358" },
};

export const FALLACY_LABELS: Record<string, string> = {
  ad_hominem: "Ad hominem",
  strawman: "Strawman",
  false_dilemma: "False dilemma",
  slippery_slope: "Slippery slope",
  red_herring: "Red herring",
  hasty_generalization: "Hasty generalization",
  whataboutism: "Whataboutism",
  post_hoc: "Post hoc",
};

export function categoryLabel(a: Pick<Annotation, "category" | "subtype">): string {
  if (a.category === "L" && a.subtype) return FALLACY_LABELS[a.subtype] ?? a.subtype;
  return CATEGORIES[a.category].label;
}

export function isStruck(a: Annotation, removed?: string): boolean {
  return Boolean(removed) || a.status === "withdrawn" || a.status === "killed";
}

/** Speaker ids are lowercase slugs ("el_sayed"); show them as names. */
export function speakerName(id: string): string {
  if (id === "moderator") return "Moderator";
  return id
    .split("_")
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join("-");
}

export function fmtTime(t: number | null | undefined): string {
  if (t == null || !Number.isFinite(t)) return "--:--";
  const s = Math.max(0, Math.floor(t));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
