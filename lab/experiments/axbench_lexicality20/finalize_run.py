"""Certify cardinality and hashes for one completed axb44 model stratum."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any

import yaml

from experiments.axbench_lexicality20.protocol import file_sha256, load_bundle
from nla_lab.study import validate_spec_integrity


class FinalizeError(RuntimeError):
    pass


def result_count(directory: Path, phase: str) -> tuple[int, int]:
    files, records = 0, 0
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        spec = payload.get("spec", {})
        if spec.get("study", {}).get("phase") != phase:
            continue
        validate_spec_integrity(spec, required=True)
        observed = len(payload.get("records", []))
        if observed != int(spec["expected_records"]):
            raise FinalizeError(f"incomplete result {path}")
        if spec.get("record_target_model_perplexity") is True and any(
            "target_model_output_perplexity" not in record
            for record in payload.get("records", [])
        ):
            raise FinalizeError(f"missing target-model perplexity field in {path}")
        files += 1
        records += observed
    return files, records


def atomic_json(path: Path, value: Any) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
        suffix=".tmp", delete=False,
    ) as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()
    bundle = load_bundle()
    verification = json.loads((args.run_root / "model_verification.json").read_text(encoding="utf-8"))
    if verification.get("network_model_downloads") != 0:
        raise FinalizeError("model verification does not certify zero downloads")
    if verification.get("model_key") != args.model_key:
        raise FinalizeError("model verification identity mismatch")
    counts = {}
    expected = {"canary": 2, "calibration": 920}
    for phase in ("canary", "calibration", "evaluation"):
        files, records = result_count(args.run_root / "results" / phase, phase)
        counts[phase] = {"files": files, "records": records}
        manifest = yaml.safe_load(
            (args.run_root / "specs" / phase / "_materialization-manifest.yaml").read_text(encoding="utf-8")
        )
        if records != int(manifest["expected_records"]):
            raise FinalizeError(f"{phase} result/spec cardinality mismatch")
        if phase in expected and records != expected[phase]:
            raise FinalizeError(f"{phase} records {records} != {expected[phase]}")
    for phase in ("calibration", "evaluation"):
        judge = json.loads((args.run_root / f"judge_{phase}.json").read_text(encoding="utf-8"))
        if judge.get("complete") is not True or len(judge.get("records", {})) != counts[phase]["records"]:
            raise FinalizeError(f"{phase} judge artifact is incomplete")
    receipt = json.loads((args.run_root / "alpha_receipt.json").read_text(encoding="utf-8"))
    if receipt.get("status") != "sealed" or len(receipt.get("chosen_alpha", {})) != 10:
        raise FinalizeError("alpha receipt is incomplete")
    files = {}
    for path in sorted(args.run_root.rglob("*")):
        if not path.is_file() or path.name in {"COMPLETE.json", "RUN_STATUS.json"} or path.suffix == ".tmp":
            continue
        files[path.relative_to(args.run_root).as_posix()] = {
            "bytes": path.stat().st_size, "sha256": file_sha256(path),
        }
    payload = {
        "schema": "axb44-complete/v1", "status": "complete",
        "campaign_id": bundle["campaign"]["campaign_id"],
        "review_payload_sha256": bundle["review_payload_sha256"],
        "model_key": args.model_key, "network_model_downloads": 0,
        "counts": counts, "alpha_receipt_sha256": file_sha256(args.run_root / "alpha_receipt.json"),
        "files": files,
    }
    atomic_json(args.run_root / "COMPLETE.json", payload)
    print(f"COMPLETE: {args.model_key} {counts}")


if __name__ == "__main__":
    try:
        main()
    except FinalizeError as exc:
        raise SystemExit(f"finalization failed: {exc}") from exc
