"""Thin OpenAI-compat client for cockpit Planner / Auditor L3 / Profile Writer.

Per PLAN_OSS_STRATEGY §2: all 3 Engine-side LLM agents go through one OpenAI-compat
endpoint. User configures via X-LLM-* headers; key never persisted.

Why we don't use the openai SDK's higher-level client:
- We want explicit retry/timeout control
- We want JSON-mode fallback (some compat providers don't support response_format)
- We want zero state — just functions, no class instances with cached keys
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .models import LLMConfig

logger = logging.getLogger("cockpit.llm")


class LLMError(Exception):
    """Wrapper for LLM-call failures (network / rate limit / bad response)."""


@dataclass
class LLMResponse:
    content: str
    raw_json: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0


def chat_json(
    cfg: LLMConfig,
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    timeout_s: float = 60.0,
    max_retries: int = 2,
) -> LLMResponse:
    """Call the OpenAI-compat /chat/completions endpoint expecting JSON output.

    Most providers (OpenAI, DeepSeek, Qwen, Doubao, OpenRouter, Ollama) all accept
    `response_format={type: "json_object"}`. If a provider doesn't, the LLM still
    usually returns JSON when asked clearly in the prompt — we trust the prompt.

    Returns LLMResponse. Raises LLMError on unrecoverable failure.
    """
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(timeout=timeout_s) as client:
                r = client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                # 429 / 5xx are retryable; 4xx (other) are not
                retryable = r.status_code == 429 or 500 <= r.status_code < 600
                msg = f"LLM HTTP {r.status_code}: {r.text[:300]}"
                if not retryable or attempt == max_retries:
                    raise LLMError(msg)
                logger.warning("LLM call %s, retrying (%d/%d)", msg, attempt + 1, max_retries)
                time.sleep(1.5 ** attempt)
                continue

            data = r.json()
            choice0 = data["choices"][0]["message"]
            content = choice0.get("content", "")
            usage = data.get("usage") or {}
            return LLMResponse(
                content=content,
                raw_json=data,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )
        except httpx.HTTPError as e:
            last_err = e
            if attempt == max_retries:
                raise LLMError(f"LLM network error after {max_retries + 1} attempts: {e}") from e
            logger.warning("LLM network error %s, retrying", e)
            time.sleep(1.5 ** attempt)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise LLMError(f"LLM response shape error: {e}; body={r.text[:300] if 'r' in dir() else 'n/a'}") from e

    raise LLMError(f"LLM call failed: {last_err}")


def parse_json_content(content: str) -> dict[str, Any]:
    """Parse the assistant's JSON content. Tolerates markdown-fenced JSON."""
    s = content.strip()
    # Strip ```json ... ``` fences if present
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s.rsplit("\n", 1)[0] if "\n" in s else s[:-3]
        # Remove leading "json"
        if s.lstrip().lower().startswith("json"):
            s = s.split("\n", 1)[1] if "\n" in s else s[4:]
    return json.loads(s)
