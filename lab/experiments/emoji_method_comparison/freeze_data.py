"""Freeze and validate calibration prompts and CAA construction rows.

The full Concept500 parquet is an input to the one-time ``freeze-caa`` command
only. The checked-in construction manifest contains the exact selected source
rows and text hashes, so RunPod builds never fetch or resample upstream data.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any

import yaml


HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
CALIBRATION_PATH = HERE / "prompts.calibration.yaml"
CAA_PATH = HERE / "caa.construction.yaml"
CALIBRATION_SCHEMA = "emoji-method-comparison-calibration-bank/v1"
CAA_SCHEMA = "emoji-method-comparison-caa-construction/v1"
STUDY_ID = "crossmodel-qwen-l20-gemma-l32-caa-nla-emoji-v5"
TRAIN_FILE_SHA256 = "29ae5b03d8f60bb7ad474d714da7d033de2af66339f1d053c8e7084ec69332a6"
TRAIN_REVISION = "ad8a5d60c4616b599c24dd6689f05f696ec610f3"
CALIBRATION_PAYLOAD_SHA256 = "9215021227360177a1d6aff82ffa84a1338473b0859c51ee1816e7f6124db48b"
TARGET_PATTERN = re.compile(
    r"emoji|emoticon|smiley|pictograph|unicode|glyph|[\U0001F000-\U0001FAFF]",
    re.IGNORECASE,
)
EXCLUSION_PATTERN = re.compile(
    r"\b(?:latest|current news|breaking news|today's news|celebrity|actor|"
    r"actress|singer|politician|president|prime minister)\b|"
    r"\b(?:write|generate|debug|fix|implement) (?:a |an )?(?:program|code|"
    r"function|script)\b|\b(?:calculate|compute|solve|prove|equation)\b|"
    r"\b(?:json|csv|xml|table format|bullet points?)\b|"
    r"\b(?:weapon|bomb|malware|phishing|self-harm)\b",
    re.IGNORECASE,
)


class DataFreezeError(ValueError):
    """Raised when a frozen input bank is incomplete or drifts."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DataFreezeError(message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(rows), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalized_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _ordinary_input(text: Any) -> bool:
    if not isinstance(text, str) or not 30 <= len(text) <= 300:
        return False
    if text != " ".join(text.split()) or "\n" in text:
        return False
    if TARGET_PATTERN.search(text) or EXCLUSION_PATTERN.search(text):
        return False
    return text[-1] in ".?!"


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), f"{path.name} must contain a mapping")
    return value


def validate_calibration_bank(
    data: Mapping[str, Any], *, heldout: Mapping[str, Any] | None = None
) -> None:
    _require(data.get("schema") == CALIBRATION_SCHEMA, "wrong calibration schema")
    _require(data.get("study_id") == STUDY_ID, "wrong calibration study")
    _require(data.get("status") == "selected_frozen_pre_output", "calibration is not frozen")
    rows = data.get("prompts")
    _require(isinstance(rows, list) and len(rows) == 6, "calibration must contain six prompts")
    _require(
        _payload_sha256(rows) == CALIBRATION_PAYLOAD_SHA256,
        "calibration prompt payload drifted",
    )
    expected_ids = [f"historical-{index:02d}" for index in range(1, 7)]
    _require([row.get("id") for row in rows] == expected_ids, "calibration IDs drifted")
    texts: set[str] = set()
    hashes: set[str] = set()
    for row in rows:
        text = row.get("text")
        _require(isinstance(text, str) and text.strip() == text, "invalid calibration text")
        digest = _text_sha256(text)
        _require(row.get("text_sha256") == digest, f"calibration hash drifted: {row.get('id')}")
        _require(row.get("split") == "calibration", "wrong calibration split")
        _require(not TARGET_PATTERN.search(text), "target leaked into calibration prompt")
        texts.add(text)
        hashes.add(digest)
    _require(len(texts) == 6 and len(hashes) == 6, "calibration prompts are duplicated")
    if heldout is not None:
        heldout_texts = {str(row["text"]) for row in heldout["prompts"]}
        heldout_hashes = {str(row["input_sha256"]) for row in heldout["prompts"]}
        _require(not texts & heldout_texts, "calibration text overlaps held-out prompts")
        _require(not hashes & heldout_hashes, "calibration hash overlaps held-out prompts")


def select_caa_rows(frame: Any, *, forbidden_hashes: set[str]) -> list[dict[str, Any]]:
    required = {"input", "concept_genre", "dataset_category", "concept_id"}
    _require(required.issubset(frame.columns), "train parquet is missing required fields")
    selected_by_normalized: dict[str, dict[str, Any]] = {}
    for source_row, row in frame.iterrows():
        text = row["input"]
        if row["concept_genre"] != "text" or row["dataset_category"] != "instruction":
            continue
        if not _ordinary_input(text):
            continue
        digest = _text_sha256(text)
        if digest in forbidden_hashes:
            continue
        normalized = _normalized_key(text)
        candidate = {
            "source_row": int(source_row),
            "source_concept_id": int(row["concept_id"]),
            "input_sha256": digest,
            "text": text,
        }
        current = selected_by_normalized.get(normalized)
        if current is None or candidate["source_row"] < current["source_row"]:
            selected_by_normalized[normalized] = candidate
    ordered = sorted(selected_by_normalized.values(), key=lambda item: item["input_sha256"])
    _require(len(ordered) >= 300, f"only {len(ordered)} eligible unique CAA inputs")
    output = ordered[:300]
    for index, row in enumerate(output):
        row["id"] = f"caa-{index + 1:03d}"
        row["positive_option"] = "A" if index % 2 == 0 else "B"
    return output


