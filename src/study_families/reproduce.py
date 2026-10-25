#!/usr/bin/env python3
"""Rebuild the four added paper-study summaries and optional figures."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from study_families.figures import build_all
from study_families.verify import verify_all


def _write_headline_csv(summary: dict, path: Path) -> None:
    """Write a small human-readable index of headline quantities."""

    rows: list[tuple[str, str, str, float]] = []
    for model, values in summary["outcome_blind_axb44"]["models"].items():
        for method, value in values["harmonic"].items():
            rows.append(("outcome_blind_axb44", model, f"judge_h.{method}", value))
        for method, value in values["strict_hit"].items():
            rows.append(("outcome_blind_axb44", model, f"strict_hit.{method}", value))
    for model, values in summary["freeform_emoji"].items():
        for method, outcome in values["ordinary_effects"].items():
            rows.append(("freeform_emoji", model, f"presence_effect.{method}", outcome["effect"]))
        rows.append(("freeform_emoji", model, "prompt_presence", values["explicit_prompt_presence"]))
    wordle = summary["freeform_wordle"]
    rows.extend(
        (
            ("freeform_wordle", "qwen", "natural_home_at_0.4", wordle["qwen_natural_dwelling"]["home_at_0.4"]),
            ("freeform_wordle", "qwen", "natural_home_at_0.6", wordle["qwen_natural_dwelling"]["home_at_0.6"]),
            ("freeform_wordle", "gemma", "natural_house_at_0.2", wordle["gemma_natural_dwelling"]["house_at_0.2"]),
            ("freeform_wordle", "gemma", "natural_home_at_0.2", wordle["gemma_natural_dwelling"]["home_at_0.2"]),
        )
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("study", "model", "metric", "value"))
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("artifacts/rebuilt/extended_studies"),
        help="disposable output directory; archived inputs are never modified",
    )
    parser.add_argument("--figures", action="store_true", help="also build four plots")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    summary = verify_all()
    (args.out_dir / "verification.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _write_headline_csv(summary, args.out_dir / "headline_metrics.csv")
    if args.figures:
        build_all(args.out_dir / "figures")
    print(f"rebuilt study-family products under {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
