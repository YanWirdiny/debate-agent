# STATUS (2026-11-14)

Snapshot of the board in [PLAN.md](PLAN.md) (section 4 phases, section 7 P1 status), checked against
`origin/main`. On main: PRs #7, #8, #9 (P1 pipeline) and #10 (economy_v2 clip).

**Summary (updated, branch `p1/finish-p2`):** all code is done. P1 finished P2's remaining modules
(authorized). `INTEL=real` runs end to end on economy_v2 in tests (LLMs mocked). What is left is
**data and keys**: GROQ/GEMINI/FACTCHECK keys, the reviewed claim banks for harris and trump, topic
passages, the eval set, clip media. Then: frontend.

## P1: Pipeline (Yan)

| Phase | Item | Status |
|---|---|---|
| 1 | `db.py` + Mongo (Atlas), indexes | ✅ done; Atlas verified (`debate-agent` database) |
| 1 | `ingest.py`: AudioPump, Deepgram streaming, reconnect + transcript fallback | ✅ done; Deepgram key accepted |
| 1 | `segmenter.py`, `hubs.py`, `main.py` routes, `mock_stream.py` | ✅ done |
| 1 | `eventlog.py`: hash chain, 250 ms batches, jsonl fallback + local archive | ✅ done |
| 1 | `label_clip.py` | ✅ done |
| 2 | SessionContext, aggregator (dedupe, kill, routing), dispatcher, metrics | ✅ done |
| 2 | Second clip timeline | ⬜ not started (economy_v2 is clip 1) |
| 3 | Warm-up, 2 async verdict workers, prefetch, summarizer | ✅ done (against fake lanes) |
| 3 | `REPLAY_TRANSCRIPT`, `REPLAY_EVENTS`, report JSON + HTML | ✅ done |
| 3 | Record one good run per clip | ⬜ blocked: needs real lanes + clip media |
| - | Operator "pending" indicator | 🟡 built + tested on `p1/pending-indicator`; contract PR awaits P2 approval |
| - | `check_setup.py`, `RUNBOOK.md` | ✅ done |
| 4 (demo prep) | Local Docker Mongo for demo day | ⏸ optional, decide before the demo |

## P2: Intelligence + data (Wenley)

| Item | Status on main |
|---|---|
| economy_v2: `clip.json`, `timeline.json`, `transcript.jsonl` | ✅ merged (#10); validated, replays end to end |
| economy_v2: `clip.mp4`, `audio.wav` | ⬜ gitignored, share via drive |
| `llm/client.py`, `kb/store.py`, `verify.py`, claims bank path | ✅ Wenley (#12) |
| claims retrieval + stance, U -> slow job; `consistency.py`; `fallacy.py` | ✅ done by P1 (`p1/finish-p2`) |
| `intel/async_jobs.py` (grounded verdict, prefetch, summarize), `intel/warmup.py` | ✅ done by P1 |
| prompts: stance, fallacy, nli, grounded_verdict, prefetch, summarize, build_bank | ✅ done by P1 (review wording) |
| `fetch_factchecks`, `build_dossier`, `build_topics`, `embed_kb`, `run_eval` | ✅ done by P1 |
| `INTEL=bank` (Groq only) mode | ✅ done by P1 |
| LLM keys in `.env` (GROQ, GEMINI, GOOGLE_FACTCHECK, LLM_MODEL_*) | ⬜ needed for any real run |
| `data/dossiers/harris`, `data/dossiers/trump` (reviewed claim banks) | ⬜ run the prep scripts, then HUMAN REVIEW |
| `data/topics/*` passages, `data/eval/labeled.jsonl` | ⬜ build + review |
| Answers to P1's asks (PLAN.md section 7) | ⏳ pending |

## Sync points

| Sync | Needs | State |
|---|---|---|
| 1 (H8): real clip -> Deepgram -> captions, one real bank hit | clip audio; P2 claims lane + KB | P1 side ready |
| 2 (H14): all categories on a real clip, audience gate, kill, no moderator flags | P2 lanes | blocked on P2 |
| 3 (H18): feature freeze, CONTEXT.md section 12 done | Sync 2 | blocked |

## Open items

- Speaker ids in economy_v2 are `harris` and `trump`; P2's dossier and KB ids must match exactly.
- Everyone sets `MONGODB_DB=debate-agent` (hyphen) in their `.env`.
- economy_v2: moderator segment ends at 4.08 s, its timeline turn at 4.0 s (harmless).
- Branch `cursor/fake-lanes-skip-moderator-42f4` is redundant (the dispatcher already skips
  moderator segments, `session.py`) and can be deleted, along with the merged `cursor/*`, `p1/*`
  and `phase0/*` branches.
- Frontend: built (`frontend/`, Vite + React + Tailwind + zustand): operator, audience, report views.
  Verified over the real WebSockets end to end (load, start, flags, kill, removal, report); not yet
  checked by eye in a browser, and not with a real clip video.

## Next

1. Keys in `.env`, then build + review the KB (RUNBOOK section 2) and one live `INTEL=real` run.
2. Wenley: review `p1/finish-p2` (prompts wording, stt_safe scope, dedupe guards); share economy_v2 media.
3. P1: live Deepgram run of economy_v2 with `INTEL=fake`; second clip (El-Sayed vs Rogers, ids
   `el_sayed`, `rogers`) timeline as soon as its video exists (ffmpeg now installed).
