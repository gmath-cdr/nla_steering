#!/usr/bin/env python3
"""Fail-closed verification for the four paper-facing study families."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from study_families.common import (
    ArchiveSpec,
    ArtifactError,
    DATA_ROOT,
    ROOT,
    load_json,
    verify_complete_archive,
)
from study_families.selected_panel_judges import rebuild_all as rebuild_judge_council
from study_families.wordle import rebuild_summary as rebuild_wordle


ARCHIVES = (
    ArchiveSpec(
        "outcome_blind_axb44/archives/axb44-688f4c1-qwen-complete.tar.gz",
        "4b3266e6de74951c93c093845afb5d84ac3fa5f29b16c27317218678e1124f38",
        "axb44-complete/v1",
        "qwen",
        "evaluation",
        1072,
    ),
    ArchiveSpec(
        "outcome_blind_axb44/archives/axb44-688f4c1-gemma-complete.tar.gz",
        "b706a0639399d42d192c5758e38aa9c00a136241c22f4363b5094e3d956f47cb",
        "axb44-complete/v1",
        "gemma",
        "evaluation",
        1072,
    ),
    ArchiveSpec(
        "freeform_emoji/archives/emoji-crossmodel-d6d17af-complete.tar.gz",
        "f05712c03a90bf1b972b0185a5cd17e682524dd1d465155ed42afebe7dbceb1c",
        "emoji-method-comparison-complete-run/v1",
        "qwen",
        "heldout",
        3520,
    ),
    ArchiveSpec(
        "freeform_emoji/archives/gemma3-12b-d0679f6-complete.tar.gz",
        "2dea905ce09a7badda9015e44bb78df4582da83b9f3979a1013dca02ba505308",
        "emoji-method-comparison-complete-run/v1",
        "gemma3-12b",
        "heldout",
        2720,
    ),
)


def _require_close(actual: float, expected: float, label: str, tolerance: float = 1e-12) -> None:
    if abs(float(actual) - float(expected)) > tolerance:
        raise ArtifactError(f"{label}: expected {expected}, got {actual}")


def _verify_axb44() -> dict[str, Any]:
    """Check the frozen judged analysis and strict lexical sensitivity result."""

    analysis = load_json(DATA_ROOT / "outcome_blind_axb44" / "derived" / "analysis.json")
    strict = load_json(
        DATA_ROOT / "outcome_blind_axb44" / "strict_lexical" / "strict_blind10.json"
    )
    expected_harmonic = {
        "qwen": {"diffmean": 0.4975, "nla": 0.315, "axbench_prompt_steering": 0.91625},
        "gemma": {"diffmean": 0.3775, "nla": 0.22, "axbench_prompt_steering": 1.075},
    }
    for model_key, methods in expected_harmonic.items():
        model = analysis["models"][model_key]
        if int(model["evaluation_rows"]) != 1072:
            raise ArtifactError(f"AxB44 {model_key} evaluation row count drifted")
        for method, expected in methods.items():
            actual = model["summaries"]["balanced_union"][method]["mean_axbench_harmonic"]
            _require_close(actual, expected, f"AxB44 {model_key}/{method} harmonic")

    expected_strict = {
        "qwen": {"nla": 0.225, "diffmean": 0.2625, "caa": 0.0},
        "gemma": {"nla": 0.1375, "diffmean": 0.325, "caa": 0.0125},
    }
    for model_key, methods in expected_strict.items():
        for method, expected in methods.items():
            actual = strict["models"][model_key]["arm_rates"][method]["strict"]["balanced_union"][
                "rate"
            ]
            _require_close(actual, expected, f"AxB44 {model_key}/{method} strict hit")

    return {
        "models": {
            model_key: {
                "evaluation_rows": analysis["models"][model_key]["evaluation_rows"],
                "harmonic": {
                    method: analysis["models"][model_key]["summaries"]["balanced_union"][method][
                        "mean_axbench_harmonic"
                    ]
                    for method in expected_harmonic[model_key]
                },
                "strict_hit": {
                    method: strict["models"][model_key]["arm_rates"][method]["strict"][
                        "balanced_union"
                    ]["rate"]
                    for method in expected_strict[model_key]
                },
            }
            for model_key in ("qwen", "gemma")
        }
    }


def _verify_emoji() -> dict[str, Any]:
    """Check matched-arm ordinary-prompt effects and explicit-prompt ceilings."""

    analysis = load_json(DATA_ROOT / "freeform_emoji" / "derived" / "analysis.json")
    expected = {
        "qwen": {
            "caa": (0.1, 0.0, 1.0),
            "nla_phrase": (0.4, 1 / 120, 1.0),
            "nla_glyph": (0.6, 19 / 120, 0.004019979900100499),
        },
        "gemma3-12b": {
            "caa": (0.2, 19 / 120, 0.0009699951500242498),
            "nla_phrase": (0.2, 10 / 120, 0.016114919425402875),
            "nla_glyph": (0.15, 34 / 120, 0.00005999970000149999),
        },
    }
    output: dict[str, Any] = {}
    for model_key, arms in expected.items():
        model = analysis["models"][model_key]
        output[model_key] = {"raw_records": model["raw_records"], "ordinary_effects": {}}
        for arm, (alpha, effect, holm_p) in arms.items():
            claim = model["claims"][arm]
            ordinary = claim["tracks"]["ordinary_optional"]
            _require_close(claim["locked_alpha"], alpha, f"emoji {model_key}/{arm} alpha")
            _require_close(
                ordinary["effect_vs_alpha_zero"], effect, f"emoji {model_key}/{arm} effect"
            )
            _require_close(claim["holm_adjusted_primary_p"], holm_p, f"emoji {model_key}/{arm} Holm p")
            output[model_key]["ordinary_effects"][arm] = {
                "alpha": claim["locked_alpha"],
                "effect": ordinary["effect_vs_alpha_zero"],
                "ci95": ordinary["ci95_vs_alpha_zero"],
                "holm_adjusted_p": claim["holm_adjusted_primary_p"],
            }
        prompt = model["absolute"]["explicit_prompt"]["ordinary_optional"]
        _require_close(prompt["presence"], 1.0, f"emoji {model_key} prompt ceiling")
        output[model_key]["explicit_prompt_presence"] = prompt["presence"]
        if model["instruction_quality_status"] != "pending_blinded_rating":
            raise ArtifactError(f"emoji {model_key} instruction-quality status was unexpectedly changed")
    return output


def verify_all() -> dict[str, Any]:
    """Run every offline check and return a compact machine-readable summary."""

    archive_summaries = [verify_complete_archive(spec) for spec in ARCHIVES]
    council = rebuild_judge_council()
    wordle = rebuild_wordle()
    return {
        "schema": "extended-study-verification/v1",
        "archives": [
            {
                "archive": row["archive"],
                "archive_sha256": row["archive_sha256"],
                "model_key": row["model_key"],
                "records": row["records"],
                "registered_files": row["registered_files"],
            }
            for row in archive_summaries
        ],
        "outcome_blind_axb44": _verify_axb44(),
        "selected_panel_judge_council": {
            judge: {
                "model": summary["judge_model"],
                "parse_status_counts": summary["parse_status_counts"],
                "arms": len(summary["per_arm"]),
            }
            for judge, summary in council.items()
        },
        "freeform_emoji": _verify_emoji(),
        "freeform_wordle": wordle,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional path for the rebuilt summary JSON")
    args = parser.parse_args()
    summary = verify_all()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    wordle_scope = summary["freeform_wordle"]["scope"]
    wordle_files = sum(
        int(wordle_scope[key])
        for key in ("qwen_early_files", "qwen_wordle2_files", "gemma_files")
    )
    print(
        f"Study families valid: {len(summary['archives'])} run archives, "
        f"{len(summary['selected_panel_judge_council'])} complete judge checkpoints, "
        f"AxB44/emoji headline metrics, and {wordle_files} Wordle result files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
