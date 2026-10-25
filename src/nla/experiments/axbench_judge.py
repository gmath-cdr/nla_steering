"""Versioned, resumable LLM judging for prompt-specific AxBench results."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from experiments import results


PROMPT_VERSION = "axbench-concept-presence-v1"
DEFAULT_MODEL = "gpt-4o-mini-2024-07-18"
API_ENDPOINT = "https://api.openai.com/v1/chat/completions"
SYSTEM_PROMPT = """You are a strict evaluator of whether a generated answer expresses a target concept.
Return JSON only: {\"strict_hit\": boolean, \"rationale\": string}.
Set strict_hit true only when the answer substantively mentions, describes, or clearly invokes the target concept. Do not count a superficial word match, a negation, an instruction to avoid the concept, or unrelated text. Do not judge helpfulness, style, or factuality."""


def _request(api_key: str, model: str, concept: str, response: str) -> dict[str, Any]:
    user = f"Target concept:\n{concept}\n\nGenerated answer:\n{response}"
    payload = {"model": model, "max_tokens": 160, "temperature": 0,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user}]}
    headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
    with httpx.Client(timeout=60) as client:
        reply = client.post(API_ENDPOINT, headers=headers, json=payload)
        reply.raise_for_status()
    body = reply.json()
    text = body["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"judge did not return JSON: {text[:200]!r}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed.get("strict_hit"), bool):
        raise ValueError("judge response omitted boolean strict_hit")
    return {"strict_hit": parsed["strict_hit"], "rationale": str(parsed.get("rationale", "")),
            "raw_response": text, "returned_model": body.get("model", model)}


def evaluate(rows: list[dict[str, Any]], concepts: dict[int, str], *, api_key_env: str,
             model: str, output: str) -> dict[str, Any]:
    """Annotate result rows in place, checkpointing after every API response."""
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"--judge requires {api_key_env} to be set")
    protocol = {"provider": "openai", "api_endpoint": API_ENDPOINT, "model": model,
                "prompt_version": PROMPT_VERSION, "system_prompt": SYSTEM_PROMPT,
                "api_key_env": api_key_env, "evaluated_at": datetime.now(timezone.utc).isoformat()}
    for row in rows:
        concept_id = int(row["label"].split("__", 1)[0][1:])
        concept = concepts[concept_id]
        for sample in row["per_prompt"]:
            old = sample.get("judge")
            if old and old.get("protocol") == {key: protocol[key] for key in ("provider", "api_endpoint", "model", "prompt_version")}:
                continue
            verdict = _request(api_key, model, concept, sample["response"])
            sample["judge"] = {**verdict, "protocol": {key: protocol[key] for key in
                                ("provider", "api_endpoint", "model", "prompt_version")}}
            results.save(output, rows)
    summary_rows = []
    for row in rows:
        hits = [sample["judge"]["strict_hit"] for sample in row["per_prompt"]]
        summary_rows.append({"label": row["label"], "n_samples": len(hits),
                             "strict_hit_rate": sum(hits) / len(hits) if hits else 0.0})
    return {"judge_protocol": protocol, "arms": summary_rows}
