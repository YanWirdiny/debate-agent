# PLAN.md: Backend execution plan (2 people, parallel)

> How we split the backend described in `ARCHITECTURE.md` and `CONTEXT.md` between two people so we can
> build in parallel without merge conflicts. Frontend is out of scope for now; it builds later against the
> same contracts (`models.py` -> `types.ts`) and `mock_stream.py`.

## 1. The split

**Assignment:** P1 (Pipeline) = Yan. P2 (Intelligence + data) = Wenley.

The seam is the lane interface. One person owns **the pipeline** (moving data: audio -> segments ->
queues -> aggregator -> event log -> WebSocket). The other owns **the intelligence** (deciding what a
flag is: LLM calls, knowledge base, lanes, prompts, data). They meet only at typed function signatures
defined on day one.

| | **P1: Pipeline** | **P2: Intelligence + data** |
|---|---|---|
| Owns | how data moves, when things run, what is stored | what gets flagged and why |
| Live plane | ingest, Deepgram, segmenter, `SessionContext`, `SessionRunner`, queues and tasks, aggregator, event log writer, WS hubs, metrics, warm-up, replay fallbacks | LLM client, prompts, KB store, claims / fallacy / consistency lanes, grounded verdict, prefetch and summarize functions |
| Prep plane | `label_clip.py` (Deepgram prerecorded + timelines) | `fetch_factchecks.py`, `build_dossier.py`, `build_topics.py`, `embed_kb.py`, human review of the claim bank |
| Output | `report.py`, recorded runs for `REPLAY_EVENTS` | `run_eval.py`, eval set, precision numbers |
| Tests against | fake lanes (`lanes/fake.py`) | fixture segments (`tests/fixtures/segments.jsonl`), no audio needed |

Key rule for the async work: **P1 owns the tasks and queues, P2 owns the functions they call.**
P1's `async_verdict_worker` loop pulls from `slow_q` and calls P2's `grounded_verdict()` / `prefetch()`;
P1's `summarizer` task calls P2's `summarize()`. Neither person edits the other's side of that call.

## 2. File ownership

Each file has exactly one owner. Only the owner edits it. If you need a change in someone else's file,
ask (or open a tiny PR they approve). This is the main conflict-avoidance rule.

```
backend/
  pyproject.toml, uv.lock     SHARED  (deps added only in Phase 0 or a "deps:" commit, see section 5)
  .env.example                SHARED  (append-only, one line per var)
  app/
    models.py                 SHARED  (frozen after Phase 0; changes = "contract:" PR approved by both)
    config.py                 SHARED  (append-only settings)
    interfaces.py             SHARED  (frozen function signatures, Protocols)
    main.py                   P1
    session.py                P1      SessionRunner, SessionContext, view_for
    ingest.py                 P1      AudioPump, TimelineLookup, transcript replay
    segmenter.py              P1
    aggregator.py             P1
    eventlog.py               P1      EventWriter, hash chain, jsonl fallback
    hubs.py                   P1      audience/operator WS hubs
    metrics.py                P1
    report.py                 P1
    db.py                     P1      motor client, collections, indexes (P2 asks for KB indexes)
    lanes/fake.py             P1      fake lanes for pipeline testing
    lanes/claims.py           P2
    lanes/fallacy.py          P2
    lanes/consistency.py      P2
    kb/store.py               P2      KnowledgeBase
    llm/client.py             P2      groq_json, gemini_json, fallback, retry, logging
    llm/prompts/*.md          P2
    intel/async_jobs.py       P2      grounded_verdict(), prefetch(), summarize()
    verify.py                 P2      span/quote substring checks, STT-confidence guard
  scripts/
    mock_stream.py            P1
    label_clip.py             P1
    fetch_factchecks.py, build_dossier.py, build_topics.py, embed_kb.py, run_eval.py   P2
  tests/
    contracts/                SHARED  (written in Phase 0)
    pipeline/                 P1
    intel/                    P2
    fixtures/                 P2 owns segments.jsonl and kb_small/; P1 owns clips fixtures
data/
  clips/                      P1
  dossiers/, topics/, eval/   P2
```