def validate_caa_manifest(
    data: Mapping[str, Any], *, calibration: Mapping[str, Any], heldout: Mapping[str, Any]
) -> None:
    _require(data.get("schema") == CAA_SCHEMA, "wrong CAA construction schema")
    _require(data.get("study_id") == STUDY_ID, "wrong CAA study")
    _require(data.get("status") == "selected_frozen_pre_output", "CAA rows are not frozen")
    source = data.get("source", {})
    _require(source.get("revision") == TRAIN_REVISION, "CAA source revision drifted")
    _require(source.get("file_sha256") == TRAIN_FILE_SHA256, "CAA source file hash drifted")
    rows = data.get("rows")
    _require(isinstance(rows, list) and len(rows) == 300, "CAA must contain 300 rows")
    _require(data.get("selection", {}).get("payload_sha256") == _payload_sha256(rows), "CAA payload hash drifted")
    _require([row.get("id") for row in rows] == [f"caa-{i:03d}" for i in range(1, 301)], "CAA IDs drifted")
    _require(sum(row.get("positive_option") == "A" for row in rows) == 150, "CAA A balance drifted")
    _require(sum(row.get("positive_option") == "B" for row in rows) == 150, "CAA B balance drifted")
    texts: set[str] = set()
    hashes: set[str] = set()
    for row in rows:
        text = row.get("text")
        digest = _text_sha256(text) if isinstance(text, str) else ""
        _require(_ordinary_input(text), f"ineligible frozen CAA row {row.get('id')}")
        _require(digest == row.get("input_sha256"), f"CAA text hash drifted: {row.get('id')}")
        texts.add(text)
        hashes.add(digest)
    _require(len(texts) == 300 and len(hashes) == 300, "CAA rows are duplicated")
    protected_texts = {str(row["text"]) for row in calibration["prompts"]}
    protected_texts |= {str(row["text"]) for row in heldout["prompts"]}
    protected_hashes = {str(row["text_sha256"]) for row in calibration["prompts"]}
    protected_hashes |= {str(row["input_sha256"]) for row in heldout["prompts"]}
    _require(not texts & protected_texts, "CAA rows overlap evaluation text")
    _require(not hashes & protected_hashes, "CAA rows overlap evaluation hashes")


def _atomic_yaml(path: Path, value: Mapping[str, Any]) -> None:
    rendered = yaml.safe_dump(dict(value), sort_keys=False, allow_unicode=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(path)


def freeze_caa(parquet_path: Path, output: Path) -> dict[str, Any]:
    _require(_sha256_file(parquet_path) == TRAIN_FILE_SHA256, "train parquet hash mismatch")
    try:
        import pandas as pd
    except ImportError as exc:
        raise DataFreezeError("pandas and a parquet engine are required only to freeze rows") from exc
    calibration = load_yaml(CALIBRATION_PATH)
    heldout = load_yaml(HERE / "prompts.heldout.yaml")
    validate_calibration_bank(calibration, heldout=heldout)
    forbidden = {str(row["text_sha256"]) for row in calibration["prompts"]}
    forbidden |= {str(row["input_sha256"]) for row in heldout["prompts"]}
    rows = select_caa_rows(pd.read_parquet(parquet_path), forbidden_hashes=forbidden)
    manifest = {
        "schema": CAA_SCHEMA,
        "study_id": STUDY_ID,
        "status": "selected_frozen_pre_output",
        "selected_on": "2026-08-24",
        "source": {
            "dataset": "pyvene/axbench-concept500",
            "revision": TRAIN_REVISION,
            "inventory": "prod_2b_l10_v1",
            "split": "train",
            "file_label": "2b/l10/train/data.parquet",
            "file_sha256": TRAIN_FILE_SHA256,
            "license": "CC-BY-4.0",
            "row_indexing": "zero-based physical row position in the pinned parquet",
        },
        "selection": {
            "read_fields": ["input", "concept_genre", "dataset_category", "concept_id"],
            "forbidden_selection_fields": ["output", "output_concept"],
            "rule_id": "emoji-caa-input-selection/v1",
            "sort": "ascending SHA-256 of exact UTF-8 input",
            "deduplication": "NFKC casefolded collapsed-whitespace input",
            "expected_count": 300,
            "payload_sha256": _payload_sha256(rows),
            "treatment_outputs_seen": False,
        },
        "rows": rows,
    }
    validate_caa_manifest(manifest, calibration=calibration, heldout=heldout)
    _atomic_yaml(output, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze-caa")
    freeze.add_argument("--parquet", type=Path, required=True)
    freeze.add_argument("--output", type=Path, default=CAA_PATH)
    sub.add_parser("validate")
    args = parser.parse_args()
    calibration = load_yaml(CALIBRATION_PATH)
    heldout = load_yaml(HERE / "prompts.heldout.yaml")
    validate_calibration_bank(calibration, heldout=heldout)
    if args.command == "freeze-caa":
        manifest = freeze_caa(args.parquet, args.output)
        print(f"froze {len(manifest['rows'])} CAA rows -> {args.output}")
        print(f"payload_sha256={manifest['selection']['payload_sha256']}")
    else:
        caa = load_yaml(CAA_PATH)
        validate_caa_manifest(caa, calibration=calibration, heldout=heldout)
        print(f"valid calibration={len(calibration['prompts'])}, CAA={len(caa['rows'])}")


if __name__ == "__main__":
    try:
        main()
    except DataFreezeError as exc:
        raise SystemExit(f"data freeze failed: {exc}") from exc
