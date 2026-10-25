from __future__ import annotations

import json
from pathlib import Path


def read_jsonl(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: each row must be an object")
            rows.append(row)
    if not rows:
        raise ValueError(f"{path}: no examples found")
    return rows


def validate_pairs(rows: list[dict]) -> None:
    for i, row in enumerate(rows, 1):
        missing = {"positive", "neutral"} - row.keys()
        if missing:
            raise ValueError(f"pair {i} missing fields: {sorted(missing)}")
        if not all(isinstance(row[k], str) and row[k].strip() for k in ("positive", "neutral")):
            raise ValueError(f"pair {i}: positive and neutral must be non-empty strings")


def validate_eval(rows: list[dict]) -> None:
    for i, row in enumerate(rows, 1):
        if not isinstance(row.get("prompt"), str) or not row["prompt"].strip():
            raise ValueError(f"eval row {i}: prompt must be a non-empty string")
        terms = row.get("target_terms")
        if not isinstance(terms, list) or not terms or not all(isinstance(x, str) and x for x in terms):
            raise ValueError(f"eval row {i}: target_terms must be a non-empty string list")


def write_jsonl(path: str | Path, rows: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

