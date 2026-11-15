import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
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


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str | None]:
    return dict(dotenv_values(ENV_FILE)) if ENV_FILE.is_file() else {}


def env_float(name: str, default: float) -> float:
    """A timing knob read at import time: the process environment first, then backend/.env.

    Module constants (LANE_TIMEOUT_S, ...) are not Settings fields, and nothing loads .env into
    os.environ, so without the .env lookup a value set there was silently ignored. Tests set
    DEBATE_AGENT_DOTENV=0 so a developer's .env cannot change the timings they assert on.
    """
    raw = os.environ.get(name)
    if raw is None and os.environ.get("DEBATE_AGENT_DOTENV", "1") != "0":
        raw = _dotenv().get(name)
    try:
        value = float(raw) if raw not in (None, "") else default
    except ValueError:
        return default
    return value if value > 0 else default


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
    groq_api_key_2: SecretStr = SecretStr("")  # optional: used when a call on GROQ_API_KEY fails
    gemini_api_key: SecretStr = SecretStr("")
    google_factcheck_key: SecretStr = SecretStr("")
    mongodb_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    mongodb_db: str = "debate_agent"
    llm_model_fast: str = ""
    llm_model_gemini: str = ""
    llm_model_gemini_fallback: str = ""
    llm_provider: Literal["groq", "gemini"] = "groq"
    # Gemini spend guard (app/llm/budget.py): paid key until the budget is spent, then key 2 (free tier).
    gemini_api_key_2: SecretStr = SecretStr("")
    gemini_budget_usd: float = Field(default=7.0, ge=0)
    gemini_price_in_per_m: float = Field(default=0.75, ge=0)
    gemini_price_out_per_m: float = Field(default=3.75, ge=0)
    gemini_price_grounded_call: float = Field(default=0.014, ge=0)
    replay_transcript: bool = False
    llm_cache: bool = True  # reuse stored analysis answers across runs (app/llm/cache.py); 0 = always call models
    replay_events: str = ""
    audience_min_conf: float = Field(default=0.80, ge=0, le=1)
    intel: Literal["fake", "real", "bank"] = "fake"  # fake lanes | full real intel | bank: real claims, Groq only

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
