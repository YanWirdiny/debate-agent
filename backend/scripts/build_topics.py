"""build_topics.py (prep step 4): retrieval passages per topic shard, for HUMAN REVIEW.

    python scripts/build_topics.py economy_inflation --url https://www.bls.gov/... --url https://www.cbo.gov/...
    python scripts/build_topics.py economy_inflation --urls-file data/topics/economy_inflation/urls.txt

Fetches each page (official data first, then fact-checkers), splits its visible text into short
passages (1 to 3 sentences, 120 to 700 characters) and keeps those with a figure in them (--all keeps
every passage). Drafts are appended to data/topics/<topic>/passages.jsonl with reviewed=false; review,
set reviewed=true on the passages worth keeping, then run scripts/embed_kb.py.
Shards to cover first: what the clips discuss (economy_v2: taxes/deficit, tariffs/China).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.intel import web  # noqa: E402
from scripts.fetch_factchecks import reliability  # noqa: E402
from scripts.kb_files import TOPICS, merge, read_jsonl, short_id, source_id, write_jsonl  # noqa: E402

MIN_CHARS, MAX_CHARS = 120, 700
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“])")


def passages_from(text: str, keep_all: bool) -> list[str]:
    sentences = [s.strip() for s in _SENT.split(text) if s.strip()]
    out, buf = [], ""
    for s in sentences:
        if len(s) > MAX_CHARS:
            buf = ""
            continue  # nav blobs and tables flattened into one "sentence"
        if buf and len(buf) + len(s) + 1 > MAX_CHARS or buf.count(". ") >= 2:
            out.append(buf)
            buf = ""
        buf = f"{buf} {s}".strip()
    if buf:
        out.append(buf)
    return [p for p in out if len(p) >= MIN_CHARS and (keep_all or re.search(r"\d", p))]


async def main(a: argparse.Namespace) -> int:
    urls = list(a.url)
    if a.urls_file:
        urls += [l.split("|")[0].strip() for l in Path(a.urls_file).read_text().splitlines() if l.strip() and not l.startswith("#")]
    if not urls:
        print("give --url or --urls-file")
        return 1
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=web._HEADERS) as client:
        pages = await asyncio.gather(*(web.fetch_text(u, client) for u in urls))
    drafts = []
    for url, page in zip(urls, pages):
        if page is None:
            print(f"skip (fetch failed): {url}")
            continue
        final, text = page
        title = a.title or final
        found = passages_from(text, a.all)
        for n, p in enumerate(found):
            sid = f"{source_id(final)}#{n}"
            drafts.append({"reviewed": False, "passage_id": f"{a.topic}:{short_id(final, p)}", "text": p, "topic": a.topic,
                           "source": {"source_id": sid, "title": title, "url": final, "publisher": a.publisher or final.split("/")[2],
                                      "reliability": a.reliability or reliability(final), "quote": p,
                                      "published": a.published, "retrieved_at": dt.date.today().isoformat()}})
        print(f"{len(found):3d} passages  {final}")
    out = TOPICS / a.topic / "passages.jsonl"
    n = write_jsonl(out, merge(read_jsonl(out), drafts, key=lambda r: r["passage_id"]))
    print(f"{a.topic}: {n} passages in {out} (review: set reviewed=true), then python scripts/embed_kb.py")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("topic")
    p.add_argument("--url", action="append", default=[])
    p.add_argument("--urls-file")
    p.add_argument("--title", default="")
    p.add_argument("--publisher", default="")
    p.add_argument("--reliability", choices=["primary", "factchecker", "media"])
    p.add_argument("--published", default=None, help="YYYY-MM-DD of the source")
    p.add_argument("--all", action="store_true", help="keep passages without figures too")
    sys.exit(asyncio.run(main(p.parse_args())))
