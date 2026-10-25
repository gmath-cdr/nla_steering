"""Validate a pulled-back direction archive and its complete build receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.emoji_method_comparison.build_directions import (
    ARCHIVE_NAME,
    CODE_PATHS,
    REPORT_NAME,
    REPO,
    validate_build_report,
)
from experiments.emoji_method_comparison.freeze_data import (
    CAA_PATH,
    CALIBRATION_PATH,
    load_yaml,
    validate_caa_manifest,
    validate_calibration_bank,
)
from experiments.emoji_method_comparison.method_lock import load_prompt_bank
from nla_lab.study import canonical_text_file_sha256


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("--model-key", choices=("qwen", "gemma3-12b"))
    args = parser.parse_args()
    archive = args.artifact_dir / ARCHIVE_NAME
    report_path = args.artifact_dir / REPORT_NAME
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_build_report(report, archive)
    if args.model_key and report["runtime"]["model_key"] != args.model_key:
        raise ValueError(
            f"expected {args.model_key}, got {report['runtime']['model_key']}"
        )

    heldout = load_prompt_bank()
    calibration = load_yaml(CALIBRATION_PATH)
    caa = load_yaml(CAA_PATH)
    validate_calibration_bank(calibration, heldout=heldout)
    validate_caa_manifest(caa, calibration=calibration, heldout=heldout)
    live = {
        "methods.lock.yaml": canonical_text_file_sha256(
            CALIBRATION_PATH.with_name("methods.lock.yaml")
        ),
        "prompts.calibration.yaml": canonical_text_file_sha256(CALIBRATION_PATH),
        "prompts.heldout.yaml": canonical_text_file_sha256(
            CALIBRATION_PATH.with_name("prompts.heldout.yaml")
        ),
        "caa.construction.yaml": canonical_text_file_sha256(CAA_PATH),
    }
    if report["lock_files"] != live:
        raise ValueError("pulled build receipt does not match the live frozen inputs")
    live_code = {
        path.resolve().relative_to(REPO.resolve()).as_posix():
        canonical_text_file_sha256(path)
        for path in CODE_PATHS
    }
    if report.get("code_sha256") != live_code:
        raise ValueError("pulled build receipt does not match the live builder code")
    print(
        f"valid {report['schema']} model={report['runtime']['model_key']} "
        f"archive={report['archive_sha256']} "
        f"receipt={report['receipt_sha256']}"
    )
    for name, receipt in report["directions"].items():
        print(f"{name}: unit_sha256={receipt['unit_direction_sha256']}")


if __name__ == "__main__":
    main()
