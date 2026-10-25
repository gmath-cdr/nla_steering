"""Bounded-concurrency OpenRouter chat-completion client, shared by the LM judges.

OpenRouter exposes an OpenAI-compatible /chat/completions endpoint; this talks to
it directly over httpx (already a project dependency) rather than pulling in the
openai SDK. No key is ever logged or persisted -- read once from the environment
per call, matching the api_key_env convention in experiments/axbench_judge.py.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

API_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class OpenRouterError(RuntimeError):
    pass


async def _one(client: httpx.AsyncClient, sem: asyncio.Semaphore, api_key: str, model: str,
                system: str | None, user: str, *, max_tokens: int, temperature: float,
                json_mode: bool, max_retries: int) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
    async with sem:
        for attempt in range(max_retries):
            reply = await client.post(API_ENDPOINT, headers=headers, json=payload, timeout=60)
            if reply.status_code in _RETRYABLE_STATUS and attempt < max_retries - 1:
                await asyncio.sleep(min(2 ** attempt, 30))
                continue
            reply.raise_for_status()
            body = reply.json()
            if "error" in body:
                raise OpenRouterError(str(body["error"]))
            choice = body["choices"][0]
            if choice.get("finish_reason") == "content_filter":
                raise OpenRouterError(f"content filtered: {body}")
            return choice["message"]["content"]
    raise OpenRouterError("unreachable")


def complete_batch(prompts: list[tuple[str | None, str]], *, api_key_env: str = "OPENROUTER_API_KEY",
                    model: str = DEFAULT_MODEL, max_tokens: int = 300, temperature: float = 0.0,
                    concurrency: int = 16, json_mode: bool = False, max_retries: int = 6) -> list[str]:
    """prompts: list of (system_prompt_or_None, user_prompt). Returns completions in the same order."""
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"requires {api_key_env} to be set")

    async def _run() -> list[str]:
        sem = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient() as client:
            tasks = [_one(client, sem, api_key, model, system, user, max_tokens=max_tokens,
                          temperature=temperature, json_mode=json_mode, max_retries=max_retries)
                     for system, user in prompts]
            return await asyncio.gather(*tasks)

    return asyncio.run(_run())
