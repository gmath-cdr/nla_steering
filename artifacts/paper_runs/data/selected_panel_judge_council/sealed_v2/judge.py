"""Resume-safe exact AxBench LM judging for generated axb44 records."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from experiments.axbench_lexicality20.axbench_judge_templates import TEMPLATES
from experiments.axbench_lexicality20.protocol import canonical_sha256, file_sha256, load_bundle
from nla_lab.study import validate_spec_integrity


SCHEMA = "axb44-axbench-judge/v1"
MODEL = "gpt-4o-mini-2024-07-18"
RATING_RE = re.compile(r"Rating:\s*(?:\[\[)?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


class JudgeError(RuntimeError):
    pass


def parse_rating_info(completion: str) -> tuple[float, str]:
    """Match AxBench's last-``Rating:`` parser and expose fail-to-zero status."""
    if "Rating:" not in completion:
        return 0.0, "missing_rating_to_zero"
    tail = completion.split("Rating:")[-1].strip().split("\n", 1)[0].strip()
    tail = tail.replace("[", "").replace("]", "").rstrip(".").strip('"\'').strip("*").strip()
    try:
        rating = float(tail)
    except ValueError:
        match = RATING_RE.search(completion)
        if match is None:
            return 0.0, "unparseable_rating_to_zero"
        rating = float(match.group(1))
    if not 0.0 <= rating <= 2.0:
        return 0.0, "out_of_range_rating_to_zero"
    return rating, "parsed"


def parse_rating(completion: str) -> float:
    return parse_rating_info(completion)[0]


