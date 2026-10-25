"""Analyze the authenticated lexical-alpha sensitivity administration.

This is a post-hoc objective-matching analysis.  It asks whether the strict
lexical ordering on the frozen ten-concept panel changes when alpha is chosen
by calibration lexical hits instead of the original AxBench harmonic score.
The sealed harmonic-tuned analysis remains primary.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
from typing import Any, Iterable

from experiments.axbench_lexicality20.lexical_select_alpha import (
    ANALYSIS_ID,
    EXPECTED_LEXICON_SHA256,
    exact_matches,
    file_sha256,
)
from experiments.axbench_lexicality20.lexical_judge_union import (
    load_items as load_judge_items,
    validate_complete_checkpoint,
)
from experiments.axbench_lexicality20.lexical_union import (
    NAMED_ARMS,
    load_union,
)


SCHEMA = "axb44-lexical-alpha-analysis/v1"
TREATMENTS = ("diffmean", "caa", "nla")
CONTROL_SEEDS = (17, 42, 101)
TRACKS = ("balanced_union", "concept500_negative", "shared_alpaca_transfer")
BOOTSTRAP_REPETITIONS = 10_000
ARM_INTERVAL_REPETITIONS = 20_000
ORIGINAL_RECORDS_SHA256 = (
    "3a08194425e9a1a459370e831483ebdd1ce8e6e72b6f0840998ce99ab0e51efa"
)
ORIGINAL_SUMMARY_SHA256 = (
    "ed01862759b93ea55aee24ba3d85aa19953f9dc6a6e33091a64d9507dcc11619"
)
ORIGINAL_JUDGE_ANALYSIS_SHA256 = (
    "f5689e7b3d1c44c9186df872700f7365aa0199353b6da1507604741a428a73ec"
)


class SensitivityAnalysisError(RuntimeError):
    """Raised when inputs or statistical units differ from the frozen plan."""


def mean(values: Iterable[float]) -> float:
    rows = list(values)
    if not rows:
        raise SensitivityAnalysisError("cannot average an empty collection")
    return sum(rows) / len(rows)


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise SensitivityAnalysisError("cannot take a percentile of no values")
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def concept_bootstrap_interval(
    rows: list[dict[str, Any]], *, seed: int,
) -> dict[str, Any]:
    """Resample concepts and retain all prompt and generation rows within each."""

    grouped: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        grouped[int(row["concept_id"])].append(float(row["strict_hit"]))
    concepts = sorted(grouped)
    if len(concepts) != 10 or any(len(grouped[concept]) != 8 for concept in concepts):
        raise SensitivityAnalysisError("arm interval requires 10 concepts x 8 rows")
    rng = random.Random(seed)
    draws = []
    for _ in range(ARM_INTERVAL_REPETITIONS):
        values = [
            value
            for concept in rng.choices(concepts, k=len(concepts))
            for value in grouped[concept]
        ]
        draws.append(mean(values))
    return {
        "ci95_low": percentile(draws, 0.025),
        "ci95_high": percentile(draws, 0.975),
        "bootstrap_unit": "concept_with_all_prompt_and_generation_rows_retained",
        "bootstrap_repetitions": ARM_INTERVAL_REPETITIONS,
        "bootstrap_seed": seed,
    }


def multilevel_difference_interval(
    rows: list[dict[str, Any]], *, seed: int,
) -> dict[str, Any]:
    """Resample concepts, then prompts, retaining both paired generation seeds."""

    grouped: dict[int, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        grouped[int(row["concept_id"])][str(row["prompt_id"])].append(
            float(row["difference"])
        )
    concepts = sorted(grouped)
    if len(concepts) != 10:
        raise SensitivityAnalysisError("paired comparison must contain 10 concepts")
    for concept in concepts:
        if len(grouped[concept]) != 4 or any(
            len(values) != 2 for values in grouped[concept].values()
        ):
            raise SensitivityAnalysisError(
                "paired comparison requires 4 prompts x 2 seeds per concept"
            )
    rng = random.Random(seed)
    draws = []
    for _ in range(BOOTSTRAP_REPETITIONS):
        values: list[float] = []
        for concept in rng.choices(concepts, k=len(concepts)):
            prompts = sorted(grouped[concept])
            for prompt in rng.choices(prompts, k=len(prompts)):
                values.extend(grouped[concept][prompt])
        draws.append(mean(values))
    return {
        "mean_difference": mean(row["difference"] for row in rows),
        "ci95_low": percentile(draws, 0.025),
        "ci95_high": percentile(draws, 0.975),
        "n_pairs": len(rows),
        "bootstrap_unit": "concept_then_prompt_with_both_generation_seeds_retained",
        "bootstrap_repetitions": BOOTSTRAP_REPETITIONS,
        "bootstrap_seed": seed,
        "p_value": None,
        "inference_status": "descriptive_post_hoc_no_hypothesis_test",
    }


def pair_key(row: dict[str, Any]) -> tuple[int, str, int]:
    return (
        int(row["concept_id"]),
        str(row["prompt_id"]),
        int(row["generation_seed"]),
    )


def read_lexicons(path: Path) -> dict[str, list[str]]:
    if file_sha256(path) != EXPECTED_LEXICON_SHA256:
        raise SensitivityAnalysisError("frozen lexicon hash mismatch")
    payload = json.loads(path.read_text(encoding="utf-8"))
    lexicons = payload.get("lexicons")
    if not isinstance(lexicons, dict) or len(lexicons) != 10:
        raise SensitivityAnalysisError("frozen lexicon payload is invalid")
    return {str(key): list(value) for key, value in lexicons.items()}


def score_rows(
    rows: list[dict[str, Any]], lexicons: dict[str, list[str]],
) -> list[dict[str, Any]]:
    scored = []
    for row in rows:
        terms = lexicons.get(str(row["concept_id"]))
        if not terms:
            raise SensitivityAnalysisError("a union row has no frozen lexicon")
        matched = sorted(
            {value.lower() for value in exact_matches(row["sentence"], terms)}
        )
        scored.append({**row, "strict_hit": int(bool(matched)), "matched_terms": matched})
    return scored


def load_original_records(path: Path) -> dict[str, list[dict[str, Any]]]:
    if file_sha256(path) != ORIGINAL_RECORDS_SHA256:
        raise SensitivityAnalysisError("original strict record artifact hash mismatch")
    output = {"qwen": [], "gemma": []}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("model") in output and row.get("arm_id") in NAMED_ARMS:
            output[row["model"]].append(row)
    for model_key, rows in output.items():
        for arm in NAMED_ARMS:
            if sum(row["arm_id"] == arm for row in rows) != 80:
                raise SensitivityAnalysisError(
                    f"original {model_key}/{arm} does not contain 80 rows"
                )
    return output


def load_original_summary(path: Path) -> dict[str, Any]:
    if file_sha256(path) != ORIGINAL_SUMMARY_SHA256:
        raise SensitivityAnalysisError("original strict summary hash mismatch")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload


def load_alpha_receipt(
    sensitivity_root: Path, manifest: dict[str, Any], model_key: str,
) -> dict[str, Any]:
    candidates = sorted(
        sensitivity_root.rglob(f"{model_key}-lexical-alpha-receipt.json")
    )
    if len(candidates) != 1:
        raise SensitivityAnalysisError("lexical alpha receipt is missing or ambiguous")
    path = candidates[0]
    if file_sha256(path) != manifest.get("lexical_alpha_receipt_sha256"):
        raise SensitivityAnalysisError("lexical alpha receipt differs from manifest")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != "axb44-lexical-alpha-receipt/v1"
        or payload.get("analysis_id") != ANALYSIS_ID
        or payload.get("model_key") != model_key
        or payload.get("status") != "sealed"
        or payload.get("held_out_response_text_read_during_selection") is not False
    ):
        raise SensitivityAnalysisError("lexical alpha receipt identity mismatch")
    return payload


def arm_rows(
    rows: list[dict[str, Any]], arm: str, track: str = "balanced_union",
) -> list[dict[str, Any]]:
    selected = [row for row in rows if row["arm_id"] == arm]
    if track != "balanced_union":
        selected = [row for row in selected if row["prompt_track"] == track]
    expected = 80 if track == "balanced_union" else 40
    if len(selected) != expected:
        raise SensitivityAnalysisError(
            f"{arm}/{track} has {len(selected)} rows, expected {expected}"
        )
    return selected


def raw_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    perplexities = [
        float(row["perplexity"])
        for row in rows
        if row.get("perplexity") is not None
        and math.isfinite(float(row["perplexity"]))
    ]
    words = [
        float(row.get("probes", {}).get("n_words"))
        for row in rows
        if row.get("probes", {}).get("n_words") is not None
    ]
    truncation = [
        bool(row.get("generation", {}).get("hit_max_new_tokens")) for row in rows
    ]
    return {
        "mean_target_model_perplexity": mean(perplexities) if perplexities else None,
        "finite_perplexity_n": len(perplexities),
        "mean_words": mean(words) if words else None,
        "word_count_n": len(words),
        "hit_max_new_tokens_rate": mean(float(value) for value in truncation),
        "hit_max_new_tokens_n": sum(truncation),
    }


def summarize_arm(
    rows: list[dict[str, Any]], *, seed: int,
) -> dict[str, Any]:
    result = {
        "n": len(rows),
        "strict_hits": sum(int(row["strict_hit"]) for row in rows),
        "strict_rate": mean(float(row["strict_hit"]) for row in rows),
    }
    result.update(concept_bootstrap_interval(rows, seed=seed))
    result.update(raw_diagnostics(rows))
    result["tracks"] = {
        track: {
            "n": len(selected),
            "strict_hits": sum(int(row["strict_hit"]) for row in selected),
            "strict_rate": mean(float(row["strict_hit"]) for row in selected),
        }
        for track in TRACKS[1:]
        if (selected := [row for row in rows if row["prompt_track"] == track])
    }
    result["per_concept"] = {
        str(concept_id): {
            "n": len(selected),
            "strict_hits": sum(int(row["strict_hit"]) for row in selected),
            "strict_rate": mean(float(row["strict_hit"]) for row in selected),
        }
        for concept_id in sorted({int(row["concept_id"]) for row in rows})
        if (selected := [row for row in rows if int(row["concept_id"]) == concept_id])
    }
    return result


def paired_difference(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    *,
    seed: int,
) -> dict[str, Any]:
    left_index = {pair_key(row): row for row in left}
    right_index = {pair_key(row): row for row in right}
    if len(left_index) != 80 or set(left_index) != set(right_index):
        raise SensitivityAnalysisError("paired comparison identities do not match 80/80")
    paired = [
        {
            "concept_id": key[0],
            "prompt_id": key[1],
            "generation_seed": key[2],
            "difference": float(left_index[key]["strict_hit"])
            - float(right_index[key]["strict_hit"]),
        }
        for key in sorted(left_index)
    ]
    return multilevel_difference_interval(paired, seed=seed)


def alpha_disclosure(receipt: dict[str, Any]) -> dict[str, Any]:
    table: list[dict[str, Any]] = []
    for concept_id, methods in sorted(
        receipt["selection_details"].items(), key=lambda row: int(row[0])
    ):
        for method, grid in sorted(methods.items()):
            chosen = float(receipt["chosen_alpha"][concept_id][method])
            matching = [row for row in grid if float(row["alpha"]) == chosen]
            if len(matching) != 1:
                raise SensitivityAnalysisError("chosen alpha is absent from score grid")
            table.append(
                {
                    "concept_id": int(concept_id),
                    "arm_id": method,
                    "selected_alpha": chosen,
                    "calibration_hits": int(matching[0]["hits"]),
                    "calibration_n": int(matching[0]["n"]),
                    "all_zero_grid": all(int(row["hits"]) == 0 for row in grid),
                }
            )
    if len(table) != 30:
        raise SensitivityAnalysisError("alpha disclosure must contain 30 selections")
    return {
        "selection_table": table,
        "changed_from_original_h_tuning": receipt["changed_from_original_h_tuning"],
        "changed_choice_count": int(receipt["changed_choice_count"]),
        "all_zero_calibration_groups": receipt["all_zero_calibration_groups"],
        "all_zero_group_count": int(receipt["all_zero_group_count"]),
        "selection_resolution": "four_binary_calibration_rows_per_method_concept_alpha",
        "tie_rule": "smallest_alpha_including_all_zero_ties",
    }


def random_contrasts(
    rows: list[dict[str, Any]], manifest: dict[str, Any], *, seed: int,
) -> dict[str, Any]:
    by_id = {row["cell_id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise SensitivityAnalysisError("union cell IDs are not unique")
    mappings = manifest.get("treatment_to_random", [])
    if len(mappings) != 240:
        raise SensitivityAnalysisError("manifest must map 240 treatment cells")
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    mapped_treatments: set[str] = set()
    for mapping in mappings:
        treatment = by_id.get(mapping.get("treatment_cell_id"))
        if treatment is None or treatment["arm_id"] not in TREATMENTS:
            raise SensitivityAnalysisError("random mapping lacks a named treatment")
        if treatment["cell_id"] in mapped_treatments:
            raise SensitivityAnalysisError("a treatment cell has two random mappings")
        mapped_treatments.add(treatment["cell_id"])
        controls = [by_id.get(cell_id) for cell_id in mapping.get("random_cell_ids", [])]
        if len(controls) != 3 or any(control is None for control in controls):
            raise SensitivityAnalysisError("random mapping does not contain three controls")
        if {int(control["control_seed"]) for control in controls} != set(CONTROL_SEEDS):
            raise SensitivityAnalysisError("random mapping control seeds drifted")
        for control in controls:
            if pair_key(control) != pair_key(treatment):
                raise SensitivityAnalysisError("random/treatment pair identity drifted")
            if (
                float(control["alpha"]) != float(treatment["alpha"])
                or str(control["mode"]) != str(treatment["mode"])
            ):
                raise SensitivityAnalysisError("random control is not alpha/mode matched")
            grouped[(treatment["arm_id"], int(control["control_seed"]))].append(
                {
                    "concept_id": treatment["concept_id"],
                    "prompt_id": treatment["prompt_id"],
                    "generation_seed": treatment["generation_seed"],
                    "difference": float(treatment["strict_hit"])
                    - float(control["strict_hit"]),
                }
            )
    expected_treatments = {
        row["cell_id"] for row in rows if row["arm_id"] in TREATMENTS
    }
    if mapped_treatments != expected_treatments:
        raise SensitivityAnalysisError("random map does not cover every treatment once")
    output = {}
    for treatment_index, treatment in enumerate(TREATMENTS):
        output[treatment] = {}
        for seed_index, control_seed in enumerate(CONTROL_SEEDS):
            paired = grouped[(treatment, control_seed)]
            if len(paired) != 80:
                raise SensitivityAnalysisError("treatment/random role count is not 80")
            output[treatment][str(control_seed)] = multilevel_difference_interval(
                paired, seed=seed + treatment_index * 10 + seed_index
            )
    return output


def attach_judge(
    model_key: str,
    rows: list[dict[str, Any]],
    judge: dict[str, Any],
) -> dict[str, Any]:
    if (
        judge.get("schema") != "axb44-lexical-alpha-judge/v1"
        or judge.get("analysis_id") != ANALYSIS_ID
        or judge.get("complete") is not True
        or judge.get("judge_model") != "gpt-4o-mini-2024-07-18"
        or judge.get("judge_temperature") != 0
        or int(judge.get("record_count", -1)) != 2120
        or int(judge.get("call_count", -1)) != 6360
    ):
        raise SensitivityAnalysisError("optional judge artifact is not a complete union pass")
    records = judge.get("records", {})
    output = {}
    for arm in NAMED_ARMS:
        selected = arm_rows(rows, arm)
        judged = []
        for row in selected:
            key = f"{model_key}:{row['cell_id']}"
            value = records.get(key)
            if not isinstance(value, dict):
                raise SensitivityAnalysisError("judge union lacks a named-arm record")
            if value.get("cell_id") != row["cell_id"]:
                raise SensitivityAnalysisError("judge record cell identity mismatch")
            judged.append(value)
        output[arm] = {
            "n": len(judged),
            "mean_axbench_harmonic": mean(
                float(value["axbench_harmonic"]) for value in judged
            ),
            "mean_concept": mean(float(value["scores"]["concept"]) for value in judged),
            "mean_instruction": mean(
                float(value["scores"]["instruction"]) for value in judged
            ),
            "mean_fluency": mean(float(value["scores"]["fluency"]) for value in judged),
        }
    return output


def original_judge_anchors(path: Path) -> dict[str, Any]:
    if file_sha256(path) != ORIGINAL_JUDGE_ANALYSIS_SHA256:
        raise SensitivityAnalysisError("original judge analysis hash mismatch")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        model_key: payload["models"][model_key]["summaries"]["balanced_union"]
        for model_key in ("qwen", "gemma")
    }


def analyze_model(
    *,
    model_key: str,
    source_root: Path,
    sensitivity_root: Path,
    lexicons: dict[str, list[str]],
    original_rows: list[dict[str, Any]],
    original_summary: dict[str, Any],
    sensitivity_complete_sha256: str,
    seed: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    union, manifest = load_union(
        model_key=model_key,
        source_root=source_root,
        sensitivity_root=sensitivity_root,
        sensitivity_complete_sha256=sensitivity_complete_sha256,
    )
    rows = score_rows(union, lexicons)
    receipt = load_alpha_receipt(sensitivity_root, manifest, model_key)
    current = {
        arm: summarize_arm(arm_rows(rows, arm), seed=seed + index)
        for index, arm in enumerate(NAMED_ARMS)
    }
    old_by_arm = {
        arm: [row for row in original_rows if row["arm_id"] == arm]
        for arm in NAMED_ARMS
    }
    expected_old = original_summary["models"][model_key]["arm_rates"]
    old_rates = {}
    for arm in NAMED_ARMS:
        expected_name = "prompt" if arm == "axbench_prompt_steering" else arm
        rate = mean(float(row["strict_hit"]) for row in old_by_arm[arm])
        stored = float(expected_old[expected_name]["strict"]["balanced_union"]["rate"])
        if abs(rate - stored) > 1e-12:
            raise SensitivityAnalysisError("original strict rows disagree with summary")
        old_rates[arm] = {"n": 80, "strict_rate": rate}

    named_contrasts = {
        "nla_minus_diffmean": paired_difference(
            arm_rows(rows, "nla"), arm_rows(rows, "diffmean"), seed=seed + 20
        ),
        "nla_minus_caa": paired_difference(
            arm_rows(rows, "nla"), arm_rows(rows, "caa"), seed=seed + 21
        ),
    }
    tuning_deltas = {
        arm: paired_difference(
            arm_rows(rows, arm), old_by_arm[arm], seed=seed + 30 + index
        )
        for index, arm in enumerate(NAMED_ARMS)
    }
    return (
        {
            "model_key": model_key,
            "union_rows": len(rows),
            "lexical_tuned": current,
            "original_h_tuned_strict": old_rates,
            "lexical_tuned_minus_original_h_tuned": tuning_deltas,
            "lexical_tuned_method_contrasts": named_contrasts,
            "matched_random_controls": random_contrasts(
                rows, manifest, seed=seed + 100
            ),
            "alpha_selection": alpha_disclosure(receipt),
            "complete_sha256": {
                "source": file_sha256(source_root / "COMPLETE.json"),
                "lexical_sensitivity": file_sha256(
                    sensitivity_root / "COMPLETE.json"
                ),
            },
        },
        rows,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwen-source", type=Path, required=True)
    parser.add_argument("--qwen-sensitivity", type=Path, required=True)
    parser.add_argument("--qwen-sensitivity-complete-sha256", required=True)
    parser.add_argument("--qwen-sensitivity-archive", type=Path, required=True)
    parser.add_argument("--qwen-sensitivity-archive-sha256", required=True)
    parser.add_argument("--gemma-source", type=Path, required=True)
    parser.add_argument("--gemma-sensitivity", type=Path, required=True)
    parser.add_argument("--gemma-sensitivity-complete-sha256", required=True)
    parser.add_argument("--gemma-sensitivity-archive", type=Path, required=True)
    parser.add_argument("--gemma-sensitivity-archive-sha256", required=True)
    parser.add_argument("--lexicons", type=Path, required=True)
    parser.add_argument("--original-records", type=Path, required=True)
    parser.add_argument("--original-summary", type=Path, required=True)
    parser.add_argument("--original-judge-analysis", type=Path, required=True)
    parser.add_argument("--judge", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-seed", type=int, default=20260904)
    args = parser.parse_args()

    for model_key in ("qwen", "gemma"):
        archive = getattr(args, f"{model_key}_sensitivity_archive").resolve()
        expected_archive_sha256 = getattr(
            args, f"{model_key}_sensitivity_archive_sha256"
        )
        if (
            len(expected_archive_sha256) != 64
            or file_sha256(archive) != expected_archive_sha256
        ):
            raise SensitivityAnalysisError(
                f"{model_key} retrieved sensitivity archive hash mismatch"
            )

    lexicons = read_lexicons(args.lexicons.resolve())
    old_records = load_original_records(args.original_records.resolve())
    old_summary = load_original_summary(args.original_summary.resolve())
    models: dict[str, Any] = {}
    scored_rows: dict[str, list[dict[str, Any]]] = {}
    for offset, model_key in enumerate(("qwen", "gemma")):
        model, rows = analyze_model(
            model_key=model_key,
            source_root=getattr(args, f"{model_key}_source").resolve(),
            sensitivity_root=getattr(args, f"{model_key}_sensitivity").resolve(),
            lexicons=lexicons,
            original_rows=old_records[model_key],
            original_summary=old_summary,
            sensitivity_complete_sha256=getattr(
                args, f"{model_key}_sensitivity_complete_sha256"
            ),
            seed=args.bootstrap_seed + offset * 1000,
        )
        models[model_key] = model
        scored_rows[model_key] = rows

    judge_payload = None
    judge_validation = None
    if args.judge:
        judge_payload = json.loads(args.judge.resolve().read_text(encoding="utf-8"))
        judge_items, judge_receipts = load_judge_items(args)
        judge_validation = validate_complete_checkpoint(
            judge_payload,
            judge_items,
            judge_receipts,
            checkpoint_path=args.judge.resolve(),
        )
        for model_key in ("qwen", "gemma"):
            models[model_key]["lexical_tuned_judge_descriptive"] = attach_judge(
                model_key, scored_rows[model_key], judge_payload
            )
    payload = {
        "schema": SCHEMA,
        "analysis_id": ANALYSIS_ID,
        "evidential_status": "post_hoc_objective_matching_sensitivity",
        "question": (
            "Does the held-out strict lexical ordering on the same ten frozen "
            "concepts change when alpha is selected by calibration strict hits "
            "instead of calibration AxBench harmonic score?"
        ),
        "primary_original_result_unchanged": True,
        "new_hypothesis_tests": False,
        "models": models,
        "original_h_tuned_judge_anchors": original_judge_anchors(
            args.original_judge_analysis.resolve()
        ),
        "judge_provenance": judge_validation,
        "provenance": {
            "lexicons_sha256": file_sha256(args.lexicons.resolve()),
            "original_strict_records_sha256": file_sha256(
                args.original_records.resolve()
            ),
            "original_strict_summary_sha256": file_sha256(
                args.original_summary.resolve()
            ),
            "original_judge_analysis_sha256": file_sha256(
                args.original_judge_analysis.resolve()
            ),
            "judge_union_sha256": (
                file_sha256(args.judge.resolve()) if args.judge else None
            ),
            "sensitivity_archives": {
                model_key: {
                    "sha256": file_sha256(
                        getattr(args, f"{model_key}_sensitivity_archive").resolve()
                    ),
                    "complete_sha256": file_sha256(
                        getattr(args, f"{model_key}_sensitivity").resolve()
                        / "COMPLETE.json"
                    ),
                }
                for model_key in ("qwen", "gemma")
            },
        },
        "interpretation_boundary": [
            "The original harmonic-tuned administration remains primary.",
            "This post-hoc sensitivity changes only the calibration objective on the frozen panel.",
            "It does not isolate concept selection because panel content and prompt banks still differ.",
            "Intervals are descriptive and no new p-values or Holm claims are produced.",
            "Prompting is an explicit-instruction reference rather than an activation method.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
