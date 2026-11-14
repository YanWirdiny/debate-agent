"""Warm-up (ARCHITECTURE.md section 8, steps 2 and 4). Called by P1's runner after the KB is loaded,
before `ready`. Fails loudly with a fix-it message: better at warm-up than mid-demo.

- Embedding model: one dummy encode (loads the local all-MiniLM-L6-v2 weights).
- One dummy Groq call per hot prompt, concurrently (connection + provider prefix cache).
  claim_extract must succeed; the others only warn.
- Gemini explicit cache: skipped. Digests + definitions are far below the minimum cacheable size,
  so implicit caching covers them (ARCHITECTURE.md: "if above the model's minimum size").
"""
from __future__ import annotations

import asyncio
import logging
import time

from app.interfaces import KnowledgeBase
from app.lanes.claims import Extraction, Stance
from app.lanes.consistency import NLI
from app.lanes.fallacy import FallacyResult
from app.llm.client import groq_json

log = logging.getLogger(__name__)

WARM_TIMEOUT_S = 8.0
EMBED_HINT = ("embedding model not available locally; run `python scripts/embed_kb.py --download-model` "
              "once (needs internet) before the demo")

_DUMMIES = {
    "claim_extract": ({"sentence": "Thank you.", "recent_segments": [], "last_question": None,
                       "rolling_summary": "", "speaker_digest": None}, Extraction),
    "fallacy": ({"speaker_digest": None, "last_question": None, "opponent_turn": "", "speaker_turn": "",
                 "sentence": "Thank you."}, FallacyResult),
    "stance": ({"claim": "Warm-up.", "passages": [{"passage_id": "p0", "publisher": "none", "published": None,
                                                   "text": "Unrelated text."}]}, Stance),
    "nli": ({"claim": "Warm-up.", "statements": [{"statement_id": "s0", "date": "2000-01-01",
                                                  "text": "Unrelated text."}]}, NLI),
}


async def warm_up(kb: KnowledgeBase, speaker_ids: list[str]) -> None:
    if kb is None:
        raise ValueError("real intel needs the knowledge base (check MONGODB_URI, run scripts/embed_kb.py)")
    t = time.perf_counter()
    try:
        for text in ("warm up", "The deficit rose by 5 trillion dollars.", "Prices fell 2 percent."):
            await asyncio.to_thread(kb.search_passages, text, None, 1)  # first encodes are slow; pay them now
    except OSError as e:  # sentence-transformers with local_files_only and no cached weights
        raise ValueError(EMBED_HINT) from e
    embed_ms = (time.perf_counter() - t) * 1000

    async def one(prompt_id: str):
        variables, schema = _DUMMIES[prompt_id]
        return await groq_json(prompt_id, variables, schema, timeout=WARM_TIMEOUT_S)

    t = time.perf_counter()
    results = await asyncio.gather(*(one(p) for p in _DUMMIES), return_exceptions=True)
    failed = [p for p, r in zip(_DUMMIES, results) if isinstance(r, BaseException)]
    if "claim_extract" in failed:
        raise ValueError("Groq warm-up call failed: check GROQ_API_KEY and LLM_MODEL_FAST "
                         "(or LLM_PROVIDER=gemini with GEMINI_API_KEY)")
    if failed:
        log.warning("warm-up: prompts failed (lanes will show pending if this persists): %s", ", ".join(failed))
    log.info("warm-up: speakers=%s embed=%.0f ms llm=%.0f ms", ",".join(speaker_ids), embed_ms,
             (time.perf_counter() - t) * 1000)
