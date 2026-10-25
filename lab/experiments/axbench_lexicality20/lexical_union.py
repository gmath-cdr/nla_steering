"""Load the authenticated held-out union for lexical-alpha sensitivity.

The union references two immutable stores: records reused from the original
H-tuned AxB44 campaign and records generated under the lexical-alpha receipt.
This loader resolves those references without copying or silently deduplicating
responses and fails closed on every recorded file, cell, and row identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from experiments.axbench_lexicality20.lexical_gen_specs import _cell_id
from experiments.axbench_lexicality20.lexical_select_alpha import (
    ANALYSIS_ID,
    EXPECTED_SOURCE,
    file_sha256,
)
from experiments.axbench_lexicality20.protocol import canonical_sha256


EXPECTED_UNION_ROWS = {"qwen": 1096, "gemma": 1024}
EXPECTED_NAMED_ROWS = 80
NAMED_ARMS = (
    "unsteered",
    "axbench_prompt_steering",
    "diffmean",
    "caa",
    "nla",
)


class UnionError(RuntimeError):
    """Raised when a sensitivity union is not the sealed administration."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnionError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UnionError(f"JSON root must be a mapping: {path}")
    return value


def verify_integrity(payload: dict[str, Any], *, label: str) -> None:
    unsigned = dict(payload)
    claimed = unsigned.pop("integrity_sha256", None)
    if claimed != canonical_sha256(unsigned):
        raise UnionError(f"{label} integrity mismatch")


def verify_file_map(root: Path, payload: dict[str, Any], *, label: str) -> None:
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise UnionError(f"{label} has no file inventory")
    for relative, receipt in files.items():
        path = root / str(relative)
        if (
            not isinstance(receipt, dict)
            or not path.is_file()
            or path.stat().st_size != int(receipt.get("bytes", -1))
            or file_sha256(path) != receipt.get("sha256")
        ):
            raise UnionError(f"{label} file mismatch: {relative}")


def _source_complete(source_root: Path, model_key: str) -> dict[str, Any]:
    path = source_root / "COMPLETE.json"
    if not path.is_file() or file_sha256(path) != EXPECTED_SOURCE[model_key]["complete_sha256"]:
        raise UnionError("original campaign COMPLETE hash mismatch")
    payload = load_json(path)
    if payload.get("schema") != "axb44-complete/v1" or payload.get("status") != "complete":
        raise UnionError("original campaign COMPLETE identity mismatch")
    return payload


def _sensitivity_complete(
    sensitivity_root: Path,
    model_key: str,
    expected_complete_sha256: str,
) -> dict[str, Any]:
    path = sensitivity_root / "COMPLETE.json"
    if (
        len(expected_complete_sha256) != 64
        or file_sha256(path) != expected_complete_sha256
    ):
        raise UnionError("lexical sensitivity COMPLETE hash mismatch")
    payload = load_json(path)
    if (
        payload.get("schema") != "axb44-lexical-alpha-complete/v1"
        or payload.get("status") != "complete"
        or payload.get("analysis_id") != ANALYSIS_ID
        or payload.get("model_key") != model_key
        or int(payload.get("counts", {}).get("total", -1)) != EXPECTED_UNION_ROWS[model_key]
    ):
        raise UnionError("lexical sensitivity COMPLETE identity/count mismatch")
    verify_file_map(sensitivity_root, payload, label="lexical sensitivity COMPLETE")
    return payload


def _registered_path(
    root: Path,
    complete: dict[str, Any],
    relative: str,
    expected_sha256: str,
) -> Path:
    receipt = complete.get("files", {}).get(relative)
    path = root / relative
    if (
        not isinstance(receipt, dict)
        or receipt.get("sha256") != expected_sha256
        or not path.is_file()
        or path.stat().st_size != int(receipt.get("bytes", -1))
        or file_sha256(path) != expected_sha256
    ):
        raise UnionError(f"referenced result failed its COMPLETE inventory: {relative}")
    return path


