import { Fragment } from "react";
import { CATEGORIES, isStruck } from "../categories";
import type { Annotation, RemovalStatus } from "../types";
import { FlagChip } from "./FlagChip";

interface Props {
  text: string;
  annotations: Annotation[];
  removed?: Record<string, RemovalStatus>;
  chipSize?: "sm" | "md";
}

/**
 * Segment text with each flagged span underlined in its category color, followed by its letter chip.
 * Solid underline = verified, dashed = candidate, struck = withdrawn/killed. Overlapping spans keep the
 * first underline; the later flag still gets its chip.
 */
export function AnnotatedText({ text, annotations, removed = {}, chipSize = "md" }: Props) {
  const parts: React.ReactNode[] = [];
  let pos = 0;
  const valid = annotations
    .filter((a) => a.span_char_end <= text.length && text.slice(a.span_char_start, a.span_char_end) === a.span_text)
    .sort((a, b) => a.span_char_start - b.span_char_start);
  const orphanChips: Annotation[] = annotations.filter((a) => !valid.includes(a));

  for (const a of valid) {
    const struck = isStruck(a, removed[a.annotation_id]);
    const chip = <FlagChip key={`c-${a.annotation_id}`} a={a} struck={struck} size={chipSize} />;
    if (a.span_char_start < pos) {
      parts.push(<Fragment key={`o-${a.annotation_id}`}> {chip}</Fragment>);
      continue;
    }
    if (a.span_char_start > pos) parts.push(text.slice(pos, a.span_char_start));
    const { color } = CATEGORIES[a.category];
    parts.push(
      <Fragment key={a.annotation_id}>
        <span
          className={struck ? "line-through decoration-2 opacity-60" : ""}
          style={
            struck
              ? { textDecorationColor: color }
              : {
                  textDecorationLine: "underline",
                  textDecorationColor: color,
                  textDecorationThickness: a.status === "verified" ? "3px" : "2px",
                  textDecorationStyle: a.status === "verified" ? "solid" : "dashed",
                  textUnderlineOffset: "4px",
                }
          }
        >
          {text.slice(a.span_char_start, a.span_char_end)}
        </span>{" "}
        {chip}
      </Fragment>,
    );
    pos = a.span_char_end;
  }
  if (pos < text.length) parts.push(text.slice(pos));
  for (const a of orphanChips) {
    parts.push(
      <Fragment key={`x-${a.annotation_id}`}>
        {" "}
        <FlagChip a={a} struck={isStruck(a, removed[a.annotation_id])} size={chipSize} />
      </Fragment>,
    );
  }
  return <>{parts}</>;
}
