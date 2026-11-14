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
| `INTEL` | `fake` (fake lanes, no LLM keys needed) or `real` (P2's lanes) |
| `REPLAY_TRANSCRIPT` | `1` = pre-made transcript instead of Deepgram; also lets the server start without keys |
| `REPLAY_EVENTS` | `<session_id>` = replay a recorded run (no network needed) |

Check everything: `python scripts/check_setup.py` (Mongo reachable + indexes, KB loaded, Deepgram key,
each clip's audio/timeline/transcript). Fix every `FAIL`.

## 2. Prepare a clip (P1)

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
uvicorn app.main:app --workers 1          # one worker: session state is in memory
python scripts/mock_stream.py --speed 4   # no server: print what operator/audience would receive
```

Operator WebSocket `/ws/operator`: send `{"type":"load","clip_id":"<id>"}` -> `ready` (warm-up done),
then `{"type":"start","clip_id":"<id>"}` -> `started` with `t0_wall` (frontend calls `video.play()`).
`{"type":"kill","annotation_id":"..."}` removes a flag everywhere. Audience: `/ws/audience` (receive only).
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
