"""Bounded structured-output calls. Never log provider exceptions or prompt data."""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import math
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError
from app.config import Settings, env_float
from app.llm.cache import CACHE, cache_key
from app.llm.budget import BUDGET, _secret, cost_usd, fingerprint, is_rate_limited
from app.models import LLMResult

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).with_name("prompts")
# The fallback is not started with less than this left.
MIN_FALLBACK_BUDGET_S = 0.35
# asyncio.timeout fires slightly after the requested deadline. Hold this extra
# back from the primary so a slow attempt still leaves MIN_FALLBACK_BUDGET_S.
FALLBACK_SLACK_S = 0.03
# Groq + Gemini as one pair: the primary never takes the whole deadline. It leaves the fallback a
# share of it, up to what that model really needs (Gemini flash-lite ~2-3 s, Groq < 1 s). With
# the 1.2 s design budget this is MIN_FALLBACK_BUDGET_S; with LANE_TIMEOUT_S=10 Gemini gets 3 s.
FALLBACK_SHARE = 0.3
GEMINI_FALLBACK_RESERVE_S = env_float("GEMINI_FALLBACK_RESERVE_S", 3.0)
GROQ_FALLBACK_RESERVE_S = env_float("GROQ_FALLBACK_RESERVE_S", 1.5)
# LLM_MODEL_FAST may list several Groq models ("a,b,c"). Groq rate limits are per model, so calls
# rotate across them and a retry goes to the next one. A model that answers 429 sits out for its
# Retry-After (capped), so calls stop burning their budget on it and go to the next model or Gemini.
# With GROQ_API_KEY_2 set, a failed call on key 1 is tried again on key 2 (another account, its own
# limits); cooldowns are per key and model.
_GROQ_RR = itertools.count()
GROQ_COOLDOWN: dict[str, float] = {}  # "<key fingerprint>:<model>" -> monotonic time it may be used again
GROQ_COOLDOWN_DEFAULT_S = 20.0
GROQ_COOLDOWN_MAX_S = 60.0


def fallback_reserve(provider: str | None, total: float) -> float:
    """Seconds of `total` held back from the primary for the fallback `provider`."""
    if provider is None:
        return 0.0
    cap = GROQ_FALLBACK_RESERVE_S if provider == "groq" else GEMINI_FALLBACK_RESERVE_S
    return max(MIN_FALLBACK_BUDGET_S + FALLBACK_SLACK_S, min(cap, total * FALLBACK_SHARE))