def harmonic(scores: list[float]) -> float:
    return 0.0 if any(score == 0 for score in scores) else len(scores) / sum(1 / score for score in scores)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
        suffix=".tmp", delete=False,
    ) as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def chat(prompt: str, api_key: str, *, timeout: int = 120) -> dict[str, Any]:
    body = json.dumps({
        "model": MODEL, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    request = Request(
        "https://api.openai.com/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS endpoint
        payload = json.loads(response.read().decode("utf-8"))
    completion = str(payload["choices"][0]["message"]["content"]).strip()
    rating, parse_status = parse_rating_info(completion)
    return {
        "completion": completion, "rating": rating, "parse_status": parse_status,
        "returned_model": payload.get("model"), "usage": payload.get("usage", {}),
        "response_id": payload.get("id"),
        "completed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def call_with_retry(prompt: str, api_key: str, key: str) -> tuple[str, dict[str, Any]]:
    for attempt in range(6):
        try:
            response = chat(prompt, api_key)
            response["retry_count"] = attempt
            return key, response
        except (HTTPError, URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
            if attempt == 5:
                raise JudgeError(f"judge call failed after retries for {key}: {exc}") from exc
            time.sleep(min(30.0, (2 ** attempt) + random.Random(key + str(attempt)).random()))
    raise AssertionError("unreachable")


def load_items(results_dir: Path, phase: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    items, hashes = [], {}
    for path in sorted(results_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        spec = payload.get("spec", {})
        if spec.get("study", {}).get("schema") != "axb44-run/v1":
            continue
        if spec["study"].get("phase") != phase:
            continue
        validate_spec_integrity(spec, required=True)
        if len(payload.get("records", [])) != int(spec["expected_records"]):
            raise JudgeError(f"incomplete result: {path}")
        hashes[path.name] = file_sha256(path)
        for index, record in enumerate(payload["records"]):
            items.append({
                "record_key": f"{path.name}#{index}", "result_file": path.name,
                "result_sha256": hashes[path.name], "record_index": index,
                "arm_id": spec["study"]["arm_id"], "model_key": payload["runtime"]["model_key"],
                "alpha": float(record["alpha"]), "concept_id": int(record["concept_id"]),
                "concept_label": record["concept_label"], "prompt_id": record["prompt_id"],
                "prompt_track": record.get("prompt_track"),
                "instruction": record.get("carrier_prompt", record["prompt"]),
                "sentence": record["text"], "generation_seed": int(record["sample"]),
            })
    if not items:
        raise JudgeError(f"no axb44 {phase} records found in {results_dir}")
    if len({item["record_key"] for item in items}) != len(items):
        raise JudgeError("duplicate result record keys")
    return items, hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("calibration", "evaluation"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        raise JudgeError("workers must be in [1,16]")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise JudgeError("OPENAI_API_KEY is not set")
    bundle = load_bundle()
    items, result_hashes = load_items(args.results_dir, args.phase)
    existing = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {
        "schema": SCHEMA, "campaign_id": bundle["campaign"]["campaign_id"],
        "review_payload_sha256": bundle["review_payload_sha256"],
        "phase": args.phase, "judge_model": MODEL, "judge_temperature": 0,
        "result_file_sha256": result_hashes, "calls": {}, "records": {},
    }
    identity = (existing.get("schema"), existing.get("campaign_id"), existing.get("phase"), existing.get("judge_model"))
    expected = (SCHEMA, bundle["campaign"]["campaign_id"], args.phase, MODEL)
    if identity != expected or existing.get("result_file_sha256") != result_hashes:
        raise JudgeError("existing judge checkpoint identity/result hashes drifted")
    calls = existing["calls"]
    pending: list[tuple[str, str]] = []
    for item in items:
        prompts = {
            "concept": TEMPLATES["concept"].format(
                concept=item["concept_label"], sentence=item["sentence"]
            ),
            "instruction": TEMPLATES["instruction"].format(
                instruction=item["instruction"], sentence=item["sentence"]
            ),
            "fluency": TEMPLATES["fluency"].format(sentence=item["sentence"]),
        }
        for metric, prompt in prompts.items():
            key = canonical_sha256({
                "model": MODEL, "temperature": 0, "record_key": item["record_key"],
                "metric": metric, "prompt": prompt,
            })
            if key not in calls:
                pending.append((key, prompt))
    pending_prompts = dict(pending)
    print(f"judge records={len(items)} calls_total={len(items)*3} pending={len(pending)}")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(call_with_retry, prompt, api_key, key): key for key, prompt in pending}
        completed_since_save = 0
        for future in as_completed(futures):
            key, response = future.result()
            response["request_prompt_sha256"] = hashlib.sha256(
                pending_prompts[key].encode("utf-8")
            ).hexdigest()
            response["request_prompt"] = pending_prompts[key]
            calls[key] = response
            completed_since_save += 1
            if completed_since_save >= 20:
                atomic_json(args.output, existing)
                completed_since_save = 0
    records = {}
    for item in items:
        scores, call_keys = {}, {}
        for metric, prompt in {
            "concept": TEMPLATES["concept"].format(concept=item["concept_label"], sentence=item["sentence"]),
            "instruction": TEMPLATES["instruction"].format(instruction=item["instruction"], sentence=item["sentence"]),
            "fluency": TEMPLATES["fluency"].format(sentence=item["sentence"]),
        }.items():
            key = canonical_sha256({"model": MODEL, "temperature": 0, "record_key": item["record_key"], "metric": metric, "prompt": prompt})
            scores[metric] = float(calls[key]["rating"])
            call_keys[metric] = key
        records[item["record_key"]] = {
            **item, "scores": scores,
            "axbench_harmonic": harmonic(list(scores.values())), "call_keys": call_keys,
        }
    existing["records"] = records
    existing["complete"] = len(records) == len(items) and len(calls) == len(items) * 3
    existing["usage"] = {
        key: sum(int(call.get("usage", {}).get(field, 0)) for call in calls.values())
        for key, field in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"), ("total_tokens", "total_tokens"))
    }
    atomic_json(args.output, existing)
    if not existing["complete"]:
        raise JudgeError("judge output did not reach expected cardinality")
    print(f"complete judge artifact -> {args.output}")


if __name__ == "__main__":
    main()