MongoDB collections follow the same rule: P1 owns `events`, `sessions`, `timelines`;
P2 owns `claims`, `statements`, `passages`, `eval_results`.

## 3. Phase 0: contracts, together (H0 to H2)

Done side by side (pair on one screen or a live call). Nothing else starts until this is merged to `main`.

1. Repo skeleton: every file in section 2 exists, with stubs that raise `NotImplementedError`.
2. `pyproject.toml` with **all** expected deps (fastapi, uvicorn, pydantic v2, pydantic-settings, httpx,
   deepgram-sdk, sentence-transformers, numpy, motor, openai, google-genai, pytest, pytest-asyncio).
   Lock once.
3. `models.py`: all of CONTEXT.md section 6, plus the internal types both sides pass around:
   `Claim`, `ClaimRecord`, `Passage`, `Statement`, `BankHit`, `SpeakerDigest`, `LaneContext`,
   `SlowJob`, `LLMResult`, `Word`, `Clip`, WS message models.
4. `interfaces.py`: the frozen signatures from ARCHITECTURE.md section 5, plus the async-job functions:
   ```python
   # lanes (P2 implements, P1 calls)
   async def run_claims(seg, view, kb, emit, slow_q) -> None
   async def run_fallacy(seg, view, emit) -> None
   # run_consistency is called from inside run_claims (P2 internal)

   # async jobs (P2 implements, P1's worker tasks call)
   async def grounded_verdict(job: SlowJob, kb) -> Annotation | None
   async def prefetch(job: SlowJob) -> tuple[str, list[Passage]]   # (topic, passages); P1 calls kb.add_passages
   async def summarize(prev: str, turns: list[Segment]) -> str

   # KB (P2 implements, P1 calls at warm-up)
   class KnowledgeBase: load(), match_bank(), search_passages(), search_statements(), add_passages(),
                        topic_for(text) -> str | None   # nearest topic centroid, used by P1 for active_topic
   ```
   `emit` is `Emit = Callable[[Annotation | ClaimRecord], None]` (see contract changes below).

   **Status:** P2's Phase 0 (PR #2, commit `7aecd21`) is the base on `main`. Branch
   `p1/phase0-contracts` completes Phase 0 on top of it; 58 tests pass.

   **P1 additions (P1-owned files):** fake lanes that really flag (`lanes/fake.py`),
   `Aggregator.is_public`, FastAPI `app` in `main.py`, `tests/pipeline/test_p1_basics.py`.

   **Contract changes, PENDING P2 APPROVAL in the PR:**
   - Claim ledger: `Emit = Callable[[Annotation | ClaimRecord], None]` in `interfaces.py`, used by all
     lanes. P1 routes `ClaimRecord` into `SessionContext.claim_ledger`, which the consistency lane reads.
   - WS messages: client `LoadMessage {"type": "load", "clip_id"}` (server warms up, replies `ready`,
     then `start` plays) and server `BannerMessage {"type": "banner", "level", "text"}` for operator alerts.
   - P2 hooks: `warm_up(kb, speaker_ids)` in new `app/intel/warmup.py` (stub) and
     `KnowledgeBase.digest(speaker_id) -> SpeakerDigest | None` (stub).
   - Stub tests: `IMPLEMENTED` set in `test_interfaces.py` skips the "stub raises" check for pieces
     that are implemented on purpose; signatures and type hints are still checked. Proposal for
     Phase 1: drop the "raises" checks entirely.
   - `tests/fixtures/segments_labels.jsonl` (P2-owned, drafted by P1 for review): expected category
     and subtype per fixture segment, with an alignment test.
5. `config.py`: pydantic Settings with every env var in CONTEXT.md section 8; `.env.example`.
6. `tests/contracts/`: round-trip every model through JSON; check the fake lanes and the stubs match
   `interfaces.py` signatures.
7. `tests/fixtures/segments.jsonl`: 20 hand-written segments (both speakers + moderator, a few known
   false claims and fallacies) so P2 can work without audio from minute one.
