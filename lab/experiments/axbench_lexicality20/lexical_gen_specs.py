"""Materialize only missing held-out cells for lexical-alpha sensitivity.

The full sensitivity dataset is a hash-addressed union of authenticated cells
from the original H-tuned campaign and newly generated cells at lexical-tuned
alphas.  This builder never overwrites the original run and refuses partial or
ambiguous reuse.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from experiments.axbench_lexicality20.gen_specs import (
    REPO,
    all_prompt_entries,
    baseline_specs,
    common,
    method,
    prompt_entry,
    vector_spec,
    write_specs,
)
from experiments.axbench_lexicality20.lexical_select_alpha import (
    ANALYSIS_ID,
    EXPECTED_CAMPAIGN_ID,
    EXPECTED_REVIEW_SHA256,
    EXPECTED_SOURCE,
    TREATMENTS,
    LexicalSelectionError,
    _load_json,
    _validate_source_root,
    atomic_json,
    file_sha256,
    validate_receipt,
)
from experiments.axbench_lexicality20.protocol import (
    assignments,
    canonical_sha256,
    load_bundle,
)
from nla_lab.study import canonical_text_file_sha256, seal_spec, validate_spec_integrity


MANIFEST_SCHEMA = "axb44-lexical-alpha-materialization/v1"
CONTROL_SEEDS = (17, 42, 101)
MODE_OF = {"diffmean": "all", "caa": "all", "nla": "last_and_decode"}
RANDOM_ARM = re.compile(r"^random_(17|42|101)_(all|last_and_decode)_a")
HERE = Path(__file__).resolve().parent
SENSITIVITY_CODE_PATHS = (
    HERE / "lexical_select_alpha.py",
    HERE / "lexical_gen_specs.py",
)


class DeltaSpecError(ValueError):
    """Raised when a delta materialization would break provenance."""


def _alpha_key(value: float) -> str:
    return format(float(value), ".10g")


def _cell_payload(
    *,
    model_key: str,
    kind: str,
    concept_id: int,
    prompt_id: str,
    generation_seed: int,
    alpha: float,
    mode: str,
    arm_id: str | None = None,
    control_seed: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model_key": model_key,
        "kind": kind,
        "concept_id": int(concept_id),
        "prompt_id": str(prompt_id),
        "generation_seed": int(generation_seed),
        "alpha": float(alpha),
        "mode": str(mode),
    }
    if kind == "random":
        if control_seed not in CONTROL_SEEDS or arm_id is not None:
            raise DeltaSpecError("invalid random cell identity")
        payload["control_seed"] = int(control_seed)
    else:
        if arm_id is None or control_seed is not None:
            raise DeltaSpecError("invalid named-arm cell identity")
        payload["arm_id"] = str(arm_id)
    return payload


def _cell_id(payload: dict[str, Any]) -> str:
    return canonical_sha256({"namespace": "axb44-lexical-alpha-cell/v1", **payload})


def _lookup_key(payload: dict[str, Any]) -> tuple[Any, ...]:
    identity = payload.get("arm_id") if payload["kind"] != "random" else payload.get("control_seed")
    return (
        payload["kind"],
        identity,
        int(payload["concept_id"]),
        str(payload["prompt_id"]),
        int(payload["generation_seed"]),
        _alpha_key(float(payload["alpha"])),
        str(payload["mode"]),
    )


def _source_reference(
    *,
    cell: dict[str, Any],
    relative: str,
    result_sha256: str,
    spec_sha256: str,
    record_index: int,
) -> dict[str, Any]:
    return {
        "cell_id": _cell_id(cell),
        "cell": cell,
        "source": "original_h_tuned_campaign",
        "result_file": relative,
        "result_sha256": result_sha256,
        "spec_sha256": spec_sha256,
        "record_index": int(record_index),
    }


def _load_source_evaluation(
    run_root: Path,
    model_key: str,
    complete: dict[str, Any],
) -> tuple[
    dict[tuple[Any, ...], dict[str, Any]],
    str,
    dict[tuple[int, str, int], str],
]:
    complete_files = complete.get("files", {})
    if not isinstance(complete_files, dict):
        raise DeltaSpecError("source COMPLETE files mapping is absent")
    runtime_model = EXPECTED_SOURCE[model_key]["runtime_model_key"]
    index: dict[tuple[Any, ...], dict[str, Any]] = {}
    baseline_hashes: set[str] = set()
    random_direction_hashes: dict[tuple[int, str, int], str] = {}
    record_count = 0
    for path in sorted((run_root / "results" / "evaluation").glob("*.json")):
        relative = path.relative_to(run_root).as_posix()
        registered = complete_files.get(relative)
        if not isinstance(registered, dict):
            raise DeltaSpecError(f"unregistered evaluation result: {relative}")
        result_sha256 = file_sha256(path)
        if result_sha256 != registered.get("sha256"):
            raise DeltaSpecError(f"source evaluation result hash mismatch: {relative}")
        payload = _load_json(path)
        spec = payload.get("spec")
        if not isinstance(spec, dict):
            raise DeltaSpecError(f"source evaluation result has no spec: {relative}")
        validate_spec_integrity(spec, required=True)
        study = spec.get("study", {})
        if (
            study.get("schema") != "axb44-run/v1"
            or study.get("phase") != "evaluation"
            or study.get("campaign_id") != EXPECTED_CAMPAIGN_ID
            or study.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
            or payload.get("runtime", {}).get("model_key") != runtime_model
        ):
            raise DeltaSpecError(f"source evaluation identity mismatch: {relative}")
        records = payload.get("records", [])
        if not isinstance(records, list) or len(records) != int(spec.get("expected_records", -1)):
            raise DeltaSpecError(f"source evaluation cardinality mismatch: {relative}")
        arm_id = str(study.get("arm_id"))
        mode = str(spec.get("mode"))
        random_match = RANDOM_ARM.match(arm_id)
        if arm_id == "unsteered":
            baseline_hashes.add(str(spec["spec_integrity"]["sha256"]))
        for record_index, record in enumerate(records):
            record_count += 1
            if random_match:
                direction_integrity = payload.get("direction_integrity", {})
                unit_hash = direction_integrity.get("unit_direction_sha256")
                if (
                    direction_integrity.get("schema") != "steering-tensor-f32-le/v1"
                    or not isinstance(unit_hash, str)
                    or len(unit_hash) != 64
                ):
                    raise DeltaSpecError(
                        f"source random direction receipt is invalid: {relative}"
                    )
                cell = _cell_payload(
                    model_key=model_key,
                    kind="random",
                    concept_id=int(record["concept_id"]),
                    prompt_id=str(record["prompt_id"]),
                    generation_seed=int(record["sample"]),
                    alpha=float(record["alpha"]),
                    mode=mode,
                    control_seed=int(random_match.group(1)),
                )
                direction_key = (
                    int(record["concept_id"]),
                    str(record["prompt_id"]),
                    int(random_match.group(1)),
                )
                prior_hash = random_direction_hashes.setdefault(direction_key, unit_hash)
                if prior_hash != unit_hash:
                    raise DeltaSpecError(
                        f"random direction hash drift across matched cells: {direction_key}"
                    )
            else:
                if arm_id not in {*TREATMENTS, "unsteered", "axbench_prompt_steering"}:
                    raise DeltaSpecError(f"unknown source arm {arm_id!r}")
                cell = _cell_payload(
                    model_key=model_key,
                    kind="named",
                    concept_id=int(record["concept_id"]),
                    prompt_id=str(record["prompt_id"]),
                    generation_seed=int(record["sample"]),
                    alpha=float(record["alpha"]),
                    mode=mode,
                    arm_id=arm_id,
                )
            key = _lookup_key(cell)
            if key in index:
                raise DeltaSpecError(f"duplicate source evaluation cell: {key}")
            index[key] = _source_reference(
                cell=cell,
                relative=relative,
                result_sha256=result_sha256,
                spec_sha256=str(spec["spec_integrity"]["sha256"]),
                record_index=record_index,
            )
    if record_count != 1072 or len(index) != 1072:
        raise DeltaSpecError("source evaluation is not the exact 1,072-cell administration")
    if len(baseline_hashes) != 1:
        raise DeltaSpecError("source evaluation has an ambiguous baseline spec")
    if len(random_direction_hashes) != 120:
        raise DeltaSpecError(
            f"source has {len(random_direction_hashes)} random bases, expected 120"
        )
    return index, next(iter(baseline_hashes)), random_direction_hashes


def _verified_source_file(
    run_root: Path,
    complete: dict[str, Any],
    relative: str,
) -> tuple[Path, str]:
    path = run_root / relative
    registered = complete.get("files", {}).get(relative)
    if not isinstance(registered, dict):
        raise DeltaSpecError(f"source file is not registered: {relative}")
    observed = file_sha256(path)
    if observed != registered.get("sha256"):
        raise DeltaSpecError(f"source file hash mismatch: {relative}")
    return path, observed


def _mark_sensitivity(spec: dict[str, Any], receipt_sha256: str) -> dict[str, Any]:
    output = deepcopy(spec)
    output.pop("spec_integrity", None)
    output["name"] = output["name"].replace("axb44-", "axb44-lexical-alpha-v1-", 1)
    output["study"] = {
        **output["study"],
        "analysis_id": ANALYSIS_ID,
        "evidential_status": "post_hoc_sensitivity",
        "lexical_alpha_receipt_sha256": receipt_sha256,
    }
    role = output["design"]["arm_role"]
    output["design"] = {
        "schema": "steering-study/v1",
        "intent": "exploratory",
        "preregistered": False,
        "arm_role": role,
        "notes": [
            "Post-hoc sensitivity: alpha selected on frozen calibration lexical hits.",
            "Original H-tuned held-out analysis remains primary and unchanged.",
        ],
    }
    code_hashes = dict(output["expected_code_sha256"])
    for path in SENSITIVITY_CODE_PATHS:
        code_hashes[path.relative_to(REPO).as_posix()] = canonical_text_file_sha256(path)
    output["expected_code_sha256"] = code_hashes
    return seal_spec(output)


def _generated_references(spec: dict[str, Any], model_key: str) -> list[dict[str, Any]]:
    random_match = RANDOM_ARM.match(str(spec["study"]["arm_id"]))
    references: list[dict[str, Any]] = []
    for prompt in spec["prompts"]:
        for alpha in spec["alphas"]:
            for generation_seed in prompt["generation_seeds"]:
                if random_match:
                    cell = _cell_payload(
                        model_key=model_key,
                        kind="random",
                        concept_id=int(prompt["concept_id"]),
                        prompt_id=str(prompt["id"]),
                        generation_seed=int(generation_seed),
                        alpha=float(alpha),
                        mode=str(spec["mode"]),
                        control_seed=int(random_match.group(1)),
                    )
                else:
                    cell = _cell_payload(
                        model_key=model_key,
                        kind="named",
                        concept_id=int(prompt["concept_id"]),
                        prompt_id=str(prompt["id"]),
                        generation_seed=int(generation_seed),
                        alpha=float(alpha),
                        mode=str(spec["mode"]),
                        arm_id=str(spec["study"]["arm_id"]),
                    )
                references.append(
                    {
                        "cell_id": _cell_id(cell),
                        "cell": cell,
                        "source": "new_lexical_alpha_generation",
                        "spec_name": spec["name"],
                        "spec_sha256": spec["spec_integrity"]["sha256"],
                    }
                )
    if len(references) != int(spec["expected_records"]):
        raise DeltaSpecError(f"generated cell count mismatch in spec {spec['name']}")
    return references


def _source_or_generated(
    cell: dict[str, Any],
    source_index: dict[tuple[Any, ...], dict[str, Any]],
    generated_index: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, Any]:
    key = _lookup_key(cell)
    in_source = key in source_index
    in_generated = key in generated_index
    if in_source == in_generated:
        label = "both reused and generated" if in_source else "neither reused nor generated"
        raise DeltaSpecError(f"cell is {label}: {key}")
    return source_index[key] if in_source else generated_index[key]


def build_materialization(
    *,
    model_key: str,
    run_root: Path,
    receipt_path: Path,
    output_dir: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    bundle = load_bundle()
    receipt = _load_json(receipt_path)
    validate_receipt(receipt, model_key=model_key)
    receipt_sha256 = file_sha256(receipt_path)
    complete, original_receipt = _validate_source_root(run_root, model_key)
    if receipt["source"]["run_complete_sha256"] != file_sha256(run_root / "COMPLETE.json"):
        raise DeltaSpecError("lexical receipt points to another source run")
    if receipt["source"]["original_h_alpha_receipt_sha256"] != file_sha256(
        run_root / "alpha_receipt.json"
    ):
        raise DeltaSpecError("lexical receipt points to another H-tuned alpha receipt")

    verification_path, verification_sha256 = _verified_source_file(
        run_root, complete, "model_verification.json"
    )
    report_path, report_sha256 = _verified_source_file(
        run_root, complete, "build/direction_build_report.json"
    )
    _verified_source_file(run_root, complete, "build/directions.npz")
    verification = _load_json(verification_path)
    report = _load_json(report_path)
    if (
        verification.get("model_key") != model_key
        or report.get("model_key") != model_key
        or verification.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
        or report.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
        or verification.get("network_model_downloads") != 0
    ):
        raise DeltaSpecError("source model/direction receipt identity mismatch")

    source_index, source_baseline_hash, random_direction_hashes = _load_source_evaluation(
        run_root, model_key, complete
    )
    recomputed_baselines, recomputed_baseline_hash = baseline_specs(
        bundle, verification, model_key=model_key, phase="evaluation"
    )
    if recomputed_baseline_hash != source_baseline_hash:
        raise DeltaSpecError("live code cannot reproduce the archived baseline spec hash")
    if len(recomputed_baselines) != 2:
        raise DeltaSpecError("baseline spec construction drifted")

    prompts_by_concept: dict[int, list[dict[str, Any]]] = {}
    prompt_lookup: dict[str, dict[str, Any]] = {}
    for concept in bundle["campaign"]["concepts"]:
        concept_id = int(concept["concept_id"])
        prompts = [
            prompt_entry(
                bundle,
                model_key=model_key,
                concept_id=concept_id,
                source=source,
                sample_count=2,
            )
            for source in assignments(bundle, concept_id=concept_id, split="evaluation")
        ]
        prompts_by_concept[concept_id] = prompts
        prompt_lookup.update({prompt["id"]: prompt for prompt in prompts})

    specs: list[dict[str, Any]] = []
    named_expected: list[dict[str, Any]] = []
    treatment_cells: dict[tuple[str, int, str, int], dict[str, Any]] = {}
    random_groups: dict[tuple[int, str, int, float, str], set[str]] = defaultdict(set)

    # Baseline and PromptSteering are unchanged references from the original run.
    for concept_id, prompts in prompts_by_concept.items():
        for prompt in prompts:
            for generation_seed in prompt["generation_seeds"]:
                for arm_id in ("unsteered", "axbench_prompt_steering"):
                    named_expected.append(
                        _cell_payload(
                            model_key=model_key,
                            kind="named",
                            concept_id=concept_id,
                            prompt_id=prompt["id"],
                            generation_seed=generation_seed,
                            alpha=0.0,
                            mode="all",
                            arm_id=arm_id,
                        )
                    )

    for concept in bundle["campaign"]["concepts"]:
        concept_id = int(concept["concept_id"])
        prompts = prompts_by_concept[concept_id]
        choices = receipt["chosen_alpha"][str(concept_id)]
        for arm_id in ("diffmean", "caa"):
            alpha = float(choices[arm_id])
            original_alpha = float(original_receipt["chosen_alpha"][str(concept_id)][arm_id])
            if alpha != original_alpha:
                spec = vector_spec(
                    bundle,
                    verification,
                    report,
                    model_key=model_key,
                    phase="evaluation",
                    arm_id=arm_id,
                    concept_id=concept_id,
                    prompts=prompts,
                    alphas=[alpha],
                    member_key=f"c{concept_id}.{arm_id}",
                    mode="all",
                    baseline_hash=source_baseline_hash,
                )
                specs.append(_mark_sensitivity(spec, receipt_sha256))
            for prompt in prompts:
                for generation_seed in prompt["generation_seeds"]:
                    cell = _cell_payload(
                        model_key=model_key,
                        kind="named",
                        concept_id=concept_id,
                        prompt_id=prompt["id"],
                        generation_seed=generation_seed,
                        alpha=alpha,
                        mode="all",
                        arm_id=arm_id,
                    )
                    named_expected.append(cell)
                    treatment_cells[(arm_id, concept_id, prompt["id"], generation_seed)] = cell
                    for control_seed in CONTROL_SEEDS:
                        random_groups[(concept_id, prompt["id"], control_seed, alpha, "all")].add(
                            arm_id
                        )

        arm_id = "nla"
        alpha = float(choices[arm_id])
        original_alpha = float(original_receipt["chosen_alpha"][str(concept_id)][arm_id])
        for prompt in prompts:
            if alpha != original_alpha:
                source_id = prompt["id"].split("--", 1)[1]
                spec = vector_spec(
                    bundle,
                    verification,
                    report,
                    model_key=model_key,
                    phase="evaluation",
                    arm_id=arm_id,
                    concept_id=concept_id,
                    prompts=[prompt],
                    alphas=[alpha],
                    member_key=f"c{concept_id}.{source_id}.nla",
                    mode="last_and_decode",
                    baseline_hash=source_baseline_hash,
                )
                specs.append(_mark_sensitivity(spec, receipt_sha256))
            for generation_seed in prompt["generation_seeds"]:
                cell = _cell_payload(
                    model_key=model_key,
                    kind="named",
                    concept_id=concept_id,
                    prompt_id=prompt["id"],
                    generation_seed=generation_seed,
                    alpha=alpha,
                    mode="last_and_decode",
                    arm_id=arm_id,
                )
                named_expected.append(cell)
                treatment_cells[(arm_id, concept_id, prompt["id"], generation_seed)] = cell
                for control_seed in CONTROL_SEEDS:
                    random_groups[
                        (concept_id, prompt["id"], control_seed, alpha, "last_and_decode")
                    ].add(arm_id)

    # First index all treatment specs, then generate only random groups absent
    # from the authenticated source administration.
    generated_references: list[dict[str, Any]] = []
    for spec in specs:
        generated_references.extend(_generated_references(spec, model_key))
    generated_index = {
        _lookup_key(reference["cell"]): reference for reference in generated_references
    }
    if len(generated_index) != len(generated_references):
        raise DeltaSpecError("new treatment specs contain duplicate cells")

    random_expected: list[dict[str, Any]] = []
    for (concept_id, prompt_id, control_seed, alpha, mode), matches in sorted(
        random_groups.items()
    ):
        prompt = prompt_lookup[prompt_id]
        group_cells = [
            _cell_payload(
                model_key=model_key,
                kind="random",
                concept_id=concept_id,
                prompt_id=prompt_id,
                generation_seed=generation_seed,
                alpha=alpha,
                mode=mode,
                control_seed=control_seed,
            )
            for generation_seed in prompt["generation_seeds"]
        ]
        source_presence = [_lookup_key(cell) in source_index for cell in group_cells]
        if any(source_presence) and not all(source_presence):
            raise DeltaSpecError("source has a partial matched-random prompt group")
        if not any(source_presence):
            alpha_name = str(alpha).replace(".", "p")
            arm_id = f"random_{control_seed}_{mode}_a{alpha_name}"
            spec = common(
                bundle,
                verification,
                model_key=model_key,
                name=(
                    f"axb44-{model_key}-evaluation-{arm_id}-c{concept_id}-"
                    f"{prompt_id.split('--', 1)[1]}"
                ),
                prompts=[prompt],
                arm_id=arm_id,
                phase="evaluation",
            )
            direction_seed = int(
                canonical_sha256(
                    {
                        "namespace": "axb44-random-direction/v1",
                        "model_key": model_key,
                        "concept_id": concept_id,
                        "prompt_id": prompt_id,
                        "control_seed": control_seed,
                    }
                )[:15],
                16,
            )
            spec.update(
                {
                    "alphas": [float(alpha)],
                    "mode": mode,
                    "direction": "random",
                    "direction_seed": direction_seed,
                    "expected_direction_integrity": {
                        "unit_direction_sha256": random_direction_hashes[
                            (concept_id, prompt_id, control_seed)
                        ],
                    },
                    "linked_baseline_spec_sha256": source_baseline_hash,
                    "method": method(
                        "random_isotropic",
                        notes=[
                            f"Matched to treatment arms: {sorted(matches)}",
                            f"Predeclared control identity {control_seed}",
                        ],
                    ),
                    "design": {
                        "schema": "steering-study/v1",
                        "intent": "exploratory",
                        "preregistered": False,
                        "arm_role": "negative_control",
                        "notes": [],
                    },
                    "expected_records": 2,
                }
            )
            specs.append(_mark_sensitivity(seal_spec(spec), receipt_sha256))
        random_expected.extend(group_cells)

    # Rebuild the generated index after all missing-random specs are present.
    generated_references = []
    for spec in specs:
        generated_references.extend(_generated_references(spec, model_key))
    generated_index = {
        _lookup_key(reference["cell"]): reference for reference in generated_references
    }
    if len(generated_index) != len(generated_references):
        raise DeltaSpecError("new specs contain duplicate cells")

    expected_cells = named_expected + random_expected
    expected_keys = [_lookup_key(cell) for cell in expected_cells]
    if len(expected_keys) != len(set(expected_keys)):
        raise DeltaSpecError("full sensitivity design contains duplicate cells")
    resolved = [
        _source_or_generated(cell, source_index, generated_index) for cell in expected_cells
    ]
    reused = [reference for reference in resolved if reference["source"] == "original_h_tuned_campaign"]
    generated = [
        reference for reference in resolved if reference["source"] == "new_lexical_alpha_generation"
    ]
    if len(generated) != len(generated_references):
        raise DeltaSpecError("generated specs include cells outside the full sensitivity design")

    treatment_to_random: list[dict[str, Any]] = []
    for (arm_id, concept_id, prompt_id, generation_seed), treatment in sorted(
        treatment_cells.items()
    ):
        random_ids = []
        for control_seed in CONTROL_SEEDS:
            random_cell = _cell_payload(
                model_key=model_key,
                kind="random",
                concept_id=concept_id,
                prompt_id=prompt_id,
                generation_seed=generation_seed,
                alpha=float(treatment["alpha"]),
                mode=str(treatment["mode"]),
                control_seed=control_seed,
            )
            random_ids.append(_cell_id(random_cell))
        treatment_to_random.append(
            {"treatment_cell_id": _cell_id(treatment), "random_cell_ids": random_ids}
        )

    source_cell_ids = {reference["cell_id"] for reference in reused}
    generated_cell_ids = {reference["cell_id"] for reference in generated}
    if source_cell_ids & generated_cell_ids:
        raise DeltaSpecError("a sensitivity cell is both reused and regenerated")
    if len(source_cell_ids | generated_cell_ids) != len(expected_cells):
        raise DeltaSpecError("sensitivity cell union is incomplete")

    counts = {
        "named_baseline_and_prompt_rows": 160,
        "named_treatment_rows": 240,
        "unique_random_rows": len(random_expected),
        "full_unique_analysis_rows": len(expected_cells),
        "reused_rows": len(reused),
        "new_rows": len(generated),
        "new_treatment_rows": sum(
            reference["cell"]["kind"] == "named" for reference in generated
        ),
        "new_random_rows": sum(
            reference["cell"]["kind"] == "random" for reference in generated
        ),
        "new_specs": len(specs),
        "judge_calls_if_all_new_rows_scored": len(generated) * 3,
    }
    if counts["named_baseline_and_prompt_rows"] != sum(
        cell["arm_id"] in {"unsteered", "axbench_prompt_steering"}
        for cell in named_expected
    ):
        raise DeltaSpecError("baseline/prompt cardinality drift")
    if counts["named_treatment_rows"] != sum(
        cell["arm_id"] in TREATMENTS for cell in named_expected
    ):
        raise DeltaSpecError("treatment cardinality drift")
    expected_new = {
        "qwen": {"new_rows": 528, "new_treatment_rows": 144, "new_random_rows": 384},
        "gemma": {"new_rows": 568, "new_treatment_rows": 160, "new_random_rows": 408},
    }[model_key]
    for key, value in expected_new.items():
        if counts[key] != value:
            raise DeltaSpecError(
                f"{model_key} {key} drifted: observed {counts[key]}, expected {value}"
            )

    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "status": "sealed",
        "analysis_id": ANALYSIS_ID,
        "campaign_id": EXPECTED_CAMPAIGN_ID,
        "review_payload_sha256": EXPECTED_REVIEW_SHA256,
        "model_key": model_key,
        "evidential_status": "post_hoc_sensitivity",
        "source_run": {
            "complete_sha256": file_sha256(run_root / "COMPLETE.json"),
            "original_h_alpha_receipt_sha256": file_sha256(
                run_root / "alpha_receipt.json"
            ),
            "model_verification_sha256": verification_sha256,
            "direction_build_report_sha256": report_sha256,
            "direction_archive_sha256": file_sha256(run_root / "build" / "directions.npz"),
            "random_direction_unit_sha256": {
                f"c{concept_id}|{prompt_id}|seed{control_seed}": unit_hash
                for (concept_id, prompt_id, control_seed), unit_hash in sorted(
                    random_direction_hashes.items()
                )
            },
        },
        "lexical_alpha_receipt_sha256": receipt_sha256,
        "counts": counts,
        "generated_specs": [
            {
                "name": spec["name"],
                "sha256": spec["spec_integrity"]["sha256"],
                "expected_records": spec["expected_records"],
            }
            for spec in specs
        ],
        "reused_cells": reused,
        "generated_cells": generated,
        "treatment_to_random": treatment_to_random,
        "interpretation_boundary": (
            "Lexical outcomes are evaluated at lexical-tuned alphas. The original "
            "H-tuned analysis remains primary; hit-tuned H is descriptive only."
        ),
    }
    manifest["integrity_sha256"] = canonical_sha256(manifest)

    if output_dir.exists() or manifest_path.exists():
        raise DeltaSpecError("refusing to overwrite a sensitivity spec or manifest path")
    write_specs(output_dir, specs)
    atomic_json(manifest_path, manifest)
    return manifest


def validate_manifest(
    manifest: dict[str, Any], *, model_key: str | None = None
) -> None:
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("status") != "sealed"
        or manifest.get("analysis_id") != ANALYSIS_ID
        or manifest.get("campaign_id") != EXPECTED_CAMPAIGN_ID
        or manifest.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
    ):
        raise DeltaSpecError("sensitivity materialization identity/status mismatch")
    if model_key is not None and manifest.get("model_key") != model_key:
        raise DeltaSpecError("sensitivity materialization model mismatch")
    unsigned = dict(manifest)
    observed = unsigned.pop("integrity_sha256", None)
    if observed != canonical_sha256(unsigned):
        raise DeltaSpecError("sensitivity materialization integrity mismatch")
    reused_ids = {row["cell_id"] for row in manifest.get("reused_cells", [])}
    generated_ids = {row["cell_id"] for row in manifest.get("generated_cells", [])}
    if reused_ids & generated_ids:
        raise DeltaSpecError("manifest reuses and regenerates the same cell")
    if len(reused_ids | generated_ids) != manifest["counts"]["full_unique_analysis_rows"]:
        raise DeltaSpecError("manifest full cell union is incomplete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--lexical-alpha-receipt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = build_materialization(
            model_key=args.model_key,
            run_root=args.run_root.resolve(),
            receipt_path=args.lexical_alpha_receipt.resolve(),
            output_dir=args.output_dir.resolve(),
            manifest_path=args.manifest.resolve(),
        )
        validate_manifest(manifest, model_key=args.model_key)
    except LexicalSelectionError as exc:
        raise SystemExit(f"materialization failed: {exc}") from exc
    print(
        f"sealed {args.model_key} delta: {manifest['counts']['new_specs']} specs / "
        f"{manifest['counts']['new_rows']} new rows; "
        f"{manifest['counts']['reused_rows']} reused rows"
    )


if __name__ == "__main__":
    main()
