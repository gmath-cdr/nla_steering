#!/usr/bin/env python3
"""Write deterministic SHA-256 checksums for the frozen paper snapshot."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "artifacts/paper_runs/data"
OUTPUT = ROOT / "artifacts/paper_runs/paper-sha256sums.txt"


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


files = sorted(path for path in DATA.rglob("*") if path.is_file())
lines = [f"{digest(path)}  {path.relative_to(ROOT).as_posix()}" for path in files]
OUTPUT.write_text("\n".join(lines) + "\n")
print(f"wrote {OUTPUT.relative_to(ROOT)} for {len(files)} files")