def _groq_rate_limited(exc: BaseException) -> float | None:
    """Cooldown seconds for a Groq 429, else None. Reads status and Retry-After only, never the body."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 429:
        return None
    try:
        after = float(exc.response.headers.get("retry-after", ""))
    except ValueError:
        after = GROQ_COOLDOWN_DEFAULT_S
    return min(max(after, 1.0), GROQ_COOLDOWN_MAX_S)


def _reasoning(model: str) -> dict:
    """Reasoning models spend max_tokens thinking and miss the 0.8 s budget: turn it down."""
    if model.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low"}
    if "qwen3" in model:
        return {"reasoning_effort": "none"}
    return {}


class LLMError(RuntimeError):
    """Sanitized failure; provider responses may contain credentials or transcript text."""


def _settings() -> Settings:
    # Read configuration without demanding unrelated STT/Fact Check credentials.
    return Settings(replay_transcript=True)


def _prompt(prompt_id: str) -> tuple[str, str]:
    if not prompt_id.replace("_", "").isalnum():
        raise ValueError("invalid prompt id")
    text = (PROMPTS / f"{prompt_id}.md").read_text()
    if "Phase 0 placeholder" in text:
        raise LLMError("prompt is not implemented")
    return text, hashlib.sha256(text.encode()).hexdigest()


async def _groq(model: str, key: str, prompt: str, payload: str, schema: type[BaseModel], remaining: float):
    # JSON mode works across Groq models; schema is supplied in the instruction and
    # validated locally (strict JSON-schema mode is model-dependent).
    async with httpx.AsyncClient(timeout=remaining) as client:
        response = await client.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
            "model": model, "temperature": 0, "max_tokens": 1024,
            "response_format": {"type": "json_object"}, **_reasoning(model),
            "messages": [
                {"role": "system", "content": prompt + "\nReturn JSON matching this schema:\n" + json.dumps(schema.model_json_schema())},
                {"role": "user", "content": payload},
            ],
        })
        response.raise_for_status()
        body = response.json()
        if body["choices"][0].get("finish_reason") != "stop":
            raise LLMError("incomplete provider output")
        return body["choices"][0]["message"]["content"], body.get("usage", {})


async def _gemini(model: str, key: str, prompt: str, payload: str, schema: type[BaseModel], remaining: float, grounded: bool, cache_name: str | None):
    from google import genai
    from google.genai import types

    # response_json_schema, not response_schema: the SDK turns a pydantic model with extra="forbid"
    # into an `additional_properties` field the Gemini API rejects (400 INVALID_ARGUMENT).
    config = dict(temperature=0, max_output_tokens=2048, response_mime_type="application/json",
                  response_json_schema=schema.model_json_schema())
    if cache_name:
        config["cached_content"] = cache_name
        # Gemini does not permit system_instruction alongside cached_content.
        payload = prompt + "\nUntrusted input JSON:\n" + payload
    else:
        config["system_instruction"] = prompt
    if grounded:
        config["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    async with genai.Client(api_key=key, http_options=types.HttpOptions(timeout=max(1, int(remaining * 1000)), retry_options=types.HttpRetryOptions(attempts=1))).aio as client:
        response = await client.models.generate_content(model=model, contents=payload, config=types.GenerateContentConfig(**config))
        if not response.candidates or str(response.candidates[0].finish_reason).split(".")[-1] != "STOP":
            raise LLMError("incomplete provider output")
        usage = response.usage_metadata
        return response.text, {"prompt_tokens": usage.prompt_token_count or 0, "completion_tokens": usage.candidates_token_count or 0,
                               "thinking_tokens": getattr(usage, "thoughts_token_count", 0) or 0, "cached_tokens": usage.cached_content_token_count or 0} if usage else {}


def _error_kind(exc: BaseException) -> str:
    """Exception class plus HTTP/gRPC status only (e.g. "ClientError 429 RESOURCE_EXHAUSTED"). Never the
    message: provider bodies can echo prompts or keys."""
    parts = [type(exc).__name__]
    if isinstance(exc, httpx.HTTPStatusError):
        parts.append(str(exc.response.status_code))
    for attr in ("code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, str)) and not isinstance(value, bool) and str(value) not in parts:
            parts.append(str(value)[:40])
    return " ".join(parts)


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, httpx.TimeoutException))


async def _call(prompt_id: str, variables: dict, schema: type[BaseModel], timeout: float, primary: str, grounded: bool = False, cache_name: str | None = None) -> LLMResult:
    """Analysis cache first (app/llm/cache.py); on a miss the models, and a valid answer is stored."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if not getattr(_settings(), "llm_cache", False):
        return await _call_models(prompt_id, variables, schema, timeout, primary, grounded, cache_name)
    _, prompt_hash = _prompt(prompt_id)
    key = cache_key(prompt_id, prompt_hash, schema.__name__, grounded, variables)
    hit = CACHE.get(key)
    if hit is not None:
        data, model_id, provider = hit
        try:
            data = schema.model_validate(data).model_dump(mode="json")
        except ValidationError:
            hit = None  # schema changed since it was stored: ask the models again
        else:
            log.info("llm cache hit prompt=%s model=%s", prompt_id, model_id)
            return LLMResult(data=data, model_id=model_id, prompt_hash=prompt_hash, provider=provider, usage={})
    result = await _call_models(prompt_id, variables, schema, timeout, primary, grounded, cache_name)
    CACHE.put(key, prompt_id, result.data, result.model_id, result.provider)
    return result


