# RUNBOOK: running the backend (dev and demo day)

Commands run from `backend/` unless noted. `.venv/bin/python` can be `uv run python`.

## 1. One-time setup

```bash
cd backend
uv sync                      # or: python -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env         # fill in keys; never commit .env
```

`.env` essentials:

| Var | Value |
|---|---|
| `MONGODB_URI` | Atlas `mongodb+srv://user:pass@cluster.../` (dev) or `mongodb://localhost:27017` (demo) |
| `MONGODB_DB` | database name; P1 creates `events`, `sessions`, `timelines` + indexes on first start |
| `DEEPGRAM_API_KEY` | live STT and `label_clip.py` |
| `INTEL` | `fake` (test lanes, no keys) / `real` (full intel: Groq + Gemini) / `bank` (Groq only: claims + consistency, no fallacy, no web verdicts) |
| `REPLAY_TRANSCRIPT` | `1` = pre-made transcript instead of Deepgram; also lets the server start without keys |
| `REPLAY_EVENTS` | `<session_id>` = replay a recorded run (no network needed) |

Check everything: `python scripts/check_setup.py` (Mongo reachable + indexes, KB loaded, Deepgram key,
each clip's audio/timeline/transcript). Fix every `FAIL`.

## 2. Build the knowledge base (once per speaker, needs GOOGLE_FACTCHECK_KEY + GEMINI_API_KEY)

```bash
python scripts/embed_kb.py --download-model                 # once, with internet (embedding model)
python scripts/fetch_factchecks.py trump --name "Donald Trump" --query tariffs --query deficit
python scripts/build_dossier.py trump --name "Donald Trump" --office "Former President"
# REVIEW data/dossiers/trump/claims.jsonl + statements.jsonl: check each row, set "reviewed": true
python scripts/build_topics.py trade_tariffs --url <official data page> --url <fact-check page>
# REVIEW data/topics/<topic>/passages.jsonl, set "reviewed": true on passages worth keeping
python scripts/embed_kb.py --check && python scripts/embed_kb.py   # validate, then load into Mongo
python scripts/run_eval.py                                  # needs data/eval/labeled.jsonl (50-100 rows)
```

Speaker ids = the clip timeline ids (`harris`, `trump`, `el_sayed`, `rogers`). Equal depth per speaker.
Only reviewed rows load; a claim whose quote is not verbatim in its source is skipped with a message.
`data/raw/` (fetched pages) is gitignored; `data/sources/`, `dossiers/`, `topics/` are the reviewed record.

## 2b. Prepare a clip (P1)

```bash
mkdir -p ../data/clips/<id> && cp clip.mp4 ../data/clips/<id>/
ffmpeg -i ../data/clips/<id>/clip.mp4 -ac 1 -ar 16000 -sample_fmt s16 ../data/clips/<id>/audio.wav
python scripts/label_clip.py draft <id> --title "..." --video ../data/clips/<id>/clip.mp4   # see who dg0/dg1/dg2 are
python scripts/label_clip.py draft <id> --map 0=moderator:moderator --map 1=<speaker_a> --map 2=<speaker_b>
python scripts/label_clip.py show <id>     # read every turn; fix boundaries/ids in timeline.json
python scripts/label_clip.py build <id>    # transcript.jsonl from the fixed timeline (STT fallback)
python scripts/label_clip.py push <id>     # timeline -> Mongo
```

Speaker ids must match P2's dossier ids (`data/dossiers/<speaker_id>/`). Media files are gitignored.

## 3. Run

```bash
uvicorn app.main:app --workers 1          # one worker: session state is in memory (port 8000)
python scripts/mock_stream.py --speed 4   # no server: print what operator/audience would receive
cd ../frontend && npm install && npm run dev   # UI on http://localhost:5173 (proxies the backend)
```

UI: `/operator` (pick clip -> Load -> wait for warm-up -> Start; Kill on any flag), `/audience` (open on
the projector; click once to enable sound; video and captions follow the operator), `/reports/<session_id>`.
The audience browser plays `data/clips/<id>/clip.mp4` itself, synced to the server clock (drift > 300 ms -> seek).
Broadcast delay: the audience picture runs 2.5 s behind the live pipeline (`/audience?delay=3` to change it,
`?delay=0` for none). Captions appear word by word as they are spoken (server `partial` messages with
real word timings) and flags appear as their phrase ends, never ahead of the video. The operator is live:
lines marked "not on air" have not reached the audience yet, so a kill there is never seen.

Operator WebSocket `/ws/operator`: send `{"type":"load","clip_id":"<id>"}` -> `ready` (warm-up done),
then `{"type":"start","clip_id":"<id>"}` -> `started` with `t0_wall` (frontend calls `video.play()`).
`{"type":"kill","annotation_id":"..."}` removes a flag everywhere. Audience: `/ws/audience` (receive only).
Operator only: `{"type":"pending","segment_id","lane","reason":"timeout"|"error"}` = that lane did not check
that sentence (final, no late result). Show it as a grey "not checked" marker; the report counts them.
HTTP: `/health`, `/clips`, `/sessions`, `/report/<session_id>` (JSON), `/report/<session_id>.html` (print to PDF).

Every run is logged to Mongo and archived locally in `data/runs/<session_id>/log.jsonl` (gitignored).

## 4. Before the demo

1. Demo laptop: `docker compose up -d mongo` (repo root), restore the KB from Atlas (commands in
   `docker-compose.yml`), set `MONGODB_URI=mongodb://localhost:27017`, run `check_setup.py`.
2. For each clip: one full good run with `INTEL=real`. Note the session id (`/sessions`).
   Check its report. That run is the `REPLAY_EVENTS` fallback.
3. Rehearse each fallback once (below).

## 5. When something breaks during the demo

| Symptom | What happens / what to do |
|---|---|
| Deepgram drops | Automatic: banner, reconnect (3 tries), resume at the current clip time; then switches to the pre-made transcript. |
| STT unusable at the venue | Restart with `REPLAY_TRANSCRIPT=1` (UI shows a replay banner). |
| Mongo unreachable | Automatic: events keep going to `data/runs/<id>/`; import later with `db.import_events_jsonl`. |
| Wi-Fi gone entirely | Restart with `REPLAY_EVENTS=<good session id>`; replays the recorded run at 1x, labelled as a replay. |
| Wrong flag on screen | Operator kill button. Killed is final. |
| Wrong speaker label | Kill the affected flags; fix `timeline.json` after the demo (never live). |
| Metrics show loop lag > 100 ms | Machine overloaded: close other apps. |
