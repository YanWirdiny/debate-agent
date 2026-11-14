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
