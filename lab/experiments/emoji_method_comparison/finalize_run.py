"""Write a hash manifest only after one model's full run validates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import time

from experiments.emoji_method_comparison.analysis import load_results
from experiments.emoji_method_comparison.harness import (
    HarnessError,
    expected_heldout_counts,
    load_bundle,
    sha256_file,
)
from experiments.emoji_method_comparison.method_lock import PRIMARY_IDS
from nla_lab.study import validate_alpha_receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma3-12b"), required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()
    required = (
        args.run_root / "model_verification.json",
        args.run_root / "build" / "directions.npz",
        args.run_root / "build" / "direction_build_report.json",
        args.run_root / "alpha_receipt.json",
        args.run_root / "heldout_summary.json",
        args.run_root / "instruction_quality_blinded.csv",
        args.run_root / "instruction_quality_key.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise HarnessError(f"finalization is missing artifacts: {missing}")
    verification = json.loads(required[0].read_text(encoding="utf-8"))
    if verification.get("network_downloads") != 0 or verification.get("model_key") != args.model_key:
        raise HarnessError("model verification does not prove a zero-download model setup")
    receipt = json.loads((args.run_root / "alpha_receipt.json").read_text(encoding="utf-8"))
    validate_alpha_receipt(receipt, require_frozen=True)
    bundle = load_bundle()
    if receipt.get("model_key") != args.model_key:
        raise HarnessError("alpha receipt model differs from finalization model")
    if receipt.get("lock_bundle_sha256") != bundle["lock_bundle_sha256"]:
        raise HarnessError("alpha receipt lock differs from finalization code/data")
    canary = load_results(args.run_root / "results" / "canary")
    calibration = load_results(args.run_root / "results" / "calibration")
    heldout = load_results(args.run_root / "results" / "heldout")
    alpha_count = len(
        bundle["lock"]["model_matrix"][args.model_key]["calibration_alphas"]
    )
    expected_calibration = len(PRIMARY_IDS) * 6 * 3 * alpha_count
    expected_heldout = expected_heldout_counts(bundle, receipt)
    counts = {
        "canary_result_files": len(canary),
        "canary_records": sum(len(row["records"]) for row in canary),
        "calibration_result_files": len(calibration),
        "calibration_records": sum(len(row["records"]) for row in calibration),
        "heldout_result_files": len(heldout),
        "heldout_records": sum(len(row["records"]) for row in heldout),
    }
    expected = {
        "canary_result_files": 1, "canary_records": 2,
        "calibration_result_files": len(PRIMARY_IDS),
        "calibration_records": expected_calibration,
        "heldout_result_files": expected_heldout["result_files"],
        "heldout_records": expected_heldout["records"],
    }
    if counts != expected:
        raise HarnessError(f"final record counts {counts} != {expected}")
    files = []
    for path in sorted(args.run_root.rglob("*")):
        if path.is_file() and path.name not in {"COMPLETE.json", "RUN_STATUS.json"}:
            files.append({
                "path": path.relative_to(args.run_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    payload = {
        "schema": "emoji-method-comparison-complete-run/v1",
        "status": "complete",
        "model_key": args.model_key,
        "completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "network_model_downloads": 0,
        "counts": counts,
        "file_count": len(files),
        "total_bytes": sum(row["bytes"] for row in files),
        "files": files,
    }
    output = args.run_root / "COMPLETE.json"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent,
        prefix=output.name + ".", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
        temporary = Path(handle.name)
    temporary.replace(output)
    print(f"complete {args.model_key}: {counts}, files={len(files)}")


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"run finalization failed: {exc}") from exc
