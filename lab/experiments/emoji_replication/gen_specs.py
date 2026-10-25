"""Validate the emoji study or materialize its excluded plumbing canary."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

import yaml

from harness import HarnessError, build_canary_spec, load_study


def _atomic_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "canary"))
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    bundle = load_study()
    report = bundle["prompt_report"]
    print(f"study: {bundle['study']['study_id']} ({bundle['study']['status']})")
    print(f"prompts: {report['count']} ({report['status']})")
    print(f"prompt hash: {report['sha256']}")
    print(f"vectors verified: {', '.join(sorted(bundle['sources']))}")
    if args.command == "validate":
        return
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="nla-emoji-canary-"))
    spec = build_canary_spec(bundle)
    path = output / f"{spec['name']}.yaml"
    _atomic_yaml(path, spec)
    print(f"wrote excluded two-record canary: {path}")


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"preflight failed: {exc}") from exc
