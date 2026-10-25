"""Evaluate one complete model-stratified held-out emoji result family."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from experiments.emoji_method_comparison.analysis import (
    evaluate_heldout,
    load_results,
    write_blinded_rating_sheet,
)
from experiments.emoji_method_comparison.harness import HarnessError, load_bundle


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma3-12b"), required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--alpha-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rating-sheet", type=Path, required=True)
    parser.add_argument("--rating-key", type=Path, required=True)
    args = parser.parse_args()
    results = load_results(args.results_dir)
    bundle = load_bundle()
    summary = evaluate_heldout(results, bundle, model_key=args.model_key)
    receipt = json.loads(args.alpha_receipt.read_text(encoding="utf-8"))
    rating = write_blinded_rating_sheet(
        results, receipt, model_key=args.model_key,
        sheet_path=args.rating_sheet, key_path=args.rating_key,
    )
    summary["instruction_quality"]["rating_sheet"] = {
        "path": str(args.rating_sheet), **rating,
    }
    _atomic_json(args.output, summary)
    print(
        f"wrote {args.output}: {summary['raw_result_files']} files, "
        f"{summary['raw_records']} records; blinded ratings={rating['rows']}"
    )


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"held-out evaluation failed: {exc}") from exc
