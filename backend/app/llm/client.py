"""Bounded structured-output calls. Never log provider exceptions or prompt data."""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import math
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError
from app.config import Settings
from app.llm.budget import BUDGET, cost_usd, is_rate_limited
from app.models import LLMResult

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).with_name("prompts")
# Realistic Gemini latency on the hot path. The fallback is not started with less than this left.
MIN_FALLBACK_BUDGET_S = 0.35
# asyncio.timeout fires slightly after the requested deadline. Hold this extra
# back from Groq so a slow attempt still leaves MIN_FALLBACK_BUDGET_S.
FALLBACK_SLACK_S = 0.03
# LLM_MODEL_FAST may list several Groq models ("a,b,c"). Groq rate limits are per model, so calls
# rotate across them and a retry goes to the next one.
_GROQ_RR = itertools.count()


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


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, httpx.TimeoutException))


async def _call(prompt_id: str, variables: dict, schema: type[BaseModel], timeout: float, primary: str, grounded: bool = False, cache_name: str | None = None) -> LLMResult:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    prompt, prompt_hash = _prompt(prompt_id)
    payload = json.dumps(variables, ensure_ascii=False)
    settings = _settings()
    if primary == "groq" and settings.llm_provider == "gemini":
        primary = "gemini"
    # One primary retry, and only after a fast failure. A timeout is not retried:
    # the first attempt already held the deadline minus the Gemini reserve.
    has_fallback = primary == "groq"
    fast_models = [m.strip() for m in str(settings.llm_model_fast).split(",") if m.strip()]
    groq_turn = [next(_GROQ_RR)]

    def model_for(provider: str) -> str:
        if provider == "groq":
            return fast_models[groq_turn[0] % len(fast_models)] if fast_models else ""
        if provider == "fallback":
            return settings.llm_model_gemini_fallback
        return settings.llm_model_gemini

    def warn_attempt(provider: str, exc: BaseException) -> None:
        # SDK exceptions can include raw bodies. Log only provider, model, and the static prompt hash.
        message = "llm attempt failed provider=%s model=%s prompt_hash=%s"
        if not isinstance(exc, (ValidationError, ValueError, KeyError, IndexError, httpx.HTTPError, TimeoutError, LLMError)):
            message = "llm SDK attempt failed provider=%s model=%s prompt_hash=%s"
        log.warning(message, "groq" if provider == "groq" else "gemini", model_for(provider), prompt_hash)

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
                if gk.tier == "paid" and is_rate_limited(exc) and i + 1 < len(keys):
                    BUDGET.rate_limited(gk.key)
                    continue
                raise
            if gk.tier == "paid":
                BUDGET.add(gk.key, cost_usd(settings, usage, grounded))
            return raw, usage, gk.tier
        raise TimeoutError

    async def invoke(provider: str, remaining: float) -> LLMResult:
        is_groq = provider == "groq"
        model = model_for(provider)
        if not model:
            raise LLMError("provider key or model is not configured")
        tier = ""
        async with asyncio.timeout(remaining):
            if is_groq:
                key = settings.groq_api_key.get_secret_value()
                if not key:
                    raise LLMError("provider key or model is not configured")
                raw, usage = await _groq(model, key, prompt, payload, schema, remaining)
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
            for attempt in range(2):  # first try + one retry
                budget_left = deadline - loop.time()
                reserve = (MIN_FALLBACK_BUDGET_S + FALLBACK_SLACK_S) if has_fallback else 0.0
                # Leave the fallback window plus a little slack. asyncio.timeout
                # fires late, so reserving only the minimum lands just under it.
                # When the deadline is already shorter than that reserve, spend
                # what is left on this attempt.
                remaining = budget_left - reserve if budget_left > reserve else budget_left
                if remaining <= 0:
                    break
                if attempt:
                    groq_turn[0] += 1  # retry on the next fast model (its own rate limit)
                try:
                    return await invoke(primary, remaining)
                except Exception as exc:
                    warn_attempt(primary, exc)
                    if _is_timeout(exc) or attempt == 1:
                        break  # a timeout is not retried; the one retry is already spent
            budget_left = deadline - loop.time()
            if has_fallback and budget_left >= MIN_FALLBACK_BUDGET_S:
                try:
                    return await invoke("fallback", budget_left)
                except Exception as exc:
                    warn_attempt("fallback", exc)
            elif has_fallback:
                log.warning("llm fallback skipped prompt_hash=%s remaining_s=%.3f", prompt_hash, budget_left)
    except TimeoutError:
        pass
    raise LLMError("structured-output call exhausted its bounded attempts") from None


async def groq_json(prompt_id: str, variables: dict, schema: type[BaseModel], timeout: float = 1.2) -> LLMResult:
    return await _call(prompt_id, variables, schema, timeout, "groq")


async def gemini_json(prompt_id: str, variables: dict, schema: type[BaseModel], grounded: bool = False, cache_name: str | None = None, timeout: float = 20.0) -> LLMResult:
    return await _call(prompt_id, variables, schema, timeout, "gemini", grounded, cache_name)
