"""Reviewed knowledge-base files in git (CONTEXT.md section 7) and their conversion to Mongo shapes.

data/
  raw/<speaker_id>/sources.jsonl        fetched source material {url,title,publisher,published,reliability,text,...}
  dossiers/<speaker_id>/
    digest.json                         SpeakerDigest
    claims.jsonl                        {reviewed, claim{text,decontextualized,entities,numbers,topic},
                                         verdict_category, explanation, evidence[Evidence]}
    statements.jsonl                    Statement (statement_id optional, generated)
  sources/sources.jsonl                 full retained source texts behind bank evidence (one per source_id)
  topics/<topic>/passages.jsonl         retrieval passages {reviewed, passage_id, text, source, topic}

Drafts are written with reviewed=false. A human flips reviewed=true row by row. Only reviewed claims
and passages are loaded into Mongo (scripts/embed_kb.py).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

DATA = Path(__file__).resolve().parents[2] / "data"
RAW, DOSSIERS, TOPICS, SOURCES = DATA / "raw", DATA / "dossiers", DATA / "topics", DATA / "sources" / "sources.jsonl"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return len(rows)


def source_id(url: str) -> str:
    return "src:" + hashlib.sha256(url.encode()).hexdigest()[:16]


def short_id(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def merge(existing: list[dict], new: list[dict], key) -> list[dict]:
    """Keep every existing row (reviewed edits are never overwritten); append new rows by key."""
    seen = {key(r) for r in existing}
    return existing + [r for r in new if key(r) not in seen and not seen.add(key(r))]
