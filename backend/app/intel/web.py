"""Code-side check of web evidence: fetch the cited page and confirm the quote is on it.
A model's quote or URL is never trusted on its own (ARCHITECTURE.md section 6)."""
from __future__ import annotations

import html
import logging
import re

import httpx

from app.verify import normalize_source

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 6.0
MAX_BYTES = 3_000_000
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; debate-factcheck/0.1)"}


def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    return normalize_source(html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw)))


def _loose(text: str) -> str:
    return re.sub(r"[^a-z0-9%$.]+", " ", normalize_source(text).lower()).strip()


async def fetch_text(url: str, client: httpx.AsyncClient | None = None) -> tuple[str, str] | None:
    """(final_url, visible text) or None. Follows redirects (Gemini grounding links redirect)."""
    if not url.startswith(("http://", "https://")):
        return None
    own = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=True, headers=_HEADERS)
    try:
        r = await client.get(url)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return None
        return str(r.url), page_text(r.text[:MAX_BYTES])
    except httpx.HTTPError:
        log.info("evidence fetch failed for a cited page")
        return None
    finally:
        if own:
            await client.aclose()


def quote_on_page(quote: str, text: str) -> bool:
    """Verbatim after whitespace/quote-mark normalization, or after dropping punctuation and case
    (HTML often splits sentences across tags). Short quotes never count."""
    q = normalize_source(quote)
    if len(q) < 20:
        return False
    return q in text or _loose(q) in _loose(text)
