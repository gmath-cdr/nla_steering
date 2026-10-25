"""Fail-closed helpers for the prospective emoji replication study."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import sys
from typing import Any
import zipfile

import numpy as np
import yaml


HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
REPO = LAB.parent
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

HISTORICAL_PROMPTS = {
    "Tell me about your day.",
    "What should I cook for dinner tonight?",
    "Describe the weather in autumn.",
    "Give me tips for staying focused while studying.",
    "Recommend a weekend activity.",
}
CONSTRUCTION_PROMPT = "Tell me about your day."


class HarnessError(ValueError):
    """Raised before model loading when a study lock is inconsistent."""


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HarnessError(f"{path.name} must contain a mapping")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tensor_f32_sha256(array: np.ndarray) -> str:
    value = np.asarray(array, dtype="<f4").reshape(-1)
    digest = hashlib.sha256()
    digest.update(b"steering-tensor-f32-le/v1\0")
    digest.update(json.dumps(list(value.shape), separators=(",", ":")).encode())
    digest.update(b"\0")
    digest.update(value.tobytes(order="C"))
    return digest.hexdigest()


def _code_hashes() -> dict[str, str]:
    from nla_lab.study import canonical_text_file_sha256

    paths = (
        HERE / "harness.py",
        HERE / "gen_specs.py",
        HERE / "freeze_prompts.py",
        LAB / "experiments" / "run.py",
        LAB / "experiments" / "run_specs.py",
        LAB / "analysis" / "transcripts.py",
        LAB / "nla_lab" / "__init__.py",
        LAB / "nla_lab" / "generation.py",
        LAB / "nla_lab" / "methods.py",
        LAB / "nla_lab" / "probes.py",
        LAB / "nla_lab" / "session.py",
        LAB / "nla_lab" / "study.py",
        LAB / "nla_lab" / "target_metrics.py",
        LAB / "nla_lab" / "vendor_nla.py",
    )
    return {
        path.relative_to(REPO).as_posix(): canonical_text_file_sha256(path)
        for path in paths
    }


def _validate_pre_output_review(prompt: Mapping[str, Any]) -> None:
    receipt = prompt.get("pre_output_review")
    if not isinstance(receipt, Mapping):
        raise HarnessError(f"frozen prompt {prompt['id']} lacks its review receipt")
    reviews = receipt.get("reviews")
    if not isinstance(reviews, list) or len(reviews) != 2:
        raise HarnessError(f"frozen prompt {prompt['id']} needs exactly two reviews")
    names: set[str] = set()
    for review in reviews:
        if not isinstance(review, Mapping):
            raise HarnessError(f"prompt {prompt['id']} has a malformed review")
        name = str(review.get("reviewer", "")).strip()
        if not name or name in names:
            raise HarnessError(f"prompt {prompt['id']} needs distinct reviewers")
        names.add(name)
        if any(review.get(field) is not True for field in (
            "target_absent", "behavior_not_required", "answerable", "category_fit",
        )):
            raise HarnessError(f"review did not approve prompt {prompt['id']}")
        score = review.get("emoji_affordance")
        if not isinstance(score, int) or score not in {0, 1, 2}:
            raise HarnessError(f"prompt {prompt['id']} has unfinished affordance")
    adjudication = receipt.get("adjudication")
    if not isinstance(adjudication, Mapping) or not str(
        adjudication.get("reviewer", "")
    ).strip():
        raise HarnessError(f"prompt {prompt['id']} lacks named adjudication")
    score = adjudication.get("emoji_affordance")
    expected = 0 if prompt["split"] == "transfer" else 1
    if score != expected:
        raise HarnessError(
            f"prompt {prompt['id']} adjudicated affordance {score!r}; expected {expected}"
        )


def _validate_prompts(study: Mapping[str, Any], bank: Mapping[str, Any]) -> dict:
    from nla_lab.study import canonical_sha256, normalize_prompts
    from nla_lab.target_metrics import emoji_cluster_spans, lexical_tokens

    if bank.get("schema") != "emoji-replication-prompt-bank/v1":
        raise HarnessError("prompt bank has the wrong schema")
    prompts = normalize_prompts(bank.get("prompts"))
    forbidden = {
        str(word).casefold() for word in bank.get("policy", {}).get(
            "forbidden_target_terms", []
        )
    }
    counts: Counter[tuple[str, str]] = Counter()
    frozen = bank.get("status") == "frozen"
    for prompt in prompts:
        if "category" not in prompt:
            raise HarnessError(f"prompt {prompt['id']} lacks a category")
        text = prompt["text"]
        if text in HISTORICAL_PROMPTS or text == CONSTRUCTION_PROMPT:
            raise HarnessError(f"prompt {prompt['id']} reuses a forbidden prompt")
        leaked = forbidden & set(lexical_tokens(text))
        if leaked:
            raise HarnessError(
                f"prompt {prompt['id']} leaks target terms {sorted(leaked)}"
            )
        if emoji_cluster_spans(text):
            raise HarnessError(f"prompt {prompt['id']} contains a pictograph")
        if frozen:
            _validate_pre_output_review(prompt)
        counts[(prompt["split"], str(prompt["category"]))] += 1

    expected = study["prompt_design"]
    for split in ("calibration", "evaluation", "transfer"):
        split_prompts = [p for p in prompts if p["split"] == split]
        required = int(expected[split]["required_prompts"])
        if len(split_prompts) != required:
            raise HarnessError(
                f"{split} has {len(split_prompts)} prompts; expected {required}"
            )
        category_counts = {
            str(key): int(value)
            for key, value in expected[split]["categories"].items()
        }
        observed = {
            key: counts[(split, key)] for key in category_counts
        }
        if observed != category_counts:
            raise HarnessError(
                f"{split} category counts {observed} != {category_counts}"
            )
    return {
        "status": str(bank.get("status")),
        "count": len(prompts),
        "sha256": canonical_sha256(bank),
        "prompts": prompts,
    }


def _validate_sources(source: Mapping[str, Any]) -> dict[str, dict]:
    if source.get("schema") != "emoji-replication-source-manifest/v1":
        raise HarnessError("source manifest has the wrong schema")
    archives = source.get("archives")
    arms = source.get("arms")
    if not isinstance(archives, Mapping) or not isinstance(arms, Mapping):
        raise HarnessError("source manifest requires archives and arms")
    expected_arms = {"av_emoji", "av_glyph", "av_phrase", "ar_freeform", "av_excited"}
    if set(arms) != expected_arms:
        raise HarnessError("source manifest arm family is incomplete")
    validated: dict[str, dict] = {}
    for arm_id, arm_raw in arms.items():
        arm = dict(arm_raw)
        archive = archives.get(arm.get("archive"))
        if not isinstance(archive, Mapping):
            raise HarnessError(f"{arm_id} references an unknown archive")
        path = LAB / str(archive["path"])
        if not path.is_file() or sha256_file(path) != archive.get("sha256"):
            raise HarnessError(f"{arm_id} archive bytes do not match the registry")
        member = str(arm["member"])
        with zipfile.ZipFile(path) as zipped:
            member_name = f"{member}.npy"
            try:
                member_bytes = zipped.read(member_name)
            except KeyError as exc:
                raise HarnessError(f"{arm_id} archive member is absent") from exc
        if hashlib.sha256(member_bytes).hexdigest() != arm.get("npz_member_sha256"):
            raise HarnessError(f"{arm_id} archived member bytes do not match the registry")
        with np.load(path) as data:
            if member not in data:
                raise HarnessError(f"{arm_id} archive member is absent")
            raw = np.asarray(data[member], dtype=np.float32).reshape(-1)
        if raw.size != 3584 or not np.isfinite(raw).all():
            raise HarnessError(f"{arm_id} is not a finite Qwen L20 direction")
        raw_hash = tensor_f32_sha256(raw)
        unit = raw / max(float(np.linalg.norm(raw)), 1e-12)
        unit_hash = tensor_f32_sha256(unit)
        if raw_hash != arm.get("source_member_f32_sha256"):
            raise HarnessError(f"{arm_id} source tensor hash mismatch")
        if unit_hash != arm.get("unit_direction_sha256"):
            raise HarnessError(f"{arm_id} unit direction hash mismatch")
        validated[arm_id] = {
            **arm,
            "direction_file": f"{archive['path']}#{member}",
            "source_archive_sha256": str(archive["sha256"]),
        }
    return validated


def load_study(study_path: Path = HERE / "study.yaml") -> dict[str, Any]:
    from nla_lab.study import canonical_sha256
    from nla_lab.target_metrics import validate_target_metric

    study = load_yaml(study_path)
    if study.get("schema") != "emoji-replication-preregistration/v1":
        raise HarnessError("study has the wrong schema")
    status = study.get("status")
    if status not in {"draft_pending_human_prompt_review", "frozen"}:
        raise HarnessError(f"unknown study status {status!r}")
    base = study_path.parent
    source = load_yaml(base / str(study["source_manifest"]))
    bank = load_yaml(base / str(study["prompt_bank"]))
    prompt_report = _validate_prompts(study, bank)
    sources = _validate_sources(source)
    validate_target_metric(study["target_metric"])
    frozen = status == "frozen" and prompt_report["status"] == "frozen"
    if status == "frozen" and not frozen:
        raise HarnessError("study and prompt bank must freeze together")
    if frozen and study.get("open_decisions"):
        raise HarnessError("a frozen study cannot retain open decisions")
    hashes = {
        "study": canonical_sha256(study),
        "source": canonical_sha256(source),
        "prompts": prompt_report["sha256"],
        "code": _code_hashes(),
    }
    return {
        "study": study,
        "source": source,
        "sources": sources,
        "prompt_bank": bank,
        "prompt_report": prompt_report,
        "hashes": hashes,
        "frozen": frozen,
    }


def _method(arm_id: str, source: Mapping[str, Any] | None = None) -> dict:
    if arm_id == "baseline":
        root = {
            "method": "none", "text_origin": "none",
            "contrast_relation": "none", "edit_granularity": "none",
            "pairing_style": "not_applicable",
        }
        view, role = "none", "none"
    elif arm_id.startswith("random_"):
        root = {
            "method": "random_isotropic", "text_origin": "not_applicable",
            "contrast_relation": "not_applicable",
            "edit_granularity": "not_applicable",
            "pairing_style": "not_applicable",
        }
        view, role = "unit_l2", "whole"
    else:
        if source is None:
            raise HarnessError(f"{arm_id} requires source metadata")
        root = {
            "method": source["root_method"],
            "text_origin": source["text_origin"],
            "contrast_relation": source["contrast_relation"],
            "edit_granularity": source["edit_granularity"],
            "pairing_style": source.get("pairing_style", "not_applicable"),
        }
        view, role = "unit_l2", "whole"
    return {
        "schema": "steering-taxonomy/v1",
        "root": root,
        "derivation": {"operator": "none", "output_role": role},
        "vector_view": view,
    }


def _prompts_for(bundle: Mapping[str, Any], phase: str) -> list[dict]:
    return [
        dict(prompt) for prompt in bundle["prompt_report"]["prompts"]
        if prompt["split"] == phase
    ]


def build_spec(
    bundle: Mapping[str, Any], *, arm_id: str, phase: str,
    alphas: list[float] | None = None, allow_draft: bool = False,
) -> dict[str, Any]:
    from nla_lab.methods import resolve_design, resolve_method
    from nla_lab.study import seal_spec

    study = bundle["study"]
    if phase not in {"calibration", "evaluation", "transfer"}:
        raise HarnessError(f"unknown phase {phase!r}")
    if not bundle["frozen"] and not allow_draft:
        raise HarnessError("research specs require a frozen prompt review")
    if phase != "calibration":
        raise HarnessError(
            "held-out materialization is locked until the sealed alpha-receipt "
            "and family evaluator are implemented"
        )
    source = bundle["sources"].get(arm_id)
    random_map = {
        row["id"]: int(row["direction_seed"])
        for row in study["controls"]["random_arms"]
    }
    known = set(bundle["sources"]) | {"baseline"} | set(random_map)
    if arm_id not in known:
        raise HarnessError(f"unknown arm {arm_id!r}")
    if alphas is None:
        if arm_id == "baseline":
            selected_alphas = [0.0]
        elif arm_id in study["primary_treatments"]:
            selected_alphas = [float(value) for value in study["calibration"]["alphas"]]
        else:
            raise HarnessError(f"{arm_id} is not calibrated independently")
    else:
        selected_alphas = [float(value) for value in alphas]
    if 0.0 not in selected_alphas or len(selected_alphas) != len(set(selected_alphas)):
        raise HarnessError("alphas must be unique and include zero")

    intent = "confirmatory" if bundle["frozen"] else "screening"
    role = (
        "baseline_reference" if arm_id == "baseline" else
        "negative_control" if arm_id in random_map else
        "active_control" if arm_id == "av_excited" else "treatment"
    )
    prompts = _prompts_for(bundle, phase)
    runtime = study["runtime"]
    spec: dict[str, Any] = {
        "name": f"emojirep-qwen-{phase}-{arm_id}".replace("_", "-"),
        "matrix_id": "EMOJI-REPLICATION-QWEN-L20-v1",
        "method": _method(arm_id, source),
        "design": {
            "schema": "steering-study/v1", "intent": intent,
            "preregistered": bool(bundle["frozen"]), "arm_role": role,
            "notes": ["Prospective emoji construction/generalization replication."],
        },
        "study": {
            "schema": "emoji-replication-run/v1",
            "study_id": study["study_id"], "phase": phase,
            "arm_id": arm_id, "primary_metric": study["calibration"]["primary_metric"],
            "space_id": runtime["space_id"], "locks": dict(bundle["hashes"]),
        },
        "expected_runtime": {
            key: runtime[key] for key in ("model_key", "layer", "hidden_width")
        },
        "expected_code_sha256": dict(bundle["hashes"]["code"]),
        "execution_guard": {
            "require_explicit_model_env": True,
            "require_clean_git": True,
            "require_spec_integrity": True,
            "require_external_output_dir": True,
        },
        "prompts": prompts,
        "alphas": selected_alphas,
        "mode": runtime["mode"],
        "samples_per_prompt": int(runtime["samples_per_prompt"][phase]),
        "seed_offset": int(runtime["seed_offsets"][phase]),
        "seed_scheme": runtime["seed_scheme"],
        "temperature": float(runtime["temperature"]),
        "max_new_tokens": int(runtime["max_new_tokens"]),
        "target_metric": dict(study["target_metric"]),
        "probes": ["emojis", "exclamations", "n_words", "unique_word_ratio"],
    }
    if arm_id in random_map:
        spec.update(direction="random", direction_seed=random_map[arm_id])
    elif source is not None:
        spec["direction_file"] = source["direction_file"]
        spec["expected_direction_integrity"] = {
            "source_archive_sha256": source["source_archive_sha256"],
            "source_member_f32_sha256": source["source_member_f32_sha256"],
            "unit_direction_sha256": source["unit_direction_sha256"],
        }
    spec["expected_records"] = (
        len(prompts) * len(selected_alphas) * spec["samples_per_prompt"]
    )
    method = resolve_method(spec)
    resolve_design(spec, method)
    return seal_spec(spec)


def build_canary_spec(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Build a two-record plumbing check excluded from the research corpus."""
    from nla_lab.study import seal_spec

    spec = build_spec(
        bundle, arm_id="av_emoji", phase="calibration", allow_draft=True
    )
    spec.pop("spec_integrity", None)
    spec["name"] = "emojirep-qwen-infrastructure-canary"
    spec["study"].update(canary=True, excluded_from_research_corpus=True)
    spec["prompts"] = [{
        "id": "historical-plumbing-canary", "split": "canary",
        "category": "excluded_historical", "text": CONSTRUCTION_PROMPT,
    }]
    spec["alphas"] = [0.0, 0.1]
    spec["samples_per_prompt"] = 1
    spec["seed_offset"] = 90000
    spec["max_new_tokens"] = 32
    spec["expected_records"] = 2
    return seal_spec(spec)
