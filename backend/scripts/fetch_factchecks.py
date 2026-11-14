"""fetch_factchecks.py (prep step 1): collect fact-checks of a speaker into data/raw/<speaker_id>/sources.jsonl.

    python scripts/fetch_factchecks.py harris --name "Kamala Harris" --query economy --query tariffs
    python scripts/fetch_factchecks.py trump --name "Donald Trump" --url https://www.bls.gov/...   # add any page

Google Fact Check Tools API (claims:search, needs GOOGLE_FACTCHECK_KEY) finds fact-check articles about
the speaker; each article page is fetched and its visible text kept as source material. --url adds any
other page (official data, transcripts). Rerunnable: rows already present (same url) are kept.
Nothing here is a verdict; build_dossier.py drafts from this material and a human reviews.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.db import lenient_settings  # noqa: E402
from app.intel import web  # noqa: E402
from scripts.kb_files import RAW, merge, read_jsonl, write_jsonl  # noqa: E402

API = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
FACTCHECKERS = ("politifact", "factcheck.org", "apnews", "reuters", "washingtonpost", "snopes", "fullfact")
PRIMARY = (".gov", "bls.gov", "bea.gov", "census.gov", "cbo.gov")


def reliability(url: str) -> str:
    host = url.split("/")[2] if "://" in url else url
    if any(host.endswith(p) for p in PRIMARY):
        return "primary"
    return "factchecker" if any(f in host for f in FACTCHECKERS) else "media"


async def search(client: httpx.AsyncClient, key: str, query: str, max_age_days: int | None, pages: int) -> list[dict]:
    out, token = [], None
    for _ in range(pages):
        params = {"query": query, "languageCode": "en", "pageSize": 50, "key": key}
        if max_age_days:
            params["maxAgeDays"] = max_age_days
        if token:
            params["pageToken"] = token
        r = await client.get(API, params=params)
        r.raise_for_status()
        body = r.json()
        out += body.get("claims", [])
        token = body.get("nextPageToken")
        if not token:
            break
    return out


def review_rows(claims: list[dict], speaker_name: str) -> list[dict]:
    rows = []
    last = speaker_name.split()[-1].lower()
    for c in claims:
        if last not in (c.get("claimant") or "").lower():
            continue  # only claims attributed to this speaker
        for rv in c.get("claimReview", []):
            if rv.get("url"):
                rows.append({"url": rv["url"], "title": rv.get("title") or "", "publisher": (rv.get("publisher") or {}).get("name", ""),
                             "published": (rv.get("reviewDate") or "")[:10] or None, "reliability": reliability(rv["url"]),
                             "claim_text": c.get("text", ""), "claim_date": (c.get("claimDate") or "")[:10] or None,
                             "rating": rv.get("textualRating", "")})
    return rows


async def main(a: argparse.Namespace) -> int:
    out = RAW / a.speaker_id / "sources.jsonl"
    existing = read_jsonl(out)
    have = {r["url"] for r in existing}
    found: list[dict] = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=web._HEADERS) as client:
        if a.query or not a.url:
            key = lenient_settings().google_factcheck_key.get_secret_value()
            if not key:
                print("GOOGLE_FACTCHECK_KEY is empty (backend/.env); use --url only, or set the key")
                return 1
            for q in a.query or [""]:
                claims = await search(client, key, f"{a.name} {q}".strip(), a.max_age_days, a.pages)
                found += review_rows(claims, a.name)
        found += [{"url": u, "title": "", "publisher": "", "published": None, "reliability": reliability(u)} for u in a.url]
        todo = [r for r in {r["url"]: r for r in found}.values() if r["url"] not in have]
        pages = await asyncio.gather(*(web.fetch_text(r["url"], client) for r in todo))
    added = []
    for r, page in zip(todo, pages):
        if page is None or len(page[1]) < 400:
            print(f"skip (fetch failed or empty): {r['url']}")
            continue
        added.append(r | {"url": page[0], "text": page[1], "fetched": dt.date.today().isoformat()})
    n = write_jsonl(out, merge(existing, added, key=lambda r: r["url"]))
    print(f"{a.speaker_id}: {len(added)} new source(s), {n} total -> {out}")
    print("next: python scripts/build_dossier.py", a.speaker_id)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("speaker_id")
    p.add_argument("--name", required=True, help="speaker's full name as fact-checkers write it")
    p.add_argument("--query", action="append", default=[], help="topic words added to the name (repeatable)")
    p.add_argument("--url", action="append", default=[], help="extra page to keep as source (repeatable)")
    p.add_argument("--max-age-days", type=int, default=None)
    p.add_argument("--pages", type=int, default=2)
    sys.exit(asyncio.run(main(p.parse_args())))
