"""Run several same-model specs in one process while reusing loaded models.

This is the paid-wave entry point. Inline NLA directions are ordered first so
the base/AV/AR session is loaded once; file-vector, random, and baseline specs
then reuse that compatible session. Every individual spec still performs its
own integrity, checkpoint, package, cardinality, and output-directory checks.

Example from ``lab/``::

    export NLA_MODEL=qwen
    python experiments/run_specs.py experiments/axbench/specs/evaluation/qwen/meanwhile \
      --output-dir /workspace/axb-results/evaluation
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


def _expand(paths: list[Path]) -> list[Path]:
    output: list[Path] = []
    for path in paths:
        if path.is_dir():
            output.extend(sorted(
                candidate for candidate in path.glob("*.yaml")
                if not candidate.name.startswith("_materialization-")
            ))
        elif path.is_file():
            output.append(path)
        else:
            raise ValueError(f"spec path does not exist: {path}")
    if not output:
        raise ValueError("no YAML specs were supplied")
    resolved = [path.resolve() for path in output]
    if len(set(resolved)) != len(resolved):
        raise ValueError("the same spec was supplied more than once")
    return resolved


def _load_rank(path: Path) -> tuple[int, str]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    direction = spec.get("direction")
    if isinstance(direction, dict):
        rank = 0  # requires base + AR (+ AV unless --no-echo)
    elif spec.get("direction_file"):
        rank = 1
    elif direction == "random":
        rank = 2
    else:
        rank = 3
    return rank, path.name


def _completed_result(spec_path: Path, output_dir: Path) -> Path | None:
    """Return a matching complete result, failing closed on stale collisions."""

    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    name = str(spec["name"])
    expected_integrity = spec.get("spec_integrity", {}).get("sha256")
    expected_records = int(spec.get("expected_records", -1))
    matches = sorted(output_dir.glob(f"{name}_*.json")) if output_dir.exists() else []
    complete: list[Path] = []
    for path in matches:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot validate existing result {path}: {exc}") from exc
        observed_integrity = payload.get("spec", {}).get("spec_integrity", {}).get("sha256")
        if observed_integrity != expected_integrity:
            raise ValueError(
                f"existing result {path} has the same experiment name but a "
                "different spec integrity hash"
            )
        if len(payload.get("records", [])) != expected_records:
            raise ValueError(f"existing result {path} is incomplete")
        complete.append(path)
    if len(complete) > 1:
        raise ValueError(f"multiple complete outputs exist for {name}: {complete}")
    return complete[0] if complete else None


def main() -> None:
    # Keep CPU-only spec expansion/test imports free of the GPU environment.
    import run

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("specs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--models-root", type=Path, default=None)
    parser.add_argument("--no-echo", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip a spec only when a complete output with the exact integrity "
             "hash and record count already exists",
    )
    args = parser.parse_args()

    specs = sorted(_expand(args.specs), key=_load_rank)
    print(f"batch contains {len(specs)} specs; model session reuse is enabled")
    for index, spec in enumerate(specs, start=1):
        completed = _completed_result(spec, args.output_dir)
        if completed is not None:
            if not args.resume:
                raise ValueError(
                    f"complete output already exists for {spec.name}: {completed}; "
                    "use --resume to skip it"
                )
            print(f"\n===== [{index}/{len(specs)}] {spec.name}: already complete =====")
            print(f"skipping exact result {completed}")
            continue
        print(f"\n===== [{index}/{len(specs)}] {spec.name} =====")
        argv = [
            str(spec),
            "--output-dir", str(args.output_dir),
            "--require-explicit-method",
            "--reuse-session",
        ]
        if args.models_root is not None:
            argv.extend(["--models-root", str(args.models_root)])
        if args.no_echo:
            argv.append("--no-echo")
        run.main(argv)
    print(f"batch complete: {len(specs)} specs")


if __name__ == "__main__":
    main()
