#!/usr/bin/env python3
"""Regenerate the authenticated paper-data inventory deterministically."""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "artifacts" / "paper_runs" / "data"
OUTPUT = ROOT / "artifacts" / "paper_runs" / "paper-sha256sums.txt"
TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".py", ".sha256"}
TEXT_FILENAMES = {"SHA256SUMS"}


def digest(path: Path) -> str:
    """Hash text in canonical LF form and leave binary bytes untouched."""

    payload = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
        payload = payload.replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    files = sorted(path for path in DATA.rglob("*") if path.is_file())
    lines = [f"{digest(path)}  {path.relative_to(ROOT).as_posix()}" for path in files]
    temporary = OUTPUT.with_suffix(OUTPUT.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(OUTPUT)
    print(f"wrote {len(files)} paper artifact checksums to {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
