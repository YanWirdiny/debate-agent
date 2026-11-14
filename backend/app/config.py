from typing import Literal
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
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
