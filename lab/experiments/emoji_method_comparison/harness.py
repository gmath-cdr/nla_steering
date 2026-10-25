"""Fail-closed behavioral harness for the frozen cross-model emoji study."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import yaml


HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
REPO = LAB.parent
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.emoji_method_comparison.build_directions import (  # noqa: E402
    ARCHIVE_NAME,
    CODE_PATHS as BUILD_CODE_PATHS,
    REPORT_NAME,
    REPO as BUILD_REPO,
    validate_build_report,
)
from experiments.emoji_method_comparison.freeze_data import (  # noqa: E402
    CAA_PATH,
    CALIBRATION_PATH,
    load_yaml,
    validate_caa_manifest,
    validate_calibration_bank,
)
from experiments.emoji_method_comparison.method_lock import (  # noqa: E402
    LOCK_PATH,
    PRIMARY_IDS,
    PROMPT_BANK_PATH,
    PROMPT_CONTROL,
    load_method_lock,
    load_prompt_bank,
)
from nla_lab.methods import resolve_design, resolve_method  # noqa: E402
from nla_lab.study import (  # noqa: E402
    canonical_sha256,
    canonical_text_file_sha256,
    seal_spec,
    validate_alpha_receipt,
)


RUN_SCHEMA = "emoji-method-comparison-run/v1"
LOCK_BUNDLE_SCHEMA = "emoji-method-comparison-lock-bundle/v1"
TARGET_METRIC = {
    "schema": "target-metric/v1",
    "kind": "emoji_clusters",
    "minimum_presence_count": 1,
}
RANDOM_ARMS = {"random_101": 101, "random_202": 202, "random_303": 303}
ALL_HELDOUT_ARMS = (*PRIMARY_IDS, *RANDOM_ARMS, "baseline", "explicit_prompt")
RUN_MARKERS = (
    "config.json",
    "generation_config.json",
    "model.safetensors.index.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "tokenizer.model",
    "special_tokens_map.json",
    "added_tokens.json",
    "chat_template.jinja",
    "nla_meta.yaml",
)
CODE_PATHS = (
    HERE / "harness.py",
    HERE / "analysis.py",
    HERE / "gen_specs.py",
    HERE / "select_alpha.py",
    HERE / "evaluate.py",
    HERE / "verify_persistent_models.py",
    HERE / "validate_canary.py",
    HERE / "finalize_run.py",
    HERE / "run_model_end_to_end.sh",
    HERE / "method_lock.py",
    LAB / "experiments" / "run.py",
    LAB / "experiments" / "run_specs.py",
    LAB / "analysis" / "transcripts.py",
    LAB / "nla_lab" / "generation.py",
    LAB / "nla_lab" / "methods.py",
    LAB / "nla_lab" / "probes.py",
    LAB / "nla_lab" / "session.py",
    LAB / "nla_lab" / "study.py",
    LAB / "nla_lab" / "target_metrics.py",
    LAB / "nla_lab" / "vendor_nla.py",
    LAB / "tools" / "session_start.sh",
)


class HarnessError(ValueError):
    """Raised before model loading when a frozen input or receipt drifts."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def active_treatment_arms(alpha_receipt: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the ordered, calibration-safe treatment family in a sealed receipt."""
    validate_alpha_receipt(alpha_receipt, require_frozen=True)
    chosen = {str(key) for key in alpha_receipt["chosen_alpha_by_arm"]}
    excluded = {str(key) for key in alpha_receipt["treatment_exclusions"]}
    _require(not (chosen & excluded), "alpha receipt treatment partition overlaps")
    _require(
        chosen | excluded == set(PRIMARY_IDS),
        "alpha receipt does not account for every primary treatment",
    )
    _require(
        all(float(value) != 0.0 for value in alpha_receipt["chosen_alpha_by_arm"].values()),
        "held-out treatment alpha must be nonzero",
    )
    return tuple(arm_id for arm_id in PRIMARY_IDS if arm_id in chosen)


def heldout_arm_ids(alpha_receipt: Mapping[str, Any]) -> tuple[str, ...]:
    return (*active_treatment_arms(alpha_receipt), *RANDOM_ARMS, "baseline", "explicit_prompt")


