"""Materialize sealed calibration or held-out specs for ``axb44``."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

import yaml

from experiments.axbench_lexicality20.protocol import (
    assignments, canonical_sha256, generation_seeds, load_bundle, visible_prompt,
)
from nla_lab.study import canonical_text_file_sha256, seal_spec


HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
REPO = LAB.parent
MODEL_ENV = {"qwen": "qwen", "gemma": "gemma3-12b"}
CODE_PATHS = (
    HERE / "protocol.py", HERE / "gen_specs.py",
    LAB / "experiments" / "run.py", LAB / "experiments" / "run_specs.py",
    LAB / "nla_lab" / "session.py", LAB / "nla_lab" / "methods.py",
    LAB / "nla_lab" / "study.py", LAB / "nla_lab" / "generation.py",
)


class SpecError(ValueError):
    pass


def method(root: str, *, vector: bool = True, notes: list[str] | None = None) -> dict[str, Any]:
    fields = {
        "none": ("none", "none", "none", "not_applicable"),
        "prompt_instruction": ("authored", "none", "none", "not_applicable"),
        "diffmean_activation_contrast": ("dataset", "matched_activation_pairs", "not_applicable", "unconstrained"),
        "caa_mean_activation_contrast": ("dataset", "matched_activation_pairs", "not_applicable", "matched_scaffold"),
        "nla_av_edit_ar": ("av_generated_substrate", "edited_vs_original", "clause", "not_applicable"),
        "random_isotropic": ("not_applicable", "not_applicable", "not_applicable", "not_applicable"),
    }
    text_origin, relation, granularity, pairing = fields[root]
    return {
        "schema": "steering-taxonomy/v1",
        "root": {
            "method": root, "text_origin": text_origin,
            "contrast_relation": relation, "edit_granularity": granularity,
            "pairing_style": pairing,
        },
        "derivation": {
            "operator": "none", "output_role": "whole" if vector else "none",
            "parent_vector_key": None, "parameters": None,
        },
        "vector_view": "unit_l2" if vector else "none",
        "notes": notes or [],
    }


def design(role: str) -> dict[str, Any]:
    return {
        "schema": "steering-study/v1", "intent": "confirmatory",
        "preregistered": True, "arm_role": role, "notes": [],
    }


def prompt_entry(
    bundle: dict[str, Any], *, model_key: str, concept_id: int,
    source: dict[str, Any], sample_count: int, visible: bool = False,
) -> dict[str, Any]:
    carrier = source["text"]
    return {
        "id": f"c{concept_id}--{source['prompt_id']}",
        "text": visible_prompt(bundle, concept_id, carrier) if visible else carrier,
        "carrier_text": carrier,
        "split": source["split"], "track": source["track"],
        "concept_id": concept_id, "concept_label": source["concept_label"],
        "generation_seeds": generation_seeds(
            bundle, model_key=model_key, concept_id=concept_id,
            prompt_id=source["prompt_id"], sample_count=sample_count,
        ),
    }


def common(
    bundle: dict[str, Any], verification: dict[str, Any], *, model_key: str,
    name: str, prompts: list[dict[str, Any]], arm_id: str, phase: str,
) -> dict[str, Any]:
    profile = bundle["campaign"]["models"][model_key]
    sample_count = 1 if phase in {"canary", "calibration"} else 2
    code_hashes = {
        path.relative_to(REPO).as_posix(): canonical_text_file_sha256(path)
        for path in CODE_PATHS
    }
    return {
        "name": name,
        "prompts": prompts,
        "samples_per_prompt": sample_count,
        "temperature": float(bundle["campaign"]["generation"]["target_temperature"]),
        "max_new_tokens": int(bundle["campaign"]["generation"]["max_new_tokens"]),
        "record_target_model_perplexity": True,
        "probes": ["n_words", "unique_word_ratio", "emojis", "exclamations"],
        "study": {
            "schema": "axb44-run/v1", "campaign_id": bundle["campaign"]["campaign_id"],
            "phase": phase, "arm_id": arm_id,
            "review_payload_sha256": bundle["review_payload_sha256"],
        },
        "expected_runtime": {
            "model_key": MODEL_ENV[model_key], "layer": int(profile["layer"]),
            "hidden_width": int(profile["hidden_width"]),
        },
        "expected_base_checkpoint_marker_sha256": verification["checkpoints"]["base"]["marker_sha256"],
        "expected_code_sha256": code_hashes,
        "execution_guard": {
            "require_explicit_model_env": True, "require_clean_git": True,
            "require_spec_integrity": True, "require_external_output_dir": True,
        },
    }


def all_prompt_entries(
    bundle: dict[str, Any], *, model_key: str, split: str, visible: bool,
) -> list[dict[str, Any]]:
    count = 1 if split == "calibration" else 2
    rows = []
    for concept in bundle["campaign"]["concepts"]:
        cid = int(concept["concept_id"])
        for source in assignments(bundle, concept_id=cid, split=split):
            rows.append(prompt_entry(
                bundle, model_key=model_key, concept_id=cid, source=source,
                sample_count=count, visible=visible,
            ))
    return rows


def baseline_specs(
    bundle: dict[str, Any], verification: dict[str, Any], *, model_key: str, phase: str,
) -> tuple[list[dict[str, Any]], str]:
    baseline = common(
        bundle, verification, model_key=model_key,
        name=f"axb44-{model_key}-{phase}-unsteered",
        prompts=all_prompt_entries(bundle, model_key=model_key, split=phase, visible=False),
        arm_id="unsteered", phase=phase,
    )
    baseline.update({
        "alphas": [0.0], "method": method("none", vector=False),
        "design": design("baseline_reference"), "mode": "all",
    })
    baseline["expected_records"] = len(baseline["prompts"]) * baseline["samples_per_prompt"]
    baseline = seal_spec(baseline)
    baseline_hash = baseline["spec_integrity"]["sha256"]

    prompting = common(
        bundle, verification, model_key=model_key,
        name=f"axb44-{model_key}-{phase}-prompt",
        prompts=all_prompt_entries(bundle, model_key=model_key, split=phase, visible=True),
        arm_id="axbench_prompt_steering", phase=phase,
    )
    prompting.update({
        "alphas": [0.0], "method": method(
            "prompt_instruction", vector=False,
            notes=["Exact frozen AxBench-generated visible instruction prepended before Question:."],
        ),
        "design": design("baseline_reference"), "mode": "all",
    })
    prompting["expected_records"] = len(prompting["prompts"]) * prompting["samples_per_prompt"]
    return [baseline, seal_spec(prompting)], baseline_hash


def vector_spec(
    bundle: dict[str, Any], verification: dict[str, Any], report: dict[str, Any],
    *, model_key: str, phase: str, arm_id: str, concept_id: int,
    prompts: list[dict[str, Any]], alphas: list[float], member_key: str,
    mode: str, baseline_hash: str,
) -> dict[str, Any]:
    roots = {
        "diffmean": "diffmean_activation_contrast", "caa": "caa_mean_activation_contrast",
        "nla": "nla_av_edit_ar",
    }
    spec = common(
        bundle, verification, model_key=model_key,
        name=f"axb44-{model_key}-{phase}-{arm_id}-c{concept_id}-" +
             (prompts[0]["id"].split("--", 1)[1] if arm_id == "nla" else "fixed"),
        prompts=prompts, arm_id=arm_id, phase=phase,
    )
    spec.update({
        "alphas": [float(value) for value in alphas], "mode": mode,
        "direction_file": f"{report['archive']}#{member_key}",
        "expected_direction_integrity": {
            "unit_direction_sha256": report["members"][member_key]["unit_direction_f32_sha256"],
        },
        "linked_baseline_spec_sha256": baseline_hash,
        "method": method(roots[arm_id]), "design": design("treatment"),
    })
    spec["expected_records"] = len(prompts) * len(alphas) * spec["samples_per_prompt"]
    return seal_spec(spec)


def calibration_specs(
    bundle: dict[str, Any], verification: dict[str, Any], report: dict[str, Any],
    *, model_key: str,
) -> list[dict[str, Any]]:
    specs, baseline_hash = baseline_specs(
        bundle, verification, model_key=model_key, phase="calibration"
    )
    alphas = [float(value) for value in bundle["campaign"]["models"][model_key]["alpha_grid"]]
    for concept in bundle["campaign"]["concepts"]:
        cid = int(concept["concept_id"])
        sources = assignments(bundle, concept_id=cid, split="calibration")
        prompts = [prompt_entry(
            bundle, model_key=model_key, concept_id=cid, source=source,
            sample_count=1,
        ) for source in sources]
        for arm in ("diffmean", "caa"):
            specs.append(vector_spec(
                bundle, verification, report, model_key=model_key,
                phase="calibration", arm_id=arm, concept_id=cid,
                prompts=prompts, alphas=alphas, member_key=f"c{cid}.{arm}",
                mode="all", baseline_hash=baseline_hash,
            ))
        for prompt in prompts:
            source_id = prompt["id"].split("--", 1)[1]
            specs.append(vector_spec(
                bundle, verification, report, model_key=model_key,
                phase="calibration", arm_id="nla", concept_id=cid,
                prompts=[prompt], alphas=alphas,
                member_key=f"c{cid}.{source_id}.nla", mode="last_and_decode",
                baseline_hash=baseline_hash,
            ))
    if sum(int(spec["expected_records"]) for spec in specs) != 920:
        raise SpecError("calibration cardinality drifted from approved 920")
    return specs


def canary_specs(
    bundle: dict[str, Any], verification: dict[str, Any], report: dict[str, Any],
    *, model_key: str,
) -> list[dict[str, Any]]:
    concept = bundle["campaign"]["concepts"][0]
    cid = int(concept["concept_id"])
    source = assignments(bundle, concept_id=cid, split="calibration")[0]
    prompt = prompt_entry(
        bundle, model_key=model_key, concept_id=cid, source=source, sample_count=1
    )
    baseline = common(
        bundle, verification, model_key=model_key,
        name=f"axb44-{model_key}-canary-unsteered", prompts=[prompt],
        arm_id="unsteered_canary", phase="canary",
    )
    baseline.update({
        "alphas": [0.0], "method": method("none", vector=False),
        "design": design("baseline_reference"), "mode": "all", "expected_records": 1,
    })
    baseline = seal_spec(baseline)
    treatment = vector_spec(
        bundle, verification, report, model_key=model_key, phase="canary",
        arm_id="diffmean", concept_id=cid, prompts=[prompt],
        alphas=[float(bundle["campaign"]["models"][model_key]["alpha_grid"][0])],
        member_key=f"c{cid}.diffmean", mode="all",
        baseline_hash=baseline["spec_integrity"]["sha256"],
    )
    return [baseline, treatment]


def load_alpha_receipt(path: Path, bundle: dict[str, Any], model_key: str) -> dict[str, Any]:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "axb44-alpha-receipt/v1" or receipt.get("status") != "sealed":
        raise SpecError("evaluation requires a sealed axb44 alpha receipt")
    if receipt.get("model_key") != model_key or receipt.get("review_payload_sha256") != bundle["review_payload_sha256"]:
        raise SpecError("alpha receipt identity drift")
    copy = dict(receipt)
    observed = copy.pop("integrity_sha256", None)
    if observed != canonical_sha256(copy):
        raise SpecError("alpha receipt integrity mismatch")
    return receipt


def evaluation_specs(
    bundle: dict[str, Any], verification: dict[str, Any], report: dict[str, Any],
    receipt: dict[str, Any], *, model_key: str,
) -> list[dict[str, Any]]:
    specs, baseline_hash = baseline_specs(
        bundle, verification, model_key=model_key, phase="evaluation"
    )
    random_cells: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for concept in bundle["campaign"]["concepts"]:
        cid = int(concept["concept_id"])
        choices = receipt["chosen_alpha"][str(cid)]
        sources = assignments(bundle, concept_id=cid, split="evaluation")
        prompts = [prompt_entry(
            bundle, model_key=model_key, concept_id=cid, source=source,
            sample_count=2,
        ) for source in sources]
        for arm in ("diffmean", "caa"):
            alpha = float(choices[arm])
            specs.append(vector_spec(
                bundle, verification, report, model_key=model_key,
                phase="evaluation", arm_id=arm, concept_id=cid,
                prompts=prompts, alphas=[alpha], member_key=f"c{cid}.{arm}",
                mode="all", baseline_hash=baseline_hash,
            ))
            for prompt in prompts:
                for seed in (17, 42, 101):
                    random_cells[(cid, prompt["id"], seed, alpha, "all")].add(arm)
        alpha = float(choices["nla"])
        for prompt in prompts:
            source_id = prompt["id"].split("--", 1)[1]
            specs.append(vector_spec(
                bundle, verification, report, model_key=model_key,
                phase="evaluation", arm_id="nla", concept_id=cid,
                prompts=[prompt], alphas=[alpha],
                member_key=f"c{cid}.{source_id}.nla", mode="last_and_decode",
                baseline_hash=baseline_hash,
            ))
            for seed in (17, 42, 101):
                random_cells[(cid, prompt["id"], seed, alpha, "last_and_decode")].add("nla")
    prompt_lookup = {
        prompt["id"]: prompt
        for prompt in all_prompt_entries(
            bundle, model_key=model_key, split="evaluation", visible=False
        )
    }
    for (cid, prompt_id, control_seed, alpha, mode), matches in sorted(random_cells.items()):
        prompt = prompt_lookup[prompt_id]
        arm_id = f"random_{control_seed}_{mode}_a{str(alpha).replace('.', 'p')}"
        spec = common(
            bundle, verification, model_key=model_key,
            name=f"axb44-{model_key}-evaluation-{arm_id}-c{cid}-{prompt_id.split('--', 1)[1]}",
            prompts=[prompt], arm_id=arm_id, phase="evaluation",
        )
        direction_seed = int(canonical_sha256({
            "namespace": "axb44-random-direction/v1", "model_key": model_key,
            "concept_id": cid, "prompt_id": prompt_id, "control_seed": control_seed,
        })[:15], 16)
        spec.update({
            "alphas": [float(alpha)], "mode": mode, "direction": "random",
            "direction_seed": direction_seed,
            "linked_baseline_spec_sha256": baseline_hash,
            "method": method("random_isotropic", notes=[
                f"Matched to treatment arms: {sorted(matches)}",
                f"Predeclared control identity {control_seed}",
            ]),
            "design": design("negative_control"),
        })
        spec["expected_records"] = len(spec["prompts"]) * spec["samples_per_prompt"]
        specs.append(seal_spec(spec))
    if sum(int(spec["expected_records"]) for spec in specs) > 1120:
        raise SpecError("evaluation cardinality exceeds approved worst case")
    return specs


def write_specs(path: Path, specs: list[dict[str, Any]]) -> None:
    if path.exists():
        raise SpecError(f"refusing to overwrite existing spec directory: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=path.name + ".", dir=path.parent))
    rendered = [(temporary_dir / f"{index:03d}-{spec['name']}.yaml", yaml.safe_dump(
        spec, sort_keys=False, allow_unicode=True
    )) for index, spec in enumerate(specs)]
    try:
        for target, content in rendered:
            target.write_text(content, encoding="utf-8")
        manifest = {
            "schema": "axb44-materialization/v1", "spec_count": len(specs),
            "expected_records": sum(int(spec["expected_records"]) for spec in specs),
            "specs": [{"name": spec["name"], "sha256": spec["spec_integrity"]["sha256"]}
                      for spec in specs],
        }
        (temporary_dir / "_materialization-manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
        temporary_dir.replace(path)
    except Exception:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("canary", "calibration", "evaluation"))
    parser.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--model-verification", type=Path, required=True)
    parser.add_argument("--direction-report", type=Path, required=True)
    parser.add_argument("--alpha-receipt", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    bundle = load_bundle()
    verification = json.loads(args.model_verification.read_text(encoding="utf-8"))
    report = json.loads(args.direction_report.read_text(encoding="utf-8"))
    for value, label in ((verification, "verification"), (report, "direction report")):
        if value.get("model_key") != args.model_key or value.get("review_payload_sha256") != bundle["review_payload_sha256"]:
            raise SpecError(f"{label} identity mismatch")
    if args.phase in {"canary", "calibration"}:
        if args.alpha_receipt is not None:
            raise SpecError(f"{args.phase} must not receive an alpha receipt")
        specs = (
            canary_specs(bundle, verification, report, model_key=args.model_key)
            if args.phase == "canary" else
            calibration_specs(bundle, verification, report, model_key=args.model_key)
        )
    else:
        if args.alpha_receipt is None:
            raise SpecError("evaluation requires --alpha-receipt")
        receipt = load_alpha_receipt(args.alpha_receipt, bundle, args.model_key)
        specs = evaluation_specs(bundle, verification, report, receipt, model_key=args.model_key)
    write_specs(args.output_dir, specs)
    print(f"wrote {len(specs)} sealed specs / {sum(s['expected_records'] for s in specs)} records")


if __name__ == "__main__":
    main()
