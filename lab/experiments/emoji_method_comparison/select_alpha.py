"""Create the sealed per-model alpha receipt from all primary calibration results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from experiments.emoji_method_comparison.analysis import (
    load_results,
    make_alpha_receipt,
)
from experiments.emoji_method_comparison.harness import HarnessError, load_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma3-12b"), required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = make_alpha_receipt(
        load_results(args.results_dir), load_bundle(), model_key=args.model_key
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=args.output.parent,
        prefix=args.output.name + ".", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
        temporary = Path(handle.name)
    temporary.replace(args.output)
    print(f"wrote {args.output}")
    for arm, alpha in receipt["chosen_alpha_by_arm"].items():
        print(f"{arm}: alpha={alpha}")
    for arm, exclusion in receipt["treatment_exclusions"].items():
        print(f"{arm}: EXCLUDED ({exclusion['receipt']['reason']})")


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"alpha selection failed: {exc}") from exc
