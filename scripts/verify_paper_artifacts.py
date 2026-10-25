#!/usr/bin/env python3
"""Validate paper-result coverage, file references, and snapshot checksums."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAPER_ROOT = ROOT / "artifacts/paper_runs"
DATA = PAPER_ROOT / "data"
TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".py", ".sha256"}
TEXT_FILENAMES = {"SHA256SUMS"}


def digest(path: Path) -> str:
    """Hash text in canonical LF form and leave binary artifacts byte-exact."""

    hasher = hashlib.sha256()
    if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
        # Existing Windows checkouts may predate .gitattributes. Canonicalizing
        # newlines here gives the same digest as a fresh LF-enforced checkout.
        hasher.update(path.read_bytes().replace(b"\r\n", b"\n"))
    else:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


manifest = json.loads((PAPER_ROOT / "paper-results-manifest.json").read_text())
results = manifest["results"]
ids = [entry["id"] for entry in results]
if len(ids) != len(set(ids)):
    raise SystemExit("duplicate result id in paper-results-manifest.json")

# The first 17 groups came from the pinned upstream snapshot. Later studies
# carry their own provenance key so they are not falsely attributed to that
# upstream commit merely because they share the same data directory.
additional_sources = set(manifest.get("additional_artifact_sources", {}))

for entry in results:
    sources = entry["sources"]
    if entry["status"] == "bundled" and not sources:
        raise SystemExit(f"bundled entry has no sources: {entry['id']}")
    if entry["status"] != "bundled" and sources:
        raise SystemExit(f"unavailable entry unexpectedly has sources: {entry['id']}")
    provenance_source = entry.get("provenance_source")
    if provenance_source is not None and provenance_source not in additional_sources:
        raise SystemExit(
            f"unknown provenance source for {entry['id']}: {provenance_source}"
        )
    for source in sources:
        if not (DATA / source).is_file():
            raise SystemExit(f"manifest source is missing: {entry['id']}: {source}")

expected: dict[str, str] = {}
for line in (PAPER_ROOT / "paper-sha256sums.txt").read_text().splitlines():
    checksum, relative = line.split("  ", 1)
    expected[relative] = checksum

actual_files = sorted(path for path in DATA.rglob("*") if path.is_file())
actual_names = {path.relative_to(ROOT).as_posix() for path in actual_files}
if actual_names != set(expected):
    missing = sorted(set(expected) - actual_names)
    extra = sorted(actual_names - set(expected))
    raise SystemExit(f"checksum inventory mismatch; missing={missing[:3]} extra={extra[:3]}")
for path in actual_files:
    relative = path.relative_to(ROOT).as_posix()
    if digest(path) != expected[relative]:
        raise SystemExit(f"checksum mismatch: {relative}")

bundled = sum(entry["status"] == "bundled" for entry in results)
gaps = len(results) - bundled
gap_label = "gap" if gaps == 1 else "gaps"
print(
    f"paper artifact manifest valid: {bundled} bundled result groups, "
    f"{gaps} documented {gap_label}, {len(actual_files)} files"
)
