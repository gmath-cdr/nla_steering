#!/usr/bin/env python3
"""Copy the allow-listed final paper artifacts from an NLA checkout.

The resulting snapshot is independent of the source checkout. Machine logs,
failed/partial outputs, superseded pre-repair files, and model checkpoints are
intentionally excluded. Saved experiment vectors and completion transcripts
are retained.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELECTION = ROOT / "artifacts/paper_runs/selection.txt"
DESTINATION = ROOT / "artifacts/paper_runs/data"

EXCLUDED_PARTS = {"checkpoints", "logs", "queue_logs"}
EXCLUDED_NAME_PARTS = (
    ".failed",
    "_failed",
    "partial",
    "pre_norm_match",
    "pre_quality_fix",
    "pre_axbench",
    "simplified_metrics",
    "single_sample",
    "unbatched",
)
EXCLUDED_SUFFIXES = {".log", ".pid", ".patch"}


def selected_paths() -> list[Path]:
    return [
        Path(line.strip())
        for line in SELECTION.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def excluded(relative: Path) -> bool:
    lowered = relative.as_posix().lower()
    return (
        bool(EXCLUDED_PARTS.intersection(relative.parts))
        or any(token in lowered for token in EXCLUDED_NAME_PARTS)
        or relative.suffix.lower() in EXCLUDED_SUFFIXES
    )


def copy_one(source_root: Path, relative: Path) -> tuple[int, int]:
    source = source_root / relative
    if not source.exists():
        raise FileNotFoundError(f"selected artifact is missing: {source}")
    files = [source] if source.is_file() else sorted(path for path in source.rglob("*") if path.is_file())
    copied_files = copied_bytes = 0
    for path in files:
        inside = relative if source.is_file() else relative / path.relative_to(source)
        if excluded(inside):
            continue
        destination = DESTINATION / inside
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied_files += 1
        copied_bytes += path.stat().st_size
    return copied_files, copied_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="path to natural_language_autoencoders or its artifacts directory",
    )
    args = parser.parse_args()
    source_root = args.source.resolve()
    if source_root.name != "artifacts":
        source_root /= "artifacts"
    if not source_root.is_dir():
        parser.error(f"artifact directory does not exist: {source_root}")

    total_files = total_bytes = 0
    for relative in selected_paths():
        copied_files, copied_bytes = copy_one(source_root, relative)
        total_files += copied_files
        total_bytes += copied_bytes
        print(f"copied {relative}: {copied_files} files")
    print(f"paper snapshot: {total_files} files, {total_bytes / 1024 / 1024:.1f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
