from pathlib import Path
import pytest
from app.config import ENV_FILE, MissingRuntimeConfig, Settings

REQUIRED_KEYS = [
    "DEEPGRAM_API_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_FACTCHECK_KEY",
]
REQUIRED_MODELS = [
    "LLM_MODEL_FAST",
    "LLM_MODEL_GEMINI",
    "LLM_MODEL_GEMINI_FALLBACK",
]

def test_env_inventory():
    example = Path(__file__).parents[2] / ".env.example"
    keys = {line.split("=", 1)[0].lower() for line in example.read_text().splitlines() if line and not line.startswith("#")}
    assert keys == Settings.model_fields.keys()

def test_env_parsing(monkeypatch):
    monkeypatch.setenv("REPLAY_TRANSCRIPT", "1")
    monkeypatch.setenv("REPLAY_EVENTS", "recorded-session")
    monkeypatch.setenv("AUDIENCE_MIN_CONF", "0.9")
    monkeypatch.setenv("GROQ_API_KEY", "secret-fixture")
    settings = Settings(_env_file=None)
    assert settings.replay_transcript is True
    assert settings.replay_events == "recorded-session"
    assert settings.audience_min_conf == .9
    assert "secret-fixture" not in repr(settings)

def _clear_runtime_env(monkeypatch):
    for name in [*REQUIRED_KEYS, *REQUIRED_MODELS, "REPLAY_TRANSCRIPT", "REPLAY_EVENTS"]:
        monkeypatch.delenv(name, raising=False)

def test_env_file_is_backend_dotenv_not_cwd(tmp_path, monkeypatch):
    assert ENV_FILE == Path(__file__).resolve().parents[2] / ".env"
    assert ENV_FILE.is_absolute()
    assert Path(Settings.model_config["env_file"]) == ENV_FILE
    if ENV_FILE.exists():
        pytest.skip("backend/.env already present")
    _clear_runtime_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GROQ_API_KEY=from-cwd\nREPLAY_TRANSCRIPT=0\n")
    ENV_FILE.write_text("GROQ_API_KEY=from-backend-file\nREPLAY_TRANSCRIPT=1\n")
    try:
        settings = Settings()
    finally:
        ENV_FILE.unlink(missing_ok=True)
    assert settings.groq_api_key.get_secret_value() == "from-backend-file"
    assert settings.replay_transcript is True

def test_missing_settings_fail_fast(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("REPLAY_TRANSCRIPT", "0")
    monkeypatch.setenv("REPLAY_EVENTS", "")
    monkeypatch.setenv("GROQ_API_KEY", "present")
    monkeypatch.setenv("LLM_MODEL_FAST", "fast-model")
    with pytest.raises(MissingRuntimeConfig) as exc:
        Settings(_env_file=None)
    message = str(exc.value)
    assert "API keys: DEEPGRAM_API_KEY, GEMINI_API_KEY, GOOGLE_FACTCHECK_KEY" in message
    assert "LLM_MODEL_* settings: LLM_MODEL_GEMINI, LLM_MODEL_GEMINI_FALLBACK" in message
    assert "GROQ_API_KEY" not in message
    assert "LLM_MODEL_FAST" not in message
    assert exc.value.missing == [
        "DEEPGRAM_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_FACTCHECK_KEY",
        "LLM_MODEL_GEMINI",
        "LLM_MODEL_GEMINI_FALLBACK",
    ]

@pytest.mark.parametrize("replay", [{"REPLAY_TRANSCRIPT": "1"}, {"REPLAY_EVENTS": "recorded-session"}])
def test_replay_fallback_skips_required_settings(monkeypatch, replay):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("REPLAY_TRANSCRIPT", "0")
    monkeypatch.setenv("REPLAY_EVENTS", "")
    for name, value in replay.items():
        monkeypatch.setenv(name, value)
    Settings(_env_file=None)

def test_complete_settings_start(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("REPLAY_TRANSCRIPT", "0")
    monkeypatch.setenv("REPLAY_EVENTS", "")
    for name in [*REQUIRED_KEYS, *REQUIRED_MODELS]:
        monkeypatch.setenv(name, "set-for-test")
    settings = Settings(_env_file=None)
    assert settings.llm_model_gemini_fallback == "set-for-test"
    assert settings.deepgram_api_key.get_secret_value() == "set-for-test"
