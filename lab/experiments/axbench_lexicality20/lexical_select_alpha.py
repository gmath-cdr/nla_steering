"""Seal post-hoc lexical alpha choices from authenticated calibration outputs.

This sensitivity analysis changes only the calibration objective.  It applies
the frozen label-derived whole-word matcher to the original calibration
generations and never reads held-out response text.  The original H-tuned
receipt remains the primary receipt and is not modified.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from experiments.axbench_lexicality20.protocol import (
    assignments,
    canonical_sha256,
    generation_seeds,
    load_bundle,
)
from nla_lab.study import validate_spec_integrity


SCHEMA = "axb44-lexical-alpha-receipt/v1"
ANALYSIS_ID = "axb44-lexical-alpha-sensitivity-v1"
EXPECTED_CAMPAIGN_ID = "axb44-crossmodel-v1"
EXPECTED_REVIEW_SHA256 = (
    "0fd7075c011d08192185a8fa60603942eef7458ebadf03bbb0f5974edf85cd56"
)
EXPECTED_LEXICON_SHA256 = (
    "6ce54f08a066281cf99b5df3d061bc254b3f27aa83ae682d5649c0ad18eb98c2"
)
EXPECTED_SOURCE = {
    "qwen": {
        "runtime_model_key": "qwen",
        "complete_sha256": (
            "faa6d651ee5f3f3336bf90f47b72c34c4e435282a8706bdf58cf53210570519d"
        ),
        "alpha_receipt_sha256": (
            "b074e0e5fe31a0b48cae98695ffc58c2812af54abf2983d924fac3d5268d78d8"
        ),
    },
    "gemma": {
        "runtime_model_key": "gemma3-12b",
        "complete_sha256": (
            "d251d0d70d7abcdbb9bb3292296db30f0c59afd4c73044b14ed7a08f812d4912"
        ),
        "alpha_receipt_sha256": (
            "96a328b819c8f8f0ad5f0e3923368438b65c26b5cb9ef481c0a0f42da34f4004"
        ),
    },
}
TREATMENTS = ("diffmean", "caa", "nla")


class LexicalSelectionError(ValueError):
    """Raised when a source or calibration invariant does not hold."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
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


def exact_matches(text: str, terms: list[str]) -> list[str]:
    """Frozen matcher vendored from discovery500.py at commit 3924034.

    The implementation is intentionally identical to the matcher already used
    to score the chosen-in-advance panel.  Its source file and lexicon hashes
    are recorded in the new receipt.
    """

    if not terms:
        return []
    pattern = re.compile(
        r"(?i)(?<!\w)(?:"
        + "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
        + r")(?!\w)"
    )
    return pattern.findall(text)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LexicalSelectionError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LexicalSelectionError(f"expected a JSON object in {path}")
    return value


