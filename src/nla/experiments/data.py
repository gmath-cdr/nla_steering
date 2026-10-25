"""AxBench JSONL and parquet loaders without a pandas dependency."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def concepts(metadata: str) -> dict[int, str]:
    output = {}
    for line in Path(metadata).read_text().splitlines():
        row = json.loads(line)
        if row.get("concept_id") is not None:
            output[int(row["concept_id"])] = str(row.get("concept", "")).strip()
    return output


def mapped_prompts(mapping: str, concept_id: int, limit: int) -> list[str]:
    prompts = []
    for line in Path(mapping).read_text().splitlines():
        row = json.loads(line)
        if row.get("tgt_concept_id") == concept_id and row.get("instruction"):
            prompts.append(row["instruction"].strip())
    return prompts[:limit]


def train_rows(parquet: str, concept_id: int) -> list[dict]:
    """Load the positive/negative AxBench rows for one concept via pyarrow."""
    import pyarrow.parquet as parquet_io

    rows = parquet_io.read_table(parquet).to_pylist()
    return [row for row in rows if int(row["concept_id"]) == concept_id]
