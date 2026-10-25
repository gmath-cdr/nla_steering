"""Validation and spec materialization for the AxBench-derived pilot.

This module has no model dependency. It can validate the preregistration and
produce ordinary ``experiments/run.py`` specs, while the existing runner keeps
raw-generation provenance. Draft prompt banks fail closed unless the caller
explicitly requests screening specs.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from nla_lab.methods import resolve_design, resolve_method  # noqa: E402
from nla_lab.study import (  # noqa: E402
    canonical_text_file_sha256,
    normalize_prompts,
    seal_spec,
    validate_alpha_receipt,
)
from nla_lab.target_metrics import (  # noqa: E402
    lexical_tokens,
    score_target,
    validate_target_metric,
)


class HarnessError(ValueError):
    """Raised before any generation when a study lock is incomplete."""


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HarnessError(f"{path} must contain a YAML mapping")
    return value


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def concept_index(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = registry.get("concepts")
    if not isinstance(rows, list) or not rows:
        raise HarnessError("concept registry must contain a non-empty concepts list")
    output: dict[str, dict[str, Any]] = {}
    qualified: set[tuple[str, int]] = set()
    inventory = str(registry.get("source_inventory", ""))
    if not inventory:
        raise HarnessError("concept registry is missing source_inventory")
    for raw in rows:
        if not isinstance(raw, dict):
            raise HarnessError("every concept row must be a mapping")
        key = str(raw.get("key", ""))
        concept_id = raw.get("concept_id")
        if not key or key in output or not isinstance(concept_id, int):
            raise HarnessError(f"invalid or duplicate concept row {raw!r}")
        identity = (inventory, concept_id)
        if identity in qualified:
            raise HarnessError(f"duplicate source-qualified concept {identity!r}")
        validate_target_metric(raw.get("scorer", {}))
        output[key] = dict(raw)
        qualified.add(identity)
    return output


def validate_source_registry(
    source: Mapping[str, Any], registry: Mapping[str, Any]
) -> None:
    upstream = source.get("upstream", {})
    if upstream.get("revision") != registry.get("source_revision"):
        raise HarnessError("source and concept registry revisions disagree")
    if upstream.get("inventory") != registry.get("source_inventory"):
        raise HarnessError("source and concept registry inventories disagree")
    if not upstream.get("inventory_path") or not upstream.get("inventory_model"):
        raise HarnessError("source manifest lacks inventory path/model identity")


def _contains_forbidden(text: str, term: str) -> bool:
    if not term:
        return False
    if any(not (c.isalnum() or c in "_'\N{RIGHT SINGLE QUOTATION MARK} -") for c in term):
        return term.casefold() in text.casefold()
    term_tokens = lexical_tokens(term)
    if len(term_tokens) == 1:
        return term_tokens[0] in lexical_tokens(text)
    return " ".join(term_tokens) in " ".join(lexical_tokens(text))


def _validate_pre_output_review(prompt: Mapping[str, Any], applies: list[str]) -> None:
    receipt = prompt.get("pre_output_review")
    if not isinstance(receipt, Mapping):
        raise HarnessError(f"frozen prompt {prompt['id']} lacks pre_output_review")
    reviews = receipt.get("reviews")
    if not isinstance(reviews, list) or len(reviews) < 2:
        raise HarnessError(f"frozen prompt {prompt['id']} needs two reviews")
    reviewers: set[str] = set()
    expected = set(applies)
    for review in reviews:
        if not isinstance(review, Mapping):
            raise HarnessError(f"prompt {prompt['id']} has a malformed review")
        reviewer = str(review.get("reviewer", "")).strip()
        if not reviewer or reviewer in reviewers:
            raise HarnessError(f"prompt {prompt['id']} needs distinct named reviewers")
        reviewers.add(reviewer)
        if review.get("target_absent") is not True or review.get("behavior_optional") is not True:
            raise HarnessError(
                f"prompt {prompt['id']} review must affirm target absence/optionality"
            )
        scores = review.get("affordance")
        if not isinstance(scores, Mapping) or set(scores) != expected:
            raise HarnessError(f"prompt {prompt['id']} review scores wrong concepts")
        if any(not isinstance(value, int) or value not in {0, 1, 2} for value in scores.values()):
            raise HarnessError(f"prompt {prompt['id']} affordance scores must be 0/1/2")
    adjudication = receipt.get("adjudication")
    if not isinstance(adjudication, Mapping) or not str(
        adjudication.get("reviewer", "")
    ).strip():
        raise HarnessError(f"prompt {prompt['id']} lacks named adjudication")
    scores = adjudication.get("affordance")
    if not isinstance(scores, Mapping) or set(scores) != expected:
        raise HarnessError(f"prompt {prompt['id']} adjudication scores wrong concepts")
    if any(not isinstance(value, int) or value not in {0, 1, 2} for value in scores.values()):
        raise HarnessError(f"prompt {prompt['id']} adjudication scores must be 0/1/2")


def validate_prompt_bank(
    bank: Mapping[str, Any],
    concepts: Mapping[str, Mapping[str, Any]],
    selected: list[str],
) -> dict[str, Any]:
    if bank.get("schema") != "axbench-prompt-bank/v1":
        raise HarnessError("prompt bank schema must be axbench-prompt-bank/v1")
    prompts = normalize_prompts(bank.get("prompts"))
    frozen = bank.get("status") == "frozen"
    selected_set = set(selected)
    unknown_selected = selected_set - set(concepts)
    if unknown_selected:
        raise HarnessError(f"unknown selected concepts: {sorted(unknown_selected)}")
    by_split: Counter[tuple[str, str]] = Counter()
    seen_text: set[str] = set()
    for prompt in prompts:
        applies = prompt.get("concepts")
        if not isinstance(applies, list) or not applies:
            raise HarnessError(f"prompt {prompt['id']} needs a non-empty concepts list")
        unknown = set(map(str, applies)) - set(concepts)
        if unknown:
            raise HarnessError(f"prompt {prompt['id']} has unknown concepts {sorted(unknown)}")
        applies = list(map(str, applies))
        if frozen:
            _validate_pre_output_review(prompt, applies)
            if prompt["split"] in {"calibration", "evaluation"}:
                final_scores = prompt["pre_output_review"]["adjudication"][
                    "affordance"
                ]
                for key in applies:
                    expected_score = 2 if key == "lists" else 1
                    if final_scores[key] != expected_score:
                        raise HarnessError(
                            f"frozen primary prompt {prompt['id']} must have "
                            f"adjudicated affordance {expected_score} for {key}"
                        )
        normalized_text = " ".join(prompt["text"].split()).casefold()
        if normalized_text in seen_text:
            raise HarnessError(f"duplicate prompt text at {prompt['id']}")
        seen_text.add(normalized_text)
        for key in applies:
            if key not in selected_set:
                continue
            for forbidden in concepts[key].get("prompt_forbidden", []):
                if _contains_forbidden(prompt["text"], str(forbidden)):
                    raise HarnessError(
                        f"prompt {prompt['id']} leaks forbidden target "
                        f"{forbidden!r} for {key}"
                    )
            by_split[(key, str(prompt["split"]))] += 1
    for key in selected:
        for split in ("calibration", "evaluation", "transfer"):
            if by_split[(key, split)] == 0:
                raise HarnessError(f"concept {key} has no {split} prompts")
    if frozen:
        minimums = bank.get("policy", {}).get("minimum_prompt_blocks_per_concept")
        if not isinstance(minimums, Mapping):
            raise HarnessError("frozen prompt bank lacks machine-checked split minimums")
        for split in ("calibration", "evaluation", "transfer"):
            value = minimums.get(split)
            if not isinstance(value, int) or value < 1:
                raise HarnessError(f"frozen prompt minimum for {split} must be >= 1")
            for key in selected:
                if by_split[(key, split)] < value:
                    raise HarnessError(
                        f"concept {key}/{split} has {by_split[(key, split)]} prompts; "
                        f"frozen minimum is {value}"
                    )
    return {
        "status": str(bank.get("status", "unknown")),
        "prompt_count": len(prompts),
        "prompt_bank_sha256": canonical_sha256(bank),
        "counts": {
            key: {split: by_split[(key, split)] for split in (
                "calibration", "evaluation", "transfer"
            )}
            for key in selected
        },
    }


def validate_study_status(study: Mapping[str, Any]) -> None:
    status = study.get("status")
    if status not in {"draft_pending_prompt_freeze", "frozen"}:
        raise HarnessError(f"unknown study status {status!r}")
    if status == "frozen" and study.get("open_decisions"):
        raise HarnessError("frozen study cannot retain open_decisions")


def load_study(study_path: Path = HERE / "study.yaml") -> dict[str, Any]:
    study = load_yaml(study_path)
    if study.get("schema") != "axbench-nla-preregistration/v1":
        raise HarnessError("study schema must be axbench-nla-preregistration/v1")
    validate_study_status(study)
    base = study_path.parent
    source = load_yaml(base / str(study["source_manifest"]))
    registry = load_yaml(base / str(study["concept_registry"]))
    prompt_bank = load_yaml(base / str(study["prompt_bank"]))
    validate_source_registry(source, registry)
    concepts = concept_index(registry)
    selected = [str(key) for key in study.get("pilot_concepts", [])]
    expected_primary = {
        key: str(concepts[key].get("primary_metric", "target_rate_per_100_words"))
        for key in selected
    }
    if study.get("outcomes", {}).get("primary_metrics") != expected_primary:
        raise HarnessError("study and concept-registry primary metrics disagree")
    for key, field in expected_primary.items():
        emitted = score_target("", concepts[key]["scorer"])
        if field not in emitted:
            raise HarnessError(f"concept {key} primary metric {field!r} is not emitted")
    prompt_report = validate_prompt_bank(prompt_bank, concepts, selected)
    code_paths = (
        HERE / "harness.py",
        HERE / "analysis.py",
        HERE / "evaluate.py",
        HERE / "freeze_prompts.py",
        HERE / "build_j_atoms.py",
        HERE / "gen_specs.py",
        LAB / "experiments" / "run.py",
        LAB / "experiments" / "run_specs.py",
        LAB / "analysis" / "transcripts.py",
        LAB / "nla_lab" / "__init__.py",
        LAB / "nla_lab" / "session.py",
        LAB / "nla_lab" / "vendor_nla.py",
        LAB / "nla_lab" / "study.py",
        LAB / "nla_lab" / "target_metrics.py",
        LAB / "nla_lab" / "jlens_atoms.py",
        LAB / "nla_lab" / "methods.py",
        LAB / "nla_lab" / "probes.py",
    )
    code_hashes = {
        path.relative_to(LAB.parent).as_posix(): canonical_text_file_sha256(path)
        for path in code_paths
    }
    return {
        "study": study,
        "source": source,
        "registry": registry,
        "concepts": concepts,
        "prompt_bank": prompt_bank,
        "prompt_report": prompt_report,
        "hashes": {
            "study": canonical_sha256(study),
            "source": canonical_sha256(source),
            "concepts": canonical_sha256(registry),
            "prompts": prompt_report["prompt_bank_sha256"],
            "code": code_hashes,
        },
    }


def _root(kind: str) -> dict[str, str]:
    if kind == "none":
        return {
            "method": "none", "text_origin": "none", "contrast_relation": "none",
            "edit_granularity": "none", "pairing_style": "not_applicable",
        }
    if kind == "random_isotropic":
        method = "random_isotropic"
        origin = relation = granularity = pairing = "not_applicable"
    elif kind == "j_lens_atom":
        method = "j_lens_atom"
        origin = relation = granularity = pairing = "not_applicable"
    elif kind == "nla_ar_contrast":
        method = "nla_ar_contrast"
        origin, relation, granularity, pairing = (
            "template_instantiated", "paired_descriptions", "not_applicable",
            "matched_scaffold",
        )
    else:
        raise HarnessError(f"unknown arm kind {kind!r}")
    return {
        "method": method,
        "text_origin": origin,
        "contrast_relation": relation,
        "edit_granularity": granularity,
        "pairing_style": pairing,
    }


def method_for(kind: str) -> dict[str, Any]:
    return {
        "schema": "steering-taxonomy/v1",
        "root": _root(kind),
        "derivation": {
            "operator": "none",
            "output_role": "none" if kind == "none" else "whole",
        },
        "vector_view": "none" if kind == "none" else "unit_l2",
    }


def design_for(kind: str, phase: str, frozen: bool) -> dict[str, Any]:
    role = (
        "baseline_reference" if kind == "none" else
        "negative_control" if kind == "random_isotropic" else "treatment"
    )
    return {
        "schema": "steering-study/v1",
        "intent": "confirmatory" if phase in {"evaluation", "transfer"} and frozen else "screening",
        "preregistered": bool(frozen),
        "arm_role": role,
        "notes": [
            "Prospective AxBench-derived study; prompt and analysis locks are "
            "recorded under experiments/axbench/."
        ],
    }


def _runtime(study_bundle: Mapping[str, Any], model_key: str) -> dict[str, Any]:
    spaces = load_yaml(LAB / "registry" / "residual_spaces.yaml")["spaces"]
    configured = study_bundle["study"].get("models", {}).get(model_key)
    if not isinstance(configured, dict):
        raise HarnessError(f"study has no model configuration for {model_key!r}")
    if configured.get("enabled") is not True:
        reason = configured.get("blocked_reason", "model is not enabled")
        raise HarnessError(f"study model {model_key!r} is blocked: {reason}")
    space_id = configured.get("space_id")
    matches = [space for space in spaces if space.get("space_id") == space_id]
    if len(matches) != 1:
        raise HarnessError(f"could not uniquely resolve residual space {space_id!r}")
    space = matches[0]
    return {
        "model_key": space["model_key"],
        "layer": int(space["layer_index"]),
        "hidden_width": int(space["hidden_width"]),
        "space_id": space_id,
        "j_lens_artifact": space.get("j_lens_artifact"),
        "j_lens_sha256": space.get("j_lens_sha256"),
    }


def prompts_for(
    bank: Mapping[str, Any], concept_key: str, split: str
) -> list[dict[str, Any]]:
    output = []
    for prompt in normalize_prompts(bank["prompts"]):
        if prompt["split"] == split and concept_key in prompt.get("concepts", []):
            output.append({
                key: value for key, value in prompt.items()
                if key not in {"concepts", "rationale"}
            })
    if not output:
        raise HarnessError(f"no prompts for {concept_key}/{split}")
    return output


def _arm(study: Mapping[str, Any], arm_id: str) -> dict[str, Any]:
    matches = [dict(arm) for arm in study.get("arms", []) if arm.get("id") == arm_id]
    if len(matches) != 1:
        raise HarnessError(f"could not uniquely resolve arm {arm_id!r}")
    return matches[0]


def _validate_atom_declaration(
    concept: Mapping[str, Any], receipt: Mapping[str, Any], concept_hash: str
) -> None:
    if receipt.get("concept_registry_sha256") != concept_hash:
        raise HarnessError("atom receipt was built from a different concept registry")
    declaration = concept["direct_j_atom"]
    variants = [declaration["preferred_variant"], declaration["fallback_variant"]]
    token = receipt.get("token")
    if not isinstance(token, Mapping):
        raise HarnessError("built atom receipt lacks token resolution")
    resolutions = token.get("resolutions")
    if not isinstance(resolutions, Mapping):
        raise HarnessError("built atom receipt lacks per-variant token resolutions")
    for variant in variants:
        resolution = resolutions.get(variant)
        expected_surface = (
            declaration["token_text"] if variant == "bare"
            else " " + declaration["token_text"]
        )
        if not isinstance(resolution, Mapping) or resolution.get("surface") != expected_surface:
            raise HarnessError("atom token surface differs from the current declaration")
    eligible = [variant for variant in variants if resolutions[variant].get("single") is True]
    if not eligible or token.get("chosen_variant") != eligible[0]:
        raise HarnessError("atom receipt did not use the declared token-variant priority")
    chosen_ids = resolutions[eligible[0]].get("ids")
    if not isinstance(chosen_ids, list) or chosen_ids != [token.get("token_id")]:
        raise HarnessError("atom receipt token ID disagrees with its chosen resolution")


def build_spec(
    bundle: Mapping[str, Any],
    *,
    model_key: str,
    concept_key: str,
    arm_id: str,
    phase: str,
    atom_receipt: Mapping[str, Any] | None = None,
    alpha_receipt: Mapping[str, Any] | None = None,
    allow_draft: bool = False,
) -> dict[str, Any]:
    study = bundle["study"]
    concepts = bundle["concepts"]
    concept = concepts.get(concept_key)
    if concept is None or concept_key not in study.get("pilot_concepts", []):
        raise HarnessError(f"concept {concept_key!r} is not in the pilot")
    arm = _arm(study, arm_id)
    kind = str(arm["kind"])
    if kind == "j_lens_atom" and "direct_j_atom" not in concept:
        raise HarnessError(f"concept {concept_key} is not eligible for a pure J atom")
    prompt_status = bundle["prompt_report"]["status"]
    frozen = study.get("status") == "frozen" and prompt_status == "frozen"
    if not frozen and not allow_draft:
        raise HarnessError(
            "study/prompt bank is not frozen; use allow_draft only for screening previews"
        )
    if phase not in {"calibration", "evaluation", "transfer"}:
        raise HarnessError(f"unknown phase {phase!r}")
    if phase in {"evaluation", "transfer"} and not frozen:
        raise HarnessError("held-out specs cannot be materialized from a draft prompt bank")
    runtime = _runtime(bundle, model_key)
    inventory = str(bundle["registry"]["source_inventory"])
    target_metric = dict(concept["scorer"])
    target_metric["schema"] = "target-metric/v1"
    validate_target_metric(target_metric)

    effect_direction = str(concept.get("effect_direction", "increase"))
    if effect_direction not in {"increase", "decrease"}:
        raise HarnessError(f"invalid effect_direction for {concept_key}: {effect_direction}")
    alpha_sign = -1.0 if effect_direction == "decrease" else 1.0
    if phase in {"evaluation", "transfer"}:
        if not isinstance(alpha_receipt, Mapping):
            raise HarnessError("held-out specs require a frozen alpha receipt")
        try:
            validate_alpha_receipt(alpha_receipt, require_frozen=True)
        except ValueError as exc:
            raise HarnessError(str(exc)) from exc
        if alpha_receipt.get("model_key") != model_key or alpha_receipt.get("concept_key") != concept_key:
            raise HarnessError("alpha receipt model/concept does not match the requested spec")
        current_lock = canonical_sha256(bundle["hashes"])
        if alpha_receipt.get("lock_bundle_sha256") != current_lock:
            raise HarnessError(
                "alpha receipt was selected under a different preregistration lock"
            )

    if kind == "none":
        # The generic runner deliberately collapses a no-direction census to
        # alpha zero. Keep the materialized matrix and its cardinality lock in
        # agreement with that behavior.
        alphas = [0.0]
    elif phase == "calibration":
        magnitudes = list(study["models"][model_key]["calibration_alphas"])
        alphas = [0.0 if float(value) == 0 else alpha_sign * float(value) for value in magnitudes]
    else:
        if kind == "random_isotropic":
            chosen_values = [float(value) for value in alpha_receipt["random_control_alphas"]]
            alphas = [0.0] + [value for value in chosen_values if value != 0]
        else:
            chosen_by_arm = alpha_receipt.get("chosen_alpha_by_arm", {})
            if arm_id not in chosen_by_arm:
                raise HarnessError(f"alpha receipt has no locked value for arm {arm_id!r}")
            chosen = float(chosen_by_arm[arm_id])
            alphas = [0.0, chosen] if chosen != 0 else [0.0]

    slug = f"axbjs-{model_key}-{concept_key}-{phase}-{arm_id}".replace("_", "-")
    spec: dict[str, Any] = {
        "name": slug,
        "matrix_id": f"AXB-JSPACE-{inventory}",
        "method": method_for(kind),
        "design": design_for(kind, phase, frozen),
        "study": {
            "schema": "axbench-derived-run/v1",
            "study_id": study["study_id"],
            "phase": phase,
            "concept_key": concept_key,
            "source_identity": f"{inventory}:{concept['concept_id']}",
            "arm_id": arm_id,
            "effect_direction": effect_direction,
            "primary_metric": str(
                concept.get("primary_metric", "target_rate_per_100_words")
            ),
            "space_id": runtime["space_id"],
            "locks": dict(bundle["hashes"]),
        },
        "expected_runtime": {
            "model_key": runtime["model_key"],
            "layer": runtime["layer"],
            "hidden_width": runtime["hidden_width"],
        },
        "expected_code_sha256": dict(bundle["hashes"]["code"]),
        "execution_guard": {
            "require_explicit_model_env": True,
            "require_clean_git": bool(frozen),
            "require_spec_integrity": True,
            "require_external_output_dir": bool(frozen),
        },
        "prompts": prompts_for(bundle["prompt_bank"], concept_key, phase),
        "alphas": alphas,
        "mode": study["generation"]["mode"],
        "samples_per_prompt": int(study["generation"]["samples_per_prompt"][phase]),
        "seed_offset": int(study["generation"]["generation_seed_offset"]),
        "seed_scheme": str(study["generation"]["seed_scheme"]),
        "temperature": float(study["generation"]["temperature"]),
        "max_new_tokens": int(study["generation"]["max_new_tokens"]),
        "target_metric": target_metric,
        "monitor_metrics": {
            key: dict(concepts[key]["scorer"])
            for key in study["pilot_concepts"]
        },
        "probes": [
            "n_words", "unique_word_ratio", "list_markers", "hedge_words",
            "balanced_parentheticals",
        ],
    }
    spec["expected_records"] = (
        len(spec["prompts"]) * len(spec["alphas"]) * spec["samples_per_prompt"]
    )
    if phase in {"evaluation", "transfer"}:
        spec["expected_base_checkpoint_marker_sha256"] = dict(
            alpha_receipt["base_checkpoint_marker_sha256"]
        )
        spec["expected_package_versions"] = dict(
            alpha_receipt["calibration_package_versions"]
        )
    if kind == "random_isotropic":
        spec.update(direction="random", direction_seed=int(arm["direction_seed"]))
    elif kind == "nla_ar_contrast":
        contrast = concept.get("ar_contrast")
        if not isinstance(contrast, dict):
            raise HarnessError(f"concept {concept_key} has no reviewed AR contrast")
        spec["direction"] = {
            "positive": str(contrast["positive"]),
            "negative": str(contrast["negative"]),
        }
        if phase in {"evaluation", "transfer"}:
            integrity = alpha_receipt["treatment_direction_integrity"].get(arm_id)
            if not isinstance(integrity, Mapping):
                raise HarnessError("alpha receipt lacks the NLA treatment direction lock")
            spec["expected_direction_integrity"] = {
                "unit_direction_sha256": integrity["unit_direction_sha256"]
            }
    elif kind == "j_lens_atom":
        if not isinstance(atom_receipt, Mapping) or atom_receipt.get("status") != "built":
            raise HarnessError("pure J-atom specs require a built atom receipt")
        required = {
            "space_id", "layer", "hidden_width", "lens_sha256", "atom_formula",
            "concept_registry_sha256",
            "effective_norm_class", "effective_norm_response_f32_sha256",
            "checkpoint_marker_sha256", "unembedding_row_f32_sha256",
            "token", "archive_path", "archive_member",
            "source_archive_sha256", "source_member_f32_sha256",
            "unit_direction_sha256", "split_half_available", "split_half_atom_cosine",
            "split_lens_sha256",
        }
        missing = required - set(atom_receipt)
        if missing:
            raise HarnessError(f"atom receipt is missing provenance fields {sorted(missing)}")
        if atom_receipt.get("model_key") != model_key or atom_receipt.get("concept_key") != concept_key:
            raise HarnessError("atom receipt model/concept does not match the requested spec")
        _validate_atom_declaration(
            concept, atom_receipt, str(bundle["hashes"]["concepts"])
        )
        if (
            atom_receipt["space_id"] != runtime["space_id"]
            or int(atom_receipt["layer"]) != runtime["layer"]
            or int(atom_receipt["hidden_width"]) != runtime["hidden_width"]
            or atom_receipt["lens_sha256"] != runtime["j_lens_sha256"]
        ):
            raise HarnessError("atom receipt residual space or lens does not match the study")
        atom_gate = study["direct_j_atom_gate"]
        if atom_receipt["atom_formula"] != atom_gate["formula"]:
            raise HarnessError("atom receipt formula version does not match the study")
        if phase in {"evaluation", "transfer"} and atom_gate["heldout_requires_split_half"]:
            split_cos = atom_receipt.get("split_half_atom_cosine")
            if not atom_receipt.get("split_half_available") or split_cos is None:
                raise HarnessError("held-out pure atom requires split-half stability evidence")
            if float(split_cos) < float(atom_gate["minimum_split_half_atom_cosine"]):
                raise HarnessError("pure atom failed the preregistered split-half cosine gate")
        spec["direction_file"] = (
            f"{atom_receipt['archive_path']}#{atom_receipt['archive_member']}"
        )
        spec["expected_direction_integrity"] = {
            key: atom_receipt[key]
            for key in (
                "source_archive_sha256", "source_member_f32_sha256",
                "unit_direction_sha256",
            )
        }
        if phase in {"evaluation", "transfer"}:
            calibrated = alpha_receipt["treatment_direction_integrity"].get(arm_id)
            if not isinstance(calibrated, Mapping):
                raise HarnessError("alpha receipt lacks the direct-J direction lock")
            if (
                calibrated.get("unit_direction_sha256")
                != atom_receipt["unit_direction_sha256"]
            ):
                raise HarnessError(
                    "held-out direct atom differs from the direction calibrated for alpha"
                )
        atom_markers = dict(atom_receipt["checkpoint_marker_sha256"])
        if (
            "expected_base_checkpoint_marker_sha256" in spec
            and spec["expected_base_checkpoint_marker_sha256"] != atom_markers
        ):
            raise HarnessError(
                "direct atom and calibration receipt use different base checkpoints"
            )
        spec["expected_base_checkpoint_marker_sha256"] = atom_markers
        spec["expected_atom_runtime"] = {
            "token": dict(atom_receipt["token"]),
            "unembedding_row_f32_sha256": atom_receipt["unembedding_row_f32_sha256"],
            "effective_norm_response_f32_sha256": atom_receipt[
                "effective_norm_response_f32_sha256"
            ],
        }
        spec["study"]["atom_receipt_sha256"] = canonical_sha256(atom_receipt)
        spec["study"]["atom_provenance"] = dict(atom_receipt)
    if alpha_receipt is not None:
        spec["study"]["alpha_receipt_sha256"] = canonical_sha256(alpha_receipt)
        spec["study"]["alpha_receipt"] = dict(alpha_receipt)

    # Exercise the same controlled-taxonomy validation as the real runner.
    method = resolve_method(spec)
    resolve_design(spec, method)
    return seal_spec(spec)
