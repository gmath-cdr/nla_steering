"""Validate canaries and certify the lexical-alpha sensitivity outputs.

The sensitivity run reuses authenticated records from the original H-tuned
AxB44 campaign.  This module keeps the two analyses separate: historical
records remain read-only references, while newly generated records live in a
new run root and must match the sealed delta manifest exactly.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from typing import Any

import yaml

from experiments.axbench_lexicality20.lexical_gen_specs import (
    MANIFEST_SCHEMA,
    _cell_id,
)
from experiments.axbench_lexicality20.lexical_select_alpha import (
    ANALYSIS_ID,
    EXPECTED_CAMPAIGN_ID,
    EXPECTED_REVIEW_SHA256,
    EXPECTED_SOURCE,
    file_sha256,
)
from experiments.axbench_lexicality20.protocol import canonical_sha256
from nla_lab.study import validate_spec_integrity


CANARY_SPEC_NAMES = {
    "qwen": (
        "axb44-qwen-evaluation-unsteered",
        "axb44-qwen-evaluation-diffmean-c20-fixed",
        "axb44-qwen-evaluation-random_17_all_a0p1-c20-alpaca-0041",
    ),
    "gemma": (
        "axb44-gemma-evaluation-unsteered",
        "axb44-gemma-evaluation-diffmean-c20-fixed",
        "axb44-gemma-evaluation-random_17_all_a0p1-c20-alpaca-0041",
    ),
}
EXPECTED_PACKAGES = {
    "torch": "2.8.0+cu128",
    "transformers": "4.57.6",
    "numpy": "2.1.2",
    "PyYAML": "6.0.3",
}
COMPLETE_SCHEMA = "axb44-lexical-alpha-complete/v1"
CANARY_SCHEMA = "axb44-lexical-alpha-canary/v1"


class SensitivityValidationError(RuntimeError):
    """Raised when new and historical records cannot be safely combined."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SensitivityValidationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SensitivityValidationError(f"JSON root must be a mapping: {path}")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SensitivityValidationError(f"cannot read YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SensitivityValidationError(f"YAML root must be a mapping: {path}")
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def _write_or_match(path: Path, value: dict[str, Any], label: str) -> None:
    """Make finalization restartable without accepting a changed receipt."""

    if path.exists():
        if _load_json(path) != value:
            raise SensitivityValidationError(f"existing {label} differs: {path}")
        return
    _atomic_json(path, value)


def _single_result(directory: Path, name: str) -> Path:
    matches = sorted(directory.glob(f"{name}_*.json"))
    if len(matches) != 1:
        raise SensitivityValidationError(
            f"expected one result for {name} in {directory}, found {len(matches)}"
        )
    return matches[0]


def _source_complete(run_root: Path, model_key: str) -> dict[str, Any]:
    complete_path = run_root / "COMPLETE.json"
    expected = EXPECTED_SOURCE[model_key]["complete_sha256"]
    if not complete_path.is_file() or file_sha256(complete_path) != expected:
        raise SensitivityValidationError("source COMPLETE hash mismatch")
    complete = _load_json(complete_path)
    if (
        complete.get("status") != "complete"
        or complete.get("model_key") != model_key
        or complete.get("campaign_id") != EXPECTED_CAMPAIGN_ID
        or complete.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
    ):
        raise SensitivityValidationError("source COMPLETE identity mismatch")
    return complete


def _registered_source_file(
    run_root: Path,
    complete: dict[str, Any],
    relative: str,
) -> Path:
    receipt = complete.get("files", {}).get(relative)
    path = run_root / relative
    if (
        not isinstance(receipt, dict)
        or not path.is_file()
        or file_sha256(path) != receipt.get("sha256")
        or path.stat().st_size != int(receipt.get("bytes", -1))
    ):
        raise SensitivityValidationError(f"source file failed COMPLETE: {relative}")
    return path


def _validate_runtime(payload: dict[str, Any], spec: dict[str, Any], model_key: str) -> None:
    runtime = payload.get("runtime", {})
    expected_runtime = spec.get("expected_runtime", {})
    if any(runtime.get(key) != expected_runtime.get(key) for key in ("model_key", "layer", "hidden_width")):
        raise SensitivityValidationError("result runtime identity mismatch")
    if runtime.get("checkpoint_marker_sha256", {}).get("base") != spec.get(
        "expected_base_checkpoint_marker_sha256"
    ):
        raise SensitivityValidationError("result base checkpoint markers drifted")
    packages = runtime.get("package_versions", {})
    if any(packages.get(name) != version for name, version in EXPECTED_PACKAGES.items()):
        raise SensitivityValidationError("result package versions differ from prior run")
    gpu = runtime.get("gpu", {})
    if "A100" not in str(gpu.get("name")) or gpu.get("compute_capability") != [8, 0]:
        raise SensitivityValidationError("incremental run did not use an A100 compute-8.0 GPU")
    if gpu.get("torch_cuda_version") != "12.8":
        raise SensitivityValidationError("incremental run used a different CUDA runtime")
    if runtime.get("git", {}).get("dirty") is not False:
        raise SensitivityValidationError("incremental run did not use a clean Git tree")
    if runtime.get("model_key") != EXPECTED_SOURCE[model_key]["runtime_model_key"]:
        raise SensitivityValidationError("result uses the wrong model stratum")