def _record_matches(record: dict[str, Any], cell: dict[str, Any]) -> bool:
    return (
        int(record.get("concept_id", -1)) == int(cell["concept_id"])
        and str(record.get("prompt_id")) == str(cell["prompt_id"])
        and int(record.get("sample", -1)) == int(cell["generation_seed"])
        and float(record.get("alpha", -1)) == float(cell["alpha"])
    )


def load_union(
    *,
    model_key: str,
    source_root: Path,
    sensitivity_root: Path,
    sensitivity_complete_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return one authenticated row per cell plus the sealed manifest."""

    source_root = source_root.resolve()
    sensitivity_root = sensitivity_root.resolve()
    source_complete = _source_complete(source_root, model_key)
    sensitivity_complete = _sensitivity_complete(
        sensitivity_root,
        model_key,
        sensitivity_complete_sha256,
    )
    index_path = sensitivity_root / "COMBINED_INDEX.json"
    if file_sha256(index_path) != sensitivity_complete.get("combined_index_sha256"):
        raise UnionError("combined-index hash differs from sensitivity COMPLETE")
    index = load_json(index_path)
    verify_integrity(index, label="combined index")
    if (
        index.get("schema") != "axb44-lexical-alpha-combined-index/v1"
        or index.get("analysis_id") != ANALYSIS_ID
        or index.get("model_key") != model_key
        or int(index.get("counts", {}).get("total", -1)) != EXPECTED_UNION_ROWS[model_key]
    ):
        raise UnionError("combined-index identity/count mismatch")

    manifest_relative = next(
        (
            name
            for name, receipt in sensitivity_complete["files"].items()
            if receipt.get("sha256") == sensitivity_complete.get("materialization_manifest_sha256")
        ),
        None,
    )
    if manifest_relative is None:
        raise UnionError("materialization manifest is absent from COMPLETE inventory")
    manifest = load_json(sensitivity_root / manifest_relative)
    verify_integrity(manifest, label="materialization manifest")
    expected_counts = {
        "qwen": {
            "full_unique_analysis_rows": 1096,
            "named_baseline_and_prompt_rows": 160,
            "named_treatment_rows": 240,
            "new_rows": 528,
            "reused_rows": 568,
        },
        "gemma": {
            "full_unique_analysis_rows": 1024,
            "named_baseline_and_prompt_rows": 160,
            "named_treatment_rows": 240,
            "new_rows": 568,
            "reused_rows": 456,
        },
    }[model_key]
    if (
        manifest.get("schema") != "axb44-lexical-alpha-materialization/v1"
        or manifest.get("status") != "sealed"
        or manifest.get("analysis_id") != ANALYSIS_ID
        or manifest.get("campaign_id") != "axb44-crossmodel-v1"
        or manifest.get("evidential_status") != "post_hoc_sensitivity"
        or manifest.get("model_key") != model_key
        or any(
            int(manifest.get("counts", {}).get(key, -1)) != value
            for key, value in expected_counts.items()
        )
    ):
        raise UnionError("materialization manifest identity/count mismatch")

    payload_cache: dict[Path, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_kind, entries in (
        ("new_lexical_alpha_generation", index.get("generated", [])),
        ("original_h_tuned_campaign", index.get("reused", [])),
    ):
        root = sensitivity_root if source_kind.startswith("new_") else source_root
        complete = sensitivity_complete if source_kind.startswith("new_") else source_complete
        for entry in entries:
            if entry.get("source") != source_kind:
                raise UnionError("combined-index source label mismatch")
            cell = entry.get("cell")
            cell_id = str(entry.get("cell_id"))
            if not isinstance(cell, dict) or cell_id != _cell_id(cell) or cell_id in seen:
                raise UnionError("combined-index cell identity is invalid or duplicated")
            seen.add(cell_id)
            relative = str(entry.get("result_file"))
            result_path = _registered_path(root, complete, relative, str(entry.get("result_sha256")))
            payload = payload_cache.setdefault(result_path, load_json(result_path))
            record_index = int(entry.get("record_index", -1))
            records = payload.get("records", [])
            if not 0 <= record_index < len(records):
                raise UnionError("combined-index record offset is invalid")
            record = records[record_index]
            if not isinstance(record, dict) or not _record_matches(record, cell):
                raise UnionError("combined-index record does not match its cell")
            spec = payload.get("spec", {})
            if spec.get("study", {}).get("phase") != "evaluation":
                raise UnionError("combined-index row is not a held-out evaluation")
            if cell.get("kind") == "named":
                arm_id = str(cell.get("arm_id"))
                if arm_id not in NAMED_ARMS or spec.get("study", {}).get("arm_id") != arm_id:
                    raise UnionError("named-arm identity mismatch")
                control_seed = None
            elif cell.get("kind") == "random":
                control_seed = int(cell.get("control_seed", -1))
                arm_id = f"random_{control_seed}_matched"
                if not str(spec.get("study", {}).get("arm_id", "")).startswith(
                    f"random_{control_seed}_"
                ):
                    raise UnionError("random-arm identity mismatch")
            else:
                raise UnionError("unknown union cell kind")
            rows.append(
                {
                    "cell_id": cell_id,
                    "source": source_kind,
                    "kind": cell["kind"],
                    "arm_id": arm_id,
                    "control_seed": control_seed,
                    "model_key": model_key,
                    "concept_id": int(record["concept_id"]),
                    "concept_label": str(record["concept_label"]),
                    "prompt_id": str(record["prompt_id"]),
                    "prompt_track": str(record.get("prompt_track")),
                    "instruction": str(record.get("carrier_prompt", record["prompt"])),
                    "sentence": str(record["text"]),
                    "generation_seed": int(record["sample"]),
                    "alpha": float(record["alpha"]),
                    "mode": str(spec.get("mode")),
                    "perplexity": record.get("target_model_output_perplexity"),
                    "generation": record.get("generation", {}),
                    "probes": record.get("probes", {}),
                    "result_file": relative,
                    "result_sha256": str(entry["result_sha256"]),
                    "record_index": record_index,
                }
            )
    if len(rows) != EXPECTED_UNION_ROWS[model_key] or len(seen) != len(rows):
        raise UnionError("resolved union has the wrong row count")
    for arm_id in NAMED_ARMS:
        arm = [row for row in rows if row["arm_id"] == arm_id]
        if len(arm) != EXPECTED_NAMED_ROWS:
            raise UnionError(f"named arm does not contain 80 rows: {arm_id}")
        if {
            track: sum(row["prompt_track"] == track for row in arm)
            for track in ("concept500_negative", "shared_alpaca_transfer")
        } != {
            "concept500_negative": 40,
            "shared_alpaca_transfer": 40,
        }:
            raise UnionError(f"named arm does not preserve the 40/40 tracks: {arm_id}")
        concepts = {int(row["concept_id"]) for row in arm}
        if len(concepts) != 10:
            raise UnionError(f"named arm does not contain 10 concepts: {arm_id}")
        for concept_id in concepts:
            concept_rows = [
                row for row in arm if int(row["concept_id"]) == concept_id
            ]
            prompt_groups: dict[str, list[dict[str, Any]]] = {}
            for row in concept_rows:
                prompt_groups.setdefault(str(row["prompt_id"]), []).append(row)
            if (
                len(concept_rows) != 8
                or len(prompt_groups) != 4
                or any(
                    len(group) != 2
                    or len({int(row["generation_seed"]) for row in group}) != 2
                    for group in prompt_groups.values()
                )
            ):
                raise UnionError(
                    f"named arm lacks 4 prompts x 2 seeds for concept {concept_id}: "
                    f"{arm_id}"
                )
    return rows, manifest
