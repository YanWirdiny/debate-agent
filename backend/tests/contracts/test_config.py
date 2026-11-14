from pathlib import Path
from app.config import Settings

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
