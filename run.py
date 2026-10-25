#!/usr/bin/env python3
"""Small, dependency-free entry point for common reproduction workflows."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAPER_TABLES = (
    "tab_panel_full", "tab_gemma_independent", "tab_discovery500_summary",
    "tab_confirmation30_all", "tab_unembedding_per_concept", "tab_sae_sweeps",
    "tab_reft_panel14", "tab_axbench_rescore", "tab_position", "tab_reframing",
    "tab_factual_three_arm", "tab_gemma10_quality", "tab_gemma_six",
)


def pythonpath() -> str:
    paths = [
        ROOT / "src",
        ROOT / "src/nla",
        ROOT / "src/nla_inference",
        ROOT / "src/axbench",
        ROOT / "src/jacobian_lens",
        ROOT / "src/contrast_steering/src",
    ]
    return os.pathsep.join(str(path) for path in paths)


def run(command: list[str]) -> int:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = pythonpath() + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode


def self_test() -> int:
    """Rebuild the bundled no-GPU results and check their frozen values."""
    if run([sys.executable, "scripts/verify_source_tree.py"]) != 0:
        return 1
    if run([sys.executable, "scripts/verify_paper_artifacts.py"]) != 0:
        return 1
    with tempfile.TemporaryDirectory(prefix="nla-paper-assert-") as temporary:
        if run([sys.executable, "analysis/reproduce.py", "assert", "--out", temporary]) != 0:
            return 1
    # The four studies added after the original snapshot have their own
    # fail-closed verifier because their complete archives and judge records
    # use different schemas from the earlier panel artifacts.
    if run([sys.executable, "-m", "study_families.verify"]) != 0:
        return 1

    cases = (
        ("qwen-panel14", "panel14_qwen", 224, 0.7901785714285714),
        ("gemma-panel14", "panel14_gemma", 224, 0.8482142857142857),
        ("gemma-panel10", "panel10_gemma", 160, 0.8125),
    )
    script = ROOT / "src/nla/experiments/rescore_prompting_quality.py"
    release = ROOT / "artifacts/release/prompting_quality"
    with tempfile.TemporaryDirectory(prefix="nla-repro-self-test-") as temporary:
        temporary_root = Path(temporary)
        for label, stem, expected_rows, expected_rate in cases:
            output_dir = temporary_root / label
            output_dir.mkdir()
            return_code = run([
                sys.executable,
                str(script),
                "--unsteered-results", str(release / f"{stem}_matched_panel.json"),
                "--prompting-results", str(release / f"{stem}_prompting.json"),
                "--output-results", str(output_dir / "results.json"),
                "--output-summary", str(output_dir / "summary.json"),
            ])
            if return_code != 0:
                return return_code
            summary = json.loads((output_dir / "summary.json").read_text())
            actual_rows = summary["rescored"]["n"]
            actual_rate = summary["rescored"]["quality_pass_rate"]
            if actual_rows != expected_rows or abs(actual_rate - expected_rate) > 1e-12:
                print(
                    f"FAILED {label}: got {actual_rows} rows at {actual_rate:.6f}; "
                    f"expected {expected_rows} at {expected_rate:.6f}",
                    file=sys.stderr,
                )
                return 1
            print(f"PASS {label}: {actual_rows} rows, {actual_rate:.1%} quality pass")
    print("standalone no-GPU reproduction passed")
    return 0


def compare_results() -> int:
    """Recompute archived aggregates and compare them with all frozen values."""
    with tempfile.TemporaryDirectory(prefix="nla-result-comparison-") as temporary:
        return_code = run([
            sys.executable,
            "analysis/reproduce.py",
            "assert",
            "--out",
            temporary,
        ])
    if return_code == 0:
        print("PASS: all 188 archived-result metrics match the frozen reference values")
    return return_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify", help="check that all vendored source trees are present")
    subparsers.add_parser("verify-paper", help="verify paper-result coverage and artifact checksums")
    subparsers.add_parser("verify-studies", help="verify the added AxB44, judge, emoji, and Wordle studies")
    subparsers.add_parser("self-test", help="verify and rebuild all bundled no-GPU results")
    subparsers.add_parser("compare-results", help="compare all archived aggregates with frozen references")
    subparsers.add_parser("print-pythonpath", help="print an export command for vendored imports")

    reproduce = subparsers.add_parser("reproduce", help="rebuild all archived paper tables and figures")
    reproduce.add_argument("--out-dir", type=Path, default=ROOT / "artifacts/rebuilt/paper")
    reproduce.add_argument("--tables-only", action="store_true")

    study_reproduction = subparsers.add_parser(
        "reproduce-studies", help="rebuild the added study summaries and optional figures"
    )
    study_reproduction.add_argument(
        "--out-dir", type=Path, default=ROOT / "artifacts/rebuilt/extended_studies"
    )
    study_reproduction.add_argument("--figures", action="store_true")

    study_extraction = subparsers.add_parser(
        "extract-studies", help="verify and safely expand complete AxB44 or emoji archives"
    )
    study_extraction.add_argument("family", choices=("axb44", "emoji", "all"))
    study_extraction.add_argument(
        "--out-dir", type=Path, default=ROOT / "artifacts/rebuilt/extracted_studies"
    )

    paper_table = subparsers.add_parser("paper-table", help="aggregate one paper table independently")
    paper_table.add_argument("name", choices=PAPER_TABLES)
    paper_table.add_argument("--out-dir", type=Path)

    rescore = subparsers.add_parser("rescore-prompting", help="re-score prompting quality from completion JSON")
    rescore.add_argument("--unsteered", required=True, type=Path)
    rescore.add_argument("--prompting", required=True, type=Path)
    rescore.add_argument("--out-dir", required=True, type=Path)

    baselines = subparsers.add_parser("panel-baselines", help="run a frozen GPU panel protocol")
    baselines.add_argument("--model-key", required=True, choices=("qwen", "gemma"))
    baselines.add_argument("--protocol", required=True, type=Path)
    baselines.add_argument("--output", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "verify":
        return run([sys.executable, "scripts/verify_source_tree.py"])
    if args.command == "verify-paper":
        return run([sys.executable, "scripts/verify_paper_artifacts.py"])
    if args.command == "verify-studies":
        return run([sys.executable, "-m", "study_families.verify"])
    if args.command == "self-test":
        return self_test()
    if args.command == "compare-results":
        return compare_results()
    if args.command == "print-pythonpath":
        print(f'export PYTHONPATH="{pythonpath()}:$PYTHONPATH"')
        return 0
    if args.command == "reproduce":
        mode = "tables" if args.tables_only else "all"
        command = [sys.executable, "analysis/reproduce.py", mode, "--out", str(args.out_dir)]
        if args.tables_only:
            command.append("--png")
        return run(command)
    if args.command == "reproduce-studies":
        command = [
            sys.executable,
            "-m",
            "study_families.reproduce",
            "--out-dir",
            str(args.out_dir),
        ]
        if args.figures:
            command.append("--figures")
        return run(command)
    if args.command == "extract-studies":
        return run([
            sys.executable,
            "-m",
            "study_families.extract",
            args.family,
            "--out-dir",
            str(args.out_dir),
        ])
    if args.command == "paper-table":
        output = args.out_dir or ROOT / "artifacts/rebuilt/tables" / args.name
        return run([
            sys.executable, "analysis/reproduce.py", "tables", "--table", args.name,
            "--out", str(output), "--png",
        ])
    if args.command == "rescore-prompting":
        args.out_dir.mkdir(parents=True, exist_ok=True)
        return run([
            sys.executable,
            "src/nla/experiments/rescore_prompting_quality.py",
            "--unsteered-results", str(args.unsteered),
            "--prompting-results", str(args.prompting),
            "--output-results", str(args.out_dir / "results.json"),
            "--output-summary", str(args.out_dir / "summary.json"),
        ])
    return run([
        sys.executable,
        "src/nla/experiments/run_panel_baselines.py",
        "--model-key", args.model_key,
        # The legacy runner resolves its config relative to src/nla; make the
        # public entry point accept repository-relative paths consistently.
        "--config", str(args.protocol.resolve()),
        "--output", str(args.output),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