def expected_heldout_counts(
    bundle: Mapping[str, Any], alpha_receipt: Mapping[str, Any]
) -> dict[str, int]:
    """Compute cardinality from the frozen receipt, including failed-arm exclusions."""
    arms = heldout_arm_ids(alpha_receipt)
    chunk_count = len(prompt_chunks(bundle))
    prompt_count = len(heldout_prompts(bundle))
    samples = int(bundle["lock"]["common_intervention"]["heldout_samples_per_prompt"])
    random_alpha_count = 1 + len(alpha_receipt["random_control_alphas"])
    records = 0
    for arm_id in arms:
        alpha_count = 2 if arm_id in PRIMARY_IDS else random_alpha_count if arm_id in RANDOM_ARMS else 1
        records += prompt_count * samples * alpha_count
    return {"result_files": len(arms) * chunk_count, "records": records}


def _code_hashes() -> dict[str, str]:
    missing = [str(path) for path in CODE_PATHS if not path.is_file()]
    _require(not missing, f"execution code paths are missing: {missing}")
    return {
        path.relative_to(REPO).as_posix(): canonical_text_file_sha256(path)
        for path in CODE_PATHS
    }


def load_bundle() -> dict[str, Any]:
    lock = load_method_lock()
    heldout = load_prompt_bank()
    calibration = load_yaml(CALIBRATION_PATH)
    caa = load_yaml(CAA_PATH)
    validate_calibration_bank(calibration, heldout=heldout)
    validate_caa_manifest(caa, calibration=calibration, heldout=heldout)
    lock_bundle = {
        "schema": LOCK_BUNDLE_SCHEMA,
        "methods_lock_sha256": canonical_text_file_sha256(LOCK_PATH),
        "calibration_bank_sha256": canonical_text_file_sha256(CALIBRATION_PATH),
        "heldout_bank_sha256": canonical_text_file_sha256(PROMPT_BANK_PATH),
        "caa_manifest_sha256": canonical_text_file_sha256(CAA_PATH),
        "execution_code_sha256": _code_hashes(),
    }
    return {
        "lock": lock,
        "heldout": heldout,
        "calibration": calibration,
        "caa": caa,
        "locks": lock_bundle,
        "lock_bundle_sha256": canonical_sha256(lock_bundle),
    }


