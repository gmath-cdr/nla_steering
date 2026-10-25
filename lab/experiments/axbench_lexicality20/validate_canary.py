"""Require the two-record infrastructure canary before calibration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nla_lab.study import validate_spec_integrity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, required=True)
    args = parser.parse_args()
    files = sorted(args.results_dir.glob("*.json"))
    if len(files) != 2:
        raise SystemExit(f"canary requires exactly two result files, found {len(files)}")
    rows = []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate_spec_integrity(payload["spec"], required=True)
        if payload["spec"]["study"].get("phase") != "canary":
            raise SystemExit(f"non-canary result in canary directory: {path}")
        if len(payload.get("records", [])) != 1:
            raise SystemExit(f"canary result cardinality mismatch: {path}")
        rows.extend(payload["records"])
    if any(not str(row.get("text", "")).strip() for row in rows):
        raise SystemExit("canary generated an empty response")
    if len({int(row["sample"]) for row in rows}) != 1:
        raise SystemExit("canary arms did not reuse the same generation seed")
    print("CANARY PASS: 2/2 nonempty, paired seed, sealed specs")


if __name__ == "__main__":
    main()
