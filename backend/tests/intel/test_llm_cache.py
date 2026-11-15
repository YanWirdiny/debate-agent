"""Analysis cache: a repeated call is answered from the cache, new answers are added, failures are not."""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.llm import cache as cache_mod
from app.llm import client


class Output(BaseModel):
    ok: bool


def secret(v):
    return SimpleNamespace(get_secret_value=lambda: v)


@pytest.fixture
def cached(monkeypatch, tmp_path):
    c = cache_mod.AnalysisCache(tmp_path / "cache.sqlite")
    monkeypatch.setattr(client, "CACHE", c)
    monkeypatch.setattr(client, "_settings", lambda: SimpleNamespace(
        llm_cache=True, llm_provider="groq", llm_model_fast="m-fast", llm_model_gemini="m-gem",
        llm_model_gemini_fallback="m-fb", groq_api_key=secret("g"), groq_api_key_2=secret(""),
        gemini_api_key=secret("paid"), gemini_api_key_2=secret(""), gemini_budget_usd=7.0,
        gemini_price_in_per_m=1.0, gemini_price_out_per_m=10.0, gemini_price_grounded_call=0.01))
    yield c
    c.close()


async def test_second_identical_call_is_free(monkeypatch, cached):
    groq = AsyncMock(return_value=('{"ok":true}', {"prompt_tokens": 9}))
    monkeypatch.setattr(client, "_groq", groq)
    first = await client.groq_json("fallacy", {"sentence": "a"}, Output)
    second = await client.groq_json("fallacy", {"sentence": "a"}, Output)
    assert groq.await_count == 1
    assert second.data == first.data and second.model_id == "m-fast" and second.provider == "groq"
    assert second.usage == {}  # no tokens spent
    assert cached.stats()["hits"] == 1 and cached.stats()["stored"] == {"fallacy": 1}


async def test_new_inputs_are_added(monkeypatch, cached):
    groq = AsyncMock(side_effect=[('{"ok":true}', {}), ('{"ok":false}', {})])
    monkeypatch.setattr(client, "_groq", groq)
    a = await client.groq_json("fallacy", {"sentence": "a"}, Output)
    b = await client.groq_json("fallacy", {"sentence": "b"}, Output)
    assert groq.await_count == 2 and a.data != b.data
    assert cached.stats()["stored"] == {"fallacy": 2}


async def test_rolling_summary_is_not_part_of_the_key(monkeypatch, cached):
    groq = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, "_groq", groq)
    await client.groq_json("claim_extract", {"sentence": "a", "rolling_summary": ""}, Output)
    await client.groq_json("claim_extract", {"sentence": "a", "rolling_summary": "late summary"}, Output)
    assert groq.await_count == 1


async def test_prompt_or_grounding_change_is_a_miss():
    k = cache_mod.cache_key
    kw = {"model": "m-fast", "provider": "groq"}
    base = k("fallacy", "h1", "Output", False, {"sentence": "a"}, **kw)
    assert base == k("fallacy", "h1", "Output", False, {"sentence": "a"}, **kw)
    assert base != k("fallacy", "h2", "Output", False, {"sentence": "a"}, **kw)  # prompt edited
    assert base != k("fallacy", "h1", "Output", True, {"sentence": "a"}, **kw)
    assert base != k("claim_extract", "h1", "Output", False, {"sentence": "a"}, **kw)
    assert base != k("fallacy", "h1", "Output", False, {"sentence": "a"}, model="m-other", provider="groq")
    assert base != k("fallacy", "h1", "Output", False, {"sentence": "a"}, model="m-fast", provider="gemini")


async def test_failures_are_not_cached(monkeypatch, cached):
    monkeypatch.setattr(client, "_groq", AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(client, "_gemini", AsyncMock(side_effect=RuntimeError("down")))
    with pytest.raises(client.LLMError):
        await client.groq_json("fallacy", {"sentence": "a"}, Output)
    assert cached.stats()["stored"] == {}
    good = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, "_groq", good)
    assert (await client.groq_json("fallacy", {"sentence": "a"}, Output)).data == {"ok": True}
    assert good.await_count == 1


async def test_stored_answer_that_no_longer_fits_the_schema_is_asked_again(monkeypatch, cached):
    key = cache_mod.cache_key(
        "fallacy", client._prompt("fallacy")[1], "Output", False, {"sentence": "a"}, model="m-fast", provider="groq")
    cached.put(key, "fallacy", {"old_field": 1}, "m-old", "groq")
    groq = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, "_groq", groq)
    assert (await client.groq_json("fallacy", {"sentence": "a"}, Output)).data == {"ok": True}
    assert groq.await_count == 1


