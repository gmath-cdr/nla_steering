"""Validate the excluded two-record infrastructure canary."""

from __future__ import annotations

import argparse
from pathlib import Path

from experiments.emoji_method_comparison.analysis import load_results, validate_result_grid
from experiments.emoji_method_comparison.harness import HarnessError, load_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma3-12b"), required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = load_results(args.results_dir)
    if len(rows) != 1:
        raise HarnessError(f"canary directory has {len(rows)} run results; expected one")
    result = rows[0]
    validate_result_grid(result, phase="canary")
    study = result["spec"]["study"]
    if study.get("excluded_from_research_corpus") is not True:
        raise HarnessError("canary was not excluded from the research corpus")
    if result["runtime"]["model_key"] != args.model_key:
        raise HarnessError("canary model drifted")
    if len(result["records"]) != 2:
        raise HarnessError("canary must contain exactly two records")
    for record in result["records"]:
        if "target_metrics" not in record or "generation" not in record:
            raise HarnessError("canary omitted target/generation metadata")
    bundle = load_bundle()
    if study.get("lock_bundle_sha256") != bundle["lock_bundle_sha256"]:
        raise HarnessError("canary uses stale locks")
    print(f"valid excluded {args.model_key} canary: 2 records")


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"canary validation failed: {exc}") from exc