async def _call_models(prompt_id: str, variables: dict, schema: type[BaseModel], timeout: float, primary: str, grounded: bool = False, cache_name: str | None = None) -> LLMResult:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    prompt, prompt_hash = _prompt(prompt_id)
    payload = json.dumps(variables, ensure_ascii=False)
    settings = _settings()
    # Providers: "groq" (LLM_MODEL_FAST rotation), "gemini" (LLM_MODEL_GEMINI), "fallback"
    # (LLM_MODEL_GEMINI_FALLBACK). A hot-path call (groq_json) always has the other family as its
    # fallback: Groq -> Gemini normally, Gemini -> Groq with LLM_PROVIDER=gemini. gemini_json
    # (grounded verdicts, prefetch, summary) has none: Groq cannot search the web.
    fallback: str | None = None
    if primary == "groq":
        if settings.llm_provider == "gemini":
            primary, fallback = "gemini", "groq"
        else:
            fallback = "fallback"
    fast_models = [m.strip() for m in str(settings.llm_model_fast).split(",") if m.strip()]
    groq_turn = [next(_GROQ_RR)]
    groq_keys = [(label, key) for label, key in (("1", _secret(settings, "groq_api_key")),
                                                 ("2", _secret(settings, "groq_api_key_2"))) if key]
    if len(groq_keys) == 2 and groq_keys[0][1] == groq_keys[1][1]:
        groq_keys.pop()

    def cool_id(key: str, model: str) -> str:
        return f"{fingerprint(key)}:{model}"

    def groq_keys_ready(model: str) -> list[tuple[str, str]]:
        now = time.monotonic()
        return [(label, key) for label, key in groq_keys if GROQ_COOLDOWN.get(cool_id(key, model), 0.0) <= now]

    def model_for(provider: str) -> str:
        if provider == "groq":
            ready = [m for m in fast_models if groq_keys_ready(m)] if groq_keys else fast_models
            return ready[groq_turn[0] % len(ready)] if ready else ""
        if provider == "fallback":
            return settings.llm_model_gemini_fallback
        return settings.llm_model_gemini

    def warn_attempt(provider: str, model: str, exc: BaseException) -> None:
        # SDK exceptions can include raw bodies. Log only provider, model, and the static prompt hash.
        message = "llm attempt failed provider=%s model=%s prompt_hash=%s error=%s"
        if not isinstance(exc, (ValidationError, ValueError, KeyError, IndexError, httpx.HTTPError, TimeoutError, LLMError)):
            message = "llm SDK attempt failed provider=%s model=%s prompt_hash=%s error=%s"
        log.warning(message, "groq" if provider == "groq" else "gemini", model, prompt_hash, _error_kind(exc))

    async def groq_keyed(model: str, remaining: float) -> tuple[str, dict, str, str]:
        """GROQ_API_KEY first, then GROQ_API_KEY_2 on any error (except running out of time). When
        every key of `model` is rate limited, the other ready LLM_MODEL_FAST models are tried too.
        Returns (raw, usage, key label, model used)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + remaining
        models = [model] + [m for m in fast_models if m != model]
        last: BaseException | None = None
        for m in models:
            keys = groq_keys_ready(m)
            for i, (label, key) in enumerate(keys):
                left = end - loop.time()
                if left <= 0:
                    raise TimeoutError
                try:
                    async with asyncio.timeout(left):
                        raw, usage = await _groq(m, key, prompt, payload, schema, left)
                    return raw, usage, label, m
                except Exception as exc:
                    last = exc
                    cool = _groq_rate_limited(exc)
                    if cool is not None:
                        GROQ_COOLDOWN[cool_id(key, m)] = time.monotonic() + cool
                        log.warning("groq key=%s model=%s rate limited; skipped for %.0fs", label, m, cool)
                    if _is_timeout(exc):
                        raise
                    if i + 1 < len(keys):
                        log.warning("groq key=%s model=%s failed; trying key=%s", label, m, keys[i + 1][0])
            # Only a rate limit moves on to another model; other errors would repeat there.
            if last is None or _groq_rate_limited(last) is None:
                break
        if last is not None:
            raise last
        raise LLMError("provider key or model is not configured")

    async def gemini_budgeted(model: str, remaining: float) -> tuple[str, dict, str]:
        """Paid key while under GEMINI_BUDGET_USD, else (or on a 429) the free GEMINI_API_KEY_2."""
        keys = BUDGET.keys(settings)
        if not keys:
            raise LLMError("no gemini key available (unset, or paid budget spent and no GEMINI_API_KEY_2)")
        loop = asyncio.get_running_loop()
        end = loop.time() + remaining
        for i, gk in enumerate(keys):
            left = end - loop.time()
            if left <= 0:
                break
            try:
                async with asyncio.timeout(left):
                    # A context cache belongs to the paid key's project.
                    raw, usage = await _gemini(model, gk.key, prompt, payload, schema, left, grounded,
                                               cache_name if gk.tier == "paid" else None)
            except Exception as exc:
                # Any failure on the paid key (not only a 429) moves on to GEMINI_API_KEY_2; a 429
                # also keeps the paid key out for a while. Running out of time is not retried.
                if gk.tier == "paid" and i + 1 < len(keys) and not _is_timeout(exc):
                    if is_rate_limited(exc):
                        BUDGET.rate_limited(gk.key)
                    else:
                        log.warning("gemini paid key failed model=%s; trying the free key", model)
                    continue
                raise
            if gk.tier == "paid":
                BUDGET.add(gk.key, cost_usd(settings, usage, grounded))
            return raw, usage, gk.tier
        raise TimeoutError

    async def invoke(provider: str, model: str, remaining: float) -> LLMResult:
        is_groq = provider == "groq"
        if not model:
            raise LLMError("provider key or model is not configured")
        tier = ""
        async with asyncio.timeout(remaining):
            if is_groq:
                raw, usage, tier, model = await groq_keyed(model, remaining)
            else:
                raw, usage, tier = await gemini_budgeted(model, remaining)
            data = schema.model_validate_json(raw, strict=True).model_dump(mode="json")
        safe_usage = {k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool)}
        log.info("llm provider=%s model=%s%s prompt_hash=%s usage=%s", "groq" if is_groq else "gemini", model,
                 f" key={tier}" if tier else "", prompt_hash, safe_usage)
        return LLMResult(data=data, model_id=model, prompt_hash=prompt_hash, provider="groq" if is_groq else "gemini", usage=safe_usage)

    try:
        async with asyncio.timeout(timeout):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            reserve = fallback_reserve(fallback, timeout)
            for attempt in range(2):  # first try + one retry
                if attempt:
                    groq_turn[0] += 1  # retry on the next fast model (its own rate limit)
                model = model_for(primary)
                if primary == "groq" and not model:
                    log.info("llm every groq model is cooling down; straight to fallback prompt_hash=%s", prompt_hash)
                    break
                budget_left = deadline - loop.time()
                # Leave the fallback its window plus a little slack (asyncio.timeout fires late).
                # When the deadline is already shorter than that reserve, spend what is left here.
                remaining = budget_left - reserve if budget_left > reserve else budget_left
                if remaining <= 0:
                    break
                try:
                    return await invoke(primary, model, remaining)
                except Exception as exc:
                    warn_attempt(primary, model, exc)
                    if _is_timeout(exc) or attempt == 1:
                        break  # a timeout is not retried; the one retry is already spent
            budget_left = deadline - loop.time()
            if fallback and budget_left >= MIN_FALLBACK_BUDGET_S:
                model = model_for(fallback)
                if model:
                    try:
                        return await invoke(fallback, model, budget_left)
                    except Exception as exc:
                        warn_attempt(fallback, model, exc)
                else:
                    log.warning("llm fallback skipped (no ready model) prompt_hash=%s", prompt_hash)
            elif fallback:
                log.warning("llm fallback skipped prompt_hash=%s remaining_s=%.3f", prompt_hash, budget_left)
    except TimeoutError:
        pass
    raise LLMError("structured-output call exhausted its bounded attempts") from None


async def groq_json(prompt_id: str, variables: dict, schema: type[BaseModel], timeout: float = 1.2) -> LLMResult:
    return await _call(prompt_id, variables, schema, timeout, "groq")


async def gemini_json(prompt_id: str, variables: dict, schema: type[BaseModel], grounded: bool = False, cache_name: str | None = None, timeout: float = 20.0) -> LLMResult:
    return await _call(prompt_id, variables, schema, timeout, "gemini", grounded, cache_name)
