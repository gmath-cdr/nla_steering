"""Rebuild and verify the selected-panel GPT-4o-mini/Haiku/Luna summaries.

The API completions are immutable evidence. This module does not make network
calls. It reconstructs every expected call key from the frozen source records,
reparses every retained completion, and recomputes the per-arm means reported
in the appendix. The original hash-sealed API runner is retained unchanged
under the artifact directory for provenance.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import runpy
from typing import Any

from study_families.common import (
    ArtifactError,
    DATA_ROOT,
    canonical_sha256,
    load_json,
    sha256_file,
    sha256_lf_text,
)


FAMILY = DATA_ROOT / "selected_panel_judge_council"
SOURCES = FAMILY / "sources"
RAW = FAMILY / "raw"
SUMMARIES = FAMILY / "summaries"
SEALED = FAMILY / "sealed_v2"

# The API checkpoints were sealed against the manifest as retrieved on the
# original Windows machine (CRLF bytes). Git now enforces LF for portability.
# Preserve the historical binding while independently requiring the exact
# canonical content, so newline conversion cannot either break or weaken QA.
HISTORICAL_MANIFEST_SHA256 = "2f450ded5a7fbc573de7dcada99c2fbd7662198d8fc4d9a1c039173e3a722e65"
CANONICAL_MANIFEST_SHA256 = "4cfc01888a9b1ce6fb90d253e7f832f4dcfdb949eb16867b068576e8b93cf202"
HISTORICAL_CHECKPOINT_SHA256 = {
    "gpt4omini": "ea75e8a4b5d7b40baf1f18db2a7b6b87b754265767be4a1829c4d98ff79537f8",
    "haiku": "a7e5bb3cd3e533f7954a29a17636b205dd9f37ee165ab393fe34539710422498",
    "luna": "866d6c71d25835c1d2f509a15a9dca5d8fbff2269a53af77a762d3c308e013b9",
}
CANONICAL_CHECKPOINT_SHA256 = {
    "gpt4omini": "a02a2e69d0d0b1d6872ac2e7ccd71a82893b1d5fe32d3e28b2fe2e08a7f77ea7",
    "haiku": "11aab53a3c55ca1132f18b1b12df9c544c9c45498f11c22eccbeb8f50c6ab2c0",
    "luna": "0e63c4d7359b9434a556f0740fe97c49f6cdb7593fca5007ccf1a7be4d5526d8",
}

RATING_RE = re.compile(r"Rating:\s*(?:\[\[)?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def parse_rating_info(completion: str) -> tuple[float, str]:
    """Apply the exact AxBench-compatible last-``Rating:`` parsing rule."""

    if "Rating:" not in completion:
        return 0.0, "missing_rating_to_zero"
    tail = completion.split("Rating:")[-1].strip().split("\n", 1)[0].strip()
    tail = tail.replace("[", "").replace("]", "").rstrip(".").strip("\"'").strip("*").strip()
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


def strict_harmonic(scores: list[float]) -> float:
    """Match AxBench: any zero component forces the combined score to zero."""

    return 0.0 if any(score == 0 for score in scores) else len(scores) / sum(
        1 / score for score in scores
    )


def _templates() -> dict[str, str]:
    """Load the retained template source without importing GPU dependencies."""

    path = SEALED / "axbench_judge_templates.py"
    payload = runpy.run_path(str(path))["TEMPLATES"]
    return {str(key): str(value) for key, value in payload.items()}


def _items() -> list[dict[str, Any]]:
    """Recreate the 3,584 unique model-output records judged by all three models."""

    items: list[dict[str, Any]] = []
    for model_key in ("qwen", "gemma"):
        baseline_rows = load_json(SOURCES / f"{model_key}_results.json")
        for row in baseline_rows:
            items.append(
                {
                    "model_key": model_key,
                    "arm_id": row["method"],
                    "concept_id": int(row["concept_id"]),
                    "concept_label": row["concept"],
                    "instruction": row["prompt"],
                    "sample": int(row["sample_index"]),
                    "sentence": row["response"],
                }
            )

        nla_rows = load_json(SOURCES / f"{model_key}_nla.json")
        for row in nla_rows:
            for sample, sentence in enumerate(row["generation_arms"]["av_edit"]):
                items.append(
                    {
                        "model_key": model_key,
                        "arm_id": "nla_av_edit",
                        "concept_id": int(row["concept_id"]),
                        "concept_label": row["concept"],
                        "instruction": row["prompt"],
                        "sample": sample,
                        "sentence": sentence,
                    }
                )

    for item in items:
        item["record_key"] = canonical_sha256(
            {
                "model_key": item["model_key"],
                "arm_id": item["arm_id"],
                "concept_id": item["concept_id"],
                "instruction": item["instruction"],
                "sample": item["sample"],
                "sentence": item["sentence"],
            }
        )
    keys = [item["record_key"] for item in items]
    if len(keys) != len(set(keys)):
        raise ArtifactError("selected-panel source contains duplicate record identities")
    return items


def _prompts(item: dict[str, Any], templates: dict[str, str]) -> dict[str, str]:
    return {
        "concept": templates["concept"].format(
            concept=item["concept_label"], sentence=item["sentence"]
        ),
        "instruction": templates["instruction"].format(
            instruction=item["instruction"], sentence=item["sentence"]
        ),
        "fluency": templates["fluency"].format(sentence=item["sentence"]),
    }


def _call_key(judge: str, item: dict[str, Any], metric: str, prompt: str, spec: dict[str, Any]) -> str:
    return canonical_sha256(
        {
            "judge": judge,
            "model": spec["model"],
            "params": spec["params"],
            "record_key": item["record_key"],
            "metric": metric,
            "prompt": prompt,
        }
    )


def _key_map(
    judge: str,
    items: list[dict[str, Any]],
    templates: dict[str, str],
    spec: dict[str, Any],
) -> dict[str, tuple[dict[str, Any], str, str]]:
    mapping: dict[str, tuple[dict[str, Any], str, str]] = {}
    for item in items:
        for metric, prompt in _prompts(item, templates).items():
            mapping[_call_key(judge, item, metric, prompt, spec)] = (item, metric, prompt)
    return mapping


def _verify_code_and_sources(manifest: dict[str, Any]) -> None:
    """Check the historical runner's sealed source inputs without changing it."""

    source_paths = {
        name: SOURCES / name for name in manifest["source_files"]
    }
    for name, path in source_paths.items():
        expected = manifest["source_files"][name]["sha256"]
        if sha256_file(path) != expected:
            raise ArtifactError(f"selected-panel source hash mismatch: {name}")

    code_paths = {
        "judge_panel14_v2.py": SEALED / "judge_panel14_v2.py",
        "judge.py": SEALED / "judge.py",
        "axbench_judge_templates.py": SEALED / "axbench_judge_templates.py",
        "protocol.py": SEALED / "protocol.py",
    }
    for name, expected in manifest["code_sha256"].items():
        if sha256_lf_text(code_paths[name]) != expected:
            raise ArtifactError(f"selected-panel sealed code hash mismatch: {name}")


