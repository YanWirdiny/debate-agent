# STATUS (2026-11-14)

Snapshot of the board in [PLAN.md](PLAN.md) (section 4 phases, section 7 P1 status), checked against
`origin/main`. On main: PRs #7, #8, #9 (P1 pipeline) and #10 (economy_v2 clip).

**Summary:** P1 is through Phase 3 and runs end to end with `INTEL=fake` (fixture and economy_v2).
Switching to `INTEL=real` is blocked on P2's lanes and KB, which are still stubs on main.

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
| `llm/client.py`, `kb/store.py`, `verify.py` | ❌ stubs (`NotImplementedError`) |
| `lanes/claims.py`, `lanes/fallacy.py`, `lanes/consistency.py` | ❌ stubs |
| `intel/async_jobs.py`, `intel/warmup.py` | ❌ stubs |
| `fetch_factchecks`, `build_dossier`, `build_topics`, `embed_kb`, `run_eval` | ❌ stubs |
| `data/dossiers/`, `data/topics/`, `data/eval/`, `tests/fixtures/kb_small/` | ❌ empty |
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
- Frontend: only `frontend/src/types.ts`; per plan it starts after the freeze.

## Next

1. P2: share economy_v2 media; land `llm/client.py`, `kb/store.py`, claims lane (bank path) for Sync 1.
2. P2: review the `PendingMessage` contract PR (`p1/pending-indicator`).
3. P1: live Deepgram run of economy_v2 with `INTEL=fake`; second clip (El-Sayed vs Rogers, ids
   `el_sayed`, `rogers`) timeline as soon as its video exists (ffmpeg now installed).