def load_build(artifact_dir: Path, model_key: str) -> dict[str, Any]:
    report_path = artifact_dir / REPORT_NAME
    archive = artifact_dir / ARCHIVE_NAME
    _require(report_path.is_file(), f"missing direction report: {report_path}")
    _require(archive.is_file(), f"missing direction archive: {archive}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_build_report(report, archive)
    _require(report["runtime"]["model_key"] == model_key, "direction report model drifted")
    live_locks = {
        "methods.lock.yaml": canonical_text_file_sha256(LOCK_PATH),
        "prompts.calibration.yaml": canonical_text_file_sha256(CALIBRATION_PATH),
        "prompts.heldout.yaml": canonical_text_file_sha256(PROMPT_BANK_PATH),
        "caa.construction.yaml": canonical_text_file_sha256(CAA_PATH),
    }
    _require(report.get("lock_files") == live_locks, "direction build used stale frozen inputs")
    live_builder_code = {
        path.resolve().relative_to(BUILD_REPO.resolve()).as_posix():
        canonical_text_file_sha256(path)
        for path in BUILD_CODE_PATHS
    }
    _require(report.get("code_sha256") == live_builder_code, "direction build used stale builder code")
    return {
        "artifact_dir": artifact_dir.resolve(),
        "archive": archive.resolve(),
        "report_path": report_path.resolve(),
        "report": report,
        "report_file_sha256": sha256_file(report_path),
    }


def calibration_prompts(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in bundle["calibration"]["prompts"]]


def heldout_prompts(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    output = []
    for row in bundle["heldout"]["prompts"]:
        entry = dict(row)
        entry["split"] = str(row["track"])
        output.append(entry)
    return output


def prompt_chunks(bundle: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
    size = int(bundle["lock"]["common_intervention"]["heldout_chunk_size_prompts"])
    prompts = heldout_prompts(bundle)
    chunks = [prompts[index:index + size] for index in range(0, len(prompts), size)]
    _require(len(chunks) == 8 and all(len(chunk) == 5 for chunk in chunks), "held-out chunk grid drifted")
    _require(
        [row["track"] for row in chunks[5]] == ["ordinary_optional"] * 5,
        "ordinary/transfer boundary drifted",
    )
    _require(
        all(row["track"] == "formal_technical_transfer" for chunk in chunks[6:] for row in chunk),
        "transfer chunks drifted",
    )
    return chunks


def _method(arm_id: str) -> dict[str, Any]:
    if arm_id in {"baseline", "explicit_prompt"}:
        root = {
            "method": "none", "text_origin": "none",
            "contrast_relation": "none", "edit_granularity": "none",
            "pairing_style": "not_applicable",
        }
        view, role = "none", "none"
    elif arm_id in RANDOM_ARMS:
        root = {
            "method": "random_isotropic", "text_origin": "not_applicable",
            "contrast_relation": "not_applicable",
            "edit_granularity": "not_applicable",
            "pairing_style": "not_applicable",
        }
        view, role = "unit_l2", "whole"
    elif arm_id == "caa":
        root = {
            "method": "caa_mean_activation_contrast", "text_origin": "dataset",
            "contrast_relation": "matched_activation_pairs",
            "edit_granularity": "not_applicable", "pairing_style": "matched_scaffold",
        }
        view, role = "unit_l2", "whole"
    elif arm_id in {"nla_phrase", "nla_glyph"}:
        root = {
            "method": "nla_av_edit_ar", "text_origin": "av_generated_substrate",
            "contrast_relation": "edited_vs_original",
            "edit_granularity": "clause" if arm_id == "nla_phrase" else "single_site",
            "pairing_style": "not_applicable",
        }
        view, role = "unit_l2", "whole"
    elif arm_id == "jlens_word_atom":
        root = {
            "method": "j_lens_atom", "text_origin": "not_applicable",
            "contrast_relation": "not_applicable",
            "edit_granularity": "not_applicable", "pairing_style": "not_applicable",
        }
        view, role = "unit_l2", "whole"
    else:
        raise HarnessError(f"unknown arm {arm_id!r}")
    return {
        "schema": "steering-taxonomy/v1",
        "root": root,
        "derivation": {"operator": "none", "output_role": role},
        "vector_view": view,
        "notes": [
            "Frozen cross-model emoji method comparison; vectors are rebuilt independently per residual space."
        ],
    }


def _expected_markers(build: Mapping[str, Any]) -> dict[str, str]:
    markers = build["report"]["checkpoint_receipts"]["base"]["marker_sha256"]
    output = {name: markers[name] for name in RUN_MARKERS if name in markers}
    _require("config.json" in output and "tokenizer_config.json" in output, "base marker receipt is incomplete")
    return output


def _base_spec(
    bundle: Mapping[str, Any], build: Mapping[str, Any], *, model_key: str,
    arm_id: str, phase: str, prompts: list[dict[str, Any]], alphas: list[float],
    samples: int, seed_offset: int, chunk_id: str,
) -> dict[str, Any]:
    lock = bundle["lock"]
    profile = lock["model_matrix"][model_key]
    report = build["report"]
    _require(report["runtime"]["space_id"] == profile["space_id"], "build/profile space mismatch")
    name = f"emojicmp-{model_key}-{phase}-{arm_id}-{chunk_id}".replace("_", "-")
    spec: dict[str, Any] = {
        "name": name,
        "matrix_id": f"EMOJI-METHOD-COMPARISON-{model_key.upper()}-v5",
        "method": _method(arm_id),
        "design": {
            "schema": "steering-study/v1", "intent": "confirmatory",
            "preregistered": True,
            "arm_role": (
                "baseline_reference" if arm_id in {"baseline", "explicit_prompt"}
                else "negative_control" if arm_id in RANDOM_ARMS else "treatment"
            ),
            "notes": [
                "Explicit prompting is a visible upper bound, not hidden-state steering."
                if arm_id == "explicit_prompt" else
                "Primary inference is prompt-blocked and model-stratified."
            ],
        },
        "study": {
            "schema": RUN_SCHEMA,
            "study_id": lock["study_id"], "model_key": model_key,
            "phase": phase, "arm_id": arm_id, "chunk_id": chunk_id,
            "space_id": profile["space_id"], "primary_metric": "target_present",
            "locks": dict(bundle["locks"]),
            "lock_bundle_sha256": bundle["lock_bundle_sha256"],
            "direction_build_receipt_sha256": report["receipt_sha256"],
            "direction_build_report_file_sha256": build["report_file_sha256"],
        },
        "expected_runtime": {
            "model_key": model_key, "layer": int(profile["layer"]),
            "hidden_width": int(profile["hidden_width"]),
        },
        "expected_code_sha256": dict(bundle["locks"]["execution_code_sha256"]),
        "expected_base_checkpoint_marker_sha256": _expected_markers(build),
        "execution_guard": {
            "require_explicit_model_env": True, "require_clean_git": True,
            "require_spec_integrity": True, "require_external_output_dir": True,
        },
        "prompts": prompts,
        "alphas": [float(value) for value in alphas],
        "mode": "decode",
        "samples_per_prompt": int(samples),
        "seed_offset": int(seed_offset),
        "seed_scheme": "prompt_offset_v1",
        "temperature": float(lock["common_intervention"]["temperature"]),
        "max_new_tokens": int(lock["common_intervention"]["max_new_tokens"]),
        "target_metric": dict(TARGET_METRIC),
        "probes": ["emojis", "exclamations", "n_words", "unique_word_ratio"],
    }
    if arm_id in PRIMARY_IDS:
        receipt = report["directions"][arm_id]
        spec["direction_file"] = f"{build['archive'].as_posix()}#{arm_id}"
        spec["expected_direction_integrity"] = {
            "source_archive_sha256": report["archive_sha256"],
            "source_member_f32_sha256": receipt["source_member_f32_sha256"],
            "unit_direction_sha256": receipt["unit_direction_sha256"],
        }
        if arm_id == "jlens_word_atom":
            details = receipt["details"]
            spec["expected_atom_runtime"] = {
                "token": {
                    "chosen_variant": "leading_space",
                    "token_id": int(details["token"]["token_id"]),
                    "resolutions": {
                        "leading_space": {"surface": str(details["token"]["surface"])}
                    },
                },
                "unembedding_row_f32_sha256": details["unembedding_row_f32_sha256"],
                "effective_norm_response_f32_sha256": details["effective_norm_response_f32_sha256"],
            }
    elif arm_id in RANDOM_ARMS:
        spec.update(direction="random", direction_seed=RANDOM_ARMS[arm_id])
    elif arm_id == "explicit_prompt":
        for prompt in spec["prompts"]:
            original = str(prompt["text"])
            prompt["base_text_sha256"] = hashlib.sha256(original.encode("utf-8")).hexdigest()
            prompt["text"] = original + "\n\n" + PROMPT_CONTROL
            prompt["visible_target_instruction"] = True
    spec["expected_records"] = len(prompts) * len(alphas) * samples
    resolved = resolve_method(spec)
    resolve_design(spec, resolved)
    return seal_spec(spec)


def build_canary_spec(
    bundle: Mapping[str, Any], build: Mapping[str, Any], *, model_key: str
) -> dict[str, Any]:
    profile = bundle["lock"]["model_matrix"][model_key]
    prompt = calibration_prompts(bundle)[0]
    alpha = min(float(value) for value in profile["calibration_alphas"] if float(value) > 0)
    spec = _base_spec(
        bundle, build, model_key=model_key, arm_id="nla_phrase", phase="canary",
        prompts=[prompt], alphas=[0.0, alpha], samples=1, seed_offset=90000,
        chunk_id="excluded",
    )
    payload = dict(spec)
    payload.pop("spec_integrity", None)
    payload["max_new_tokens"] = 32
    payload["expected_records"] = 2
    payload["study"] = dict(payload["study"])
    payload["study"].update(excluded_from_research_corpus=True)
    return seal_spec(payload)


def build_calibration_specs(
    bundle: Mapping[str, Any], build: Mapping[str, Any], *, model_key: str
) -> list[dict[str, Any]]:
    lock = bundle["lock"]
    alphas = [float(value) for value in lock["model_matrix"][model_key]["calibration_alphas"]]
    return [
        _base_spec(
            bundle, build, model_key=model_key, arm_id=arm_id,
            phase="calibration", prompts=calibration_prompts(bundle), alphas=alphas,
            samples=int(lock["common_intervention"]["calibration_samples_per_prompt"]),
            seed_offset=int(lock["common_intervention"]["calibration_seed_offset"]),
            chunk_id="all",
        )
        for arm_id in PRIMARY_IDS
    ]


def build_heldout_specs(
    bundle: Mapping[str, Any], build: Mapping[str, Any], *, model_key: str,
    alpha_receipt: Mapping[str, Any],
) -> list[dict[str, Any]]:
    validate_alpha_receipt(alpha_receipt, require_frozen=True)
    _require(alpha_receipt.get("model_key") == model_key, "alpha receipt model drifted")
    _require(alpha_receipt.get("concept_key") == "emoji", "alpha receipt concept drifted")
    _require(alpha_receipt.get("lock_bundle_sha256") == bundle["lock_bundle_sha256"], "alpha receipt lock drifted")
    _require(
        alpha_receipt.get("base_checkpoint_marker_sha256") == _expected_markers(build),
        "alpha receipt checkpoint drifted",
    )
    chosen = {str(key): float(value) for key, value in alpha_receipt["chosen_alpha_by_arm"].items()}
    active_arms = active_treatment_arms(alpha_receipt)
    family = heldout_arm_ids(alpha_receipt)
    random_alphas = [0.0, *[float(value) for value in alpha_receipt["random_control_alphas"]]]
    lock = bundle["lock"]
    specs: list[dict[str, Any]] = []
    for chunk_index, prompts in enumerate(prompt_chunks(bundle), start=1):
        chunk_id = f"chunk{chunk_index:02d}"
        for arm_id in family:
            if arm_id in active_arms:
                alphas = [0.0, chosen[arm_id]]
            elif arm_id in RANDOM_ARMS:
                alphas = random_alphas
            else:
                alphas = [0.0]
            spec = _base_spec(
                bundle, build, model_key=model_key, arm_id=arm_id,
                phase="heldout", prompts=[dict(row) for row in prompts], alphas=alphas,
                samples=int(lock["common_intervention"]["heldout_samples_per_prompt"]),
                seed_offset=int(lock["common_intervention"]["heldout_seed_offset"])
                + (chunk_index - 1) * 5 * int(lock["common_intervention"]["heldout_samples_per_prompt"]),
                chunk_id=chunk_id,
            )
            payload = dict(spec)
            payload.pop("spec_integrity", None)
            payload["expected_package_versions"] = dict(alpha_receipt["calibration_package_versions"])
            payload["study"] = dict(payload["study"])
            payload["study"]["alpha_receipt"] = dict(alpha_receipt)
            payload["study"]["alpha_receipt_sha256"] = canonical_sha256(alpha_receipt)
            if arm_id in PRIMARY_IDS:
                calibrated = alpha_receipt["treatment_direction_integrity"][arm_id]
                if payload["expected_direction_integrity"]["unit_direction_sha256"] != calibrated["unit_direction_sha256"]:
                    raise HarnessError(f"held-out {arm_id} direction differs from calibration")
            specs.append(seal_spec(payload))
    return specs


def write_specs(specs: list[Mapping[str, Any]], output_dir: Path) -> Path:
    _require(specs, "no specs to materialize")
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for spec in specs:
        path = output_dir / f"{spec['name']}.yaml"
        temporary = path.with_suffix(".yaml.tmp")
        temporary.write_text(
            yaml.safe_dump(dict(spec), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        paths.append(path)
    manifest = {
        "schema": "emoji-method-comparison-materialization/v1",
        "spec_count": len(paths),
        "specs": [
            {
                "path": path.name,
                "sha256": canonical_text_file_sha256(path),
                "integrity": spec["spec_integrity"]["sha256"],
                "expected_records": spec["expected_records"],
            }
            for path, spec in zip(paths, specs)
        ],
    }
    manifest_path = output_dir / "_materialization-manifest.yaml"
    temporary = manifest_path.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    temporary.replace(manifest_path)
    return manifest_path
