# P2 Sync 1 implementation notes

Implemented on p2/intelligence: async Groq JSON / Gemini structured output clients,
reviewed-bank KB, provenance verification and claims extraction + bank path.
Shared models, interfaces, config, dependencies and P1 implementation files are
unchanged. The shared contract test IMPLEMENTED registry now recognizes these four
implemented targets and continues checking their signatures and resolved types.

Provider behavior follows the official [Groq structured output documentation](https://console.groq.com/docs/structured-outputs)
and [Google Gen AI Python SDK documentation](https://googleapis.github.io/python-genai/).
Groq uses JSON mode plus local strict Pydantic validation because JSON schema mode
is model-dependent. Gemini supplies the schema to the async SDK and also validates
locally. The entire call, including at most two primary attempts and one Groq ->
Gemini fallback, shares one timeout; SDK retries are disabled. Failures raise a
sanitized LLMError and cancellation propagates. Logs contain model, provider,
static prompt hash and usage, never request bodies or raw provider errors. Models
are selected from existing settings. Tests mock both adapters and provider outputs.

Bank matching deliberately trades recall for safety: cosine >= .82 and shared
entities/quantities are necessary but insufficient. Both the original extracted
assertion and resolved proposition must match reviewed text after conservative
normalization. Changed quantities, negation, units, subjects, dates or question
punctuation do not become matches through embedding similarity. Evidence must be
present in retained full source text with matching source metadata. No synthetic
fallback, network retrieval, U verdict, slow job or semantic paraphrase judgment
is introduced. Unmatched checkable claims emit ClaimRecord only.

## Decisions to resolve with P1

- `INTEL=real` still calls `intel/warmup.py` and later lanes/jobs. These remain
  explicit stubs. A Sync 1 live run needs a deliberate bank-only integration mode
  or completion of the warm-up contract; this change does not silently substitute
  fake/no-op analysis. The P2 integration test connects the actual claims lane to
  P1's emit, ledger, aggregator, audience routing and hash-chained event writer.
- P1 dedupe currently accepts cosine alone as equivalence. Claims emit records
  without embeddings so it uses exact normalized text; keep this until P1 adds
  quantity/negation/semantic guards. KB vectors remain internal to matching.
- Confirm Mongo storage shapes and `speaker_digests` collection ownership/indexes;
  the fixture README specifies the exact shapes loaded here. Claims require
  `reviewed: true`, and each evidence source id identifies one retained full text.
- `BankHit` has no reviewed verdict-confidence field. This milestone uses a fixed
  0.90 provenance confidence for accepted reviewed matches, never the cosine score.
  Agree/calibrate that policy or add reviewed confidence in a separate contract PR.
- Provision the local all-MiniLM-L6-v2 model before runtime. Stored unversioned
  embeddings are recomputed with this one encoder on load. No automatic model
  download occurs. Set MONGODB_DB=debate-agent as noted in STATUS.md.
- P1's proposed pending-message contract and usage/cache-token dashboard hook
  remain unresolved. Client failures reach P1's existing lane-failure logging.
- No real harris/trump claim-bank data has been added or reviewed. The fixture KB
  is explicitly synthetic and never automatically loaded. A real economy_v2 bank
  hit and live-provider latency remain unverified; no paid calls were made.

## Remaining P2 stubs / later milestones

Fallacy and consistency lanes; retrieval/stance and U -> slow-job handling;
intel/warmup.py; grounded verdict, prefetch and summarization; all prep/eval scripts
and prompts except claim_extract. No other P2 feature is implemented here.

## Verification

`cd backend && .venv/bin/python -m pytest tests/contracts tests/pipeline tests/intel -q`:
114 passed, 4 skipped (Mongo integration tests: no local mongod or MONGODB_TEST_URI).
The mocked Deepgram localhost tests require permission to bind local sockets.
A final conservative STT guard change was rechecked with all contract/P2 tests:
84 passed. `git diff --check` passes. No paid API requests, pushes or merges.

## Update: remaining P2 work finished by P1 (2026-11-14, authorized; branch `p1/finish-p2`)

Answers to "Decisions to resolve with P1" above:
- Bank-only mode: `INTEL=bank` (Groq only: claims incl. retrieval/stance + consistency; no fallacy,
  no Gemini; operator banner). `INTEL=real` is now complete.
- Dedupe: the aggregator now requires equal quantities and negation before cosine/text counts, so
  ClaimRecords carry their embedding again.
- `speaker_digests`: P1 owns the indexes (`db.KB_INDEXES`, all four KB collections); `embed_kb.py` loads it.
- Bank confidence stays the fixed 0.90 (reviewed provenance), unchanged.
- MiniLM: `scripts/embed_kb.py --download-model`; the store now pins it to CPU (~7 ms/sentence;
  MPS was slower for single sentences). Warm-up runs 3 dummy encodes.
- Pending contract: merged to main. Lanes that cannot produce a valid result raise -> `pending/error`.

Changes to Sync 1 code, for review:
- `verify.stt_safe`: blocks when the segment is < .85, a low-confidence word is a number/negation
  ANYWHERE in the sentence, or ANY low-confidence word is inside the span. A low-confidence filler
  word outside the span ("ago") no longer blocks (on economy_v2 the old rule blocked every flag).
- `kb.store`: passages with topic `_sources` (full source texts behind bank evidence) are excluded
  from `search_passages` and `topic_for`.

Added: retrieval + stance (quote must be verbatim in the cited passage, else U), U -> VerdictJob,
consistency (KB statements + session ledger, cosine >= .75 -> nli; session-only differences are
operator-only), fallacy (8 types, verbatim span, strawman needs the opponent's words, verified >= .80),
grounded_verdict/prefetch (quote must be found on the fetched page, else operator-only / dropped),
summarize, warm-up, all prompts, and the five prep scripts (file layout in `scripts/kb_files.py`).
Tests: `tests/intel/test_lanes_async.py`, `test_prep_scripts.py`, `test_real_session.py` (141 total).
Not verified: real provider behaviour (no keys on this machine), Gemini structured output together
with Google Search on the configured model, real claim-bank data (needs human review).
