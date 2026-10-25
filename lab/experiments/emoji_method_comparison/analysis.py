"""Locked calibration selection and held-out analysis for the emoji study."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
import csv
import hashlib
import itertools
import json
from pathlib import Path
import random
import time
from typing import Any

import numpy as np

from experiments.emoji_method_comparison.harness import (
    HarnessError,
    PRIMARY_IDS,
    RANDOM_ARMS,
    RUN_SCHEMA,
    active_treatment_arms,
    canonical_sha256,
    expected_heldout_counts,
    heldout_arm_ids,
)
from nla_lab.study import (
    normalize_prompts,
    seal_alpha_receipt,
    validate_alpha_receipt,
    validate_spec_integrity,
)


ALPHA_ANALYSIS_ID = "emoji-method-comparison-paired-calibration/v1"
ALPHA_RULE_ID = "smallest-safe-alpha-within-locked-near-best/v1"
HELDOUT_ANALYSIS_ID = "emoji-method-comparison-heldout/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_result(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "steering-run/v2" or not isinstance(value.get("records"), list):
        raise HarnessError(f"{path} is not a steering-run/v2 result")
    if value.get("spec", {}).get("study", {}).get("schema") != RUN_SCHEMA:
        raise HarnessError(f"{path} is not an emoji-method-comparison run")
    value["_path"] = str(path)
    value["_sha256"] = _sha256(path)
    return value


def load_results(directory: Path) -> list[dict[str, Any]]:
    paths = sorted(directory.rglob("*.json"))
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if candidate.get("schema") == "steering-run/v2" and candidate.get("spec", {}).get("study", {}).get("schema") == RUN_SCHEMA:
            rows.append(load_result(path))
    if not rows:
        raise HarnessError(f"no emoji-method-comparison results under {directory}")
    return rows


def validate_result_grid(result: Mapping[str, Any], *, phase: str) -> None:
    spec = result.get("spec")
    if not isinstance(spec, Mapping):
        raise HarnessError("result lacks its spec")
    validate_spec_integrity(spec, required=True)
    study = spec.get("study", {})
    if study.get("phase") != phase:
        raise HarnessError(f"expected {phase}, got {study.get('phase')}")
    if spec.get("design", {}).get("preregistered") is not True:
        raise HarnessError("research result is not marked preregistered")
    prompts = normalize_prompts(spec.get("prompts"))
    alphas = [float(value) for value in spec.get("alphas", [])]
    samples = int(spec.get("samples_per_prompt", 0))
    seed_offset = int(spec.get("seed_offset", 0))
    if not prompts or not alphas or samples < 1:
        raise HarnessError("empty prompt, alpha, or sample axis")
    if len(set(alphas)) != len(alphas) or 0.0 not in alphas:
        raise HarnessError("alpha grid must be unique and include zero")
    if spec.get("seed_scheme") != "prompt_offset_v1":
        raise HarnessError("seed scheme drifted")
    expected = {
        (prompt["id"], seed_offset + prompt_index * samples + sample, alpha)
        for prompt_index, prompt in enumerate(prompts)
        for sample in range(samples)
        for alpha in alphas
    }
    observed: set[tuple[str, int, float]] = set()
    for row in result["records"]:
        key = (str(row["prompt_id"]), int(row["sample"]), float(row["alpha"]))
        if key in observed:
            raise HarnessError(f"duplicate result cell {key}")
        observed.add(key)
        metrics = row.get("target_metrics")
        if not isinstance(metrics, Mapping) or "target_present" not in metrics:
            raise HarnessError(f"result cell {key} lacks target metrics")
        generation = row.get("generation")
        if not isinstance(generation, Mapping) or "hit_max_new_tokens" not in generation:
            raise HarnessError(f"result cell {key} lacks generation metadata")
    if observed != expected:
        raise HarnessError(
            f"result grid mismatch missing={sorted(expected - observed)[:3]} "
            f"extra={sorted(observed - expected)[:3]}"
        )
    if int(spec.get("expected_records", -1)) != len(expected):
        raise HarnessError("expected_records disagrees with the result grid")


def _validate_locks(result: Mapping[str, Any], bundle: Mapping[str, Any]) -> None:
    study = result["spec"]["study"]
    if study.get("lock_bundle_sha256") != bundle["lock_bundle_sha256"]:
        raise HarnessError("result lock-bundle digest is stale")
    if study.get("locks") != bundle["locks"]:
        raise HarnessError("result carries different frozen lock files/code")


def _cells(result: Mapping[str, Any], field: str) -> dict[tuple[str, int, float], float]:
    output: dict[tuple[str, int, float], float] = {}
    for row in result["records"]:
        key = (str(row["prompt_id"]), int(row["sample"]), float(row["alpha"]))
        output[key] = float(row["target_metrics"][field])
    return output


def _rows_at(result: Mapping[str, Any], alpha: float) -> list[Mapping[str, Any]]:
    rows = [row for row in result["records"] if float(row["alpha"]) == float(alpha)]
    if not rows:
        raise HarnessError(f"no rows at alpha {alpha}")
    return rows


def prompt_effects(
    result: Mapping[str, Any], alpha: float, *, field: str = "target_present"
) -> dict[str, float]:
    cells = _cells(result, field)
    grouped: defaultdict[str, list[float]] = defaultdict(list)
    for prompt_id, sample, observed_alpha in cells:
        if observed_alpha != float(alpha):
            continue
        baseline = (prompt_id, sample, 0.0)
        if baseline not in cells:
            raise HarnessError(f"missing paired alpha-zero cell {baseline}")
        grouped[prompt_id].append(cells[(prompt_id, sample, observed_alpha)] - cells[baseline])
    if not grouped:
        raise HarnessError(f"no paired effects at alpha {alpha}")
    return {key: float(np.mean(values)) for key, values in grouped.items()}


def _safe_row(
    row: Mapping[str, Any], baseline: Mapping[str, Any], gate: Mapping[str, Any]
) -> bool:
    metrics = row["target_metrics"]
    base = baseline["target_metrics"]
    minimum_length = float(gate["minimum_length_fraction_of_paired_baseline"]) * float(base["unicode_word_count"])
    checks = (
        float(metrics["unicode_word_count"]) >= float(gate["minimum_unicode_words"]),
        float(metrics["unicode_word_count"]) >= minimum_length,
        float(metrics["unique_unicode_word_ratio"]) >= float(gate["minimum_unique_unicode_word_ratio"]),
        float(metrics["max_token_run"]) <= float(gate["maximum_token_run"]),
        float(metrics["dominant_token_fraction"]) <= float(gate["maximum_dominant_token_fraction"]),
        float(metrics["max_character_run"]) <= float(gate["maximum_character_run"]),
        float(metrics["replacement_character_count"]) <= float(gate["maximum_replacement_characters"]),
        float(metrics["emoji_cluster_count"]) <= float(gate["maximum_emoji_clusters"]),
        float(metrics["emoji_cluster_to_word_fraction"]) <= float(gate["maximum_emoji_to_word_fraction"]),
        not bool(row["generation"]["hit_max_new_tokens"]) if gate["reject_hit_max_new_tokens"] else True,
    )
    return all(checks)


def safety_fraction(result: Mapping[str, Any], alpha: float, gate: Mapping[str, Any]) -> float:
    baselines = {
        (str(row["prompt_id"]), int(row["sample"])): row
        for row in _rows_at(result, 0.0)
    }
    selected = _rows_at(result, alpha)
    safe = []
    for row in selected:
        key = (str(row["prompt_id"]), int(row["sample"]))
        if key not in baselines:
            raise HarnessError(f"missing baseline for safety cell {key}")
        safe.append(_safe_row(row, baselines[key], gate))
    return float(np.mean(safe))


def select_one_alpha(result: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]:
    validate_result_grid(result, phase="calibration")
    _validate_locks(result, bundle)
    selection = bundle["lock"]["calibration_selection"]
    gate = bundle["lock"]["evaluation"]["quality_gate"]
    candidates = []
    alphas = sorted({float(row["alpha"]) for row in result["records"] if float(row["alpha"]) != 0.0})
    for alpha in alphas:
        effects = prompt_effects(result, alpha)
        mean_effect = float(np.mean(list(effects.values())))
        safe = safety_fraction(result, alpha, gate)
        candidates.append({
            "alpha": alpha,
            "mean_paired_presence_effect": mean_effect,
            "positive_prompt_blocks": sum(value > 0 for value in effects.values()),
            "safe_fraction": safe,
            "passes_safety": safe >= float(selection["safety_passing_fraction"]),
        })
    eligible = [row for row in candidates if row["passes_safety"]]
    if not eligible:
        return {
            "status": "excluded_no_safe_alpha",
            "rule": ALPHA_RULE_ID,
            "primary_metric": "target_present",
            "best_safe_effect": None,
            "near_best_tolerance": None,
            "chosen_alpha": None,
            "candidates": candidates,
        }
    best = max(row["mean_paired_presence_effect"] for row in eligible)
    tolerance = max(0.02, 0.05 * abs(best))
    near = [row for row in eligible if row["mean_paired_presence_effect"] >= best - tolerance]
    chosen = min(near, key=lambda row: abs(float(row["alpha"])))
    return {
        "status": "selected",
        "rule": ALPHA_RULE_ID,
        "primary_metric": "target_present",
        "best_safe_effect": best,
        "near_best_tolerance": tolerance,
        "chosen_alpha": float(chosen["alpha"]),
        "candidates": candidates,
    }


def make_alpha_receipt(
    results: Iterable[Mapping[str, Any]], bundle: Mapping[str, Any], *, model_key: str
) -> dict[str, Any]:
    rows = list(results)
    if len(rows) != len(PRIMARY_IDS):
        raise HarnessError(
            f"alpha receipt requires exactly {len(PRIMARY_IDS)} treatment results"
        )
    indexed: dict[str, Mapping[str, Any]] = {}
    base_markers: dict[str, Any] | None = None
    packages: dict[str, Any] | None = None
    git_commit: str | None = None
    build_receipt: str | None = None
    chosen: dict[str, float] = {}
    exclusions: dict[str, Any] = {}
    details: dict[str, Any] = {}
    result_hashes: dict[str, str] = {}
    direction_integrity: dict[str, Any] = {}
    for result in rows:
        validate_result_grid(result, phase="calibration")
        _validate_locks(result, bundle)
        study = result["spec"]["study"]
        if study.get("model_key") != model_key or result["runtime"].get("model_key") != model_key:
            raise HarnessError("calibration result model drifted")
        arm_id = str(study["arm_id"])
        if arm_id not in PRIMARY_IDS or arm_id in indexed:
            raise HarnessError(f"unexpected or duplicate calibration arm {arm_id!r}")
        indexed[arm_id] = result
        runtime = result["runtime"]
        if runtime.get("git", {}).get("dirty") is not False:
            raise HarnessError("calibration runtime was not clean")
        observed_markers = dict(runtime["checkpoint_marker_sha256"]["base"])
        observed_packages = dict(runtime["package_versions"])
        observed_commit = str(runtime["git"].get("commit") or "")
        observed_build = str(study.get("direction_build_receipt_sha256") or "")
        if base_markers is None:
            base_markers, packages, git_commit, build_receipt = (
                observed_markers, observed_packages, observed_commit, observed_build,
            )
        elif (base_markers, packages, git_commit, build_receipt) != (
            observed_markers, observed_packages, observed_commit, observed_build,
        ):
            raise HarnessError("calibration arms used different checkpoint/code/package/build identities")
        if result.get("method") != resolve_method_from_spec(result["spec"]):
            raise HarnessError(f"result method metadata drifted for {arm_id}")
        integrity = result.get("direction_integrity")
        if not isinstance(integrity, Mapping) or not integrity.get("unit_direction_sha256"):
            raise HarnessError(f"{arm_id} lacks direction integrity")
        expected_integrity = result["spec"].get("expected_direction_integrity", {})
        if any(integrity.get(key) != value for key, value in expected_integrity.items()):
            raise HarnessError(f"{arm_id} result direction disagrees with its sealed spec")
        selection = select_one_alpha(result, bundle)
        details[arm_id] = selection
        result_hashes[arm_id] = str(result.get("_sha256") or "")
        direction_integrity[arm_id] = dict(integrity)
        if selection["status"] == "selected":
            chosen[arm_id] = float(selection["chosen_alpha"])
        else:
            exclusion_receipt = {
                "schema": "emoji-method-comparison-treatment-exclusion/v1",
                "study_id": bundle["lock"]["study_id"],
                "model_key": model_key,
                "arm_id": arm_id,
                "reason": "no_nonzero_alpha_passed_frozen_safety_gate",
                "selection": selection,
                "calibration_result_sha256": result_hashes[arm_id],
                "unit_direction_sha256": integrity["unit_direction_sha256"],
            }
            exclusions[arm_id] = {
                "receipt": exclusion_receipt,
                "receipt_sha256": canonical_sha256(exclusion_receipt),
            }
    if set(indexed) != set(PRIMARY_IDS):
        raise HarnessError("calibration treatment family is incomplete")
    if not chosen:
        raise HarnessError(
            "all primary treatments failed the frozen safety gate; this model stratum "
            "has no held-out treatment and must stop after calibration"
        )
    receipt = seal_alpha_receipt({
        "schema": "axbench-alpha-receipt/v1",
        "status": "frozen",
        "analysis": ALPHA_ANALYSIS_ID,
        "selection_rule": ALPHA_RULE_ID,
        "model_key": model_key,
        "concept_key": "emoji",
        "chosen_alpha_by_arm": chosen,
        "random_control_alphas": sorted(set(chosen.values()), key=abs),
        "selection_details": details,
        "calibration_result_sha256": result_hashes,
        "lock_bundle_sha256": bundle["lock_bundle_sha256"],
        "base_checkpoint_marker_sha256": base_markers,
        "treatment_direction_integrity": direction_integrity,
        "treatment_exclusions": exclusions,
        "calibration_git_commit": git_commit,
        "calibration_package_versions": packages,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "audit": {
            "reviewer": "Codex-assisted deterministic run audit",
            "note": (
                "Exact Cartesian grids, frozen locks, raw record availability, paired baselines, "
                "checkpoint/code/package identity, and deterministic safety metrics passed. "
                f"{len(exclusions)} treatment arm(s) were excluded exactly as preregistered. "
                "No human relevance/fluency judgment was used to tune alpha; blinded ratings remain pending."
            ),
            "frozen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    })
    validate_alpha_receipt(receipt, require_frozen=True)
    return receipt


def resolve_method_from_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    from nla_lab.methods import resolve_method

    return resolve_method(dict(spec))


def _prompt_block_ci(values: Mapping[str, float], *, draws: int, seed: int) -> list[float]:
    array = np.asarray(list(values.values()), dtype=float)
    if array.size < 2:
        raise HarnessError("prompt bootstrap needs at least two blocks")
    rng = np.random.default_rng(seed)
    means = array[rng.integers(0, array.size, size=(draws, array.size))].mean(axis=1)
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def _signflip_p(
    values: Mapping[str, float], *, exact_limit: int, draws: int, seed: int
) -> tuple[float, str]:
    array = np.asarray(list(values.values()), dtype=float)
    observed = float(array.mean())
    tolerance = 1e-12
    if array.size <= exact_limit:
        exceed = sum(
            float(np.mean(array * np.asarray(signs))) >= observed - tolerance
            for signs in itertools.product((-1.0, 1.0), repeat=int(array.size))
        )
        return exceed / (2 ** int(array.size)), "exact"
    rng = np.random.default_rng(seed)
    exceed = 0
    remaining = draws
    while remaining:
        count = min(10000, remaining)
        signs = rng.choice((-1.0, 1.0), size=(count, array.size))
        exceed += int(np.sum((signs * array).mean(axis=1) >= observed - tolerance))
        remaining -= count
    return (exceed + 1) / (draws + 1), "monte_carlo_plus_one"


def _holm(values: Mapping[str, float]) -> dict[str, float]:
    ordered = sorted((float(value), key) for key, value in values.items())
    output: dict[str, float] = {}
    running = 0.0
    for index, (value, key) in enumerate(ordered):
        running = max(running, (len(ordered) - index) * value)
        output[key] = min(1.0, running)
    return output


def _arm_records(results: Iterable[Mapping[str, Any]], arm_id: str) -> list[Mapping[str, Any]]:
    selected = [
        row for result in results if result["spec"]["study"]["arm_id"] == arm_id
        for row in result["records"]
    ]
    if not selected:
        raise HarnessError(f"held-out arm {arm_id} has no records")
    return selected


def _metric_map(
    rows: Iterable[Mapping[str, Any]], *, field: str, alpha: float
) -> dict[tuple[str, int], float]:
    output: dict[tuple[str, int], float] = {}
    for row in rows:
        if float(row["alpha"]) != float(alpha):
            continue
        key = (str(row["prompt_id"]), int(row["sample"]))
        if key in output:
            raise HarnessError(f"duplicate merged held-out cell {key} at alpha {alpha}")
        output[key] = float(row["target_metrics"][field])
    return output


def _paired_seed_effects(rows: Iterable[Mapping[str, Any]], alpha: float) -> dict[tuple[str, int], float]:
    rows = list(rows)
    base = _metric_map(rows, field="target_present", alpha=0.0)
    active = _metric_map(rows, field="target_present", alpha=alpha)
    if set(base) != set(active):
        raise HarnessError("held-out alpha and alpha-zero cells do not pair")
    return {key: active[key] - base[key] for key in base}


def _prompt_means(values: Mapping[tuple[str, int], float], track_ids: set[str]) -> dict[str, float]:
    grouped: defaultdict[str, list[float]] = defaultdict(list)
    for (prompt_id, _seed), value in values.items():
        if prompt_id in track_ids:
            grouped[prompt_id].append(float(value))
    return {key: float(np.mean(rows)) for key, rows in grouped.items()}


def _receipt_from_results(results: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    receipts: dict[str, Mapping[str, Any]] = {}
    for result in results:
        receipt = result["spec"]["study"].get("alpha_receipt")
        if not isinstance(receipt, Mapping):
            raise HarnessError("held-out spec lacks alpha receipt")
        validate_alpha_receipt(receipt, require_frozen=True)
        digest = canonical_sha256(receipt)
        if result["spec"]["study"].get("alpha_receipt_sha256") != digest:
            raise HarnessError("held-out alpha receipt digest drifted")
        receipts[digest] = receipt
    if len(receipts) != 1:
        raise HarnessError("held-out chunks use different alpha receipts")
    return next(iter(receipts.values()))


def evaluate_heldout(
    results: Iterable[Mapping[str, Any]], bundle: Mapping[str, Any], *, model_key: str
) -> dict[str, Any]:
    rows = list(results)
    if not rows:
        raise HarnessError("held-out family is empty")
    for result in rows:
        validate_result_grid(result, phase="heldout")
        _validate_locks(result, bundle)
        if result["runtime"]["model_key"] != model_key:
            raise HarnessError("held-out family mixes models")
    receipt = _receipt_from_results(rows)
    if receipt.get("model_key") != model_key:
        raise HarnessError("held-out alpha receipt model drifted")
    active_primary = active_treatment_arms(receipt)
    expected_arms = heldout_arm_ids(receipt)
    expected_counts = expected_heldout_counts(bundle, receipt)
    if len(rows) != expected_counts["result_files"]:
        raise HarnessError(
            f"held-out family has {len(rows)} chunks; expected "
            f"{expected_counts['result_files']}"
        )
    observed_records = sum(len(result["records"]) for result in rows)
    if observed_records != expected_counts["records"]:
        raise HarnessError(
            f"held-out family has {observed_records} records; expected "
            f"{expected_counts['records']}"
        )
    for result in rows:
        runtime = result["runtime"]
        if runtime.get("git", {}).get("dirty") is not False:
            raise HarnessError("held-out runtime was not clean")
        if runtime.get("package_versions") != receipt.get("calibration_package_versions"):
            raise HarnessError("held-out packages differ from calibration")
        if runtime.get("checkpoint_marker_sha256", {}).get("base") != receipt.get("base_checkpoint_marker_sha256"):
            raise HarnessError("held-out base checkpoint differs from calibration")
    grouped_specs: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for result in rows:
        grouped_specs[str(result["spec"]["study"]["arm_id"])].append(result)
    if set(grouped_specs) != set(expected_arms):
        raise HarnessError("held-out arm family is incomplete")
    for arm_id, chunks in grouped_specs.items():
        if len(chunks) != 8 or len({row["spec"]["study"]["chunk_id"] for row in chunks}) != 8:
            raise HarnessError(f"{arm_id} held-out chunk coverage drifted")
        if arm_id in PRIMARY_IDS or arm_id in RANDOM_ARMS:
            hashes = {
                (row.get("direction_integrity") or {}).get("unit_direction_sha256")
                for row in chunks
            }
        if arm_id in PRIMARY_IDS:
            expected = receipt["treatment_direction_integrity"][arm_id]["unit_direction_sha256"]
            if hashes != {expected}:
                raise HarnessError(f"held-out {arm_id} direction differs from calibration")
        elif arm_id in RANDOM_ARMS and (None in hashes or len(hashes) != 1):
            raise HarnessError(f"held-out {arm_id} random direction changed across chunks")

    # Every hidden arm must reproduce the same unsteered text for each prompt/seed.
    hidden = [*active_primary, *RANDOM_ARMS, "baseline"]
    baseline_text: dict[tuple[str, int], str] | None = None
    for arm_id in hidden:
        current = {
            (str(row["prompt_id"]), int(row["sample"])): str(row["text"])
            for row in _arm_records(rows, arm_id) if float(row["alpha"]) == 0.0
        }
        if baseline_text is None:
            baseline_text = current
        elif current != baseline_text:
            raise HarnessError(f"alpha-zero text mismatch for hidden arm {arm_id}")

    prompts = bundle["heldout"]["prompts"]
    track_ids = {
        track: {str(row["id"]) for row in prompts if row["track"] == track}
        for track in ("ordinary_optional", "formal_technical_transfer")
    }
    analysis_lock = bundle["lock"]["evaluation"]
    bootstrap = analysis_lock["prompt_block_bootstrap"]
    signflip = analysis_lock["prompt_block_signflip"]
    claims: dict[str, dict[str, Any]] = {}
    for arm_index, arm_id in enumerate(active_primary):
        alpha = float(receipt["chosen_alpha_by_arm"][arm_id])
        treatment = _paired_seed_effects(_arm_records(rows, arm_id), alpha)
        random_effects = [
            _paired_seed_effects(_arm_records(rows, random_id), alpha)
            for random_id in RANDOM_ARMS
        ]
        if any(set(item) != set(treatment) for item in random_effects):
            raise HarnessError(f"{arm_id} and random controls do not share paired cells")
        claim_tracks: dict[str, Any] = {}
        for track_index, (track, ids) in enumerate(track_ids.items()):
            vs_zero_seed = treatment
            vs_random_seed = {
                key: treatment[key] - float(np.mean([item[key] for item in random_effects]))
                for key in treatment
            }
            vs_zero = _prompt_means(vs_zero_seed, ids)
            vs_random = _prompt_means(vs_random_seed, ids)
            p_zero, method_zero = _signflip_p(
                vs_zero, exact_limit=int(signflip["exact_through_prompt_blocks"]),
                draws=int(signflip["monte_carlo_draws_above_exact_limit"]),
                seed=int(signflip["seed"]) + arm_index * 10 + track_index,
            )
            p_random, method_random = _signflip_p(
                vs_random, exact_limit=int(signflip["exact_through_prompt_blocks"]),
                draws=int(signflip["monte_carlo_draws_above_exact_limit"]),
                seed=int(signflip["seed"]) + 100 + arm_index * 10 + track_index,
            )
            claim_tracks[track] = {
                "prompt_blocks": len(vs_zero),
                "paired_seed_cells": sum(key[0] in ids for key in treatment),
                "effect_vs_alpha_zero": float(np.mean(list(vs_zero.values()))),
                "effect_vs_mean_random": float(np.mean(list(vs_random.values()))),
                "ci95_vs_alpha_zero": _prompt_block_ci(
                    vs_zero, draws=int(bootstrap["draws"]),
                    seed=int(bootstrap["seed"]) + arm_index * 10 + track_index,
                ),
                "ci95_vs_mean_random": _prompt_block_ci(
                    vs_random, draws=int(bootstrap["draws"]),
                    seed=int(bootstrap["seed"]) + 100 + arm_index * 10 + track_index,
                ),
                "p_vs_alpha_zero": p_zero,
                "p_vs_mean_random": p_random,
                "signflip_method_vs_zero": method_zero,
                "signflip_method_vs_random": method_random,
                "intersection_union_p": max(p_zero, p_random),
            }
        treatment_chunks = grouped_specs[arm_id]
        safe_rows = []
        for result in treatment_chunks:
            gate = analysis_lock["quality_gate"]
            baselines = {
                (str(row["prompt_id"]), int(row["sample"])): row
                for row in result["records"] if float(row["alpha"]) == 0.0
            }
            safe_rows.extend(
                _safe_row(row, baselines[(str(row["prompt_id"]), int(row["sample"]))], gate)
                for row in result["records"] if float(row["alpha"]) == alpha
            )
        claims[arm_id] = {
            "locked_alpha": alpha,
            "tracks": claim_tracks,
            "deterministic_safe_fraction": float(np.mean(safe_rows)),
            "passes_deterministic_quality": float(np.mean(safe_rows)) >= float(analysis_lock["quality_gate"]["minimum_safe_output_fraction"]),
        }
    adjusted = _holm({
        arm_id: row["tracks"]["ordinary_optional"]["intersection_union_p"]
        for arm_id, row in claims.items()
    })
    for arm_id, row in claims.items():
        primary = row["tracks"]["ordinary_optional"]
        row["holm_adjusted_primary_p"] = adjusted[arm_id]
        row["statistical_and_deterministic_success"] = bool(
            adjusted[arm_id] < 0.05
            and primary["effect_vs_alpha_zero"] > 0
            and primary["effect_vs_mean_random"] > 0
            and row["passes_deterministic_quality"]
        )
        row["final_success_pending_instruction_quality"] = None

    descriptive: dict[str, Any] = {}
    for arm_id in ("baseline", "explicit_prompt"):
        arm_rows = _arm_records(rows, arm_id)
        descriptive[arm_id] = {}
        for track, ids in track_ids.items():
            selected = [
                float(row["target_metrics"]["target_present"])
                for row in arm_rows if str(row["prompt_id"]) in ids
            ]
            counts = [
                float(row["target_metrics"]["emoji_cluster_count"])
                for row in arm_rows if str(row["prompt_id"]) in ids
            ]
            descriptive[arm_id][track] = {
                "records": len(selected),
                "emoji_presence": float(np.mean(selected)),
                "mean_emoji_clusters": float(np.mean(counts)),
            }
    return {
        "schema": HELDOUT_ANALYSIS_ID,
        "study_id": bundle["lock"]["study_id"],
        "model_key": model_key,
        "lock_bundle_sha256": bundle["lock_bundle_sha256"],
        "alpha_receipt_sha256": canonical_sha256(receipt),
        "raw_result_files": len(rows),
        "raw_records": observed_records,
        "alpha_zero_match_across_hidden_arms": True,
        "active_primary_treatments": list(active_primary),
        "treatment_exclusions": dict(receipt["treatment_exclusions"]),
        "claims": claims,
        "descriptive_controls": descriptive,
        "instruction_quality": {
            "status": "pending_blinded_rating",
            "rubric": bundle["lock"]["instruction_quality"],
        },
    }


def write_blinded_rating_sheet(
    results: Iterable[Mapping[str, Any]], alpha_receipt: Mapping[str, Any], *,
    model_key: str, sheet_path: Path, key_path: Path,
) -> dict[str, Any]:
    rows = list(results)
    selected: list[dict[str, Any]] = []
    active_primary = active_treatment_arms(alpha_receipt)
    arm_alpha = {"baseline": 0.0, "explicit_prompt": 0.0, **{
        arm: float(alpha_receipt["chosen_alpha_by_arm"][arm]) for arm in active_primary
    }}
    for arm_id, alpha in arm_alpha.items():
        arm_rows = _arm_records(rows, arm_id)
        by_prompt: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in arm_rows:
            if float(row["alpha"]) == alpha:
                by_prompt[str(row["prompt_id"])].append(row)
        if len(by_prompt) != 40:
            raise HarnessError(f"rating sample for {arm_id} does not cover 40 prompts")
        for prompt_id, candidates in by_prompt.items():
            chosen = min(candidates, key=lambda row: int(row["sample"]))
            selected.append({
                "arm_id": arm_id, "alpha": alpha, "prompt_id": prompt_id,
                "prompt_split": chosen["prompt_split"], "sample": int(chosen["sample"]),
                "prompt": chosen["prompt"], "response": chosen["text"],
            })
    rng = random.Random(20260826 + (0 if model_key == "qwen" else 1))
    rng.shuffle(selected)
    key_rows = []
    blinded = []
    for index, row in enumerate(selected, start=1):
        rating_id = f"{model_key}-rating-{index:04d}"
        key_rows.append({"rating_id": rating_id, **row})
        blinded.append({
            "rating_id": rating_id, "prompt": row["prompt"],
            "response": row["response"], "relevance_0_2": "",
            "fluency_0_2": "", "notes": "",
        })
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_sheet = sheet_path.with_suffix(sheet_path.suffix + ".tmp")
    with temporary_sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(blinded[0]))
        writer.writeheader()
        writer.writerows(blinded)
    temporary_sheet.replace(sheet_path)
    mapping = {
        "schema": "emoji-method-comparison-rating-key/v1",
        "model_key": model_key,
        "rubric_id": "emoji-method-comparison-relevance-fluency/v1",
        "sheet_sha256": _sha256(sheet_path),
        "rows": key_rows,
    }
    temporary_key = key_path.with_suffix(key_path.suffix + ".tmp")
    temporary_key.write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary_key.replace(key_path)
    return {"rows": len(blinded), "sheet_sha256": mapping["sheet_sha256"]}
