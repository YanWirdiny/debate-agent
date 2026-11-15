"""Gemini spend guard: paid key until GEMINI_BUDGET_USD, then the free key; 429 -> free key."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.llm import budget, client


class Output(BaseModel):
    ok: bool


def secret(v):
    return SimpleNamespace(get_secret_value=lambda: v)


def settings(paid="paid-key", free="free-key", budget_usd=7.0):
    return SimpleNamespace(
        llm_provider="groq", llm_model_fast="m-fast", llm_model_gemini="m-gem", llm_model_gemini_fallback="m-fb",
        groq_api_key=secret("g"), gemini_api_key=secret(paid), gemini_api_key_2=secret(free),
        gemini_budget_usd=budget_usd, gemini_price_in_per_m=1.0, gemini_price_out_per_m=10.0, gemini_price_grounded_call=0.01)


class RateLimited(Exception):
    code = 429


def test_cost_counts_thinking_and_grounding():
    s = settings()
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 50_000, "thinking_tokens": 50_000}
    assert budget.cost_usd(s, usage, grounded=False) == pytest.approx(1.0 + 1.0)
    assert budget.cost_usd(s, usage, grounded=True) == pytest.approx(2.01)


async def test_paid_key_is_charged_then_free_key_after_budget(monkeypatch, gemini_ledger):
    monkeypatch.setattr(client, "_settings", lambda: settings(budget_usd=0.02))
    gem = AsyncMock(return_value=('{"ok":true}', {"prompt_tokens": 10_000, "completion_tokens": 1_000}))  # $0.02
    monkeypatch.setattr(client, "_gemini", gem)
    await client.gemini_json("summarize", {}, Output)
    assert gem.call_args.args[1] == "paid-key"
    ledger = json.loads(gemini_ledger.read_text())["spent_usd"]
    assert ledger == {budget.fingerprint("paid-key"): pytest.approx(0.02)}
    assert "paid-key" not in gemini_ledger.read_text()  # fingerprint only
    await client.gemini_json("summarize", {}, Output)
    assert gem.call_args.args[1] == "free-key"
    # Free-key calls are not charged.
    assert json.loads(gemini_ledger.read_text())["spent_usd"][budget.fingerprint("paid-key")] == pytest.approx(0.02)


async def test_budget_survives_restart(monkeypatch, gemini_ledger):
    gemini_ledger.write_text(json.dumps({"spent_usd": {budget.fingerprint("paid-key"): 7.5}}))
    budget.BUDGET.reset_cache()
    monkeypatch.setattr(client, "_settings", lambda: settings())
    gem = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, "_gemini", gem)
    await client.gemini_json("summarize", {}, Output)
    assert gem.call_args.args[1] == "free-key"


async def test_rate_limit_on_paid_key_retries_on_free_key(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings())
    calls = []

    async def gem(model, key, *args):
        calls.append(key)
        if key == "paid-key":
            raise RateLimited()
        return '{"ok":true}', {}
    monkeypatch.setattr(client, "_gemini", gem)
    await client.gemini_json("summarize", {}, Output, grounded=True, cache_name="c")
    assert calls == ["paid-key", "free-key"]
    await client.gemini_json("summarize", {}, Output)  # paid key cooling down
    assert calls[-1] == "free-key" and calls.count("paid-key") == 1


async def test_no_second_key_and_budget_spent_fails_cleanly(monkeypatch, gemini_ledger):
    gemini_ledger.write_text(json.dumps({"spent_usd": {budget.fingerprint("paid-key"): 9}}))
    budget.BUDGET.reset_cache()
    monkeypatch.setattr(client, "_settings", lambda: settings(free=""))
    gem = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, "_gemini", gem)
    with pytest.raises(client.LLMError):
        await client.gemini_json("summarize", {}, Output)
    assert gem.await_count == 0


async def test_groq_fallback_uses_budgeted_gemini(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings())
    monkeypatch.setattr(client, "_groq", AsyncMock(side_effect=RuntimeError("down")))
    gem = AsyncMock(return_value=('{"ok":true}', {"prompt_tokens": 5}))
    monkeypatch.setattr(client, "_gemini", gem)
    r = await client.groq_json("claim_extract", {}, Output)
    assert r.provider == "gemini" and r.model_id == "m-fb" and gem.call_args.args[1] == "paid-key"


async def test_fast_models_rotate_and_retry_on_the_next(monkeypatch):
    s = settings()
    s.llm_model_fast = "openai/gpt-oss-20b, qwen/qwen3.8-27b"
    monkeypatch.setattr(client, "_settings", lambda: s)
    used = []

    async def groq(model, *args):
        used.append(model)
        if len(used) == 1:
            raise RuntimeError("429")
        return '{"ok":true}', {}
    monkeypatch.setattr(client, "_groq", groq)
    await client.groq_json("claim_extract", {}, Output)
    assert used[0] != used[1] and set(used) == {"openai/gpt-oss-20b", "qwen/qwen3.8-27b"}
    await client.groq_json("claim_extract", {}, Output)
    await client.groq_json("claim_extract", {}, Output)
    assert used[2] != used[3]  # consecutive calls alternate


def test_reasoning_turned_down_for_reasoning_models():
    assert client._reasoning("openai/gpt-oss-20b") == {"reasoning_effort": "low"}
    assert client._reasoning("qwen/qwen3.8-27b") == {"reasoning_effort": "none"}
    assert client._reasoning("llama-3.1-8b-instant") == {}