def rebuild_summary(judge: str) -> dict[str, Any]:
    """Verify one raw checkpoint and recompute the exact frozen summary."""

    manifest_path = SEALED / "panel14_manifest.json"
    manifest = load_json(manifest_path)
    _verify_code_and_sources(manifest)
    if sha256_lf_text(manifest_path) != CANONICAL_MANIFEST_SHA256:
        raise ArtifactError("selected-panel canonical manifest content changed")

    if judge not in manifest["judges"]:
        raise ArtifactError(f"unknown selected-panel judge: {judge}")
    spec = manifest["judges"][judge]
    items = _items()
    if len(items) != int(manifest["n_records"]):
        raise ArtifactError("selected-panel record count differs from sealed manifest")

    templates = _templates()
    expected_template_hashes = {
        metric: hashlib.sha256(templates[metric].encode("utf-8")).hexdigest()
        for metric in ("concept", "instruction", "fluency")
    }
    if expected_template_hashes != manifest["template_sha256"]:
        raise ArtifactError("selected-panel judge templates differ from sealed manifest")

    key_map = _key_map(judge, items, templates, spec)
    keyset_hash = hashlib.sha256("\n".join(sorted(key_map)).encode("utf-8")).hexdigest()
    if keyset_hash != manifest["call_keys_sha256"][judge]:
        raise ArtifactError(f"selected-panel expected call-key set drifted for {judge}")

    checkpoint_path = RAW / f"panel14_judge_{judge}.json"
    if sha256_lf_text(checkpoint_path) != CANONICAL_CHECKPOINT_SHA256[judge]:
        raise ArtifactError(f"{judge} canonical checkpoint content changed")
    checkpoint = load_json(checkpoint_path)
    if checkpoint.get("manifest_sha256") != HISTORICAL_MANIFEST_SHA256:
        raise ArtifactError(f"{judge} checkpoint is bound to a different manifest")
    if checkpoint.get("judge_model") != spec["model"] or checkpoint.get("judge_params") != spec["params"]:
        raise ArtifactError(f"{judge} checkpoint identity differs from the manifest")
    calls = checkpoint.get("calls", {})
    if set(calls) != set(key_map):
        missing = len(set(key_map) - set(calls))
        extra = len(set(calls) - set(key_map))
        raise ArtifactError(f"{judge} call-key mismatch: {missing} missing, {extra} extra")

    parse_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    per_record: dict[str, dict[str, tuple[dict[str, Any], float]]] = defaultdict(dict)
    for key, (item, metric, prompt) in key_map.items():
        call = calls[key]
        completion = str(call.get("completion", ""))
        if not completion:
            raise ArtifactError(f"{judge} raw completion is empty at {key}")
        rating, parse_status = parse_rating_info(completion)
        if rating != call.get("rating") or parse_status != call.get("parse_status"):
            raise ArtifactError(f"{judge} stored rating does not match reparse at {key}")
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if call.get("request_prompt_sha256") != prompt_hash:
            raise ArtifactError(f"{judge} request-prompt hash mismatch at {key}")
        if not str(call.get("returned_model", "")).startswith(spec["expect_model_prefix"]):
            raise ArtifactError(f"{judge} returned-model identity mismatch at {key}")
        if not call.get("response_id") or not call.get("usage"):
            raise ArtifactError(f"{judge} response metadata missing at {key}")
        parse_counts[parse_status] += 1
        model_counts[str(call["returned_model"])] += 1
        per_record[item["record_key"]][metric] = (item, float(rating))

    grouped: dict[tuple[str, str], list[tuple[float, float, float, float]]] = defaultdict(list)
    for record_key, metrics in per_record.items():
        if set(metrics) != {"concept", "instruction", "fluency"}:
            raise ArtifactError(f"record {record_key} is missing a judge component")
        item = metrics["concept"][0]
        scores = {metric: metrics[metric][1] for metric in metrics}
        grouped[(item["model_key"], item["arm_id"])].append(
            (
                strict_harmonic(list(scores.values())),
                scores["concept"],
                scores["instruction"],
                scores["fluency"],
            )
        )

    summary: dict[str, Any] = {
        "schema": "panel14-judge/v2/summary",
        "judge": judge,
        "judge_model": spec["model"],
        "judge_params": spec["params"],
        "manifest_sha256": HISTORICAL_MANIFEST_SHA256,
        "checkpoint_sha256": HISTORICAL_CHECKPOINT_SHA256[judge],
        "parse_status_counts": dict(parse_counts),
        "returned_model_counts": dict(model_counts),
        "per_arm": {},
    }
    for (model_key, arm_id), values in sorted(grouped.items()):
        count = len(values)
        summary["per_arm"][f"{model_key}/{arm_id}"] = {
            "n": count,
            "harmonic_mean": round(sum(row[0] for row in values) / count, 4),
            "concept_mean": round(sum(row[1] for row in values) / count, 4),
            "instruction_mean": round(sum(row[2] for row in values) / count, 4),
            "fluency_mean": round(sum(row[3] for row in values) / count, 4),
        }

    expected = load_json(SUMMARIES / f"panel14_judge_{judge}.summary.json")
    if summary != expected:
        raise ArtifactError(f"recomputed {judge} summary differs from the frozen summary")
    return summary


def rebuild_all() -> dict[str, dict[str, Any]]:
    return {judge: rebuild_summary(judge) for judge in ("gpt4omini", "haiku", "luna")}
