import pytest

from app.llm import budget


@pytest.fixture(autouse=True)
def gemini_ledger(tmp_path, monkeypatch):
    """Never write the real Gemini spend ledger from tests."""
    monkeypatch.setattr(budget, "LEDGER", tmp_path / "gemini_spend.json")
    budget.BUDGET.reset_cache()
    yield budget.LEDGER
    budget.BUDGET.reset_cache()
