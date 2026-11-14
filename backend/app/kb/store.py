"""In-memory reviewed KB. Mongo shapes and fixture format are documented beside fixtures.

Sync 1 deliberately accepts only lexically identical normalized propositions. Cosine
retrieves candidates; it cannot establish equivalent meaning. Paraphrases need the
later stance/NLI path. All encoding must be called from a worker thread.
"""
from __future__ import annotations

import asyncio
import re
import threading
from typing import Any, Self

import numpy as np
from app.models import BankHit, Claim, ContractModel, Passage, SpeakerDigest, Statement
from app.verify import evidence_span


class ReviewedClaim(ContractModel):
    speaker_id: str
    reviewed: bool = False
    hit: BankHit


def proposition(text: str) -> str:
    # Keep symbols, quantities, order, units and negation. Only whitespace, case,
    # typographic apostrophes and sentence-final punctuation are normalized.
    return " ".join(text.lower().replace("’", "'").split()).rstrip(".!")


def quantities(text: str) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)*(?:\s*(?:%|percent(?:age points)?|million|billion|thousand))?", text.lower())


def equivalent(a: Claim, b: Claim) -> bool:
    left, right = a.decontextualized, b.decontextualized
    return bool(left.strip()) and quantities(left) == quantities(right) and proposition(left) == proposition(right) and proposition(a.text) in {proposition(b.text), proposition(right)}


def _cosine(a, b) -> float:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if a.ndim != 1 or b.ndim != 1 or not a.size or a.shape != b.shape or not np.isfinite(a).all() or not np.isfinite(b).all():
        return 0.0
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.clip(np.dot(a, b) / denom, -1, 1)) if denom else 0.0


