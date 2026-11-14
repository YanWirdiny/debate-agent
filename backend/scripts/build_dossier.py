"""build_dossier.py (prep step 2): draft a speaker's claim bank and dated statements for HUMAN REVIEW.

    python scripts/build_dossier.py harris --name "Kamala Harris" --office "Vice President"
    python scripts/build_dossier.py harris --limit 5      # first 5 unprocessed sources only

For each source in data/raw/<speaker_id>/sources.jsonl, one Gemini `build_bank` call drafts claims
and statements. Code checks before anything is written: every evidence quote must be verbatim in that
source's text, verdict letters must be F/M/D/V, statements need a date. Rejected drafts are counted.

Writes (merging, never overwriting rows already there, so review edits are safe):
  data/dossiers/<id>/claims.jsonl      reviewed=false drafts
  data/dossiers/<id>/statements.jsonl  reviewed=false drafts
  data/dossiers/<id>/digest.json       skeleton if missing (fill name/office/positions by hand)
  data/sources/sources.jsonl           the full source text behind each evidence quote
Then: open claims.jsonl, check every row against its source, set "reviewed": true (equal depth per speaker).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import Field  # noqa: E402

from app.llm.client import LLMError, gemini_json  # noqa: E402
from app.models import ContractModel  # noqa: E402
from app.verify import normalize_source  # noqa: E402
from scripts.kb_files import DOSSIERS, RAW, SOURCES, merge, read_jsonl, short_id, source_id, write_jsonl  # noqa: E402

MAX_SOURCE_CHARS = 120_000


class DraftEvidence(ContractModel):
    quote: str
    url: str
    title: str
    publisher: str
    published: str | None
    reliability: Literal["primary", "factchecker", "media"]


class DraftClaim(ContractModel):
    text: str
    decontextualized: str
    entities: list[str]
    numbers: list[str]
    topic: str | None
    verdict_category: Literal["F", "M", "D", "V"]
    explanation: str
    evidence: list[DraftEvidence] = Field(min_length=1)


class DraftStatement(ContractModel):
    text: str
    date: str
    source_url: str
    topic: str | None


class Bank(ContractModel):
    claims: list[DraftClaim]
    statements: list[DraftStatement]


def source_meta(src: dict, bank: Bank) -> dict:
    """One metadata record per source, shared by the sources file and every evidence citing it
    (the KB requires evidence metadata to equal its source passage's metadata)."""
    first = next((e for c in bank.claims for e in c.evidence), None)
    return {"source_id": source_id(src["url"]), "url": src["url"],
            "title": src.get("title") or (first.title if first else "") or src["url"],
            "publisher": src.get("publisher") or (first.publisher if first else ""),
            "reliability": src.get("reliability") or (first.reliability if first else "media"),
            "published": src.get("published") or (first.published if first else None),
            "retrieved_at": src.get("fetched") or ""}


def check(bank: Bank, src: dict, speaker_id: str) -> tuple[list[dict], list[dict], int]:
    text = normalize_source(src["text"])
    meta = source_meta(src, bank)
    claims, statements, rejected = [], [], 0
    for c in bank.claims:
        if not all(e.quote.strip() and normalize_source(e.quote) in text for e in c.evidence):
            rejected += 1
            continue
        evidence = [meta | {"quote": e.quote.strip()} for e in c.evidence]
        claims.append({"reviewed": False, "claim": {"text": c.text, "decontextualized": c.decontextualized,
                       "entities": c.entities, "numbers": c.numbers, "topic": c.topic},
                       "verdict_category": c.verdict_category, "explanation": c.explanation, "evidence": evidence})
    for s in bank.statements:
        if len(s.date) < 4 or normalize_source(s.text) not in text:
            rejected += 1
            continue
        statements.append({"reviewed": False, "statement_id": f"{speaker_id}:{short_id(s.text, s.date)}",
                           "speaker_id": speaker_id, "text": s.text, "date": s.date,
                           "source_url": s.source_url or src["url"], "topic": s.topic})
    return claims, statements, rejected


async def main(a: argparse.Namespace) -> int:
    sources = read_jsonl(RAW / a.speaker_id / "sources.jsonl")
    if not sources:
        print(f"no sources for {a.speaker_id}: run scripts/fetch_factchecks.py first")
        return 1
    d = DOSSIERS / a.speaker_id
    digest = d / "digest.json"
    if not digest.exists():
        d.mkdir(parents=True, exist_ok=True)
        digest.write_text(json.dumps({"speaker_id": a.speaker_id, "name": a.name or a.speaker_id, "office": a.office or "",
                                      "key_positions": [], "summary": ""}, indent=2) + "\n")
    claims_f, stmts_f = d / "claims.jsonl", d / "statements.jsonl"
    done = {e["url"] for r in read_jsonl(claims_f) for e in r["evidence"]} | {r["source_url"] for r in read_jsonl(stmts_f)}
    todo = [s for s in sources if s["url"] not in done][: a.limit or None]
    new_claims, new_stmts, used, rejected, failed = [], [], [], 0, 0
    for i, src in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {src['url']}")
        try:
            r = await gemini_json("build_bank", {
                "speaker_id": a.speaker_id, "speaker_name": a.name or a.speaker_id,
                "source": {k: src.get(k) for k in ("url", "title", "publisher", "published", "claim_text", "rating")},
                "material": src["text"][:MAX_SOURCE_CHARS],
            }, Bank, timeout=a.timeout)
        except LLMError:
            failed += 1
            continue
        bank = Bank.model_validate(r.data)
        c, s, rej = check(bank, src, a.speaker_id)
        new_claims += c
        new_stmts += s
        rejected += rej
        if c:
            used.append(source_meta(src, bank) | {"text": src["text"]})
    n_claims = write_jsonl(claims_f, merge(read_jsonl(claims_f), new_claims, key=lambda r: r["claim"]["text"]))
    n_stmts = write_jsonl(stmts_f, merge(read_jsonl(stmts_f), new_stmts, key=lambda r: r["statement_id"]))
    write_jsonl(SOURCES, merge(read_jsonl(SOURCES), used, key=lambda r: r["source_id"]))
    print(f"{a.speaker_id}: +{len(new_claims)} claim drafts ({n_claims} total), +{len(new_stmts)} statement drafts "
          f"({n_stmts} total); {rejected} rejected by quote/date checks; {failed} source(s) failed")
    print(f"next: review {claims_f} and {stmts_f} (set reviewed=true), then python scripts/embed_kb.py")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("speaker_id")
    p.add_argument("--name", default="")
    p.add_argument("--office", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--timeout", type=float, default=90.0)
    sys.exit(asyncio.run(main(p.parse_args())))