def _validate_source_root(run_root: Path, model_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = EXPECTED_SOURCE[model_key]
    complete_path = run_root / "COMPLETE.json"
    if file_sha256(complete_path) != expected["complete_sha256"]:
        raise LexicalSelectionError("source COMPLETE.json hash mismatch")
    complete = _load_json(complete_path)
    if (
        complete.get("schema") != "axb44-complete/v1"
        or complete.get("status") != "complete"
        or complete.get("model_key") != model_key
        or complete.get("campaign_id") != EXPECTED_CAMPAIGN_ID
        or complete.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
        or complete.get("network_model_downloads") != 0
        or complete.get("counts", {}).get("calibration", {}).get("records") != 920
        or complete.get("counts", {}).get("evaluation", {}).get("records") != 1072
    ):
        raise LexicalSelectionError("source completion identity/cardinality mismatch")

    original_receipt_path = run_root / "alpha_receipt.json"
    if file_sha256(original_receipt_path) != expected["alpha_receipt_sha256"]:
        raise LexicalSelectionError("source H-tuned alpha receipt hash mismatch")
    original_receipt = _load_json(original_receipt_path)
    if (
        original_receipt.get("schema") != "axb44-alpha-receipt/v1"
        or original_receipt.get("status") != "sealed"
        or original_receipt.get("model_key") != model_key
        or original_receipt.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
    ):
        raise LexicalSelectionError("source H-tuned alpha receipt identity mismatch")
    unsigned = dict(original_receipt)
    observed_integrity = unsigned.pop("integrity_sha256", None)
    if observed_integrity != canonical_sha256(unsigned):
        raise LexicalSelectionError("source H-tuned alpha receipt integrity mismatch")
    return complete, original_receipt


def _expected_calibration_cells(
    bundle: dict[str, Any], model_key: str
) -> set[tuple[int, str, str, int, float]]:
    grid = [float(value) for value in bundle["campaign"]["models"][model_key]["alpha_grid"]]
    cells: set[tuple[int, str, str, int, float]] = set()
    for concept in bundle["campaign"]["concepts"]:
        concept_id = int(concept["concept_id"])
        for source in assignments(bundle, concept_id=concept_id, split="calibration"):
            seed = generation_seeds(
                bundle,
                model_key=model_key,
                concept_id=concept_id,
                prompt_id=str(source["prompt_id"]),
                sample_count=1,
            )[0]
            prompt_id = f"c{concept_id}--{source['prompt_id']}"
            for arm_id in TREATMENTS:
                for alpha in grid:
                    cells.add((concept_id, arm_id, prompt_id, seed, alpha))
    return cells


def _load_calibration_rows(
    run_root: Path,
    model_key: str,
    complete: dict[str, Any],
    bundle: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    runtime_key = str(EXPECTED_SOURCE[model_key]["runtime_model_key"])
    complete_files = complete.get("files", {})
    if not isinstance(complete_files, dict):
        raise LexicalSelectionError("source COMPLETE files mapping is absent")

    rows: list[dict[str, Any]] = []
    result_hashes: dict[str, str] = {}
    calibration_dir = run_root / "results" / "calibration"
    for path in sorted(calibration_dir.glob("*.json")):
        relative = path.relative_to(run_root).as_posix()
        registered = complete_files.get(relative)
        if not isinstance(registered, dict):
            raise LexicalSelectionError(f"unregistered calibration result: {relative}")
        observed_hash = file_sha256(path)
        if observed_hash != registered.get("sha256"):
            raise LexicalSelectionError(f"calibration result hash mismatch: {relative}")
        payload = _load_json(path)
        spec = payload.get("spec")
        if not isinstance(spec, dict):
            raise LexicalSelectionError(f"calibration result has no spec: {relative}")
        validate_spec_integrity(spec, required=True)
        study = spec.get("study", {})
        arm_id = str(study.get("arm_id"))
        if (
            study.get("schema") != "axb44-run/v1"
            or study.get("phase") != "calibration"
            or study.get("campaign_id") != EXPECTED_CAMPAIGN_ID
            or study.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
        ):
            raise LexicalSelectionError(f"calibration spec identity mismatch: {relative}")
        if payload.get("runtime", {}).get("model_key") != runtime_key:
            raise LexicalSelectionError(f"calibration runtime model mismatch: {relative}")
        records = payload.get("records", [])
        if not isinstance(records, list) or len(records) != int(spec.get("expected_records", -1)):
            raise LexicalSelectionError(f"calibration result cardinality mismatch: {relative}")
        result_hashes[relative] = observed_hash
        if arm_id not in TREATMENTS:
            continue
        for record_index, record in enumerate(records):
            rows.append(
                {
                    "concept_id": int(record["concept_id"]),
                    "arm_id": arm_id,
                    "prompt_id": str(record["prompt_id"]),
                    "generation_seed": int(record["sample"]),
                    "alpha": float(record["alpha"]),
                    "text": str(record["text"]),
                    "result_file": relative,
                    "result_sha256": observed_hash,
                    "record_index": record_index,
                    "spec_sha256": str(spec["spec_integrity"]["sha256"]),
                }
            )

    expected_cells = _expected_calibration_cells(bundle, model_key)
    observed_cells = {
        (
            row["concept_id"],
            row["arm_id"],
            row["prompt_id"],
            row["generation_seed"],
            row["alpha"],
        )
        for row in rows
    }
    if len(rows) != 840 or len(observed_cells) != 840 or observed_cells != expected_cells:
        raise LexicalSelectionError("treatment calibration matrix is not the exact 840-cell design")
    return rows, result_hashes


def _load_lexicons(path: Path) -> dict[str, list[str]]:
    if file_sha256(path) != EXPECTED_LEXICON_SHA256:
        raise LexicalSelectionError("frozen lexicon file hash mismatch")
    payload = _load_json(path)
    lexicons = payload.get("lexicons")
    if not isinstance(lexicons, dict):
        raise LexicalSelectionError("frozen lexicon mapping is absent")
    output: dict[str, list[str]] = {}
    for concept_id, terms in lexicons.items():
        if not isinstance(terms, list) or not terms or not all(isinstance(term, str) for term in terms):
            raise LexicalSelectionError(f"invalid frozen lexicon for concept {concept_id}")
        output[str(concept_id)] = list(terms)
    return output


def choose(
    rows: list[dict[str, Any]],
    expected_grid: list[float],
    terms: list[str],
    matcher: Callable[[str, list[str]], list[str]] = exact_matches,
) -> tuple[float, list[dict[str, Any]], bool]:
    """Choose the highest mean exact hit, resolving every tie to lower alpha."""

    by_alpha: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_alpha[float(row["alpha"])].append(row)
    if set(by_alpha) != set(expected_grid):
        raise LexicalSelectionError(f"calibration alpha grid mismatch: {sorted(by_alpha)}")
    details: list[dict[str, Any]] = []
    for alpha in expected_grid:
        alpha_rows = by_alpha[alpha]
        if len(alpha_rows) != 4:
            raise LexicalSelectionError(f"alpha {alpha} has {len(alpha_rows)} rows, expected 4")
        scored = [bool(matcher(str(row["text"]), terms)) for row in alpha_rows]
        details.append(
            {
                "alpha": alpha,
                "n": 4,
                "hits": sum(scored),
                "mean_exact_whole_word_hit": sum(scored) / 4,
                "cell_sha256": [
                    canonical_sha256(
                        {
                            "concept_id": row["concept_id"],
                            "arm_id": row["arm_id"],
                            "prompt_id": row["prompt_id"],
                            "generation_seed": row["generation_seed"],
                            "alpha": row["alpha"],
                            "result_sha256": row["result_sha256"],
                            "record_index": row["record_index"],
                        }
                    )
                    for row in alpha_rows
                ],
            }
        )
    selected = max(
        details,
        key=lambda row: (row["mean_exact_whole_word_hit"], -row["alpha"]),
    )
    all_zero = all(row["hits"] == 0 for row in details)
    return float(selected["alpha"]), details, all_zero


def build_receipt(
    *,
    model_key: str,
    run_root: Path,
    lexicons_path: Path,
) -> dict[str, Any]:
    bundle = load_bundle()
    if (
        bundle["campaign"].get("campaign_id") != EXPECTED_CAMPAIGN_ID
        or bundle.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
    ):
        raise LexicalSelectionError("live campaign payload drifted")
    complete, original_receipt = _validate_source_root(run_root, model_key)
    calibration_rows, calibration_hashes = _load_calibration_rows(
        run_root, model_key, complete, bundle
    )
    lexicons = _load_lexicons(lexicons_path)
    grid = [float(value) for value in bundle["campaign"]["models"][model_key]["alpha_grid"]]

    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in calibration_rows:
        grouped[(int(row["concept_id"]), str(row["arm_id"]))].append(row)
    expected_groups = {
        (int(concept["concept_id"]), arm_id)
        for concept in bundle["campaign"]["concepts"]
        for arm_id in TREATMENTS
    }
    if set(grouped) != expected_groups:
        raise LexicalSelectionError("calibration method-by-concept groups are incomplete")

    chosen: dict[str, dict[str, float]] = defaultdict(dict)
    details: dict[str, dict[str, Any]] = defaultdict(dict)
    changed: list[dict[str, Any]] = []
    all_zero: list[dict[str, Any]] = []
    for (concept_id, arm_id), rows in sorted(grouped.items()):
        terms = lexicons.get(str(concept_id))
        if not terms:
            raise LexicalSelectionError(f"missing frozen terms for concept {concept_id}")
        selected, selection_details, is_all_zero = choose(rows, grid, terms)
        original = float(original_receipt["chosen_alpha"][str(concept_id)][arm_id])
        chosen[str(concept_id)][arm_id] = selected
        details[str(concept_id)][arm_id] = selection_details
        if selected != original:
            changed.append(
                {
                    "concept_id": concept_id,
                    "arm_id": arm_id,
                    "original_h_tuned_alpha": original,
                    "lexical_tuned_alpha": selected,
                }
            )
        if is_all_zero:
            all_zero.append(
                {
                    "concept_id": concept_id,
                    "arm_id": arm_id,
                    "selected_alpha": selected,
                }
            )

    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "sealed",
        "analysis_id": ANALYSIS_ID,
        "campaign_id": EXPECTED_CAMPAIGN_ID,
        "review_payload_sha256": EXPECTED_REVIEW_SHA256,
        "model_key": model_key,
        "evidential_status": "post_hoc_sensitivity",
        "selection_rule": (
            "max_mean_frozen_exact_whole_word_hit_over_4_calibration_assignments_"
            "tie_smaller_alpha/v1"
        ),
        "selection_metric_scope": "calibration_response_text_only",
        "held_out_response_text_read_during_selection": False,
        "reporting_rule": {
            "lexical": "compare_at_lexical_tuned_alphas",
            "axbench_harmonic": (
                "original_h_tuned_result_remains_primary; any hit-tuned H is descriptive_only"
            ),
            "new_confirmatory_p_values": False,
        },
        "source": {
            "run_complete_sha256": file_sha256(run_root / "COMPLETE.json"),
            "original_h_alpha_receipt_sha256": file_sha256(run_root / "alpha_receipt.json"),
            "calibration_result_file_sha256": calibration_hashes,
            "calibration_treatment_records": len(calibration_rows),
        },
        "matcher": {
            "implementation": (
                "exact matches() boundary regex from discovery500.py@3924034, "
                "also sealed in score_blind10_strict.sealed.py"
            ),
            "lexicons_sha256": file_sha256(lexicons_path),
        },
        "alpha_grid": grid,
        "chosen_alpha": dict(chosen),
        "selection_details": dict(details),
        "changed_from_original_h_tuning": changed,
        "changed_choice_count": len(changed),
        "all_zero_calibration_groups": all_zero,
        "all_zero_group_count": len(all_zero),
        "predeclared_interpretation": {
            "diffmean_leads": (
                "lexical ordering is robust to tuning objective on this 10-concept panel"
            ),
            "nla_closes_or_flips": (
                "lexical ordering is sensitive to tuning objective; do not call this an NLA "
                "win on the primary judge panel"
            ),
            "mixed_models": (
                "no common method winner; consistent with the submitted small-panel conclusion"
            ),
        },
    }
    receipt["integrity_sha256"] = canonical_sha256(receipt)
    return receipt


def validate_receipt(receipt: dict[str, Any], *, model_key: str | None = None) -> None:
    if (
        receipt.get("schema") != SCHEMA
        or receipt.get("status") != "sealed"
        or receipt.get("analysis_id") != ANALYSIS_ID
        or receipt.get("campaign_id") != EXPECTED_CAMPAIGN_ID
        or receipt.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
        or receipt.get("held_out_response_text_read_during_selection") is not False
    ):
        raise LexicalSelectionError("lexical alpha receipt identity/status mismatch")
    if model_key is not None and receipt.get("model_key") != model_key:
        raise LexicalSelectionError("lexical alpha receipt model mismatch")
    unsigned = dict(receipt)
    observed = unsigned.pop("integrity_sha256", None)
    if observed != canonical_sha256(unsigned):
        raise LexicalSelectionError("lexical alpha receipt integrity mismatch")
    chosen = receipt.get("chosen_alpha", {})
    if len(chosen) != 10 or any(set(row) != set(TREATMENTS) for row in chosen.values()):
        raise LexicalSelectionError("lexical alpha receipt choice matrix is incomplete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--lexicons", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = build_receipt(
        model_key=args.model_key,
        run_root=args.run_root.resolve(),
        lexicons_path=args.lexicons.resolve(),
    )
    validate_receipt(receipt, model_key=args.model_key)
    if args.output.exists():
        raise LexicalSelectionError(f"refusing to overwrite existing receipt: {args.output}")
    atomic_json(args.output, receipt)
    print(
        f"sealed {args.model_key}: 30 choices, "
        f"{receipt['changed_choice_count']} changed, "
        f"{receipt['all_zero_group_count']} all-zero groups -> {args.output}"
    )


if __name__ == "__main__":
    main()
