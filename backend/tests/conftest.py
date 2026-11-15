import os

# Before any app import: timing constants must come from code defaults, not a developer's .env.
os.environ["DEBATE_AGENT_DOTENV"] = "0"
# Tests that want the analysis cache turn it on themselves (tests/intel/test_llm_cache.py).
os.environ["LLM_CACHE"] = "0"

import pytest  # noqa: E402

from app.llm import budget, client  # noqa: E402


@pytest.fixture(autouse=True)
def groq_cooldowns():
    """A 429 in one test must not cool a Groq model for the next."""
    client.GROQ_COOLDOWN.clear()
    yield
    client.GROQ_COOLDOWN.clear()


@pytest.fixture(autouse=True)
def gemini_ledger(tmp_path, monkeypatch):
    """Never write the real Gemini spend ledger from tests."""
    monkeypatch.setattr(budget, "LEDGER", tmp_path / "gemini_spend.json")
    budget.BUDGET.reset_cache()
    yield budget.LEDGER
    budget.BUDGET.reset_cache()
