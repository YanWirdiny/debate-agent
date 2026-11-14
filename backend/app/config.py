from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed path: backend/.env next to the app package, not the process working directory.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

_API_KEYS = (
    "deepgram_api_key",
    "groq_api_key",
    "gemini_api_key",
    "google_factcheck_key",
)
_LLM_MODELS = (
    "llm_model_fast",
    "llm_model_gemini",
    "llm_model_gemini_fallback",
)


class MissingRuntimeConfig(RuntimeError):
    def __init__(self, missing_keys: list[str], missing_models: list[str]) -> None:
        self.missing_keys = list(missing_keys)
        self.missing_models = list(missing_models)
        self.missing = [*missing_keys, *missing_models]
        parts: list[str] = []
        if missing_keys:
            parts.append("API keys: " + ", ".join(missing_keys))
        if missing_models:
            parts.append("LLM_MODEL_* settings: " + ", ".join(missing_models))
        super().__init__(
            "Missing required settings (" + "; ".join(parts) + "). "
            f"Set them in {ENV_FILE} before startup. "
            "This check is skipped in replay/fallback mode "
            "(REPLAY_TRANSCRIPT or REPLAY_EVENTS)."
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore", case_sensitive=False)
    deepgram_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    google_factcheck_key: SecretStr = SecretStr("")
    mongodb_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    mongodb_db: str = "debate_agent"
    llm_model_fast: str = ""
    llm_model_gemini: str = ""
    llm_model_gemini_fallback: str = ""
    llm_provider: Literal["groq", "gemini"] = "groq"
    replay_transcript: bool = False
    replay_events: str = ""
    audience_min_conf: float = Field(default=0.80, ge=0, le=1)

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        if self.replay_transcript or self.replay_events.strip():
            return
        missing_keys = [
            name.upper()
            for name in _API_KEYS
            if not getattr(self, name).get_secret_value().strip()
        ]
        missing_models = [
            name.upper() for name in _LLM_MODELS if not str(getattr(self, name)).strip()
        ]
        if missing_keys or missing_models:
            raise MissingRuntimeConfig(missing_keys, missing_models)
