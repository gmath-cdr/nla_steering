"""Apply the mandatory human raw-output audit gate to an alpha receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis import freeze_alpha_receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--audit-note", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pending = json.loads(args.receipt.read_text(encoding="utf-8"))
    frozen = freeze_alpha_receipt(
        pending, reviewer=args.reviewer, audit_note=args.audit_note
    )
    args.output.write_text(
        json.dumps(frozen, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote frozen alpha receipt: {args.output}")


if __name__ == "__main__":
    main()
