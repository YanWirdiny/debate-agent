# STATUS (2026-11-14, night)

Board for [PLAN.md](PLAN.md) (section 4 phases). On main (`0ccbc81`): PRs #7-#17 (pipeline, economy_v2
clip, pending, P2 claim bank, lane budgets, rest of P2 by P1, frontend, live captions, demo media).
**Open: PR #19** (`p1/gemini-budget`): real-model fixes, Gemini budget, original-video audience mode.
167 backend tests pass on #19.

**Summary:** the first **real run** worked on the economy_v2 video: live Deepgram, Groq, Gemini with
Google Search, 12 segments and 10 flags. The audience can watch the untrimmed original while the agent
analyses the trimmed clip about a minute ahead. What is left is **data** (claim banks, topic passages,
eval set), prompt tuning, and a check by eye in a browser.

## Code

| Area | Item | Status |
|---|---|---|
| Pipeline | Mongo (Atlas), ingest + Deepgram, segmenter, hubs, event log, aggregator, metrics, report | ✅ |
| Pipeline | Warm-up, async verdict workers, prefetch, summarizer, `REPLAY_TRANSCRIPT`, `REPLAY_EVENTS` | ✅ |
| Pipeline | `LANE_TIMEOUT_S` settable from the environment (1.2 default, 2.5 on free-tier Groq) | 🔄 PR #19 |
| Intel | Claims (bank, retrieval + stance, U -> web verdict), consistency, fallacy, `INTEL=bank` | ✅ |
| Intel | Gemini calls work for real (`response_json_schema`; `response_schema` was rejected with 400) | 🔄 PR #19 |
| Intel | Groq: reasoning turned down; `LLM_MODEL_FAST` rotates over several models (per-model rate limits) | 🔄 PR #19 |
| Intel | Gemini spend guard: paid key to $7, then free `GEMINI_API_KEY_2`; ledger per laptop | 🔄 PR #19 |
| Intel | Prompt wording: fallacy prompt flagged the $5T deficit line wrongly; needs a tuning pass | ⬜ |
| Prep | `fetch_factchecks`, `build_dossier`, `build_topics`, `embed_kb`, `run_eval` | ✅ never run on real data |
| Prep | `align_clip.py`: trimmed clip -> original cut list from audio, plus captions for the cuts | 🔄 PR #19 |
| Frontend | Operator, audience, report; live captions at speaking pace | ✅ |
| Frontend | Audience original mode: untrimmed video, "not fact-checked" cut captions, "Agent is N s ahead" | 🔄 PR #19 |
| Frontend | Checked by eye in a browser | ⬜ not yet (extension not connected) |
| Frontend | Homepage / demo page / Live beta from `debate-agent-web-design.md` | ⬜ design only |

## Keys, models, media

| Item | Status |
|---|---|
| `DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `GEMINI_API_KEY_2`, `GOOGLE_FACTCHECK_KEY` | ✅ set and tested |
| `LLM_MODEL_FAST` | ✅ `openai/gpt-oss-20b,qwen/qwen3.8-27b,openai/gpt-oss-120b` (Groq free tier: 8k tokens/min, 1k requests/day each) |
| `LLM_MODEL_GEMINI`, `LLM_MODEL_GEMINI_FALLBACK` | ✅ `gemini-3.5-flash-lite` (2-3 s web verdicts). `gemini-3.8-flash` was 93 s-13 min: paid tier not active yet? |
| Gemini billing | ⚠️ $10 credit; check the key's project shows a paid tier; set a hard cap in Google's console |
| economy_v2 `clip.mp4` + `audio.wav`; original `data/clips/economy/clip.mp4` | ✅ on main |
| economy_v2 `source.json` (cut list 65.5 s + 37 s, 23 cut caption lines) | 🔄 PR #19; cut captions need a read-through |
| `data/dossiers/harris`, `data/dossiers/trump` (claim banks) | ⬜ empty: prep scripts, then HUMAN REVIEW |
| `data/topics/*` passages (taxes/deficit, tariffs/China first) | ⬜ empty |
| `data/eval/labeled.jsonl` (50-100 sentences) | ⬜ empty |
| Second clip: El-Sayed vs Rogers | ⬜ needs the video |

## First real run (economy_v2, live Deepgram, `INTEL=real`)

| Measure | Value |
|---|---|
| Flags | 10 (U, V, M, L); 1 verified (quote found on the page) reached the audience |
| First flag after end of sentence | 1.6-5.7 s |
| Web verdict (Gemini + Search) | 4.4-9.4 s |
| Lead in original mode (Harris / Trump) | 65.5 s / 102.5 s: every result is ready before the viewer hears the line |
| Gemini cost | ~$0.17 per run (estimate) -> ~40 runs on $7 |

## Sync points

| Sync | Needs | State |
|---|---|---|
| 1: real clip -> Deepgram -> captions, one real bank hit | reviewed bank | real clip + captions + web verdicts ✅; bank hit waits on the bank |
| 2: all categories on a real clip, audience gate, kill, no moderator flags | Sync 1 + topic passages | gate and moderator rule seen working in the real run |
| 3: feature freeze, CONTEXT.md section 12 done | Sync 2, eval, recorded run per clip | not started |

## Open items

- Few audience flags: web verdicts usually fail the "quote on page" check. Banks fix this; a second
  grounded attempt (there is a minute of lead in original mode) could too.
- Run the backend with `LANE_TIMEOUT_S=2.5` while on Groq's free tier; wait ~1 min between runs.
- Gemini ledger is per laptop: Wenley's use of the paid key is not counted on Yan's machine.
- Cut captions: Deepgram merged the moderator with Harris; fixed with `--override 140.7-142.58=moderator:moderator`.
- `debate-agent-web-design.md` is untracked and partly out of date (says real lanes are stubs).
- Old branches (`cursor/*`, `p1/*`, `phase0/*`, `data/*`) merged into main can be deleted.

## Next

1. Review and merge PR #19; watch `/audience` and `/operator` in a browser on economy_v2.
2. Build + review the harris/trump banks and topic passages (RUNBOOK section 2), `embed_kb.py`.
3. Tune the fallacy prompt; consider a second grounded attempt for web verdicts.
4. Confirm the Gemini paid tier, then retest `gemini-3.8-flash` for verdicts.
5. Eval set + `run_eval.py`; record a good run per clip; second clip; homepage/demo pages from the design doc.
