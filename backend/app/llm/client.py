"""Bounded structured-output calls. Never log provider exceptions or prompt data."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError
from app.config import Settings
from app.models import LLMResult

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).with_name("prompts")
# A Gemini fallback started with less than this left on the call deadline cannot
# finish before that deadline (and the lane cancel) fires. Skip it.
MIN_FALLBACK_BUDGET_S = 0.2


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
            "response_format": {"type": "json_object"},
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

    config = dict(temperature=0, max_output_tokens=2048, response_mime_type="application/json", response_schema=schema)
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
        return response.text, {"prompt_tokens": usage.prompt_token_count or 0, "completion_tokens": usage.candidates_token_count or 0, "cached_tokens": usage.cached_content_token_count or 0} if usage else {}


async def _call(prompt_id: str, variables: dict, schema: type[BaseModel], timeout: float, primary: str, grounded: bool = False, cache_name: str | None = None) -> LLMResult:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    prompt, prompt_hash = _prompt(prompt_id)
    payload = json.dumps(variables, ensure_ascii=False)
    settings = _settings()
    if primary == "groq" and settings.llm_provider == "gemini":
        primary = "gemini"
    # Two primary attempts, then one configured fallback, all inside ONE deadline.
    # The fallback is skipped when the time left cannot finish before that deadline.
    providers = [primary, primary] + (["fallback"] if primary == "groq" else [])
    try:
        async with asyncio.timeout(timeout):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            for index, provider in enumerate(providers):
                budget_left = deadline - loop.time()
                if provider == "fallback" and budget_left < MIN_FALLBACK_BUDGET_S:
                    log.warning("llm fallback skipped prompt_hash=%s remaining_s=%.3f", prompt_hash, budget_left)
                    break
                is_groq = provider == "groq"
                model = settings.llm_model_fast if is_groq else (settings.llm_model_gemini_fallback if provider == "fallback" else settings.llm_model_gemini)
                key = (settings.groq_api_key if is_groq else settings.gemini_api_key).get_secret_value()
                if not key or not model:
                    raise LLMError("provider key or model is not configured")
                remaining = max(.001, budget_left / (len(providers) - index))
                try:
                    async with asyncio.timeout(remaining):
                        if is_groq:
                            raw, usage = await _groq(model, key, prompt, payload, schema, remaining)
                        else:
                            raw, usage = await _gemini(model, key, prompt, payload, schema, remaining, grounded, cache_name)
                        data = schema.model_validate_json(raw, strict=True).model_dump(mode="json")
                    safe_usage = {k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool)}
                    log.info("llm provider=%s model=%s prompt_hash=%s usage=%s", "groq" if is_groq else "gemini", model, prompt_hash, safe_usage)
                    return LLMResult(data=data, model_id=model, prompt_hash=prompt_hash, provider="groq" if is_groq else "gemini", usage=safe_usage)
                except (ValidationError, ValueError, KeyError, IndexError, httpx.HTTPError, TimeoutError, LLMError):
                    log.warning("llm attempt failed provider=%s model=%s prompt_hash=%s", provider, model, prompt_hash)
                except Exception:
                    # SDK exceptions can include raw bodies. Preserve cancellation (BaseException).
                    log.warning("llm SDK attempt failed provider=%s model=%s prompt_hash=%s", provider, model, prompt_hash)
    except TimeoutError:
        pass
    raise LLMError("structured-output call exhausted its bounded attempts") from None


async def groq_json(prompt_id: str, variables: dict, schema: type[BaseModel], timeout: float = 1.2) -> LLMResult:
    return await _call(prompt_id, variables, schema, timeout, "groq")


async def gemini_json(prompt_id: str, variables: dict, schema: type[BaseModel], grounded: bool = False, cache_name: str | None = None, timeout: float = 20.0) -> LLMResult:
    return await _call(prompt_id, variables, schema, timeout, "gemini", grounded, cache_name)