def validate_canary(
    *,
    model_key: str,
    source_run_root: Path,
    canary_results: Path,
    output: Path,
) -> dict[str, Any]:
    """Require exact regenerated records for three historical execution paths."""

    complete = _source_complete(source_run_root, model_key)
    expected_names = CANARY_SPEC_NAMES[model_key]
    observed_files = sorted(canary_results.glob("*.json"))
    if len(observed_files) != len(expected_names):
        raise SensitivityValidationError("canary directory has an unexpected result count")
    rows: list[dict[str, Any]] = []
    for name in expected_names:
        new_path = _single_result(canary_results, name)
        new_payload = _load_json(new_path)
        new_spec = new_payload.get("spec")
        if not isinstance(new_spec, dict):
            raise SensitivityValidationError(f"canary {name} has no embedded spec")
        validate_spec_integrity(new_spec, required=True)
        _validate_runtime(new_payload, new_spec, model_key)

        source_matches: list[Path] = []
        for relative in complete.get("files", {}):
            if (
                relative.startswith("results/evaluation/")
                and relative.endswith(".json")
                and Path(relative).name.startswith(name + "_")
            ):
                source_matches.append(_registered_source_file(source_run_root, complete, relative))
        if len(source_matches) != 1:
            raise SensitivityValidationError(f"source result for canary {name} is ambiguous")
        source_path = source_matches[0]
        source_payload = _load_json(source_path)
        if new_spec != source_payload.get("spec"):
            raise SensitivityValidationError(f"canary spec drift for {name}")
        if new_payload.get("records") != source_payload.get("records"):
            raise SensitivityValidationError(f"canary record mismatch for {name}")
        if new_payload.get("direction_integrity") != source_payload.get("direction_integrity"):
            raise SensitivityValidationError(f"canary direction mismatch for {name}")
        rows.append(
            {
                "name": name,
                "records": len(new_payload.get("records", [])),
                "source_result_sha256": file_sha256(source_path),
                "regenerated_result_sha256": file_sha256(new_path),
                "spec_sha256": new_spec["spec_integrity"]["sha256"],
                "records_exact_match": True,
            }
        )
    receipt = {
        "schema": CANARY_SCHEMA,
        "status": "passed",
        "model_key": model_key,
        "source_complete_sha256": EXPECTED_SOURCE[model_key]["complete_sha256"],
        "checks": rows,
    }
    receipt["integrity_sha256"] = canonical_sha256(receipt)
    _write_or_match(output, receipt, "canary receipt")
    return receipt


def _validate_manifest(path: Path, model_key: str) -> dict[str, Any]:
    manifest = _load_json(path)
    claimed = manifest.pop("integrity_sha256", None)
    observed = canonical_sha256(manifest)
    manifest["integrity_sha256"] = claimed
    if claimed != observed:
        raise SensitivityValidationError("materialization manifest integrity mismatch")
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("status") != "sealed"
        or manifest.get("analysis_id") != ANALYSIS_ID
        or manifest.get("model_key") != model_key
        or manifest.get("campaign_id") != EXPECTED_CAMPAIGN_ID
        or manifest.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
    ):
        raise SensitivityValidationError("materialization manifest identity mismatch")
    return manifest


def _record_matches_cell(record: dict[str, Any], cell: dict[str, Any]) -> bool:
    return (
        int(record.get("concept_id", -1)) == int(cell["concept_id"])
        and str(record.get("prompt_id")) == str(cell["prompt_id"])
        and int(record.get("sample", -1)) == int(cell["generation_seed"])
        and float(record.get("alpha", -1)) == float(cell["alpha"])
    )


