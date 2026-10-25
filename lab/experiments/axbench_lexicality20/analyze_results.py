"""Paired held-out analysis for the frozen ``axb44-crossmodel-v1`` campaign.

This analyzer consumes only completed, retrieved model run directories. It
does not select alphas, regenerate outputs, call a judge, or inspect J-space.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
from typing import Any, Iterable


PRIMARY_METRIC = "axbench_harmonic"
TRACKS = ("balanced_union", "concept500_negative", "shared_alpaca_transfer")


class AnalysisError(RuntimeError):
    pass


def _mean(values: Iterable[float]) -> float:
    rows = list(values)
    if not rows:
        raise AnalysisError("cannot average an empty collection")
    return sum(rows) / len(rows)


def holm_adjust(p_values: dict[str, float]) -> dict[str, float]:
    """Return monotone Holm-adjusted p-values keyed like ``p_values``."""

    ordered = sorted(p_values.items(), key=lambda row: (row[1], row[0]))
    adjusted: dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for index, (key, value) in enumerate(ordered):
        running = max(running, min(1.0, float(value) * (total - index)))
        adjusted[key] = running
    return adjusted


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise AnalysisError("empty bootstrap distribution")
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def multilevel_bootstrap(
    rows: list[dict[str, Any]], *, seed: int, repetitions: int = 10_000,
) -> dict[str, float]:
    """Bootstrap paired differences over concepts, then prompts within concept."""

    grouped: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[int(row["concept_id"])][str(row["prompt_id"])].append(float(row["difference"]))
    concepts = sorted(grouped)
    if not concepts or any(not grouped[concept] for concept in concepts):
        raise AnalysisError("incomplete clusters for bootstrap")
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(repetitions):
        values: list[float] = []
        for concept in rng.choices(concepts, k=len(concepts)):
            prompts = sorted(grouped[concept])
            for prompt in rng.choices(prompts, k=len(prompts)):
                values.extend(grouped[concept][prompt])
        draws.append(_mean(values))
    lower = percentile(draws, 0.025)
    upper = percentile(draws, 0.975)
    p_lower = (1 + sum(value <= 0 for value in draws)) / (repetitions + 1)
    p_upper = (1 + sum(value >= 0 for value in draws)) / (repetitions + 1)
    return {
        "ci95_low": lower,
        "ci95_high": upper,
        "bootstrap_p_two_sided": min(1.0, 2.0 * min(p_lower, p_upper)),
        "bootstrap_repetitions": repetitions,
        "bootstrap_seed": seed,
    }


def _load_complete(run_root: Path) -> dict[str, Any]:
    path = run_root / "COMPLETE.json"
    if not path.is_file():
        raise AnalysisError(f"missing completion manifest: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "axb44-complete/v1" or payload.get("status") != "complete":
        raise AnalysisError(f"invalid completion manifest: {path}")
    if payload.get("counts", {}).get("evaluation", {}).get("records") is None:
        raise AnalysisError("completion manifest lacks evaluation count")
    return payload


def load_rows(run_root: Path) -> tuple[str, list[dict[str, Any]]]:
    """Join exact held-out judge records to raw generation diagnostics."""

    complete = _load_complete(run_root)
    artifact_path = run_root / "judge_evaluation.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    if artifact.get("complete") is not True or artifact.get("phase") != "evaluation":
        raise AnalysisError(f"incomplete evaluation judge artifact: {artifact_path}")
    raw: dict[str, dict[str, Any]] = {}
    for path in sorted((run_root / "results" / "evaluation").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        spec = payload.get("spec", {})
        if spec.get("study", {}).get("phase") != "evaluation":
            continue
        for index, record in enumerate(payload.get("records", [])):
            raw[f"{path.name}#{index}"] = {
                "mode": spec.get("mode"),
                "perplexity": record.get("target_model_output_perplexity"),
                "n_words": record.get("probes", {}).get("n_words"),
                "unique_word_ratio": record.get("probes", {}).get("unique_word_ratio"),
                "hit_max_new_tokens": record.get("generation", {}).get("hit_max_new_tokens"),
            }
    records = artifact.get("records", {})
    if set(records) != set(raw):
        raise AnalysisError("raw/judge held-out record keys do not match exactly")
    rows = []
    for key, judged in records.items():
        scores = judged["scores"]
        rows.append({
            **judged,
            "record_key": key,
            "concept_score": float(scores["concept"]),
            "instruction_score": float(scores["instruction"]),
            "fluency_score": float(scores["fluency"]),
            **raw[key],
        })
    expected = int(complete["counts"]["evaluation"]["records"])
    if len(rows) != expected:
        raise AnalysisError(f"joined {len(rows)} rows, expected {expected}")
    return str(complete["model_key"]), rows


def pair_key(row: dict[str, Any]) -> tuple[int, str, int]:
    return int(row["concept_id"]), str(row["prompt_id"]), int(row["generation_seed"])


def by_arm(rows: list[dict[str, Any]], arm: str) -> dict[tuple[int, str, int], dict[str, Any]]:
    selected = {pair_key(row): row for row in rows if row["arm_id"] == arm}
    if len(selected) != sum(row["arm_id"] == arm for row in rows):
        raise AnalysisError(f"duplicate paired cells for arm {arm}")
    return selected


def paired_rows(
    rows: list[dict[str, Any]], left: str, right: str, *, track: str,
    metric: str = PRIMARY_METRIC,
) -> list[dict[str, Any]]:
    left_rows, right_rows = by_arm(rows, left), by_arm(rows, right)
    keys = set(left_rows) & set(right_rows)
    if track != "balanced_union":
        keys = {key for key in keys if left_rows[key]["prompt_track"] == track}
    expected = 80 if track == "balanced_union" else 40
    if len(keys) != expected:
        raise AnalysisError(f"{left} vs {right} {track}: {len(keys)} pairs != {expected}")
    return [{
        "concept_id": key[0], "prompt_id": key[1], "generation_seed": key[2],
        "difference": float(left_rows[key][metric]) - float(right_rows[key][metric]),
    } for key in sorted(keys)]


def contrast(
    rows: list[dict[str, Any]], left: str, right: str, *, track: str,
    seed: int,
) -> dict[str, Any]:
    paired = paired_rows(rows, left, right, track=track)
    result = {
        "left": left, "right": right, "track": track, "n_pairs": len(paired),
        "mean_difference": _mean(row["difference"] for row in paired),
    }
    result.update(multilevel_bootstrap(paired, seed=seed))
    return result


def random_contrast(
    rows: list[dict[str, Any]], treatment: str, control_seed: int, *,
    track: str, seed: int,
) -> dict[str, Any]:
    """Compare one treatment with its alpha/mode-matched isotropic control."""

    treatment_rows = [row for row in rows if row["arm_id"] == treatment]
    controls: dict[tuple[int, str, int, str, float], dict[str, Any]] = {}
    prefix = f"random_{control_seed}_"
    for row in rows:
        if not str(row["arm_id"]).startswith(prefix):
            continue
        key = (*pair_key(row), str(row["mode"]), round(float(row["alpha"]), 10))
        if key in controls:
            raise AnalysisError(f"duplicate matched random cell {key}")
        controls[key] = row
    paired = []
    for row in treatment_rows:
        if track != "balanced_union" and row["prompt_track"] != track:
            continue
        key = (*pair_key(row), str(row["mode"]), round(float(row["alpha"]), 10))
        if key not in controls:
            raise AnalysisError(f"missing random {control_seed} for {treatment}: {key}")
        paired.append({
            "concept_id": row["concept_id"], "prompt_id": row["prompt_id"],
            "generation_seed": row["generation_seed"],
            "difference": float(row[PRIMARY_METRIC]) - float(controls[key][PRIMARY_METRIC]),
        })
    expected = 80 if track == "balanced_union" else 40
    if len(paired) != expected:
        raise AnalysisError(
            f"{treatment} vs random {control_seed} {track}: {len(paired)} != {expected}"
        )
    output = {
        "left": treatment, "right": f"random_{control_seed}_matched",
        "track": track, "n_pairs": len(paired),
        "mean_difference": _mean(row["difference"] for row in paired),
    }
    output.update(multilevel_bootstrap(paired, seed=seed))
    return output


def arm_summary(rows: list[dict[str, Any]], arm: str, track: str) -> dict[str, Any]:
    selected = [row for row in rows if row["arm_id"] == arm]
    if track != "balanced_union":
        selected = [row for row in selected if row["prompt_track"] == track]
    if not selected:
        raise AnalysisError(f"no rows for {arm}/{track}")
    perplexities = [
        float(row["perplexity"]) for row in selected
        if row["perplexity"] is not None and math.isfinite(float(row["perplexity"]))
    ]
    return {
        "arm": arm, "track": track, "n": len(selected),
        "mean_axbench_harmonic": _mean(float(row[PRIMARY_METRIC]) for row in selected),
        "mean_concept": _mean(float(row["concept_score"]) for row in selected),
        "mean_instruction": _mean(float(row["instruction_score"]) for row in selected),
        "mean_fluency": _mean(float(row["fluency_score"]) for row in selected),
        "mean_target_model_perplexity": _mean(perplexities) if perplexities else None,
        "finite_perplexity_n": len(perplexities),
    }


def per_concept(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for concept_id in sorted({int(row["concept_id"]) for row in rows}):
        concept_rows = [row for row in rows if int(row["concept_id"]) == concept_id]
        labels = {str(row["concept_label"]) for row in concept_rows}
        if len(labels) != 1:
            raise AnalysisError(f"concept {concept_id} label drift")
        output[str(concept_id)] = {
            "label": labels.pop(),
            "arms": {
                arm: {
                    "n": len(selected),
                    "mean_axbench_harmonic": _mean(
                        float(row[PRIMARY_METRIC]) for row in selected
                    ),
                    "mean_concept": _mean(float(row["concept_score"]) for row in selected),
                    "mean_instruction": _mean(
                        float(row["instruction_score"]) for row in selected
                    ),
                    "mean_fluency": _mean(float(row["fluency_score"]) for row in selected),
                }
                for arm in ("unsteered", "axbench_prompt_steering", "diffmean", "caa", "nla")
                if (selected := [row for row in concept_rows if row["arm_id"] == arm])
            },
        }
    return output


def analyze_model(run_root: Path, *, seed: int) -> dict[str, Any]:
    model, rows = load_rows(run_root)
    alpha_receipt = json.loads((run_root / "alpha_receipt.json").read_text(encoding="utf-8"))
    if alpha_receipt.get("status") != "sealed":
        raise AnalysisError("alpha receipt is not sealed")
    named_arms = ("unsteered", "axbench_prompt_steering", "diffmean", "caa", "nla")
    summaries = {
        track: {arm: arm_summary(rows, arm, track) for arm in named_arms}
        for track in TRACKS
    }
    primary = {
        "nla_vs_diffmean": contrast(rows, "nla", "diffmean", track="balanced_union", seed=seed + 1),
        "nla_vs_caa": contrast(rows, "nla", "caa", track="balanced_union", seed=seed + 2),
    }
    adjusted = holm_adjust({
        key: value["bootstrap_p_two_sided"] for key, value in primary.items()
    })
    for key, value in primary.items():
        value["holm_adjusted_p"] = adjusted[key]
    track_contrasts = {
        track: {
            "nla_vs_diffmean": contrast(rows, "nla", "diffmean", track=track, seed=seed + 10 + index * 2),
            "nla_vs_caa": contrast(rows, "nla", "caa", track=track, seed=seed + 11 + index * 2),
        }
        for index, track in enumerate(TRACKS[1:])
    }
    secondary_pairs = (("diffmean", "unsteered"), ("caa", "unsteered"),
                       ("nla", "unsteered"), ("axbench_prompt_steering", "unsteered"))
    secondary = {
        f"{left}_vs_{right}": contrast(rows, left, right, track="balanced_union", seed=seed + 30 + index)
        for index, (left, right) in enumerate(secondary_pairs)
    }
    random_controls = {
        treatment: {
            str(control_seed): random_contrast(
                rows, treatment, control_seed, track="balanced_union",
                seed=seed + 100 + treatment_index * 10 + seed_index,
            )
            for seed_index, control_seed in enumerate((17, 42, 101))
        }
        for treatment_index, treatment in enumerate(("diffmean", "caa", "nla"))
    }
    return {
        "model_key": model, "run_artifact_id": run_root.name,
        "evaluation_rows": len(rows),
        "summaries": summaries, "primary_contrasts": primary,
        "track_contrasts_descriptive": track_contrasts,
        "secondary_contrasts_descriptive": secondary,
        "matched_random_controls_descriptive": random_controls,
        "per_concept_descriptive": per_concept(rows),
        "selected_calibration_alpha": alpha_receipt["chosen_alpha"],
        "limitations": [
            "Per-concept effects are descriptive; there are two held-out prompts per track.",
            "NLA uses last-and-decode injection while CAA and DiffMean use all-position injection.",
            "PromptSteering is a visible instruction reference, not a hidden-state direction.",
            "No J-lens or J-space arm is included.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwen-run", type=Path, required=True)
    parser.add_argument("--gemma-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-seed", type=int, default=20260826)
    args = parser.parse_args()
    payload = {
        "schema": "axb44-heldout-analysis/v1",
        "campaign_id": "axb44-crossmodel-v1",
        "primary_metric": PRIMARY_METRIC,
        "bootstrap_unit": "concept_then_prompt_with_all_paired_generation_seeds",
        "models": {
            "qwen": analyze_model(args.qwen_run, seed=args.bootstrap_seed),
            "gemma": analyze_model(args.gemma_run, seed=args.bootstrap_seed + 1000),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
