"""Session report (P1), CONTEXT.md 5.8: deterministic, built only from the event log. No LLM.

`build_report(events)` -> JSON for the frontend Report view; `render_html(report)` -> a plain page
to print to PDF. Rules: no ranking anywhere (speakers alphabetical, categories in a fixed order,
items in time order, no per-speaker totals); moderator never appears as a subject; killed flags
are not shown, only counted under operator actions.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any

from app.eventlog import verify_chain
from app.metrics import HOT_PATHS, percentile
from app.models import Annotation, Event, Segment

CATEGORY_ORDER = ["F", "M", "D", "L", "V"]  # C and U have their own sections
CATEGORY_LABELS = {
    "F": "False", "M": "Misleading", "D": "Data misuse", "L": "Logical fallacy",
    "V": "Verified accurate", "C": "Differs from prior statement", "U": "Could not be verified live",
}


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def build_report(events: list[Event], eval_results: dict[str, Any] | None = None) -> dict[str, Any]:
    if not events:
        raise ValueError("empty event log")
    events = sorted(events, key=lambda e: e.seq)
    start: dict[str, Any] = {}
    end: dict[str, Any] = {}
    segments: dict[str, Segment] = {}
    final: dict[str, Annotation] = {}
    first: dict[str, Annotation] = {}
    kills = 0
    pending: dict[str, set[str]] = {}
    for e in events:
        payload = json.loads(e.payload_json)
        if e.type == "session_start":
            start = payload
        elif e.type == "session_end":
            end = payload
        elif e.type == "segment":
            seg = Segment.model_validate(payload)
            segments[seg.segment_id] = seg
        elif e.type in ("annotation", "annotation_update"):
            a = Annotation.model_validate(payload)
            final[a.annotation_id] = a
            first.setdefault(a.annotation_id, a)
        elif e.type == "kill":
            kills += 1
        elif e.type == "pending":
            pending.setdefault(payload["lane"], set()).add(payload["segment_id"])

    moderators = {s.speaker_id for s in segments.values() if s.role == "moderator"}
    participants = sorted({s.speaker_id for s in segments.values() if s.role == "speaker"})

    def item(a: Annotation) -> dict[str, Any]:
        seg = segments.get(a.segment_id)
        return {
            "annotation_id": a.annotation_id, "speaker_id": a.speaker_id, "category": a.category,
            "subtype": a.subtype, "t_start": seg.t_start if seg else None, "segment_text": seg.text if seg else "",
            "span_text": a.span_text, "opponent_span": a.opponent_span, "explanation": a.explanation,
            "evidence": [ev.model_dump() for ev in a.evidence], "status": a.status, "path": a.path,
            "confidence": a.confidence, "repeat_count": a.repeat_count, "latency_ms": first[a.annotation_id].latency_ms,
        }

    shown = sorted(
        (a for a in final.values() if a.speaker_id not in moderators and a.status != "killed"),
        key=lambda a: (segments[a.segment_id].t_start if a.segment_id in segments else 0, a.annotation_id),
    )
    highlights = []
    for spk in participants:
        cats = []
        for cat in CATEGORY_ORDER:
            items = [item(a) for a in shown if a.speaker_id == spk and a.category == cat and a.status == "verified"]
            if items:
                cats.append({"category": cat, "label": CATEGORY_LABELS[cat], "items": items})
        highlights.append({"speaker_id": spk, "categories": cats})

    firsts = [a for a in first.values() if a.path in HOT_PATHS]
    bank = [a.latency_ms for a in firsts if a.path == "bank"]
    last_seg_end = max((s.t_end for s in segments.values()), default=0.0)
    return {
        "header": {
            "session_id": events[0].session_id,
            "clip_id": start.get("clip_id"),
            "source": start.get("source"),
            "intel": start.get("intel"),
            "replay_of": start.get("replay_of"),
            "started_at": _iso(start.get("t0_wall")),
            "duration_s": round(last_seg_end, 1),
            "participants": participants,
            "model_ids": sorted({a.model_id for a in final.values() if a.model_id}),
            "prompt_hashes": sorted({a.prompt_hash for a in final.values() if a.prompt_hash}),
            "event_count": len(events),
            "log_head_hash": events[-1].hash,
            "chain_valid": verify_chain(events),
            "complete": bool(end),
        },
        "highlights": highlights,
        "differs_from_prior": [item(a) for a in shown if a.category == "C" and a.status == "verified"],
        "unverifiable": [item(a) for a in shown if a.category == "U"],
        "operator_actions": {
            "killed": sum(1 for a in final.values() if a.status == "killed"),
            "kill_events": kills,
            "withdrawn": sum(1 for a in final.values() if a.status == "withdrawn"),
        },
        "latency": {
            "p50_ms": percentile([a.latency_ms for a in firsts], 50),
            "p95_ms": percentile([a.latency_ms for a in firsts], 95),
            "p95_bank_ms": percentile(bank, 95),
            "n": len(firsts),
        },
        "unchecked": {
            "speaker_segments": sum(1 for s in segments.values() if s.role == "speaker"),
            "by_lane": {lane: len(ids) for lane, ids in sorted(pending.items())},
        },
        "session_metrics": end.get("metrics"),
        "methodology": {
            "eval": eval_results,
            "notes": [
                "Built only from the hash-chained event log; no model writes this report.",
                "Only flags verified against quoted evidence (or, for fallacies, the quoted span) are listed as highlights.",
                "Moderator speech is context and is never flagged.",
                "No ranking: speakers are alphabetical and flags are in time order.",
            ],
        },
    }


# --- HTML (print to PDF) ------------------------------------------------------------------


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _mmss(t: float | None) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}" if t is not None else ""


def _item_html(it: dict[str, Any]) -> str:
    ev = "".join(
        f'<li>&ldquo;{_e(e["quote"])}&rdquo; &mdash; <a href="{_e(e["url"])}">{_e(e["title"])}</a>, {_e(e["publisher"])}'
        f'{", " + _e(e["published"]) if e.get("published") else ""}</li>'
        for e in it["evidence"]
    )
    rep = f' <span class="muted">(said {it["repeat_count"]} times)</span>' if it["repeat_count"] > 1 else ""
    sub = f' <span class="muted">{_e(it["subtype"])}</span>' if it["subtype"] else ""
    opp = f'<p class="muted">Opponent said: &ldquo;{_e(it["opponent_span"])}&rdquo;</p>' if it.get("opponent_span") else ""
    return (f'<div class="item"><p><span class="t">{_mmss(it["t_start"])}</span> &ldquo;{_e(it["span_text"])}&rdquo;{sub}{rep}</p>'
            f'<p>{_e(it["explanation"])}</p>{opp}{"<ul>" + ev + "</ul>" if ev else ""}</div>')


def render_html(r: dict[str, Any]) -> str:
    h = r["header"]
    parts = [f"<h1>Session report: {_e(h['clip_id'])}</h1>", '<table class="meta">']
    for k in ("session_id", "started_at", "duration_s", "participants", "source", "replay_of", "model_ids",
              "prompt_hashes", "event_count", "log_head_hash", "chain_valid"):
        v = h.get(k)
        if v in (None, [], ""):
            continue
        parts.append(f"<tr><th>{_e(k.replace('_', ' '))}</th><td>{_e(', '.join(v) if isinstance(v, list) else v)}</td></tr>")
    parts.append("</table>")
    if h["replay_of"]:
        parts.append(f'<p class="note">Replay of recorded session {_e(h["replay_of"])}, not a live run.</p>')
    parts.append("<h2>Highlights by speaker</h2>")
    for sp in r["highlights"]:
        parts.append(f"<h3>{_e(sp['speaker_id'])}</h3>")
        if not sp["categories"]:
            parts.append('<p class="muted">No verified flags.</p>')
        for c in sp["categories"]:
            parts.append(f"<h4>{_e(c['label'])} ({_e(c['category'])})</h4>" + "".join(_item_html(i) for i in c["items"]))
    parts.append("<h2>Differs from prior statement</h2>")
    parts.append("".join(f"<h4>{_e(i['speaker_id'])}</h4>{_item_html(i)}" for i in r["differs_from_prior"]) or '<p class="muted">None.</p>')
    parts.append("<h2>Could not be verified live</h2>")
    parts.append("".join(f"<h4>{_e(i['speaker_id'])}</h4>{_item_html(i)}" for i in r["unverifiable"]) or '<p class="muted">None.</p>')
    lat, ops = r["latency"], r["operator_actions"]
    parts.append("<h2>Methodology</h2><ul>" + "".join(f"<li>{_e(n)}</li>" for n in r["methodology"]["notes"]))
    parts.append(f"<li>Latency, end of sentence to flag: p50 {lat['p50_ms']:.0f} ms, p95 {lat['p95_ms']:.0f} ms "
                 f"(bank hits p95 {lat['p95_bank_ms']:.0f} ms, n={lat['n']}).</li>")
    parts.append(f"<li>Operator removed {ops['killed']} flag(s); {ops['withdrawn']} withdrawn by later evidence.</li>")
    un = r["unchecked"]
    if un["by_lane"]:
        lanes = ", ".join(f"{_e(lane)} {n}" for lane, n in un["by_lane"].items())
        parts.append(f"<li>Not checked (lane timed out or failed), out of {un['speaker_segments']} speaker sentences: {lanes}. "
                     "An unflagged sentence is not necessarily accurate.</li>")
    else:
        parts.append(f"<li>Every one of the {un['speaker_segments']} speaker sentences was checked by every lane.</li>")
    parts.append("</ul>")
    ev = r["methodology"].get("eval")
    if ev and ev.get("per_category"):
        parts.append("<table class='meta'><tr><th>category</th><th>precision</th><th>recall</th><th>n</th></tr>")
        for cat, m in sorted(ev["per_category"].items()):
            parts.append(f"<tr><td>{_e(cat)}</td><td>{_e(m.get('precision'))}</td><td>{_e(m.get('recall'))}</td><td>{_e(m.get('n'))}</td></tr>")
        parts.append("</table>")
    style = ("body{font:15px/1.5 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a}"
             ".muted{color:#666}.t{font-variant-numeric:tabular-nums;color:#666;margin-right:.5rem}"
             ".item{border-left:3px solid #ccc;padding-left:.75rem;margin:.75rem 0}.meta th{text-align:left;padding-right:1rem}"
             ".note{background:#fff4d6;padding:.5rem}@media print{a{color:inherit}}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Report {_e(h['session_id'])}</title>"
            f"<style>{style}</style></head><body>{''.join(parts)}</body></html>")