8. Agree on IDs: `annotation_id = f"{segment_id}:{lane}:{n}"`, `segment_id = f"{session_id}:{seq}"`.

Exit check: `pytest tests/contracts` passes on `main`; both people pull.

## 4. Parallel phases

Each phase ends with a **sync point**: both merge to `main`, run the sync check together (15 to 30 min),
then branch again. Between sync points, nobody depends on the other's unfinished code.

### Phase 1 (H2 to H8): foundations on both sides

| P1: Pipeline | P2: Intelligence + data |
|---|---|
| `db.py` + local Mongo (Docker) + Atlas connection | `llm/client.py`: `groq_json`, `gemini_json`, timeout 1.2 s, retry once, pydantic validation, usage + prompt-hash logging |
| `ingest.py`: AudioPump at 1x (sleep to target time), TimelineLookup | `kb/store.py`: load from Mongo, numpy cosine, `match_bank`, `search_*`, `topic_for` |
| Deepgram streaming + `stt_reader` | `embed_kb.py` + a small KB fixture (`tests/fixtures/kb_small/`) |
| `segmenter.py`: sentences, speaker split, role tagging | `fetch_factchecks.py`, `build_dossier.py`: start real claim-bank drafts for 1 clip |
| `hubs.py` + `main.py` routes, `mock_stream.py` | `lanes/claims.py` **bank path only**: extract -> embed -> `match_bank` -> emit |
| `eventlog.py`: writer, seq, hash chain, 250 ms batches, jsonl fallback | `verify.py`: span/quote substring checks, low-confidence guard |
| `label_clip.py`: timeline for clip 1, hand fixed | `tests/intel/`: claims lane on fixture segments |
| Pipeline runs end to end with `lanes/fake.py` | |

**Sync 1 (H8):** real clip audio -> Deepgram -> captions on the operator WS, moderator tagged correctly,
event log chain verifies. Swap `lanes/fake.py` for the real claims lane: at least one bank-hit annotation
reaches the operator WS from the real clip.

### Phase 2 (H8 to H14): full hot path

| P1: Pipeline | P2: Intelligence + data |
|---|---|
| `SessionContext`: turns, last question, `view_for` per lane | Claims lane: retrieval + `stance` prompt, verdict -> category mapping |
| `aggregator.py`: versions, state machine, `is_public`, dedupe, kill, audience/operator routing | `lanes/fallacy.py` + `fallacy` prompt (definitions, forms, examples, legal exceptions) |
| Dispatcher under `lane_sem`, per-lane timeout -> "pending" | `lanes/consistency.py` + `nli` prompt, claim ledger |
| `metrics.py`: p50/p95, bank hit rate, loop lag; ticker to operator | `build_topics.py`, claim banks + statements for all 4 speakers, human review |
| Second clip timeline | U claims pushed to `slow_q` as `SlowJob` |

**Sync 2 (H14):** on a real clip, the operator sees F/M/D/V/L/C/U annotations with latency; the audience
only gets the ones that pass `is_public`; a kill shows up in the event log; moderator never flagged.

### Phase 3 (H14 to H18): async plane, warm-up, outputs

| P1: Pipeline | P2: Intelligence + data |
|---|---|
| Warm-up sequence (ARCHITECTURE.md section 8), `ready` broadcast | `grounded_verdict`, `prefetch`, `summarize` + their prompts |
| `async_verdict_worker` x2 and `summarizer` tasks calling P2's functions; prefetch trigger on moderator questions | Gemini explicit cache for digests + definitions |
| `REPLAY_TRANSCRIPT` and `REPLAY_EVENTS` modes | Eval set: 50 to 100 labeled sentences |
| `report.py` from the event log | `run_eval.py`: precision/recall per category -> Mongo |
| Record one good run per clip | Threshold tuning from eval results |

**Sync 3 (H18, feature freeze):** CONTEXT.md section 12 definition of done, backend parts, all checked.

### Phase 4 (H18 onward): integration, together
Run both clips repeatedly, fix false positives (raise thresholds before adding features), test fallbacks
with the network off, then frontend work begins against the frozen backend.

## 5. Git rules (avoiding conflicts)

