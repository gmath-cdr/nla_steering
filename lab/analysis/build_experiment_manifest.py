"""Build the immutable-result sidecar manifest using steering-taxonomy/v1.

Raw result JSONs are never rewritten. This script classifies their embedded
specs, retains vector ancestry through derived components/remainders, and emits
both machine-readable JSON and a flat CSV for audits/report tables.

Usage from ``lab/``::

    python analysis/build_experiment_manifest.py
    python analysis/build_experiment_manifest.py --check
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

LAB = Path(__file__).resolve().parents[1]
REPO = LAB.parent
sys.path.insert(0, str(LAB))

# Load the pure taxonomy module without importing nla_lab.__init__, whose runtime
# API intentionally imports torch/transformers. Manifest generation needs neither.
_METHODS_SPEC = importlib.util.spec_from_file_location(
    "steering_taxonomy", LAB / "nla_lab" / "methods.py"
)
if _METHODS_SPEC is None or _METHODS_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("could not load nla_lab/methods.py")
_METHODS = importlib.util.module_from_spec(_METHODS_SPEC)
_METHODS_SPEC.loader.exec_module(_METHODS)
DESIGN_SCHEMA_ID = _METHODS.DESIGN_SCHEMA_ID
TAXONOMY_ID = _METHODS.TAXONOMY_ID
resolve_design = _METHODS.resolve_design
resolve_method = _METHODS.resolve_method

DEFAULT_JSON = LAB / "registry" / "experiment_manifest.json"
DEFAULT_CSV = LAB / "registry" / "experiment_manifest.csv"

CSV_FIELDS = [
    "experiment_id",
    "experiment_name",
    "campaign",
    "result_file",
    "result_sha256",
    "matrix_id",
    "model_key",
    "model_metadata_origin",
    "space_id",
    "root_method",
    "text_origin",
    "contrast_relation",
    "edit_granularity",
    "pairing_style",
    "derivation_operator",
    "output_role",
    "parent_vector_key",
    "derivation_method",
    "derivation_dictionary",
    "derivation_k",
    "vector_view",
    "vector_file",
    "vector_key",
    "vector_artifact_sha256",
    "vector_artifact_bytes",
    "vector_member_sha256",
    "vector_integrity_status",
    "intervention_kind",
    "token_scope",
    "scaling_rule",
    "design_intent",
    "preregistered",
    "arm_role",
    "method_metadata_origin",
    "classification_confidence",
    "provenance_completeness",
    "spec_snapshot_path",
    "spec_provenance",
    "executed_spec_locator",
    "n_records",
    "n_prompts",
    "n_alphas",
    "alphas",
    "samples_per_prompt",
    "execution_status",
    "supersession_status",
    "classification_notes",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _vector_integrity(
    artifact_path: str | None,
    artifact_key: str | None,
) -> dict[str, Any]:
    """Hash the saved archive and exact NPZ member without loading tensors."""
    empty = {
        "vector_artifact_sha256": None,
        "vector_artifact_bytes": None,
        "vector_member_sha256": None,
        "vector_integrity_status": "not_applicable",
    }
    if not artifact_path:
        return empty
    candidate = (LAB / artifact_path).resolve()
    try:
        candidate.relative_to(LAB.resolve())
    except ValueError:
        return {**empty, "vector_integrity_status": "path_outside_lab"}
    if not candidate.is_file():
        return {**empty, "vector_integrity_status": "artifact_missing"}
    record = {
        **empty,
        "vector_artifact_sha256": _sha256(candidate),
        "vector_artifact_bytes": candidate.stat().st_size,
        "vector_integrity_status": "archive_verified",
    }
    if candidate.suffix.lower() != ".npz" or not artifact_key:
        return record
    member = f"{artifact_key}.npy"
    try:
        with zipfile.ZipFile(candidate) as archive:
            payload = archive.read(member)
    except (KeyError, zipfile.BadZipFile):
        record["vector_integrity_status"] = "archive_verified_member_missing"
        return record
    record["vector_member_sha256"] = hashlib.sha256(payload).hexdigest()
    record["vector_integrity_status"] = "archive_and_member_verified"
    return record


def _campaign(name: str) -> str:
    if name.startswith("axbjs-"):
        return "axbench_jspace_pilot"
    if name.startswith("emojimx2-"):
        return "emoji_methods_wave2"
    if name.startswith("emojimx-"):
        return "emoji_methods_wave1"
    if name.startswith("wordle2-"):
        return "wordle2"
    if name.startswith("gcore-"):
        return "gemma_core"
    if name.startswith("wgrad-"):
        return "wordle_gradient"
    if name.startswith("wcrank-"):
        return "wordle_salience_crank"
    return "early_qwen"


def _model_record(name: str, data: dict[str, Any]) -> tuple[str, str, str]:
    runtime = data.get("runtime") or {}
    if runtime.get("model_key"):
        key = str(runtime["model_key"])
        layer = int(runtime.get("layer"))
        width = int(runtime.get("hidden_width"))
        space = f"{key}.resid-post.L{layer}.d{width}"
        return key, "embedded_runtime", space
    if name.startswith("gcore-"):
        return (
            "gemma3-12b",
            "legacy_campaign_backfill",
            "gemma3-12b-it.resid-post.L32.d3840",
        )
    return (
        "qwen",
        "legacy_campaign_backfill",
        "qwen2.5-7b-instruct.resid-post.L20.d3584",
    )


def _spec_snapshots() -> dict[str, str]:
    snapshots: dict[str, str] = {}
    for path in sorted((LAB / "experiments").rglob("*.yaml")):
        try:
            spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(spec, dict) and spec.get("name"):
            name = str(spec["name"])
            rel = path.relative_to(REPO).as_posix()
            if name in snapshots and snapshots[name] != rel:
                raise ValueError(
                    f"duplicate checked-in spec name {name!r}: "
                    f"{snapshots[name]!r} and {rel!r}"
                )
            snapshots[name] = rel
    return snapshots


def _experiment_id(path: Path, name: str) -> str:
    stem = path.stem
    suffix = stem[len(name) + 1:] if stem.startswith(name + "_") else stem
    return f"{name}@{suffix}"


def _entry(
    path: Path,
    data: dict[str, Any],
    snapshots: dict[str, str],
) -> dict[str, Any]:
    spec = data["spec"]
    records = data["records"]
    name = str(spec["name"])
    method = resolve_method(spec)
    design = resolve_design(spec, method)
    root = method["root"]
    derivation = method["derivation"]
    derivation_parameters = derivation.get("parameters") or {}
    artifact = method["source_artifact"]
    vector_integrity = _vector_integrity(artifact["path"], artifact["key"])
    intervention = method["intervention"]
    model_key, model_origin, space_id = _model_record(name, data)
    prompts = list(dict.fromkeys(str(record["prompt"]) for record in records))
    alphas = sorted({float(record["alpha"]) for record in records})
    spec_path = snapshots.get(name)
    metadata_origin = method["classification"]["origin"]
    provenance = "complete" if metadata_origin == "explicit_spec" and model_origin == "embedded_runtime" else "partial"
    rel_result = path.relative_to(REPO).as_posix()

    return {
        "experiment_id": _experiment_id(path, name),
        "experiment_name": name,
        "campaign": _campaign(name),
        "result_file": rel_result,
        "result_sha256": _sha256(path),
        "matrix_id": spec.get("matrix_id"),
        "model_key": model_key,
        "model_metadata_origin": model_origin,
        "space_id": space_id,
        "root_method": root["method"],
        "text_origin": root["text_origin"],
        "contrast_relation": root["contrast_relation"],
        "edit_granularity": root["edit_granularity"],
        "pairing_style": root["pairing_style"],
        "derivation_operator": derivation["operator"],
        "output_role": derivation["output_role"],
        "parent_vector_key": derivation.get("parent_vector_key"),
        "derivation_method": derivation_parameters.get("method"),
        "derivation_dictionary": derivation_parameters.get("dictionary"),
        "derivation_k": derivation_parameters.get("k_requested"),
        "vector_view": method["vector_view"],
        "vector_file": artifact["path"],
        "vector_key": artifact["key"],
        **vector_integrity,
        "intervention_kind": intervention["kind"],
        "token_scope": intervention["token_scope"],
        "scaling_rule": intervention["scaling_rule"],
        "design_intent": design["intent"],
        "preregistered": design["preregistered"],
        "arm_role": design["arm_role"],
        "method_metadata_origin": metadata_origin,
        "classification_confidence": method["classification"]["confidence"],
        "provenance_completeness": provenance,
        "spec_snapshot_path": spec_path,
        "spec_provenance": (
            "checked_in_yaml_and_result_embedded"
            if spec_path else "result_embedded_only"
        ),
        "executed_spec_locator": f"{rel_result}#spec",
        "n_records": len(records),
        "n_prompts": len(prompts),
        "n_alphas": len(alphas),
        "alphas": alphas,
        "samples_per_prompt": spec.get("samples_per_prompt", 3),
        "execution_status": "completed",
        "supersession_status": "unspecified",
        "classification_notes": method["classification"]["notes"],
    }


def build_manifest() -> dict[str, Any]:
    snapshots = _spec_snapshots()
    entries: list[dict[str, Any]] = []
    for path in sorted((LAB / "results").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "spec" not in data or "records" not in data:
            continue
        entries.append(_entry(path, data, snapshots))

    root_counts = Counter(entry["root_method"] for entry in entries)
    ancestry_counts = Counter(
        f"{entry['root_method']} / {entry['derivation_operator']} / {entry['output_role']}"
        for entry in entries
    )
    reply_counts = Counter()
    for entry in entries:
        key = f"{entry['root_method']} / {entry['derivation_operator']} / {entry['output_role']}"
        reply_counts[key] += int(entry["n_records"])
    unresolved = [entry["experiment_id"] for entry in entries
                  if entry["root_method"] == "unknown"
                  or entry["derivation_operator"] == "unknown"]
    missing_snapshots = [entry["experiment_id"] for entry in entries
                         if entry["spec_snapshot_path"] is None]
    file_vectors = [entry for entry in entries if entry["vector_file"]]
    member_verified = [
        entry for entry in file_vectors
        if entry["vector_integrity_status"] == "archive_and_member_verified"
    ]

    return {
        "schema": "steering-experiment-manifest/v1",
        "method_taxonomy": TAXONOMY_ID,
        "design_schema": DESIGN_SCHEMA_ID,
        "source": "lab/results/*.json (raw files left unchanged)",
        "summary": {
            "run_count": len(entries),
            "record_count": sum(int(entry["n_records"]) for entry in entries),
            "root_method_counts": dict(sorted(root_counts.items())),
            "ancestry_counts": dict(sorted(ancestry_counts.items())),
            "ancestry_reply_counts": dict(sorted(reply_counts.items())),
            "unresolved_count": len(unresolved),
            "unresolved_experiment_ids": unresolved,
            "missing_checked_in_spec_count": len(missing_snapshots),
            "missing_checked_in_spec_ids": missing_snapshots,
            "file_vector_run_count": len(file_vectors),
            "verified_vector_member_run_count": len(member_verified),
        },
        "entries": entries,
    }


def render_json(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def render_csv(manifest: dict[str, Any]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for source in manifest["entries"]:
        row = dict(source)
        row["alphas"] = ";".join(f"{value:g}" for value in source["alphas"])
        row["classification_notes"] = " | ".join(source["classification_notes"])
        writer.writerow({field: row.get(field) for field in CSV_FIELDS})
    return output.getvalue()


def _check(path: Path, expected: str) -> bool:
    return path.exists() and path.read_text(encoding="utf-8") == expected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if committed manifest files are stale")
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--csv-out", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()

    manifest = build_manifest()
    json_text = render_json(manifest)
    csv_text = render_csv(manifest)
    if args.check:
        stale = []
        if not _check(args.json_out, json_text):
            stale.append(str(args.json_out))
        if not _check(args.csv_out, csv_text):
            stale.append(str(args.csv_out))
        if stale:
            raise SystemExit("stale experiment manifest: " + ", ".join(stale))
    else:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.csv_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json_text, encoding="utf-8")
        args.csv_out.write_text(csv_text, encoding="utf-8")
    summary = manifest["summary"]
    print(
        f"manifest: {summary['run_count']} runs, {summary['record_count']} records, "
        f"{summary['unresolved_count']} unresolved"
    )


if __name__ == "__main__":
    main()
