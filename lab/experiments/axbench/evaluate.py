"""Evaluate the locked AxBench-derived held-out result family.

Example, from ``lab/`` after copying one complete held-out phase into a clean
external directory::

    python experiments/axbench/evaluate.py \
      --phase evaluation --results-dir /workspace/axb-results/evaluation \
      --output /workspace/axb-results/evaluation_summary.json

The evaluator refuses incomplete Cartesian grids, mismatched alpha-zero text,
missing random controls, and missing preregistered treatment claims. A
tokenization-ineligible direct-J claim is excluded only when the same
pre-behavior receipt is sealed into the frozen alpha receipt carried by the
held-out specs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from analysis import evaluate_heldout_family, load_result
from harness import HarnessError, load_study


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("evaluation", "transfer"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    paths = sorted(args.results_dir.glob("*.json"))
    if not paths:
        raise HarnessError(f"no JSON result files found under {args.results_dir}")
    bundle = load_study()
    summary = evaluate_heldout_family(
        [load_result(path) for path in paths],
        bundle,
        expected_phase=args.phase,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(summary, indent=2, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=args.output.parent,
        prefix=args.output.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(args.output)
    print(f"wrote {args.output} with {summary['realized_family_size']} claims")


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"evaluation failed: {exc}") from exc
