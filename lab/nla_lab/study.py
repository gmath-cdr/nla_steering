"""Small schema helpers shared by prospective experiment runners."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any


class StudySpecError(ValueError):
    """Raised before model loading when a prospective spec is malformed."""


SPEC_INTEGRITY_SCHEMA = "steering-spec-integrity/v1"
ALPHA_RECEIPT_SCHEMA = "axbench-alpha-receipt/v1"
_ALPHA_SELECTION_FIELDS = (
    "schema", "analysis", "selection_rule", "model_key", "concept_key",
    "chosen_alpha_by_arm", "random_control_alphas", "selection_details",
    "calibration_result_sha256", "lock_bundle_sha256",
    "base_checkpoint_marker_sha256", "treatment_direction_integrity",
    "treatment_exclusions",
    "calibration_git_commit", "calibration_package_versions",
)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_text_file_sha256(path: Path) -> str:
    """Hash text with newline normalization so Windows/Linux locks agree."""
    text = path.read_text(encoding="utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def seal_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with a digest covering every other spec field."""
    output = deepcopy(dict(spec))
    output.pop("spec_integrity", None)
    output["spec_integrity"] = {
        "schema": SPEC_INTEGRITY_SCHEMA,
        "sha256": canonical_sha256(output),
    }
    return output


def validate_spec_integrity(spec: Mapping[str, Any], *, required: bool = False) -> None:
    receipt = spec.get("spec_integrity")
    if receipt is None:
        if required:
            raise StudySpecError("spec_integrity is required")
        return
    if not isinstance(receipt, Mapping) or receipt.get("schema") != SPEC_INTEGRITY_SCHEMA:
        raise StudySpecError(f"spec_integrity must use {SPEC_INTEGRITY_SCHEMA}")
    payload = dict(spec)
    payload.pop("spec_integrity", None)
    observed = canonical_sha256(payload)
    if receipt.get("sha256") != observed:
        raise StudySpecError("spec content does not match its integrity digest")


def alpha_selection_integrity_sha256(receipt: Mapping[str, Any]) -> str:
    missing = set(_ALPHA_SELECTION_FIELDS) - set(receipt)
    if missing:
        raise StudySpecError(f"alpha receipt is missing fields {sorted(missing)}")
    return canonical_sha256({key: receipt[key] for key in _ALPHA_SELECTION_FIELDS})


def seal_alpha_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    output = deepcopy(dict(receipt))
    output["selection_integrity_sha256"] = alpha_selection_integrity_sha256(output)
    return output


def validate_alpha_receipt(
    receipt: Mapping[str, Any], *, require_frozen: bool = False
) -> None:
    if receipt.get("schema") != ALPHA_RECEIPT_SCHEMA:
        raise StudySpecError(f"alpha receipt must use {ALPHA_RECEIPT_SCHEMA}")
    observed = alpha_selection_integrity_sha256(receipt)
    if receipt.get("selection_integrity_sha256") != observed:
        raise StudySpecError("alpha receipt selection fields do not match their digest")
    chosen = receipt.get("chosen_alpha_by_arm")
    if not isinstance(chosen, Mapping) or not chosen:
        raise StudySpecError("alpha receipt has no treatment choices")
    exclusions = receipt.get("treatment_exclusions")
    if not isinstance(exclusions, Mapping):
        raise StudySpecError("alpha receipt treatment_exclusions must be a mapping")
    overlap = set(map(str, chosen)) & set(map(str, exclusions))
    if overlap:
        raise StudySpecError(
            f"treatment arms cannot be both chosen and excluded: {sorted(overlap)}"
        )
    for arm_id, exclusion in exclusions.items():
        if not str(arm_id).strip() or not isinstance(exclusion, Mapping):
            raise StudySpecError("malformed treatment exclusion")
        source = exclusion.get("receipt")
        if not isinstance(source, Mapping):
            raise StudySpecError(f"treatment exclusion {arm_id!r} lacks its receipt")
        if exclusion.get("receipt_sha256") != canonical_sha256(source):
            raise StudySpecError(
                f"treatment exclusion {arm_id!r} receipt hash does not match"
            )
    expected_random = sorted({float(value) for value in chosen.values()}, key=abs)
    observed_random = [float(value) for value in receipt.get("random_control_alphas", [])]
    if observed_random != expected_random:
        raise StudySpecError("random-control alpha lock disagrees with treatment choices")
    if require_frozen:
        if receipt.get("status") != "frozen":
            raise StudySpecError("held-out use requires a frozen alpha receipt")
        audit = receipt.get("audit")
        if not isinstance(audit, Mapping) or not all(
            str(audit.get(key, "")).strip()
            for key in ("reviewer", "note", "frozen_utc")
        ):
            raise StudySpecError("frozen alpha receipt lacks its human audit record")


def normalize_prompts(raw: Any) -> list[dict[str, Any]]:
    """Normalize legacy strings and `{id,text,split}` prompt records.

    Prompt IDs are required for structured prompts because confirmatory
    analysis pairs and bootstraps by prompt rather than by raw list position.
    Extra fields are retained as scoring context (for example a closed list of
    allowed source labels in a citation task).
    """
    if not isinstance(raw, list) or not raw:
        raise StudySpecError("prompts must be a non-empty list")
    output: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(raw):
        if isinstance(item, str):
            entry = {"id": f"legacy-{index:04d}", "text": item, "split": None}
        elif isinstance(item, Mapping):
            entry = dict(item)
            missing = {"id", "text", "split"} - set(entry)
            if missing:
                raise StudySpecError(
                    f"structured prompt {index} missing fields {sorted(missing)}"
                )
            entry["id"] = str(entry["id"])
            entry["text"] = str(entry["text"])
            entry["split"] = str(entry["split"])
        else:
            raise StudySpecError(f"prompt {index} must be a string or mapping")
        if not entry["id"] or entry["id"] in ids:
            raise StudySpecError(f"duplicate or empty prompt id {entry['id']!r}")
        if not entry["text"].strip():
            raise StudySpecError(f"prompt {entry['id']!r} has empty text")
        ids.add(entry["id"])
        output.append(entry)
    return output
