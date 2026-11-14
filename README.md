# debate-agent
an ai agent that can analyze  live and  videotaped  politcial debate to allow easier insight and understanding  through logical analysis fact check and  more. 

Phase 0 backend contracts are in `backend/` (Python 3.12). To install the locked dependencies and run the contract suite:

```sh
cd backend
uv sync --locked
uv run pytest tests/contracts
```

Copy `backend/.env.example` to `backend/.env` and choose provider model IDs before implementing the runtime. No credentials or external services are needed for contract tests. Runtime components, fake lanes, scripts, and prompt files are Phase 0 placeholders; callable stubs raise `NotImplementedError`.

`app/interfaces.py` defines the shared protocols and call signatures. Internal model fields in `app/models.py` complete the shapes left unspecified in the architecture. `SlowJob`, `ServerMessage`, and `ClientMessage` are discriminated unions validated with Pydantic `TypeAdapter`. `LLMResult.data` holds the JSON payload after schema validation. Lane views are frozen at the field level; the session implementation must copy nested data when constructing snapshots.

IDs use `segment_id(session_id, seq)` and `annotation_id(segment_id, lane, n)` from `app/models.py`. Sequence and lane-local annotation indexes start at zero. The 20 synthetic fixture segments contain deliberately incorrect statements and fallacies for testing, and are not debate transcripts or reviewed claim-bank evidence.