async def test_cache_survives_restart(monkeypatch, cached, tmp_path):
    monkeypatch.setattr(client, "_groq", AsyncMock(return_value=('{"ok":true}', {})))
    await client.groq_json("fallacy", {"sentence": "a"}, Output)
    cached.close()
    reopened = cache_mod.AnalysisCache(cached.path)
    monkeypatch.setattr(client, "CACHE", reopened)
    groq = AsyncMock(return_value=('{"ok":false}', {}))
    monkeypatch.setattr(client, "_groq", groq)
    assert (await client.groq_json("fallacy", {"sentence": "a"}, Output)).data == {"ok": True}
    assert groq.await_count == 0
    reopened.close()


def _settings(**overrides):
    base = dict(
        llm_cache=True, llm_provider="groq", llm_model_fast="m-fast", llm_model_gemini="m-gem",
        llm_model_gemini_fallback="m-fb", groq_api_key=secret("g"), groq_api_key_2=secret(""),
        gemini_api_key=secret("paid"), gemini_api_key_2=secret(""), gemini_budget_usd=7.0,
        gemini_price_in_per_m=1.0, gemini_price_out_per_m=10.0, gemini_price_grounded_call=0.01,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_changing_model_or_provider_is_a_miss(monkeypatch, cached):
    groq = AsyncMock(side_effect=[('{"ok":true}', {}), ('{"ok":false}', {})])
    gemini = AsyncMock(return_value=('{"ok":false}', {}))
    monkeypatch.setattr(client, "_groq", groq)
    monkeypatch.setattr(client, "_gemini", gemini)
    first = await client.groq_json("fallacy", {"sentence": "a"}, Output)
    monkeypatch.setattr(client, "_settings", lambda: _settings(llm_model_fast="m-other"))
    second = await client.groq_json("fallacy", {"sentence": "a"}, Output)
    assert groq.await_count == 2 and first.data != second.data
    # Spacing in the same list is still the same configuration.
    monkeypatch.setattr(client, "_settings", lambda: _settings(llm_model_fast=" m-other "))
    assert (await client.groq_json("fallacy", {"sentence": "a"}, Output)).data == second.data
    assert groq.await_count == 2
    # Gemini is a different provider, even for the same sentence.
    monkeypatch.setattr(client, "_settings", lambda: _settings())
    assert (await client.gemini_json("fallacy", {"sentence": "a"}, Output)).provider == "gemini"
    assert gemini.await_count == 1
    assert cached.stats()["stored"]["fallacy"] == 3


async def test_fallback_answer_is_not_cached(monkeypatch, cached):
    monkeypatch.setattr(client, "_groq", AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(client, "_gemini", AsyncMock(return_value=('{"ok":true}', {})))
    fallback = await client.groq_json("fallacy", {"sentence": "a"}, Output)
    assert fallback.provider == "gemini" and fallback.model_id == "m-fb"
    assert cached.stats()["stored"] == {}
    # The primary is healthy on the next run, and that answer is the one that sticks.
    groq = AsyncMock(return_value=('{"ok":false}', {}))
    monkeypatch.setattr(client, "_groq", groq)
    primary = await client.groq_json("fallacy", {"sentence": "a"}, Output)
    assert primary.data == {"ok": False} and primary.provider == "groq"
    assert groq.await_count == 1
    assert (await client.groq_json("fallacy", {"sentence": "a"}, Output)).data == {"ok": False}
    assert groq.await_count == 1
    assert cached.stats()["stored"] == {"fallacy": 1}


async def test_gemini_primary_does_not_keep_a_groq_fallback(monkeypatch, cached):
    monkeypatch.setattr(client, "_settings", lambda: _settings(llm_provider="gemini"))
    monkeypatch.setattr(client, "_gemini", AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(client, "_groq", AsyncMock(return_value=('{"ok":true}', {})))
    fallback = await client.groq_json("fallacy", {"sentence": "a"}, Output, timeout=5.0)
    assert fallback.provider == "groq"
    assert cached.stats()["stored"] == {}


async def test_cache_disk_does_not_block_the_caller(monkeypatch, cached):
    """A stalled sqlite write stays on the background thread. The lane call and a later hit return."""
    assert cached._db is not None
    started = threading.Event()
    release = threading.Event()
    real = cached._db

    class BlockingDB:
        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.lstrip().startswith("INSERT"):
                started.set()
                release.wait(3)
            return real.execute(sql, *args, **kwargs)

        def close(self):
            real.close()

    cached._db = BlockingDB()  # type: ignore[assignment]
    monkeypatch.setattr(client, "_groq", AsyncMock(return_value=('{"ok":true}', {})))
    try:
        first = await asyncio.wait_for(client.groq_json("fallacy", {"sentence": "a"}, Output), 1)
        assert first.data == {"ok": True}
        assert cached.stats()["stored"] == {"fallacy": 1}
        assert started.wait(1)  # the insert is in progress, off this thread
        t0 = time.perf_counter()
        second = await asyncio.wait_for(client.groq_json("fallacy", {"sentence": "a"}, Output), 1)
        assert second.data == {"ok": True} and second.usage == {}
        assert time.perf_counter() - t0 < 0.2
    finally:
        release.set()
