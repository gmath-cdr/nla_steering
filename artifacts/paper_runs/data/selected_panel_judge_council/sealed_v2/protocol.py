"""Pure protocol helpers for the approved ``axb44-crossmodel-v1`` campaign.

This module performs no model or network work.  It is the fail-closed boundary
between the human-reviewed YAML and every builder, runner, and evaluator.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CAMPAIGN_PATH = HERE / "campaign.review.yaml"
APPROVAL_PATH = HERE / "campaign.approval.yaml"
PROMPT_RECEIPTS_PATH = HERE / "promptsteering.receipts.review.yaml"
EXPECTED_CAMPAIGN_ID = "axb44-crossmodel-v1"
EXPECTED_REVIEW_SHA256 = "0fd7075c011d08192185a8fa60603942eef7458ebadf03bbb0f5974edf85cd56"


class ProtocolError(ValueError):
    """Raised before paid work when any frozen identity has drifted."""


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProtocolError(f"{path.name} must contain a mapping")
    return value


def load_bundle() -> dict[str, Any]:
    """Load and verify the immutable review payload plus approval overlay."""
    campaign = _yaml(CAMPAIGN_PATH)
    approval = _yaml(APPROVAL_PATH)
    prompt_receipts = _yaml(PROMPT_RECEIPTS_PATH)
    if campaign.get("campaign_id") != EXPECTED_CAMPAIGN_ID:
        raise ProtocolError("campaign identity drift")
    if approval.get("campaign_id") != EXPECTED_CAMPAIGN_ID:
        raise ProtocolError("approval campaign identity drift")
    approved_payload = approval.get("approved_review_payload", {})
    if approved_payload.get("canonical_combined_sha256") != EXPECTED_REVIEW_SHA256:
        raise ProtocolError("approval does not bind the reviewed payload")
    if approval.get("status") != "approved_for_implementation_and_run":
        raise ProtocolError("campaign is not approved for implementation and run")
    permissions = approval.get("authorization", {})
    required = {
        "implement_runtime", "build_model_specific_directions",
        "run_qwen_and_gemma_after_all_hard_gates_pass",
        "run_exact_axbench_judge_after_retrieval",
        "retrieve_and_validate_all_artifacts",
        "automatically_stop_pods_after_success_or_failure",
    }
    if not isinstance(permissions, dict) or any(
        permissions.get(key) is not True for key in required
    ):
        raise ProtocolError("approval permissions are incomplete")
    if prompt_receipts.get("campaign_id") != EXPECTED_CAMPAIGN_ID:
        raise ProtocolError("PromptSteering receipt campaign drift")
    concepts = campaign.get("concepts", [])
    if len(concepts) != 10 or len({int(row["concept_id"]) for row in concepts}) != 10:
        raise ProtocolError("approved campaign must contain ten unique concepts")
    return {
        "campaign": campaign,
        "approval": approval,
        "prompt_receipts": prompt_receipts,
        "review_payload_sha256": EXPECTED_REVIEW_SHA256,
    }


def concept_by_id(bundle: dict[str, Any], concept_id: int) -> dict[str, Any]:
    rows = [
        row for row in bundle["campaign"]["concepts"]
        if int(row["concept_id"]) == int(concept_id)
    ]
    if len(rows) != 1:
        raise ProtocolError(f"unknown or duplicated concept {concept_id}")
    return rows[0]


def assignments(
    bundle: dict[str, Any], *, concept_id: int, split: str
) -> list[dict[str, Any]]:
    """Return the frozen two source-negative plus two shared assignments."""
    if split not in {"calibration", "evaluation"}:
        raise ProtocolError(f"invalid split {split!r}")
    concept = concept_by_id(bundle, concept_id)
    rows: list[dict[str, Any]] = []
    for source in concept["source_negative_prompts"]:
        if source["split"] == split:
            rows.append({
                **source,
                "track": "concept500_negative",
                "concept_id": int(concept_id),
                "concept_label": concept["label"],
            })
    for source in bundle["campaign"]["prompt_banks"]["shared_alpaca"]:
        if source["split"] == split:
            rows.append({
                **source,
                "track": "shared_alpaca_transfer",
                "concept_id": int(concept_id),
                "concept_label": concept["label"],
            })
    if len(rows) != 4 or len({row["prompt_id"] for row in rows}) != 4:
        raise ProtocolError(f"concept {concept_id} split {split} is not exactly 2+2")
    return rows


def stable_seed(*parts: object, bits: int = 31) -> int:
    """Derive a platform-stable RNG seed from a namespaced cell identity."""
    if bits not in {31, 63}:
        raise ProtocolError("stable_seed bits must be 31 or 63")
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return value & ((1 << bits) - 1)


def generation_seeds(
    bundle: dict[str, Any], *, model_key: str, concept_id: int,
    prompt_id: str, sample_count: int,
) -> list[int]:
    campaign_id = bundle["campaign"]["campaign_id"]
    return [
        stable_seed("generation", campaign_id, model_key, concept_id, prompt_id, index)
        for index in range(sample_count)
    ]


def prompt_instruction(bundle: dict[str, Any], concept_id: int) -> str:
    receipts = bundle["prompt_receipts"].get("receipts", [])
    rows = [row for row in receipts if int(row["concept_id"]) == int(concept_id)]
    if len(rows) != 1:
        raise ProtocolError(f"missing PromptSteering receipt for concept {concept_id}")
    instruction = str(rows[0].get("response_text", "")).strip()
    if not instruction:
        raise ProtocolError(f"empty PromptSteering instruction for concept {concept_id}")
    return instruction


def visible_prompt(bundle: dict[str, Any], concept_id: int, carrier: str) -> str:
    return f"{prompt_instruction(bundle, concept_id)}\n\nQuestion: {carrier}"


def iter_cells(bundle: dict[str, Any], *, model_key: str, split: str) -> Iterable[dict[str, Any]]:
    sample_count = (
        bundle["campaign"]["generation"]["calibration_samples_per_assignment"]
        if split == "calibration"
        else bundle["campaign"]["generation"]["evaluation_samples_per_assignment"]
    )
    for concept in bundle["campaign"]["concepts"]:
        concept_id = int(concept["concept_id"])
        for prompt in assignments(bundle, concept_id=concept_id, split=split):
            for sample_index, seed in enumerate(generation_seeds(
                bundle, model_key=model_key, concept_id=concept_id,
                prompt_id=prompt["prompt_id"], sample_count=sample_count,
            )):
                yield {
                    **prompt,
                    "model_key": model_key,
                    "sample_index": sample_index,
                    "generation_seed": seed,
                }


def append_concept_edit(explanation: str, addition_sentence: str) -> str:
    """Apply the exact paper edit immediately before ``</explanation>``."""
    closing = "</explanation>"
    addition = f"\n\n{addition_sentence.strip()}\n"
    if closing in explanation:
        return explanation.replace(closing, addition + closing, 1)
    return explanation + addition
