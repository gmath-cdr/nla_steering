"""Preregistered paired, prompt-blocked AxBench-pilot analysis helpers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
import hashlib
import itertools
import json
from pathlib import Path
import time
from typing import Any

import numpy as np

from harness import HarnessError, canonical_sha256
from nla_lab.study import (
    normalize_prompts,
    seal_alpha_receipt,
    validate_alpha_receipt,
    validate_spec_integrity,
)


ANALYSIS_ID = "axbench-paired-prompt-blocked/v1"
ALPHA_RULE_ID = "max-safe-signed-effect-smallest-near-best/v1"
HELDOUT_ANALYSIS_ID = "axbench-heldout-intersection-union/v1"


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
    study = value.get("spec", {}).get("study", {})
    if study.get("schema") != "axbench-derived-run/v1":
        raise HarnessError(f"{path} is not an AxBench-derived run")
    value["_path"] = str(path)
    value["_sha256"] = _sha256(path)
    return value


def _cell_map(records: Iterable[Mapping[str, Any]], field: str) -> dict[tuple[str, int, float], float]:
    output: dict[tuple[str, int, float], float] = {}
    for record in records:
        key = (
            str(record["prompt_id"]), int(record["sample"]), float(record["alpha"])
        )
        if key in output:
            raise HarnessError(f"duplicate paired cell {key}")
        metrics = record.get("target_metrics")
        if not isinstance(metrics, Mapping) or field not in metrics:
            raise HarnessError(f"record {key} is missing target metric {field!r}")
        output[key] = float(metrics[field])
    return output


def validate_calibration_matrix(result: Mapping[str, Any]) -> None:
    """Require the exact preregistered prompt x seed x alpha Cartesian grid."""
    spec = result.get("spec")
    if not isinstance(spec, Mapping):
        raise HarnessError("calibration result is missing its spec")
    validate_spec_integrity(spec, required=True)
    if spec.get("study", {}).get("phase") != "calibration":
        raise HarnessError("alpha selection may read calibration results only")
    prompts = normalize_prompts(spec.get("prompts"))
    prompt_ids = [row["id"] for row in prompts]
    if any(row.get("split") != "calibration" for row in prompts):
        raise HarnessError("calibration spec contains a non-calibration prompt")
    alphas = [float(value) for value in spec.get("alphas", [])]
    sample_count = int(spec.get("samples_per_prompt", 0))
    seed_offset = int(spec.get("seed_offset", 0))
    seed_scheme = str(spec.get("seed_scheme", "shared_sample_index_v1"))
    if not alphas or sample_count < 1:
        raise HarnessError("calibration spec has an empty alpha or sample axis")
    if seed_scheme not in {"shared_sample_index_v1", "prompt_offset_v1"}:
        raise HarnessError(f"unknown seed scheme {seed_scheme!r}")
    expected: set[tuple[str, int, float]] = set()
    for prompt_index, prompt_id in enumerate(prompt_ids):
        prompt_seed_offset = (
            seed_offset + prompt_index * sample_count
            if seed_scheme == "prompt_offset_v1" else seed_offset
        )
        for seed in range(prompt_seed_offset, prompt_seed_offset + sample_count):
            for alpha in alphas:
                expected.add((prompt_id, seed, alpha))
    observed: set[tuple[str, int, float]] = set()
    for row in result.get("records", []):
        if row.get("prompt_split") != "calibration":
            raise HarnessError("calibration result contains a non-calibration record")
        key = (str(row["prompt_id"]), int(row["sample"]), float(row["alpha"]))
        if key in observed:
            raise HarnessError(f"duplicate calibration cell {key}")
        observed.add(key)
    if observed != expected:
        missing = sorted(expected - observed)[:3]
        extra = sorted(observed - expected)[:3]
        raise HarnessError(
            f"calibration matrix is not the locked Cartesian grid; "
            f"missing={missing}, extra={extra}"
        )
    if int(spec.get("expected_records", -1)) != len(expected):
        raise HarnessError("spec expected_records does not match its Cartesian grid")


def paired_prompt_effects(
    result: Mapping[str, Any],
    alpha: float,
    *,
    field: str = "target_rate_per_100_words",
) -> dict[str, float]:
    cells = _cell_map(result["records"], field)
    by_prompt: defaultdict[str, list[float]] = defaultdict(list)
    for prompt_id, sample, observed_alpha in cells:
        if observed_alpha != float(alpha):
            continue
        base_key = (prompt_id, sample, 0.0)
        if base_key not in cells:
            raise HarnessError(f"missing paired alpha-zero cell for {(prompt_id, sample)}")
        by_prompt[prompt_id].append(cells[(prompt_id, sample, observed_alpha)] - cells[base_key])
    if not by_prompt:
        raise HarnessError(f"result contains no cells at alpha {alpha}")
    return {prompt: float(np.mean(values)) for prompt, values in by_prompt.items()}


def prompt_block_bootstrap_ci(
    prompt_effects: Mapping[str, float],
    *,
    draws: int,
    seed: int,
) -> tuple[float, float]:
    values = np.asarray(list(prompt_effects.values()), dtype=float)
    if values.size < 2:
        raise HarnessError("prompt-block bootstrap needs at least two prompt blocks")
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, values.size, size=(draws, values.size))
    means = values[indices].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def validate_heldout_matrix(
    result: Mapping[str, Any], *, expected_phase: str | None = None
) -> None:
    """Require an exact sealed evaluation/transfer Cartesian grid."""

    spec = result.get("spec")
    if not isinstance(spec, Mapping):
        raise HarnessError("held-out result is missing its spec")
    validate_spec_integrity(spec, required=True)
    phase = str(spec.get("study", {}).get("phase", ""))
    if phase not in {"evaluation", "transfer"}:
        raise HarnessError("held-out analysis accepts evaluation or transfer only")
    if expected_phase is not None and phase != expected_phase:
        raise HarnessError(
            f"held-out result phase {phase!r} does not match {expected_phase!r}"
        )
    if spec.get("design", {}).get("preregistered") is not True:
        raise HarnessError("held-out analysis requires preregistered specs")
    prompts = normalize_prompts(spec.get("prompts"))
    if any(row.get("split") != phase for row in prompts):
        raise HarnessError(f"{phase} spec contains a prompt from another split")
    alphas = [float(value) for value in spec.get("alphas", [])]
    sample_count = int(spec.get("samples_per_prompt", 0))
    seed_offset = int(spec.get("seed_offset", 0))
    seed_scheme = str(spec.get("seed_scheme", "shared_sample_index_v1"))
    if not alphas or sample_count < 1:
        raise HarnessError("held-out spec has an empty alpha or sample axis")
    if seed_scheme not in {"shared_sample_index_v1", "prompt_offset_v1"}:
        raise HarnessError(f"unknown seed scheme {seed_scheme!r}")
    expected: set[tuple[str, int, float]] = set()
    for prompt_index, prompt in enumerate(prompts):
        prompt_seed_offset = (
            seed_offset + prompt_index * sample_count
            if seed_scheme == "prompt_offset_v1"
            else seed_offset
        )
        for seed in range(prompt_seed_offset, prompt_seed_offset + sample_count):
            for alpha in alphas:
                expected.add((prompt["id"], seed, alpha))
    observed: set[tuple[str, int, float]] = set()
    for row in result.get("records", []):
        if row.get("prompt_split") != phase:
            raise HarnessError(f"{phase} result contains a record from another split")
        key = (str(row["prompt_id"]), int(row["sample"]), float(row["alpha"]))
        if key in observed:
            raise HarnessError(f"duplicate held-out cell {key}")
        observed.add(key)
    if observed != expected:
        missing = sorted(expected - observed)[:3]
        extra = sorted(observed - expected)[:3]
        raise HarnessError(
            f"held-out matrix is not the locked Cartesian grid; "
            f"missing={missing}, extra={extra}"
        )
    if int(spec.get("expected_records", -1)) != len(expected):
        raise HarnessError("held-out expected_records disagrees with the grid")


def paired_seed_effects(
    result: Mapping[str, Any],
    alpha: float,
    *,
    field: str,
) -> dict[tuple[str, int], float]:
    """Return target(alpha)-target(0) for every paired prompt/seed cell."""

    cells = _cell_map(result["records"], field)
    output: dict[tuple[str, int], float] = {}
    for prompt_id, sample, observed_alpha in cells:
        if observed_alpha != float(alpha):
            continue
        base_key = (prompt_id, sample, 0.0)
        if base_key not in cells:
            raise HarnessError(f"missing alpha-zero pair for {(prompt_id, sample)}")
        output[(prompt_id, sample)] = cells[(
            prompt_id, sample, observed_alpha
        )] - cells[base_key]
    if not output:
        raise HarnessError(f"result contains no paired cells at alpha {alpha}")
    return output


def _prompt_means(effects: Mapping[tuple[str, int], float]) -> dict[str, float]:
    grouped: defaultdict[str, list[float]] = defaultdict(list)
    for (prompt_id, _sample), value in effects.items():
        grouped[prompt_id].append(float(value))
    return {prompt_id: float(np.mean(values)) for prompt_id, values in grouped.items()}


def exact_one_sided_signflip_p(prompt_effects: Mapping[str, float]) -> float:
    """Exact prompt-block sign-flip p-value for a positive mean effect."""

    values = np.asarray(list(prompt_effects.values()), dtype=float)
    if values.size < 2:
        raise HarnessError("sign-flip test needs at least two prompt blocks")
    if values.size > 20:
        raise HarnessError("exact sign-flip test is limited to 20 prompt blocks")
    observed = float(values.mean())
    exceed = 0
    total = 2 ** int(values.size)
    tolerance = 1e-12
    for signs in itertools.product((-1.0, 1.0), repeat=int(values.size)):
        permuted = float(np.mean(values * np.asarray(signs)))
        if permuted >= observed - tolerance:
            exceed += 1
    return exceed / total


def holm_adjust(p_values: Mapping[str, float]) -> dict[str, float]:
    """Return monotone Holm-adjusted p-values keyed like the input."""

    if not p_values:
        raise HarnessError("Holm adjustment needs at least one claim")
    ordered = sorted((float(value), key) for key, value in p_values.items())
    adjusted: dict[str, float] = {}
    running = 0.0
    count = len(ordered)
    for index, (value, key) in enumerate(ordered):
        if not 0.0 <= value <= 1.0:
            raise HarnessError(f"invalid p-value for {key}: {value}")
        running = max(running, (count - index) * value)
        adjusted[key] = min(1.0, running)
    return adjusted


def assert_alpha_zero_text_match(results: Iterable[Mapping[str, Any]]) -> None:
    """Fail if shared prompt/seed alpha-zero replies differ across arms."""

    reference: dict[tuple[str, int], str] | None = None
    reference_arm = ""
    for result in results:
        arm = str(result["spec"]["study"]["arm_id"])
        rows = {
            (str(row["prompt_id"]), int(row["sample"])): str(row.get("text", ""))
            for row in result["records"]
            if float(row["alpha"]) == 0.0
        }
        if reference is None:
            reference = rows
            reference_arm = arm
            continue
        if rows != reference:
            raise HarnessError(
                f"alpha-zero texts differ between {reference_arm!r} and {arm!r}"
            )


def _heldout_alpha_receipt(result: Mapping[str, Any]) -> Mapping[str, Any]:
    study = result.get("spec", {}).get("study", {})
    receipt = study.get("alpha_receipt")
    if not isinstance(receipt, Mapping):
        raise HarnessError("held-out result lacks its frozen alpha receipt")
    try:
        validate_alpha_receipt(receipt, require_frozen=True)
    except ValueError as exc:
        raise HarnessError(str(exc)) from exc
    if study.get("alpha_receipt_sha256") != canonical_sha256(receipt):
        raise HarnessError("held-out result alpha-receipt hash does not match")
    return receipt


def evaluate_heldout_claim(
    treatment: Mapping[str, Any],
    random_results: Iterable[Mapping[str, Any]],
    baseline: Mapping[str, Any],
    bundle: Mapping[str, Any],
    *,
    expected_phase: str = "evaluation",
) -> dict[str, Any]:
    """Evaluate one treatment against paired zero and mean random controls."""

    random_rows = list(random_results)
    if len(random_rows) != 3:
        raise HarnessError("a held-out claim requires exactly three random controls")
    all_results = [baseline, treatment, *random_rows]
    current_lock = canonical_sha256(bundle["hashes"])
    for result in all_results:
        validate_heldout_matrix(result, expected_phase=expected_phase)
        spec_locks = result.get("spec", {}).get("study", {}).get("locks")
        if not isinstance(spec_locks, Mapping) or (
            canonical_sha256(spec_locks) != current_lock
        ):
            raise HarnessError("held-out result uses stale preregistration locks")
    receipts = [_heldout_alpha_receipt(result) for result in all_results]
    if any(receipt.get("lock_bundle_sha256") != current_lock for receipt in receipts):
        raise HarnessError("held-out alpha receipt uses stale preregistration locks")
    receipt_hashes = {canonical_sha256(receipt) for receipt in receipts}
    if len(receipt_hashes) != 1:
        raise HarnessError("held-out treatment and controls use different alpha receipts")
    identities = {
        (
            row["runtime"]["model_key"],
            row["spec"]["study"]["concept_key"],
            row["spec"]["study"]["phase"],
        )
        for row in all_results
    }
    if len(identities) != 1:
        raise HarnessError("held-out claim mixes model, concept, or phase")
    _model_key, concept_key, phase = next(iter(identities))
    if baseline["spec"]["study"]["arm_id"] != "baseline":
        raise HarnessError("held-out claim lacks the explicit baseline arm")
    observed_random_ids = {
        str(row["spec"]["study"]["arm_id"]) for row in random_rows
    }
    expected_random_ids = {"random_101", "random_202", "random_303"}
    if observed_random_ids != expected_random_ids:
        raise HarnessError(
            f"random controls are {sorted(observed_random_ids)}, expected "
            f"{sorted(expected_random_ids)}"
        )
    assert_alpha_zero_text_match(all_results)
    nonzero = sorted({
        float(row["alpha"])
        for row in treatment["records"]
        if float(row["alpha"]) != 0.0
    }, key=abs)
    if len(nonzero) != 1:
        raise HarnessError("held-out treatment must contain exactly one locked alpha")
    alpha = nonzero[0]
    concept = bundle["concepts"][concept_key]
    field = str(concept["primary_metric"])
    sign = 1.0 if concept["effect_direction"] == "increase" else -1.0
    treatment_seed = paired_seed_effects(treatment, alpha, field=field)
    random_seed_rows = [
        paired_seed_effects(row, alpha, field=field) for row in random_rows
    ]
    if any(set(row) != set(treatment_seed) for row in random_seed_rows):
        raise HarnessError("treatment and random controls do not share paired cells")
    signed_baseline_seed = {
        key: sign * value for key, value in treatment_seed.items()
    }
    signed_random_seed = {
        key: sign * (
            treatment_seed[key]
            - float(np.mean([row[key] for row in random_seed_rows]))
        )
        for key in treatment_seed
    }
    baseline_prompt = _prompt_means(signed_baseline_seed)
    random_prompt = _prompt_means(signed_random_seed)
    analysis = bundle["study"]["analysis"]
    draws = int(analysis["bootstrap_draws"])
    seed = int(analysis["bootstrap_seed"])
    baseline_p = exact_one_sided_signflip_p(baseline_prompt)
    random_p = exact_one_sided_signflip_p(random_prompt)
    safe_fraction = safety_fraction(
        treatment, alpha, bundle["study"]["safety_gate"]
    )
    return {
        "schema": HELDOUT_ANALYSIS_ID,
        "model_key": treatment["runtime"]["model_key"],
        "concept_key": concept_key,
        "arm_id": treatment["spec"]["study"]["arm_id"],
        "phase": phase,
        "locked_alpha": alpha,
        "effect_direction": concept["effect_direction"],
        "primary_metric": field,
        "prompt_blocks": len(baseline_prompt),
        "signed_effect_vs_baseline": float(np.mean(list(baseline_prompt.values()))),
        "signed_effect_vs_random_mean": float(np.mean(list(random_prompt.values()))),
        "ci95_vs_baseline": list(prompt_block_bootstrap_ci(
            baseline_prompt, draws=draws, seed=seed
        )),
        "ci95_vs_random_mean": list(prompt_block_bootstrap_ci(
            random_prompt, draws=draws, seed=seed + 1
        )),
        "p_vs_baseline": baseline_p,
        "p_vs_random_mean": random_p,
        "intersection_union_p": max(baseline_p, random_p),
        "safe_fraction": safe_fraction,
        "passes_safety": safe_fraction >= float(
            bundle["study"]["safety_gate"]["minimum_safe_output_fraction"]
        ),
        "alpha_zero_match": True,
    }


def evaluate_heldout_family(
    results: Iterable[Mapping[str, Any]],
    bundle: Mapping[str, Any],
    *,
    expected_phase: str = "evaluation",
) -> dict[str, Any]:
    """Evaluate and Holm-correct the locked primary claim family."""

    rows = list(results)
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (
            str(row["spec"]["study"]["concept_key"]),
            str(row["spec"]["study"]["arm_id"]),
        )
        if key in indexed:
            raise HarnessError(f"duplicate held-out result for {key}")
        indexed[key] = row
    claims: dict[str, dict[str, Any]] = {}
    exclusions: list[dict[str, str]] = []
    for declaration in bundle["study"]["primary_claims"]:
        claim_id = str(declaration["id"])
        concept_key = str(declaration["concept"])
        arm_id = str(declaration["arm"])
        treatment = indexed.get((concept_key, arm_id))
        if treatment is None:
            carrier = indexed.get((concept_key, "nla_ar"))
            if declaration.get("eligibility") and carrier is not None:
                alpha_receipt = _heldout_alpha_receipt(carrier)
                locked = alpha_receipt.get("treatment_exclusions", {}).get(arm_id)
                source = locked.get("receipt") if isinstance(locked, Mapping) else None
                if isinstance(source, Mapping):
                    _validate_direct_atom_exclusion(
                        source,
                        bundle,
                        model_key=str(carrier["runtime"]["model_key"]),
                        concept_key=concept_key,
                        checkpoint_markers=alpha_receipt[
                            "base_checkpoint_marker_sha256"
                        ],
                    )
                else:
                    raise HarnessError(
                        f"missing treatment result for claim {claim_id}; no sealed "
                        "tokenization exclusion exists"
                    )
                exclusions.append({
                    "claim_id": claim_id,
                    "reason": "pre_behavior_tokenization_ineligible",
                    "receipt_sha256": str(locked["receipt_sha256"]),
                })
                continue
            raise HarnessError(f"missing treatment result for claim {claim_id}")
        baseline = indexed.get((concept_key, "baseline"))
        randoms = [
            indexed.get((concept_key, arm_id))
            for arm_id in ("random_101", "random_202", "random_303")
        ]
        if baseline is None or any(row is None for row in randoms):
            raise HarnessError(f"claim {claim_id} lacks baseline/random controls")
        claims[claim_id] = evaluate_heldout_claim(
            treatment,
            [row for row in randoms if row is not None],
            baseline,
            bundle,
            expected_phase=expected_phase,
        )
    adjusted = holm_adjust({
        claim_id: row["intersection_union_p"] for claim_id, row in claims.items()
    })
    for claim_id, row in claims.items():
        row["holm_adjusted_p"] = adjusted[claim_id]
        row["success"] = bool(
            adjusted[claim_id] < 0.05
            and row["signed_effect_vs_baseline"] > 0
            and row["signed_effect_vs_random_mean"] > 0
            and row["passes_safety"]
            and row["alpha_zero_match"]
        )
    return {
        "schema": HELDOUT_ANALYSIS_ID,
        "analysis_plan": dict(bundle["study"]["analysis"]),
        "phase": expected_phase,
        "claims": claims,
        "excluded_claims": exclusions,
        "realized_family_size": len(claims),
    }


def _safe_record(metrics: Mapping[str, Any], gate: Mapping[str, Any]) -> bool:
    checks = (
        float(metrics["unicode_word_count"]) >= float(gate["minimum_unicode_words"]),
        float(metrics["unicode_word_count"]) <= float(gate["maximum_unicode_words"]),
        float(metrics["unique_unicode_word_ratio"]) >= float(gate["minimum_unique_unicode_word_ratio"]),
        float(metrics["max_token_run"]) <= float(gate["maximum_token_run"]),
        float(metrics["dominant_token_fraction"]) <= float(gate["maximum_dominant_token_fraction"]),
        float(metrics["max_character_run"]) <= float(gate["maximum_character_run"]),
        float(metrics["replacement_character_count"]) <= float(gate["maximum_replacement_characters"]),
        float(metrics["target_token_fraction"]) <= float(gate["maximum_target_token_fraction"]),
    )
    return all(checks)


def safety_fraction(result: Mapping[str, Any], alpha: float, gate: Mapping[str, Any]) -> float:
    rows = [row for row in result["records"] if float(row["alpha"]) == float(alpha)]
    if not rows:
        raise HarnessError(f"no safety rows at alpha {alpha}")
    return float(np.mean([_safe_record(row["target_metrics"], gate) for row in rows]))


def select_alpha(result: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]:
    validate_calibration_matrix(result)
    spec_study = result["spec"]["study"]
    concept_key = spec_study["concept_key"]
    concept = bundle["concepts"][concept_key]
    direction = concept.get("effect_direction", "increase")
    primary_field = str(concept.get("primary_metric", "target_rate_per_100_words"))
    sign = 1.0 if direction == "increase" else -1.0
    gate = bundle["study"]["safety_gate"]
    minimum_safe = float(gate["minimum_safe_output_fraction"])
    alphas = sorted({float(row["alpha"]) for row in result["records"] if float(row["alpha"]) != 0}, key=abs)
    candidates = []
    for alpha in alphas:
        prompt_effects = paired_prompt_effects(result, alpha, field=primary_field)
        raw_mean = float(np.mean(list(prompt_effects.values())))
        safe_fraction = safety_fraction(result, alpha, gate)
        candidates.append({
            "alpha": alpha,
            "mean_paired_effect": raw_mean,
            "signed_effect": sign * raw_mean,
            "safe_fraction": safe_fraction,
            "passes_safety": safe_fraction >= minimum_safe,
        })
    safe = [row for row in candidates if row["passes_safety"]]
    if not safe:
        raise HarnessError("no nonzero calibration alpha passes the preregistered safety gate")
    best = max(row["signed_effect"] for row in safe)
    tolerance = max(0.05, 0.05 * abs(best))
    near_best = [row for row in safe if row["signed_effect"] >= best - tolerance]
    chosen = min(near_best, key=lambda row: abs(row["alpha"]))
    return {
        "rule": ALPHA_RULE_ID,
        "effect_direction": direction,
        "primary_metric": primary_field,
        "near_best_tolerance": tolerance,
        "chosen_alpha": chosen["alpha"],
        "candidates": candidates,
    }


def _validate_direct_atom_exclusion(
    receipt: Mapping[str, Any],
    bundle: Mapping[str, Any],
    *,
    model_key: str,
    concept_key: str,
    checkpoint_markers: Mapping[str, str],
) -> None:
    concept = bundle["concepts"][concept_key]
    declaration = concept.get("direct_j_atom")
    if not isinstance(declaration, Mapping):
        raise HarnessError(f"concept {concept_key} has no declared direct J atom")
    expected_space = bundle["study"]["models"][model_key]["space_id"]
    expected_variants = [
        declaration["preferred_variant"], declaration["fallback_variant"]
    ]
    checks = (
        receipt.get("status") == "ineligible_tokenization",
        receipt.get("model_key") == model_key,
        receipt.get("concept_key") == concept_key,
        receipt.get("space_id") == expected_space,
        receipt.get("concept_registry_sha256") == bundle["hashes"]["concepts"],
        receipt.get("checkpoint_marker_sha256") == checkpoint_markers,
        receipt.get("token_text") == declaration["token_text"],
        receipt.get("requested_variants") == expected_variants,
    )
    if not all(checks):
        raise HarnessError(
            "direct-J exclusion receipt does not match the model, tokenizer, "
            "concept declaration, or calibration checkpoint"
        )


def make_alpha_receipt(
    results: Iterable[Mapping[str, Any]],
    bundle: Mapping[str, Any],
    *,
    direct_atom_exclusion: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = list(results)
    if not rows:
        raise HarnessError("no calibration results supplied")
    identities = {
        (row["runtime"]["model_key"], row["spec"]["study"]["concept_key"])
        for row in rows
    }
    if len(identities) != 1:
        raise HarnessError("one alpha receipt must cover exactly one model x concept")
    model_key, concept_key = next(iter(identities))
    locks = {canonical_sha256(row["spec"]["study"]["locks"]) for row in rows}
    if len(locks) != 1:
        raise HarnessError("calibration results disagree on preregistration locks")
    current_lock = canonical_sha256(bundle["hashes"])
    if next(iter(locks)) != current_lock:
        raise HarnessError("calibration results do not match the current preregistration lock")
    chosen: dict[str, float] = {}
    details: dict[str, Any] = {}
    sources: dict[str, str] = {}
    base_markers: dict[str, dict[str, str]] = {}
    direction_integrity: dict[str, dict[str, Any]] = {}
    git_commits: set[str] = set()
    package_sets: dict[str, dict[str, Any]] = {}
    for row in rows:
        validate_calibration_matrix(row)
        if row.get("spec", {}).get("design", {}).get("preregistered") is not True:
            raise HarnessError("alpha receipts require a frozen preregistered calibration spec")
        if row.get("runtime", {}).get("git", {}).get("dirty") is not False:
            raise HarnessError("alpha receipts require a clean calibration runtime")
        commit = str(row.get("runtime", {}).get("git", {}).get("commit") or "")
        if not commit:
            raise HarnessError("calibration result lacks a Git commit")
        git_commits.add(commit)
        packages = row.get("runtime", {}).get("package_versions")
        if not isinstance(packages, Mapping) or not packages:
            raise HarnessError("calibration result lacks package versions")
        package_sets[canonical_sha256(packages)] = dict(packages)
        markers = row.get("runtime", {}).get("checkpoint_marker_sha256", {}).get("base")
        if not isinstance(markers, Mapping) or not markers:
            raise HarnessError("calibration result lacks base checkpoint markers")
        base_markers[canonical_sha256(markers)] = dict(markers)
        arm_id = row["spec"]["study"]["arm_id"]
        if arm_id in sources:
            raise HarnessError(f"duplicate calibration result for arm {arm_id!r}")
        kind = row["spec"]["method"]["root"]["method"]
        observed_kind = row.get("method", {}).get("root", {}).get("method")
        if observed_kind != kind:
            raise HarnessError(
                f"result method metadata disagrees with sealed spec for arm {arm_id!r}"
            )
        sources[arm_id] = row.get("_sha256", "unknown")
        if kind in {"nla_ar_contrast", "j_lens_atom"}:
            integrity = row.get("direction_integrity")
            if not isinstance(integrity, Mapping) or not integrity.get("unit_direction_sha256"):
                raise HarnessError(f"treatment arm {arm_id!r} lacks direction integrity")
            direction_integrity[arm_id] = dict(integrity)
            selection = select_alpha(row, bundle)
            chosen[arm_id] = float(selection["chosen_alpha"])
            details[arm_id] = selection
    if "nla_ar" not in chosen:
        raise HarnessError("receipt requires the NLA-AR treatment calibration")
    if len(base_markers) != 1:
        raise HarnessError("calibration arms used different base checkpoint/tokenizer markers")
    if len(git_commits) != 1 or len(package_sets) != 1:
        raise HarnessError("calibration arms used different code commits or package versions")
    exclusions: dict[str, dict[str, Any]] = {}
    concept = bundle["concepts"][concept_key]
    direct_declared = "direct_j_atom" in concept
    if direct_declared and "direct_j_atom" not in chosen:
        if not isinstance(direct_atom_exclusion, Mapping):
            raise HarnessError(
                "token-eligible concepts require direct-J calibration or a "
                "pre-behavior ineligible-tokenization receipt"
            )
        _validate_direct_atom_exclusion(
            direct_atom_exclusion,
            bundle,
            model_key=model_key,
            concept_key=concept_key,
            checkpoint_markers=next(iter(base_markers.values())),
        )
        exclusions["direct_j_atom"] = {
            "reason": "ineligible_tokenization",
            "receipt": dict(direct_atom_exclusion),
            "receipt_sha256": canonical_sha256(direct_atom_exclusion),
        }
    elif direct_atom_exclusion is not None:
        raise HarnessError(
            "a direct-J exclusion receipt was supplied even though that arm was calibrated"
        )
    return seal_alpha_receipt({
        "schema": "axbench-alpha-receipt/v1",
        "status": "pending_raw_audit",
        "analysis": ANALYSIS_ID,
        "selection_rule": ALPHA_RULE_ID,
        "model_key": model_key,
        "concept_key": concept_key,
        "chosen_alpha_by_arm": chosen,
        "random_control_alphas": sorted(set(chosen.values()), key=abs),
        "selection_details": details,
        "calibration_result_sha256": sources,
        "lock_bundle_sha256": next(iter(locks)),
        "base_checkpoint_marker_sha256": next(iter(base_markers.values())),
        "treatment_direction_integrity": direction_integrity,
        "treatment_exclusions": exclusions,
        "calibration_git_commit": next(iter(git_commits)),
        "calibration_package_versions": next(iter(package_sets.values())),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "audit": None,
    })


def freeze_alpha_receipt(
    receipt: Mapping[str, Any], *, reviewer: str, audit_note: str
) -> dict[str, Any]:
    validate_alpha_receipt(receipt)
    if receipt.get("status") != "pending_raw_audit":
        raise HarnessError("only a pending receipt can be frozen")
    if not reviewer.strip() or not audit_note.strip():
        raise HarnessError("freezing requires a reviewer and non-empty raw-audit note")
    output = deepcopy(dict(receipt))
    output["status"] = "frozen"
    output["audit"] = {
        "reviewer": reviewer.strip(),
        "note": audit_note.strip(),
        "frozen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    validate_alpha_receipt(output, require_frozen=True)
    return output
