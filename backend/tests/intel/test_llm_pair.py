"""Groq + Gemini as one pair: fallback in both directions, a real fallback window on long budgets,
429 cooldown per Groq model, and timing knobs read from backend/.env."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel

from app import config
from app.llm import client


class Output(BaseModel):
    ok: bool


def secret(v):
    return SimpleNamespace(get_secret_value=lambda: v)


def settings(provider="groq", fast="m-fast", groq2="", gemini2=""):
    return SimpleNamespace(
        llm_provider=provider, llm_model_fast=fast, llm_model_gemini="m-gem", llm_model_gemini_fallback="m-fb",
        groq_api_key=secret("g"), groq_api_key_2=secret(groq2), gemini_api_key=secret("paid"), gemini_api_key_2=secret(gemini2),
        gemini_budget_usd=7.0, gemini_price_in_per_m=1.0, gemini_price_out_per_m=10.0, gemini_price_grounded_call=0.01)


def too_many(retry_after=None):
    request = httpx.Request("POST", "https://example.invalid")
    headers = {"retry-after": retry_after} if retry_after else {}
    return httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request, headers=headers))


def test_reserve_scales_with_the_budget():
    floor = client.MIN_FALLBACK_BUDGET_S + client.FALLBACK_SLACK_S
    assert client.fallback_reserve(None, 10) == 0
    assert client.fallback_reserve("fallback", 0.8) == pytest.approx(floor)  # 1.2 s design budget unchanged
    assert client.fallback_reserve("fallback", 10) == pytest.approx(client.GEMINI_FALLBACK_RESERVE_S)
    assert client.fallback_reserve("groq", 10) == pytest.approx(client.GROQ_FALLBACK_RESERVE_S)
    assert client.fallback_reserve("fallback", 5) == pytest.approx(1.5)


async def test_long_budget_leaves_gemini_a_usable_window(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings())
    allotted = {}

    async def hung_groq(*args):
        allotted["groq"] = args[-1]
        await asyncio.sleep(60)

    async def gemini(*args):
        allotted["gemini"] = args[5]
        return '{"ok":true}', {}
    monkeypatch.setattr(client, "_groq", hung_groq)
    monkeypatch.setattr(client, "_gemini", gemini)
    monkeypatch.setattr(client, "GEMINI_FALLBACK_RESERVE_S", 0.5)  # keep the test short
    r = await client.groq_json("claim_extract", {}, Output, timeout=2.0)
    assert r.provider == "gemini"
    assert allotted["groq"] == pytest.approx(1.5, abs=0.02)
    assert allotted["gemini"] > 0.4


async def test_gemini_primary_falls_back_to_groq(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings(provider="gemini"))
    gemini = AsyncMock(side_effect=RuntimeError("down"))
    groq = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, "_gemini", gemini)
    monkeypatch.setattr(client, "_groq", groq)
    r = await client.groq_json("claim_extract", {}, Output, timeout=5.0)
    assert r.provider == "groq" and r.model_id == "m-fast"
    assert gemini.await_count == 2 and gemini.await_args_list[0].args[0] == "m-gem"
    assert groq.await_count == 1


async def test_gemini_json_keeps_no_groq_fallback(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings(provider="gemini"))
    monkeypatch.setattr(client, "_gemini", AsyncMock(side_effect=RuntimeError("down")))
    groq = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, "_groq", groq)
    with pytest.raises(client.LLMError):
        await client.gemini_json("summarize", {}, Output, grounded=True, timeout=1.0)
    assert groq.await_count == 0


async def test_rate_limited_groq_model_sits_out(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings(fast="a,b"))
    used = []

    async def groq(model, *args):
        used.append(model)
        if model == "a":
            raise too_many("7")
        return '{"ok":true}', {}
    monkeypatch.setattr(client, "_groq", groq)
    monkeypatch.setattr(client, "_gemini", AsyncMock(return_value=('{"ok":true}', {})))
    for _ in range(4):
        r = await client.groq_json("claim_extract", {}, Output, timeout=1.0)
        assert r.provider == "groq" and r.model_id == "b"
    assert used.count("a") == 1  # after its 429, "a" is skipped
    assert any(k.endswith(":a") for k in client.GROQ_COOLDOWN)


async def test_all_groq_models_cooling_goes_straight_to_gemini(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings())
    groq = AsyncMock(side_effect=too_many())
    gemini = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, "_groq", groq)
    monkeypatch.setattr(client, "_gemini", gemini)
    assert (await client.groq_json("claim_extract", {}, Output, timeout=1.0)).provider == "gemini"
    assert (await client.groq_json("claim_extract", {}, Output, timeout=1.0)).provider == "gemini"
    assert groq.await_count == 1
    # The cooled-down call gave Gemini the whole budget, not just the reserve.
    assert gemini.await_args_list[1].args[5] > 0.9


def test_env_float_reads_dotenv(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("LANE_TIMEOUT_S=7.5\nBAD=x\nNEG=-1\n")
    monkeypatch.setattr(config, "ENV_FILE", env)
    config._dotenv.cache_clear()
    monkeypatch.delenv("LANE_TIMEOUT_S", raising=False)
    try:
        monkeypatch.setenv("DEBATE_AGENT_DOTENV", "1")
        assert config.env_float("LANE_TIMEOUT_S", 1.2) == 7.5
        assert config.env_float("BAD", 1.2) == 1.2 and config.env_float("NEG", 1.2) == 1.2
        monkeypatch.setenv("LANE_TIMEOUT_S", "3")
        assert config.env_float("LANE_TIMEOUT_S", 1.2) == 3.0  # the process environment wins
        monkeypatch.delenv("LANE_TIMEOUT_S")
        monkeypatch.setenv("DEBATE_AGENT_DOTENV", "0")
        assert config.env_float("LANE_TIMEOUT_S", 1.2) == 1.2
    finally:
        config._dotenv.cache_clear()


async def test_groq_error_on_key_1_tries_key_2(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings(groq2="g2"))
    keys = []

    async def groq(model, key, *args):
        keys.append(key)
        if key == "g":
            raise RuntimeError("key 1 broken")
        return '{"ok":true}', {}
    monkeypatch.setattr(client, "_groq", groq)
    gemini = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, "_gemini", gemini)
    r = await client.groq_json("claim_extract", {}, Output, timeout=1.0)
    assert r.provider == "groq" and keys == ["g", "g2"] and gemini.await_count == 0


async def test_groq_429_on_key_1_keeps_using_key_2(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings(groq2="g2"))
    keys = []

    async def groq(model, key, *args):
        keys.append(key)
        if key == "g":
            raise too_many("30")
        return '{"ok":true}', {}
    monkeypatch.setattr(client, "_groq", groq)
    monkeypatch.setattr(client, "_gemini", AsyncMock(return_value=('{"ok":true}', {})))
    for _ in range(3):
        assert (await client.groq_json("claim_extract", {}, Output, timeout=1.0)).provider == "groq"
    assert keys == ["g", "g2", "g2", "g2"]  # key 1 sits out its cooldown


async def test_gemini_error_on_paid_key_tries_free_key(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings(provider="gemini", gemini2="free"))
    keys = []

    async def gemini(model, key, *args):
        keys.append(key)
        if key == "paid":
            raise RuntimeError("500 from paid project")
        return '{"ok":true}', {}
    monkeypatch.setattr(client, "_gemini", gemini)
    r = await client.gemini_json("summarize", {}, Output, timeout=2.0)
    assert r.provider == "gemini" and keys == ["paid", "free"]


async def test_model_rate_limited_on_both_keys_moves_to_next_model(monkeypatch):
    monkeypatch.setattr(client, "_settings", lambda: settings(provider="gemini", fast="a,b", groq2="g2"))
    monkeypatch.setattr(client, "_gemini", AsyncMock(side_effect=RuntimeError("down")))
    used = []

    async def groq(model, key, *args):
        used.append((model, key))
        if model == "a":
            raise too_many("30")
        return '{"ok":true}', {}
    monkeypatch.setattr(client, "_groq", groq)
    # turn 1, +1 for the Gemini retry = 2 -> "a" of ["a", "b"]: the fallback starts on the limited model.
    monkeypatch.setattr(client, "_GROQ_RR", __import__("itertools").count(1))
    r = await client.groq_json("claim_extract", {}, Output, timeout=5.0)
    assert r.provider == "groq" and r.model_id == "b"
    assert used == [("a", "g"), ("a", "g2"), ("b", "g")]
