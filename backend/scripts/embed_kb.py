"""embed_kb.py (prep step 5): validate the reviewed KB files and load them into MongoDB.

    python scripts/embed_kb.py --download-model   # once, with internet: caches all-MiniLM-L6-v2 locally
    python scripts/embed_kb.py --check            # validate only, write nothing
    python scripts/embed_kb.py                    # validate + load (replaces each speaker's/topic's docs)

Loads ONLY reviewed rows: dossier claims and statements, topic passages; plus every speaker digest and
the full source texts behind bank evidence (topic "_sources": provenance only, never retrieval).
A reviewed claim whose evidence quote is not verbatim in its source text, or whose metadata differs
from its source, is reported and skipped (the KB would refuse it anyway). Vectors are not stored:
the backend re-encodes everything with the one local model at warm-up (app/kb/store.py).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as dbm  # noqa: E402
from app.kb.store import SOURCE_TOPIC, ReviewedClaim  # noqa: E402
from app.models import BankHit, Claim, Evidence, Passage, SpeakerDigest, Statement  # noqa: E402
from app.verify import evidence_span  # noqa: E402
from scripts.kb_files import DOSSIERS, SOURCES, TOPICS, read_jsonl  # noqa: E402

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
META = ("url", "title", "publisher", "reliability")


def download_model() -> int:
    from sentence_transformers import SentenceTransformer

    SentenceTransformer(MODEL).encode(["ok"])
    SentenceTransformer(MODEL, local_files_only=True).encode(["ok"])
    print(f"{MODEL} cached locally; warm-up will load it offline")
    return 0


def collect() -> tuple[dict, list[str]]:
    errors: list[str] = []
    sources = {}
    for r in read_jsonl(SOURCES):
        sources[r["source_id"]] = Passage(passage_id=r["source_id"], text=r["text"], topic=SOURCE_TOPIC, source=Evidence(
            source_id=r["source_id"], title=r["title"], url=r["url"], publisher=r["publisher"], reliability=r["reliability"],
            quote="", published=r.get("published"), retrieved_at=r.get("retrieved_at") or ""))
    out = {"claims": [], "statements": [], "speaker_digests": [], "passages": [], "speakers": [], "topics": []}
    used_sources: set[str] = set()
    for d in sorted(p for p in DOSSIERS.iterdir() if p.is_dir()) if DOSSIERS.exists() else []:
        spk = d.name
        out["speakers"].append(spk)
        if (d / "digest.json").exists():
            out["speaker_digests"].append(SpeakerDigest.model_validate_json((d / "digest.json").read_text()))
        else:
            errors.append(f"{spk}: digest.json missing")
        for i, r in enumerate(read_jsonl(d / "claims.jsonl"), 1):
            if not r.get("reviewed"):
                continue
            hit = BankHit(claim=Claim(**r["claim"]), verdict_category=r["verdict_category"], explanation=r["explanation"],
                          evidence=[Evidence(**e) for e in r["evidence"]], similarity=1.0)
            bad = [e.source_id for e in hit.evidence if e.source_id not in sources
                   or not evidence_span(e, sources[e.source_id].text)
                   or any(getattr(e, k) != getattr(sources[e.source_id].source, k) for k in META)]
            if bad or not hit.explanation.strip() or hit.verdict_category not in "FMDV":
                errors.append(f"{spk}/claims.jsonl row {i}: evidence not verbatim in its source, or metadata differs ({', '.join(bad) or 'verdict/explanation'})")
                continue
            used_sources.update(e.source_id for e in hit.evidence)
            out["claims"].append(ReviewedClaim(speaker_id=spk, reviewed=True, hit=hit))
        for r in read_jsonl(d / "statements.jsonl"):
            if r.pop("reviewed", False):
                out["statements"].append(Statement(**r))
    out["passages"] = [sources[s] for s in sorted(used_sources)]
    for t in sorted(p for p in TOPICS.iterdir() if p.is_dir()) if TOPICS.exists() else []:
        rows = [r for r in read_jsonl(t / "passages.jsonl") if r.pop("reviewed", False)]
        if rows:
            out["topics"].append(t.name)
            out["passages"] += [Passage(**r) for r in rows]
    return out, errors


async def load(kb: dict) -> None:
    db = dbm.connect()
    if not await dbm.ping(db):
        raise SystemExit("MongoDB unreachable (check MONGODB_URI / Atlas network access)")
    await dbm.ensure_indexes(db)
    spk = {"speaker_id": {"$in": kb["speakers"]}}
    await db["claims"].delete_many(spk)
    await db["statements"].delete_many(spk)
    await db["passages"].delete_many({"topic": {"$in": kb["topics"] + [SOURCE_TOPIC]}})
    for name in ("claims", "statements", "passages"):
        if kb[name]:
            await db[name].insert_many([x.model_dump(mode="json") for x in kb[name]])
    for d in kb["speaker_digests"]:
        await db["speaker_digests"].replace_one({"speaker_id": d.speaker_id}, d.model_dump(mode="json"), upsert=True)


def main(a: argparse.Namespace) -> int:
    if a.download_model:
        return download_model()
    kb, errors = collect()
    for e in errors:
        print("SKIP", e)
    print(f"speakers {kb['speakers']}: {len(kb['claims'])} reviewed claims, {len(kb['statements'])} statements, "
          f"{len(kb['speaker_digests'])} digests; topics {kb['topics']}: {len(kb['passages'])} passages (incl. sources)")
    counts = {s: sum(c.speaker_id == s for c in kb["claims"]) for s in kb["speakers"]}
    if len(set(counts.values())) > 1:
        print(f"note: unequal bank depth per speaker {counts} (CONTEXT.md: equal depth per speaker)")
    if a.check:
        return 1 if errors else 0
    asyncio.run(load(kb))
    print("loaded into MongoDB")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true")
    p.add_argument("--download-model", action="store_true")
    sys.exit(main(p.parse_args()))