class KnowledgeBase:
    def __init__(self, *, encoder: Any = None, claims: list[ReviewedClaim] | None = None, passages: list[Passage] | None = None, statements: list[Statement] | None = None, digests: list[SpeakerDigest] | None = None) -> None:
        self._lock = threading.RLock()
        self._encoder = encoder
        self._claims = [r.model_copy(deep=True) for r in claims or [] if r.reviewed]
        self._passages = [p.model_copy(deep=True) for p in passages or []]
        self._statements = [s.model_copy(deep=True) for s in statements or []]
        self._digests = {d.speaker_id: d.model_copy(deep=True) for d in digests or []}

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            if self._encoder is None:
                from sentence_transformers import SentenceTransformer
                # Do not download model weights silently during a live session.
                self._encoder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", local_files_only=True)
            vectors = np.asarray(self._encoder.encode(texts, normalize_embeddings=True), dtype=float)
        if vectors.ndim != 2 or vectors.shape[0] != len(texts) or not vectors.shape[1] or not np.isfinite(vectors).all():
            raise ValueError("embedding model returned invalid vectors")
        return vectors.tolist()

    def embed_claims(self, claims: list[Claim]) -> list[Claim]:
        return [c.model_copy(update={"embedding": v}, deep=True) for c, v in zip(claims, self._encode([c.decontextualized for c in claims]))]

    @classmethod
    async def load(cls, db: Any, speaker_ids: list[str], topics: list[str]) -> Self:
        if db is None:
            raise ValueError("real KB requires a database; use INTEL=fake only for explicitly labeled pipeline tests")
        speaker_filter = {"speaker_id": {"$in": speaker_ids}}
        topic_filter = {"topic": {"$in": topics}} if topics else {}
        async def rows(collection: str, query: dict):
            return [doc async for doc in db[collection].find(query, {"_id": 0})]
        claims, passages, statements, digests = await asyncio.gather(
            rows("claims", {**speaker_filter, "reviewed": True}), rows("passages", topic_filter),
            rows("statements", speaker_filter), rows("speaker_digests", speaker_filter),
        )
        kb = cls(claims=[ReviewedClaim.model_validate(d) for d in claims], passages=[Passage.model_validate(d) for d in passages], statements=[Statement.model_validate(d) for d in statements], digests=[SpeakerDigest.model_validate(d) for d in digests])
        # Re-encode all records with one known model; never mix unversioned stored vectors.
        await asyncio.to_thread(kb._prepare)
        return kb

    def _prepare(self) -> None:
        objects = [r.hit.claim for r in self._claims] + self._passages + self._statements
        texts = [r.hit.claim.decontextualized for r in self._claims] + [p.text for p in self._passages] + [s.text for s in self._statements]
        for obj, vector in zip(objects, self._encode(texts)):
            obj.embedding = vector

    def source_texts(self) -> dict[str, str]:
        # Ambiguous source ids are rejected rather than letting one passage overwrite another.
        with self._lock:
            sources: dict[str, str] = {}
            conflicts = set()
            for p in self._passages:
                if p.source.source_id in sources and sources[p.source.source_id] != p.text:
                    conflicts.add(p.source.source_id)
                sources[p.source.source_id] = p.text
            return {key: value for key, value in sources.items() if key not in conflicts}

    def match_bank(self, speaker_id: str, claim: Claim) -> BankHit | None:
        vector = claim.embedding or self._encode([claim.decontextualized])[0]
        with self._lock:
            sources = self.source_texts()
            matches = []
            for row in self._claims:
                hit = row.hit
                if row.speaker_id != speaker_id or hit.verdict_category not in ("F", "M", "D", "V") or not equivalent(claim, hit.claim):
                    continue
                score = _cosine(vector, hit.claim.embedding)
                shared = ({e.casefold() for e in claim.entities} & {e.casefold() for e in hit.claim.entities}) or (set(claim.numbers) & set(hit.claim.numbers))
                if score < .82 or not shared or not hit.evidence or not hit.explanation.strip():
                    continue
                # The reviewed verdict must cite retained full source text, not its own quote.
                if not all(e.source_id in sources and evidence_span(e, sources[e.source_id]) and any(p.source.source_id == e.source_id and p.source.url == e.url and p.source.publisher == e.publisher and p.source.title == e.title and p.source.reliability == e.reliability for p in self._passages) for e in hit.evidence):
                    continue
                matches.append(hit.model_copy(update={"similarity": score}, deep=True))
            if len({h.verdict_category for h in matches}) > 1:
                return None  # conflicting reviewed records require resolution
            return max(matches, key=lambda h: h.similarity, default=None)

    def search_passages(self, text: str, topic: str | None, k: int = 5) -> list[Passage]:
        if k <= 0:
            return []
        vector = self._encode([text])[0]
        with self._lock:
            # Prefer the active shard, then fill from the remaining shards.
            ranked = sorted(self._passages, key=lambda p: (topic is not None and p.topic == topic, _cosine(vector, p.embedding)), reverse=True)
            return [p.model_copy(deep=True) for p in ranked[:k]]

    def search_statements(self, speaker_id: str, text: str, k: int = 3) -> list[Statement]:
        if k <= 0:
            return []
        vector = self._encode([text])[0]
        with self._lock:
            ranked = sorted((s for s in self._statements if s.speaker_id == speaker_id), key=lambda s: _cosine(vector, s.embedding), reverse=True)
            return [s.model_copy(deep=True) for s in ranked[:k]]

    def add_passages(self, topic: str, passages: list[Passage]) -> None:
        copies = [p.model_copy(update={"topic": topic}, deep=True) for p in passages]
        for p, vector in zip(copies, self._encode([p.text for p in copies])):
            p.embedding = vector
        with self._lock:
            keyed = {p.passage_id: p for p in self._passages}
            keyed.update({p.passage_id: p for p in copies})
            self._passages = list(keyed.values())

    def topic_for(self, text: str) -> str | None:
        vector = self._encode([text])[0]
        with self._lock:
            topics = {p.topic for p in self._passages if p.topic}
            scores = [(topic, _cosine(vector, np.mean([p.embedding for p in self._passages if p.topic == topic], axis=0))) for topic in sorted(topics)]
            topic, score = max(scores, key=lambda item: item[1], default=(None, 0.0))
            return topic if score > 0 else None

    def digest(self, speaker_id: str) -> SpeakerDigest | None:
        digest = self._digests.get(speaker_id)
        return digest.model_copy(deep=True) if digest else None