def finalize(
    *,
    model_key: str,
    source_run_root: Path,
    run_root: Path,
    spec_dir: Path,
    materialization_manifest: Path,
    lexical_alpha_receipt: Path,
    canary_receipt: Path,
) -> dict[str, Any]:
    """Certify the generated/reused union without copying historical outputs."""

    complete = _source_complete(source_run_root, model_key)
    manifest = _validate_manifest(materialization_manifest, model_key)
    if file_sha256(lexical_alpha_receipt) != manifest.get("lexical_alpha_receipt_sha256"):
        raise SensitivityValidationError("lexical-alpha receipt hash mismatch")
    canary = _load_json(canary_receipt)
    canary_claimed = canary.pop("integrity_sha256", None)
    canary_observed = canonical_sha256(canary)
    canary["integrity_sha256"] = canary_claimed
    if (
        canary_claimed != canary_observed
        or canary.get("schema") != CANARY_SCHEMA
        or canary.get("status") != "passed"
        or canary.get("model_key") != model_key
    ):
        raise SensitivityValidationError("canary receipt is not a valid pass")

    spec_receipts = {row["name"]: row for row in manifest.get("generated_specs", [])}
    if len(spec_receipts) != int(manifest.get("counts", {}).get("new_specs", -1)):
        raise SensitivityValidationError("manifest generated-spec count mismatch")
    expected_by_spec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in manifest.get("generated_cells", []):
        expected_by_spec[str(row["spec_name"])].append(row)

    result_dir = run_root / "results" / "new"
    actual_results = sorted(result_dir.glob("*.json"))
    generated_index: list[dict[str, Any]] = []
    seen_cell_ids: set[str] = set()
    seen_results: set[Path] = set()
    for name, registered in sorted(spec_receipts.items()):
        spec_matches = sorted(spec_dir.glob(f"*-{name}.yaml"))
        if len(spec_matches) != 1:
            raise SensitivityValidationError(f"spec file for {name} is missing or ambiguous")
        spec_path = spec_matches[0]
        spec = _load_yaml(spec_path)
        validate_spec_integrity(spec, required=True)
        if spec.get("spec_integrity", {}).get("sha256") != registered.get("sha256"):
            raise SensitivityValidationError(f"registered spec hash mismatch: {name}")
        if spec.get("design", {}).get("preregistered") is not False:
            raise SensitivityValidationError(f"post-hoc spec is mislabeled: {name}")
        result_path = _single_result(result_dir, name)
        seen_results.add(result_path.resolve())
        payload = _load_json(result_path)
        if payload.get("spec") != spec:
            raise SensitivityValidationError(f"embedded result spec differs: {name}")
        _validate_runtime(payload, spec, model_key)
        expected_direction = spec.get("expected_direction_integrity", {})
        observed_direction = payload.get("direction_integrity", {})
        if not expected_direction or any(
            observed_direction.get(key) != value for key, value in expected_direction.items()
        ):
            raise SensitivityValidationError(f"direction integrity mismatch: {name}")
        records = payload.get("records", [])
        expected_rows = expected_by_spec.get(name, [])
        if (
            not isinstance(records, list)
            or len(records) != int(spec.get("expected_records", -1))
            or len(records) != int(registered.get("expected_records", -1))
            or len(records) != len(expected_rows)
        ):
            raise SensitivityValidationError(f"record cardinality mismatch: {name}")
        unused = list(expected_rows)
        for record_index, record in enumerate(records):
            candidates = [row for row in unused if _record_matches_cell(record, row["cell"])]
            if len(candidates) != 1:
                raise SensitivityValidationError(f"record identity mismatch: {name}#{record_index}")
            row = candidates[0]
            unused.remove(row)
            if row["cell_id"] != _cell_id(row["cell"]) or row["cell_id"] in seen_cell_ids:
                raise SensitivityValidationError("generated cell hash is invalid or duplicated")
            seen_cell_ids.add(row["cell_id"])
            generated_index.append(
                {
                    **row,
                    "result_file": result_path.relative_to(run_root).as_posix(),
                    "result_sha256": file_sha256(result_path),
                    "record_index": record_index,
                }
            )
        if unused:
            raise SensitivityValidationError(f"generated cells were not observed: {name}")
    if set(path.resolve() for path in actual_results) != seen_results:
        raise SensitivityValidationError("new result directory contains unexpected JSON files")

    reused_index: list[dict[str, Any]] = []
    for row in manifest.get("reused_cells", []):
        if row.get("source") != "original_h_tuned_campaign":
            raise SensitivityValidationError("reused cell has an invalid source label")
        cell = row.get("cell", {})
        if row.get("cell_id") != _cell_id(cell) or row["cell_id"] in seen_cell_ids:
            raise SensitivityValidationError("reused cell hash is invalid or duplicated")
        relative = str(row.get("result_file"))
        source_path = _registered_source_file(source_run_root, complete, relative)
        if file_sha256(source_path) != row.get("result_sha256"):
            raise SensitivityValidationError("reused result hash differs from manifest")
        payload = _load_json(source_path)
        spec = payload.get("spec", {})
        if spec.get("spec_integrity", {}).get("sha256") != row.get("spec_sha256"):
            raise SensitivityValidationError("reused spec hash differs from manifest")
        record_index = int(row.get("record_index", -1))
        records = payload.get("records", [])
        if not 0 <= record_index < len(records) or not _record_matches_cell(records[record_index], cell):
            raise SensitivityValidationError("reused record identity differs from manifest")
        seen_cell_ids.add(row["cell_id"])
        reused_index.append(row)

    counts = manifest.get("counts", {})
    if len(generated_index) != int(counts.get("new_rows", -1)):
        raise SensitivityValidationError("generated row total differs from manifest")
    if len(reused_index) != int(counts.get("reused_rows", -1)):
        raise SensitivityValidationError("reused row total differs from manifest")
    if len(seen_cell_ids) != int(counts.get("full_unique_analysis_rows", -1)):
        raise SensitivityValidationError("full union row total differs from manifest")

    combined = {
        "schema": "axb44-lexical-alpha-combined-index/v1",
        "analysis_id": ANALYSIS_ID,
        "model_key": model_key,
        "generated": generated_index,
        "reused": reused_index,
        "counts": {
            "generated": len(generated_index),
            "reused": len(reused_index),
            "total": len(seen_cell_ids),
        },
    }
    combined["integrity_sha256"] = canonical_sha256(combined)
    combined_path = run_root / "COMBINED_INDEX.json"
    _write_or_match(combined_path, combined, "combined index")

    files: dict[str, dict[str, Any]] = {}
    for path in sorted(run_root.rglob("*")):
        if not path.is_file() or path.name in {"COMPLETE.json", "RUN_STATUS.json"} or path.suffix == ".tmp":
            continue
        files[path.relative_to(run_root).as_posix()] = {
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
    receipt = {
        "schema": COMPLETE_SCHEMA,
        "status": "complete",
        "analysis_id": ANALYSIS_ID,
        "evidential_status": "post_hoc_sensitivity",
        "model_key": model_key,
        "campaign_id": EXPECTED_CAMPAIGN_ID,
        "review_payload_sha256": EXPECTED_REVIEW_SHA256,
        "source_complete_sha256": EXPECTED_SOURCE[model_key]["complete_sha256"],
        "materialization_manifest_sha256": file_sha256(materialization_manifest),
        "lexical_alpha_receipt_sha256": file_sha256(lexical_alpha_receipt),
        "canary_receipt_sha256": file_sha256(canary_receipt),
        "combined_index_sha256": file_sha256(combined_path),
        "counts": combined["counts"],
        "files": files,
        "interpretation_boundary": manifest.get("interpretation_boundary"),
    }
    complete_path = run_root / "COMPLETE.json"
    _write_or_match(complete_path, receipt, "sensitivity COMPLETE")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    canary = subparsers.add_parser("canary")
    canary.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    canary.add_argument("--source-run-root", type=Path, required=True)
    canary.add_argument("--canary-results", type=Path, required=True)
    canary.add_argument("--output", type=Path, required=True)
    finish = subparsers.add_parser("finalize")
    finish.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    finish.add_argument("--source-run-root", type=Path, required=True)
    finish.add_argument("--run-root", type=Path, required=True)
    finish.add_argument("--spec-dir", type=Path, required=True)
    finish.add_argument("--materialization-manifest", type=Path, required=True)
    finish.add_argument("--lexical-alpha-receipt", type=Path, required=True)
    finish.add_argument("--canary-receipt", type=Path, required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    status.add_argument("--run-root", type=Path, required=True)
    status.add_argument("--state", choices=("complete", "failed"), required=True)
    status.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == "canary":
            receipt = validate_canary(
                model_key=args.model_key,
                source_run_root=args.source_run_root.resolve(),
                canary_results=args.canary_results.resolve(),
                output=args.output.resolve(),
            )
            print(f"CANARY PASS: {args.model_key} {len(receipt['checks'])} execution paths")
        elif args.command == "finalize":
            receipt = finalize(
                model_key=args.model_key,
                source_run_root=args.source_run_root.resolve(),
                run_root=args.run_root.resolve(),
                spec_dir=args.spec_dir.resolve(),
                materialization_manifest=args.materialization_manifest.resolve(),
                lexical_alpha_receipt=args.lexical_alpha_receipt.resolve(),
                canary_receipt=args.canary_receipt.resolve(),
            )
            print(f"COMPLETE: {args.model_key} {receipt['counts']}")
        else:
            payload = {
                "schema": "axb44-lexical-alpha-run-status/v1",
                "model_key": args.model_key,
                "analysis_id": ANALYSIS_ID,
                "state": args.state,
                "exit_code": args.exit_code,
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_json(args.run_root.resolve() / "RUN_STATUS.json", payload)
            print(f"RUN STATUS: {args.model_key} {args.state} exit={args.exit_code}")
    except SensitivityValidationError as exc:
        raise SystemExit(f"lexical sensitivity validation failed: {exc}") from exc


if __name__ == "__main__":
    main()
