"""run_eval.py (prep step 7): precision/recall per category on hand-labeled sentences.

    python scripts/run_eval.py                       # data/eval/labeled.jsonl, real lanes + KB from Mongo
    python scripts/run_eval.py --no-save --verbose   # print only

labeled.jsonl rows: {"text", "speaker_id", "expected_category": "F"|"M"|"D"|"L"|"C"|"U"|"V"|null,
                     "previous": [optional earlier sentences by the same speaker, oldest first]}
Each sentence runs through the real claims (+consistency) and fallacy lanes exactly as in a session
(no async plane). A category counts as predicted if any annotation of that category is emitted.
Writes {"per_category": {cat: {precision, recall, n}}, ...} to Mongo `eval_results` (newest wins in
the report's methodology section) and data/eval/results.json. Same thresholds for every speaker.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as dbm  # noqa: E402
from app.kb.store import KnowledgeBase  # noqa: E402
from app.lanes import claims, fallacy  # noqa: E402
from app.models import Annotation, Category, ClaimRecord, LaneContext, Segment  # noqa: E402
from scripts.kb_files import DATA, read_jsonl  # noqa: E402

CATEGORIES: tuple[Category, ...] = ("F", "M", "D", "L", "C", "U", "V")


def score(rows: list[tuple[str | None, set[str]]]) -> dict[str, dict]:
    out = {}
    for c in CATEGORIES:
        tp = sum(1 for exp, pred in rows if exp == c and c in pred)
        fp = sum(1 for exp, pred in rows if exp != c and c in pred)
        fn = sum(1 for exp, pred in rows if exp == c and c not in pred)
        n = sum(1 for exp, _ in rows if exp == c)
        if n or fp:
            out[c] = {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                      "recall": round(tp / n, 3) if n else None, "n": n, "tp": tp, "fp": fp, "fn": fn}
    return out


async def predict(row: dict, i: int, kb: KnowledgeBase, timeout: float) -> tuple[set[str], list[Annotation], float]:
    def seg(text: str, n: int) -> Segment:
        return Segment(segment_id=f"eval{i}:{n}", session_id=f"eval{i}", speaker_id=row["speaker_id"], role="speaker",
                       text=text, t_start=n, t_end=n + 1, stt_confidence=1.0)
    previous = [seg(t, n) for n, t in enumerate(row.get("previous", []))]
    s = seg(row["text"], len(previous))
    emitted: list[Annotation | ClaimRecord] = []
    ledger: list[ClaimRecord] = []
    for p in previous:  # earlier sentences only build the claim ledger (consistency)
        out: list = []
        try:
            await asyncio.wait_for(claims.run_claims(p, LaneContext(claim_ledger=ledger), kb, out.append, asyncio.Queue()), timeout)
        except Exception:
            pass
        ledger += [x for x in out if isinstance(x, ClaimRecord)]
    digest = kb.digest(row["speaker_id"])
    t = time.perf_counter()
    await asyncio.gather(
        asyncio.wait_for(claims.run_claims(s, LaneContext(recent_segments=previous[-3:], speaker_digest=digest,
                                                          claim_ledger=ledger), kb, emitted.append, asyncio.Queue()), timeout),
        asyncio.wait_for(fallacy.run_fallacy(s, LaneContext(speaker_turn=previous + [s], speaker_digest=digest), emitted.append), timeout),
        return_exceptions=True)
    anns = [x for x in emitted if isinstance(x, Annotation)]
    return {a.category for a in anns}, anns, (time.perf_counter() - t) * 1000


async def main(a: argparse.Namespace) -> int:
    rows = read_jsonl(Path(a.file))
    if not rows:
        print(f"no labeled rows in {a.file}")
        return 1
    db = dbm.connect()
    if not await dbm.ping(db):
        print("MongoDB unreachable: the eval needs the real KB")
        return 1
    kb = await KnowledgeBase.load(db, sorted({r["speaker_id"] for r in rows}), [])
    results, models, latencies = [], set(), []
    for i, row in enumerate(rows):
        pred, anns, ms = await predict(row, i, kb, a.timeout)
        latencies.append(ms)
        models |= {x.model_id for x in anns if x.model_id}
        results.append((row.get("expected_category"), pred))
        if a.verbose or (row.get("expected_category") or None) not in pred | ({None} if not pred else set()):
            print(f"{'ok ' if (row.get('expected_category') in pred) or (not pred and not row.get('expected_category')) else 'BAD'} "
                  f"expected={row.get('expected_category')} predicted={sorted(pred) or '-'} {ms:.0f} ms  {row['text'][:80]}")
    per = score(results)
    doc = {"created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "n_sentences": len(rows), "per_category": per,
           "model_ids": sorted(models), "latency_p50_ms": sorted(latencies)[len(latencies) // 2],
           "file": str(Path(a.file).name)}
    print(json.dumps(per, indent=2))
    if not a.no_save:
        await db["eval_results"].insert_one(dict(doc))
        (DATA / "eval" / "results.json").write_text(json.dumps(doc, indent=2) + "\n")
        print("saved to Mongo eval_results and data/eval/results.json")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", default=str(DATA / "eval" / "labeled.jsonl"))
    p.add_argument("--timeout", type=float, default=5.0, help="per-lane budget (the live 1.2 s is checked by metrics)")
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--verbose", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