- **Branches:** `p1/<feature>` and `p2/<feature>`, cut from `main`, short-lived (merge within ~3 h).
- **Rebase on `main` before every merge**; run `pytest tests/contracts` plus your own tests.
- **Never edit a file you don't own.** Ask the owner, or open a PR they approve.
- **Contract changes** (`models.py`, `interfaces.py`): a separate tiny PR titled `contract: ...`,
  both people approve, merged immediately, both pull right away. Prefer adding optional fields over
  renaming or removing.
- **Dependencies:** one person adds them in a commit titled `deps: ...` straight to `main` and tells
  the other; never add deps inside a feature branch (lockfile conflicts).
- **`config.py` and `.env.example`:** append-only, new lines at the end of your own section
  (`# --- P1 ---` / `# --- P2 ---`).
- **Data files** (`data/`): owned per section 2; large binaries (audio, video, `.npy`) stay out of git
  and live in Mongo or a shared drive.

## 6. Who to ask when blocked

| Blocked on | Unblock without waiting |
|---|---|
| P1 needs real annotations | use `lanes/fake.py` |
| P2 needs real segments | use `tests/fixtures/segments.jsonl` |
| P2 needs `SessionContext` views | build `LaneContext` by hand in tests (it's a plain model) |
| P1 needs the KB at warm-up | load `tests/fixtures/kb_small/` |
| Either needs a contract change | `contract:` PR, ping the other, merge in minutes |

Cut order if behind (from CONTEXT.md section 11): consistency lane -> prefetch -> report polish ->
second clip. Never cut: moderator exclusion, audience verification gate, event log, replay fallback.

## 7. P1 status (2026-11-14) and what P1 needs from P2

**P1 done (branch `p1/phase2-3`, 96 tests):** Phase 1 (db.py + Mongo sink, AudioPump, Deepgram
streaming with reconnect and transcript fallback, segmenter, hubs, event log, `label_clip.py`),
Phase 2 (SessionContext views, aggregator with dedupe/kill/routing, dispatcher with lane timeouts,
metrics ticker), Phase 3 (warm-up, 2 async verdict workers, prefetch on moderator questions,
summarizer, `REPLAY_TRANSCRIPT`, `REPLAY_EVENTS`, report JSON + HTML, `/sessions`). Plus
`scripts/check_setup.py` and `RUNBOOK.md`.

**P1 left:** label real clips + record one good run per clip (needs clips and keys); the second
clip timeline; operator "pending" indicator (needs a contract message, see below).

**What the pipeline expects from P2 (please confirm or push back):**
- `INTEL=real` switches the runner to `lanes.claims.run_claims`, `lanes.fallacy.run_fallacy`,
  `intel.async_jobs.*`, `intel.warmup.warm_up` and `kb.store.KnowledgeBase.load`. P1 never calls
  `run_consistency` directly: per ARCHITECTURE.md section 3 it runs inside `run_claims`.
- `KnowledgeBase.load(db, speaker_ids, topics=[])`: P1 passes `[]`, meaning "all topic shards".
  `speaker_ids` = non-moderator ids from the clip timeline. P1 then calls `kb.digest(id)` per speaker.
- Dedupe needs each claims-lane flag's `ClaimRecord` (with `annotation_id` and, ideally,
  `claim.embedding`) emitted **before or together with** its `Annotation`.
- `grounded_verdict` returns the updated annotation with the **same `annotation_id`** (it becomes
  a new version); `None` = no change. It runs with a 20 s timeout; `prefetch` results go to
  `kb.add_passages` via `asyncio.to_thread`.
- Report methodology reads the newest document in `eval_results` with the shape
  `{"per_category": {"F": {"precision": .., "recall": .., "n": ..}, ...}}`.
- Metrics has `add_cache_tokens(n)`; P1 has no path to `LLMResult.usage` yet. Proposal: P2's client
  exposes a module-level counter P1 reads, or we skip cache tokens on the dashboard.
- Proposed contract (not done): `{"type": "pending", "segment_id", "lane"}` server message so the
  operator view can show a timed-out lane (CONTEXT.md rule 7).
