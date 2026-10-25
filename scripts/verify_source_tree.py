#!/usr/bin/env python3
"""Verify vendored source files and compact frozen release inputs.

This is the cross-platform counterpart to ``verify_source_tree.sh``. It uses
only the Python standard library so ``python run.py self-test`` behaves the
same way on Windows and on the Linux machines used for the original runs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# These files are small sentinels for each vendored implementation used by the
# paper. A full byte inventory of the release data is checked separately.
REQUIRED_SOURCE_FILES = (
    "src/nla/experiments/runner.py",
    "src/nla/experiments/rescore_prompting_quality.py",
    "src/nla/experiments/rubric_two_judge.py",
    "src/nla/experiments/factuality_three_arm.py",
    "src/nla/LICENSE",
    "src/nla_inference/nla_inference.py",
    "src/nla_inference/LICENSE",
    "src/axbench/axbench/models/steering_vector.py",
    "src/axbench/LICENSE",
    "src/jacobian_lens/jlens/lens.py",
    "src/jacobian_lens/LICENSE",
    "src/contrast_steering/src/contrast_steering/pipeline.py",
)

REQUIRED_RELEASE_FILES = (
    "artifacts/release/prompting_quality/panel14_qwen_matched_panel.json",
    "artifacts/release/prompting_quality/panel14_qwen_prompting.json",
    "artifacts/release/prompting_quality/panel14_gemma_matched_panel.json",
    "artifacts/release/prompting_quality/panel14_gemma_prompting.json",
    "artifacts/release/prompting_quality/panel10_gemma_matched_panel.json",
    "artifacts/release/prompting_quality/panel10_gemma_prompting.json",
    "artifacts/release/reliability_v1/human_scores_only_56.json",
    "artifacts/release/reliability_v1/two_judge_scores.json",
    "artifacts/release/reliability_v1/factuality_three_arm.json",
)


def sha256(path: Path) -> str:
    """Return the release digest using its canonical LF text representation."""

    digest = hashlib.sha256()
    if path.suffix.lower() in {".json", ".jsonl", ".md", ".txt"}:
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    else:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def require_files(relative_paths: tuple[str, ...], *, nonempty: bool) -> None:
    """Fail with a useful path when a required source or release file is absent."""

    for relative in relative_paths:
        path = ROOT / relative
        if not path.is_file():
            raise SystemExit(f"missing required file: {relative}")
        if nonempty and path.stat().st_size == 0:
            raise SystemExit(f"required release file is empty: {relative}")


def verify_release_checksums() -> int:
    """Validate every compact-release file listed in its frozen checksum file."""

    checksum_path = ROOT / "artifacts/release/sha256sums.txt"
    checked = 0
    for line_number, line in enumerate(
        checksum_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            expected, relative = line.split("  ", 1)
        except ValueError as exc:
            raise SystemExit(f"invalid checksum line {line_number}: {line!r}") from exc
        path = ROOT / relative
        if not path.is_file():
            raise SystemExit(f"checksummed release file is missing: {relative}")
        actual = sha256(path)
        if actual != expected:
            raise SystemExit(
                f"release checksum mismatch: {relative}; expected {expected}, got {actual}"
            )
        checked += 1
    return checked


def reject_source_symlinks() -> None:
    """Require ordinary source copies so the checkout cannot hide external state."""

    source_root = ROOT / "src"
    for path in source_root.rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"source tree contains a symlink: {path.relative_to(ROOT)}")


def main() -> int:
    require_files(REQUIRED_SOURCE_FILES, nonempty=False)
    require_files(REQUIRED_RELEASE_FILES, nonempty=True)
    checked = verify_release_checksums()
    reject_source_symlinks()
    print(f"source tree and frozen release inputs are complete: {checked} checksums")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
