# STATUS (2026-11-14, evening)

Board for [PLAN.md](PLAN.md) (section 4 phases), checked against `origin/main` at `eb22891`.
On main: PRs #7-#10 (pipeline, economy_v2 clip), #11 (pending), #12 (P2 claim-bank foundation),
#13/#14 (lane budgets), #15 (rest of P2 by P1, frontend, live captions). 156 backend tests pass.

**Summary:** all code for the demo exists and runs end to end with `INTEL=fake`, and with
`INTEL=real` in tests (LLM providers mocked). What is left is **keys, data and media**, then a real
run and frontend polish. No real Groq/Gemini call has been made yet.

## Code

| Area | Item | Status |
|---|---|---|
| Pipeline | Mongo (Atlas), ingest + Deepgram (reconnect, transcript fallback), segmenter, hubs, event log | ✅ |
| Pipeline | SessionContext, aggregator (dedupe with number/negation guards, kill, gate), metrics | ✅ |
| Pipeline | Warm-up, async verdict workers, prefetch, summarizer, `REPLAY_TRANSCRIPT`, `REPLAY_EVENTS` | ✅ |
| Pipeline | Report JSON + HTML, `check_setup.py`, `RUNBOOK.md`, `label_clip.py` | ✅ |
| Pipeline | Operator `pending` (not checked) indicator | ✅ merged |
| Intel | LLM client, KB store, verify, claims bank path (Wenley) | ✅ |
| Intel | Claims retrieval + stance, U -> grounded verdict, consistency, fallacy, warm-up | ✅ (P1, authorized) |
| Intel | Prompts: stance, fallacy, nli, grounded_verdict, prefetch, summarize, build_bank | ✅ wording still worth a review |
| Intel | Lane budgets: extraction and fallacy leave room for the Gemini fallback (#13, #14, #15) | ✅ |
| Intel | `INTEL=bank` (Groq only) mode | ✅ |
| Prep | `fetch_factchecks`, `build_dossier`, `build_topics`, `embed_kb`, `run_eval` | ✅ never run against real data |
| Prep | Embedding model (all-MiniLM-L6-v2) cached on Yan's laptop, CPU ~7 ms/sentence | ✅ |
| Frontend | Operator, audience, report views (Vite + React + Tailwind + zustand) | ✅ |
| Frontend | Live captions at speaking pace: `partial` words + 2.5 s audience broadcast delay | ✅ measured: all words arrive >= 2 s early |
| Frontend | Checked by eye in a browser / with a real clip video | ⬜ not yet |
| Frontend | More updates (requested, to be specified) | ⬜ |

## Keys, data, media (blocking a real run)

| Item | Status |
|---|---|
| `DEEPGRAM_API_KEY` | ✅ set, accepted |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_FACTCHECK_KEY`, `LLM_MODEL_*` in `backend/.env` | ⬜ empty |
| economy_v2 `clip.mp4` + `audio.wav` | ⬜ not on disk (gitignored; share via drive) |
| `data/dossiers/harris`, `data/dossiers/trump` (claim banks) | ⬜ empty: prep scripts, then HUMAN REVIEW of every row |
| `data/topics/*` passages (taxes/deficit, tariffs/China first) | ⬜ empty: build + review |
| `data/eval/labeled.jsonl` (50-100 sentences) | ⬜ empty |
| Second clip: El-Sayed vs Rogers (`el_sayed`, `rogers`) video + timeline | ⬜ needs the video |

## Sync points

| Sync | Needs | State |
|---|---|---|
| 1: real clip -> Deepgram -> captions, one real bank hit | clip audio, LLM keys, reviewed bank | code ready; waiting on keys/data/media |
| 2: all categories on a real clip, audience gate, kill, no moderator flags | Sync 1 + topic passages | code ready (tested with mocks) |
| 3: feature freeze, CONTEXT.md section 12 done | Sync 2, eval, recorded run per clip | not started |

## Open items

- Speaker ids must match the clip timelines exactly: `harris`, `trump` (economy_v2).
- Everyone sets `MONGODB_DB=debate-agent` (hyphen) in `backend/.env`.
- Unverified until keys exist: Gemini structured JSON together with Google Search on the chosen model.
- Live Deepgram sends final words in 1-3 s chunks: if captions bunch up in rehearsal, use `/audience?delay=3`.
- Contract additions since Phase 0: `PendingMessage`, `PartialMessage`/`PartialWord`, `"pending"` event type.
- Old branches (`cursor/*`, `p1/*`, `phase0/*`, `data/economy-demo`) are merged and can be deleted.
- Docker Mongo for demo day: optional, decide before the demo.

## Next

1. Fill the LLM keys and model ids in `backend/.env`; run `scripts/check_setup.py`.
2. Get economy_v2 media; one live run with `INTEL=fake`, then look at `/operator` + `/audience` in a browser.
3. Build + review the harris/trump banks and topic passages (RUNBOOK section 2), `embed_kb.py`.
4. First `INTEL=real` run on economy_v2; tune thresholds from what the operator sees; record the good run.
5. Frontend updates (next list from Yan); eval set + `run_eval.py`; second clip.
