"""Select and seal one calibration alpha per model/concept/treatment."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import tempfile
from typing import Any

from experiments.axbench_lexicality20.protocol import canonical_sha256, file_sha256, load_bundle


class SelectionError(ValueError):
    pass


def choose(rows: list[dict[str, Any]], expected_grid: list[float]) -> tuple[float, list[dict[str, Any]]]:
    by_alpha: dict[float, list[float]] = defaultdict(list)
    for row in rows:
        by_alpha[float(row["alpha"])].append(float(row["axbench_harmonic"]))
    if set(by_alpha) != set(expected_grid):
        raise SelectionError(f"calibration alpha grid mismatch: {sorted(by_alpha)}")
    details = []
    for alpha in expected_grid:
        values = by_alpha[alpha]
        if len(values) != 4:
            raise SelectionError(f"alpha {alpha} has {len(values)} scores, expected 4")
        details.append({
            "alpha": alpha, "n": len(values), "mean_axbench_harmonic": sum(values) / len(values),
            "scores": values,
        })
    selected = max(details, key=lambda row: (row["mean_axbench_harmonic"], -row["alpha"]))
    return float(selected["alpha"]), details


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
        suffix=".tmp", delete=False,
    ) as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--judge-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = load_bundle()
    artifact = json.loads(args.judge_artifact.read_text(encoding="utf-8"))
    if (
        artifact.get("schema") != "axb44-axbench-judge/v1"
        or artifact.get("phase") != "calibration"
        or artifact.get("complete") is not True
        or artifact.get("review_payload_sha256") != bundle["review_payload_sha256"]
    ):
        raise SelectionError("calibration judge artifact identity/completeness mismatch")
    records = list(artifact["records"].values())
    if len(records) != 920:
        raise SelectionError(f"calibration judge record count {len(records)} != 920")
    runtime_key = "qwen" if args.model_key == "qwen" else "gemma3-12b"
    if {row["model_key"] for row in records} != {runtime_key}:
        raise SelectionError("calibration model identity mismatch")
    grid = [float(value) for value in bundle["campaign"]["models"][args.model_key]["alpha_grid"]]
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if row["arm_id"] in {"diffmean", "caa", "nla"}:
            grouped[(int(row["concept_id"]), str(row["arm_id"]))].append(row)
    expected_keys = {
        (int(concept["concept_id"]), arm)
        for concept in bundle["campaign"]["concepts"]
        for arm in ("diffmean", "caa", "nla")
    }
    if set(grouped) != expected_keys:
        raise SelectionError("treatment calibration matrix is incomplete")
    chosen: dict[str, dict[str, float]] = defaultdict(dict)
    details: dict[str, dict[str, Any]] = defaultdict(dict)
    for (concept_id, arm), rows in sorted(grouped.items()):
        alpha, arm_details = choose(rows, grid)
        chosen[str(concept_id)][arm] = alpha
        details[str(concept_id)][arm] = arm_details
    receipt = {
        "schema": "axb44-alpha-receipt/v1", "status": "sealed",
        "campaign_id": bundle["campaign"]["campaign_id"],
        "review_payload_sha256": bundle["review_payload_sha256"],
        "model_key": args.model_key,
        "selection_rule": "max_mean_exact_axbench_harmonic_over_4_calibration_assignments_tie_smaller_alpha/v1",
        "judge_artifact_sha256": file_sha256(args.judge_artifact),
        "judge_model": artifact["judge_model"], "judge_temperature": artifact["judge_temperature"],
        "chosen_alpha": dict(chosen), "selection_details": dict(details),
        "calibration_result_file_sha256": artifact["result_file_sha256"],
    }
    receipt["integrity_sha256"] = canonical_sha256(receipt)
    atomic_json(args.output, receipt)
    print(f"sealed 30 alpha choices -> {args.output}")


if __name__ == "__main__":
    main()
