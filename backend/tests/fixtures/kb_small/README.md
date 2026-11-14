# Synthetic KB — tests only

These invented test records use example.invalid and must never seed a production
KB or be described as reviewed real-world evidence. `reviewed: true` exercises the
review gate only. Load them explicitly through mocked Mongo cursors in tests.
There is no automatic fixture fallback in the real KB.

Mongo document shapes (all collections omit Mongo `_id` when loaded):
- `claims`: `{speaker_id, reviewed: bool, hit: BankHit}`. `hit.similarity` is a
  serialization placeholder and is recomputed, never treated as verdict confidence.
- `passages`: `Passage`, including retained full `text`, topic and source metadata.
  For Sync 1 each source_id must identify one unambiguous retained source text.
- `statements`: `Statement`.
- `speaker_digests`: `SpeakerDigest` (new collection; confirm ownership/indexes with P1).

Only explicitly reviewed claim records load. Speaker filters apply to claims,
statements and digests; `topics=[]` loads all passages. Stored vectors are recomputed
with one local `sentence-transformers/all-MiniLM-L6-v2` encoder to avoid mixed-model
vectors. Provision model weights explicitly before running; the runtime uses
`local_files_only=True` and never downloads them. Tests inject an encoder.

Matching is intentionally conservative: same normalized assertion text and
resolved proposition, cosine >= .82, shared entity/number, supported evidence
provenance. Paraphrases remain unmatched until a later semantic verification path.
