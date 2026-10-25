"""Validate or materialize frozen emoji-method comparison run specs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.emoji_method_comparison.harness import (
    HarnessError,
    build_calibration_specs,
    build_canary_spec,
    build_heldout_specs,
    load_build,
    load_bundle,
    write_specs,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "canary", "calibration", "heldout"))
    parser.add_argument("--model-key", choices=("qwen", "gemma3-12b"))
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--alpha-receipt", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    bundle = load_bundle()
    print(
        f"valid {bundle['lock']['study_id']} "
        f"lock_bundle={bundle['lock_bundle_sha256']}"
    )
    if args.command == "validate":
        return
    if not args.model_key or not args.build_dir or not args.output_dir:
        raise HarnessError("materialization requires --model-key, --build-dir, and --output-dir")
    build = load_build(args.build_dir, args.model_key)
    if args.command == "canary":
        specs = [build_canary_spec(bundle, build, model_key=args.model_key)]
    elif args.command == "calibration":
        specs = build_calibration_specs(bundle, build, model_key=args.model_key)
    else:
        if args.alpha_receipt is None:
            raise HarnessError("heldout materialization requires --alpha-receipt")
        receipt = json.loads(args.alpha_receipt.read_text(encoding="utf-8"))
        specs = build_heldout_specs(
            bundle, build, model_key=args.model_key, alpha_receipt=receipt
        )
    manifest = write_specs(specs, args.output_dir)
    print(f"wrote {len(specs)} sealed specs and {manifest}")


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"spec materialization failed: {exc}") from exc
