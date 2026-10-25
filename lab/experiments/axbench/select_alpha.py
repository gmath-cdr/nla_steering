"""Create a pending alpha receipt from calibration results only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis import load_result, make_alpha_receipt
from gen_specs import validate_atom_report
from harness import load_study


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--atom-report",
        type=Path,
        default=None,
        help=(
            "pod-built atom report; required when a declared direct atom is "
            "tokenization-ineligible and therefore has no calibration result"
        ),
    )
    args = parser.parse_args()
    results = [load_result(path) for path in args.results]
    bundle = load_study()
    direct_exclusion = None
    if args.atom_report is not None:
        report = json.loads(args.atom_report.read_text(encoding="utf-8"))
        model_key = str(results[0]["runtime"]["model_key"])
        concept_key = str(results[0]["spec"]["study"]["concept_key"])
        atom_receipt = validate_atom_report(
            bundle, model_key, concept_key, report
        )
        if atom_receipt.get("status") == "ineligible_tokenization":
            direct_exclusion = atom_receipt
    receipt = make_alpha_receipt(
        results, bundle, direct_atom_exclusion=direct_exclusion
    )
    args.output.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote pending raw-audit receipt: {args.output}")
    print("held-out generation remains locked until a reviewer freezes it")


if __name__ == "__main__":
    main()
