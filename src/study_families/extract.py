#!/usr/bin/env python3
"""Safely extract authenticated complete-run archives for deeper inspection."""

from __future__ import annotations

import argparse
from pathlib import Path

from study_families.common import extract_verified_archive
from study_families.verify import ARCHIVES


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("family", choices=("axb44", "emoji", "all"))
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("artifacts/rebuilt/extracted_studies"),
        help="destination for verified, disposable archive expansions",
    )
    args = parser.parse_args()

    selected = []
    for specification in ARCHIVES:
        family = "axb44" if specification.relative_path.startswith("outcome_blind") else "emoji"
        if args.family in (family, "all"):
            selected.append((family, specification))

    for family, specification in selected:
        destination = args.out_dir / family / specification.model_key
        extracted = extract_verified_archive(specification, destination)
        print(f"verified and extracted {specification.model_key}: {extracted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
