"""Fail-closed validation for the 44-prompt campaign review package.

This script is deliberately CPU-only. It validates source identities, exact
prompt rows, split isolation, method identities, receipt text, and expected
cardinality. It does not build directions or generate model output.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
CAMPAIGN_PATH = HERE / "campaign.review.yaml"
PROMPT_RECEIPTS_PATH = HERE / "promptsteering.receipts.review.yaml"
APPROVAL_PATH = HERE / "campaign.approval.yaml"
PARENT_PANEL_PATH = HERE / "panel.frozen.yaml"
HUMAN_REVIEW_PATH = HERE / "PROMPT_METHOD_REVIEW.md"
AXBENCH_ROOT = WORKSPACE_ROOT / "external" / "AxBench"
CONCEPT_ROOT = AXBENCH_ROOT / "axbench" / "concept500" / "prod_9b_l20_v1"
TRAIN_PATH = CONCEPT_ROOT / "generate" / "train_data.parquet"
EVAL_PATH = CONCEPT_ROOT / "inference" / "latent_eval_data.parquet"
METADATA_PATH = CONCEPT_ROOT / "generate" / "metadata.jsonl"
PROMPT_TEMPLATE_PATH = AXBENCH_ROOT / "axbench" / "templates" / "prompt_templates.py"

EXPECTED_METHODS = {
    "unsteered",
    "axbench_prompt_steering",
    "diffmean",
    "caa",
    "nla_av_edit_ar",
    "random_seed_17",
    "random_seed_42",
    "random_seed_101",
}
EXPECTED_CONCEPT_IDS = {20, 95, 237, 256, 288, 313, 362, 403, 491, 499}
EXPECTED_QWEN_GRID = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.75]
EXPECTED_GEMMA_GRID = [0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5]
EXPECTED_RANDOM_SEEDS = [17, 42, 101]


class ValidationError(RuntimeError):
    """Raised for a review-package integrity failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{path.name} must contain a mapping")
    return value


def canonical_payload_sha256(*values: dict[str, Any]) -> str:
    payload = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def source_constant(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            value = ast.literal_eval(node.value)
            require(isinstance(value, str), f"{name} is not a string")
            return value
    raise ValidationError(f"Could not find {name} in {path}")


def git_head(path: Path) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={path.as_posix()}",
            "-C",
            str(path),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def validate_sources(campaign: dict[str, Any]) -> None:
    sources = campaign["sources"]
    expected_parent_hash = sources["parent_panel"]["sha256"]
    require(sha256_file(PARENT_PANEL_PATH) == expected_parent_hash, "parent panel hash mismatch")

    concept_source = sources["concept500"]
    checks = {
        TRAIN_PATH: concept_source["train_parquet_sha256"],
        EVAL_PATH: concept_source["latent_eval_parquet_sha256"],
        METADATA_PATH: concept_source["metadata_sha256"],
    }
    for path, expected in checks.items():
        require(path.is_file(), f"missing source artifact: {path}")
        require(sha256_file(path) == expected, f"source hash mismatch: {path.name}")

    expected_axbench = sources["axbench_code"]["revision"]
    require(git_head(AXBENCH_ROOT) == expected_axbench, "AxBench checkout revision mismatch")


def validate_concepts_and_prompts(
    campaign: dict[str, Any], parent: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    concepts = campaign["concepts"]
    require(len(concepts) == 10, "campaign must contain exactly ten concepts")
    require({int(item["concept_id"]) for item in concepts} == EXPECTED_CONCEPT_IDS,
            "unexpected ten-concept subset")
    require(len({item["panel_code"] for item in concepts}) == 10, "duplicate panel_code")
    require(Counter(item["stratum"] for item in concepts) == {
        "lexical_domain": 5,
        "abstract_style_attitude": 5,
    }, "concept strata must be balanced 5/5")

    parent_by_id = {
        int(item["concept_id"]): item for item in parent["panel"]["concepts"]
    }
    excluded = set()
    for values in parent["prior_work_exclusions"].values():
        if isinstance(values, list) and all(isinstance(value, int) for value in values):
            excluded.update(values)

    eval_data = pd.read_parquet(EVAL_PATH)
    all_specific: list[dict[str, Any]] = []
    for concept in concepts:
        concept_id = int(concept["concept_id"])
        require(concept_id in parent_by_id, f"concept {concept_id} absent from parent panel")
        parent_item = parent_by_id[concept_id]
        for key in ("panel_code", "stratum", "label"):
            require(concept[key] == parent_item[key], f"concept {concept_id} {key} drift")
        require(concept_id not in excluded, f"concept {concept_id} overlaps excluded prior panel")

        exact_edit = f"The representation specifically concerns: {concept['label']}."
        require(concept["nla_hidden_addition"] == exact_edit,
                f"concept {concept_id} NLA addition drift")

        prompts = concept["source_negative_prompts"]
        require(len(prompts) == 4, f"concept {concept_id} must have four source negatives")
        require(Counter(item["split"] for item in prompts) == {
            "calibration": 2,
            "evaluation": 2,
        }, f"concept {concept_id} source split must be 2/2")

        source_rows = eval_data[
            (eval_data["concept_id"] == concept_id)
            & (eval_data["category"] == "negative")
        ].reset_index(drop=True)
        require(len(source_rows) == 36, f"concept {concept_id} must have 36 negatives")
        for prompt in prompts:
            index = int(prompt["source_negative_index"])
            require(2 <= index < 26,
                    f"{prompt['prompt_id']} must avoid discovery rows 0/1 and math/code rows 26+")
            source_text = str(source_rows.iloc[index]["input"])
            require(prompt["text"] == source_text, f"{prompt['prompt_id']} source text mismatch")
            require(prompt["text_sha256"] == sha256_text(source_text),
                    f"{prompt['prompt_id']} text hash mismatch")
            require(source_rows.iloc[index]["dataset_category"] == "instruction",
                    f"{prompt['prompt_id']} is not an instruction row")
            require(concept["label"].casefold() not in source_text.casefold(),
                    f"{prompt['prompt_id']} contains its full target label")
            all_specific.append({**prompt, "concept_id": concept_id})

    shared = campaign["prompt_banks"]["shared_alpaca"]
    require(len(shared) == 4, "campaign must contain four shared Alpaca prompts")
    require(Counter(item["split"] for item in shared) == {
        "calibration": 2,
        "evaluation": 2,
    }, "shared Alpaca split must be 2/2")
    parent_prompts = {
        item["prompt_id"]: item for item in parent["carrier_prompt_design"]["prompts"]
    }
    for prompt in shared:
        require(prompt["prompt_id"] in parent_prompts,
                f"{prompt['prompt_id']} absent from frozen parent prompt bank")
        source = parent_prompts[prompt["prompt_id"]]
        for key in ("source_index", "source_dataset", "split", "text_sha256", "text"):
            require(prompt[key] == source[key], f"{prompt['prompt_id']} {key} drift")
        require(prompt["text_sha256"] == sha256_text(prompt["text"]),
                f"{prompt['prompt_id']} text hash mismatch")

    all_prompts = all_specific + shared
    ids = [item["prompt_id"] for item in all_prompts]
    texts = [item["text"] for item in all_prompts]
    require(len(ids) == 44 and len(set(ids)) == 44, "prompt IDs must be 44/44 unique")
    require(len(texts) == 44 and len(set(texts)) == 44,
            "carrier strings must be 44/44 unique")

    declared = campaign["prompt_banks"]["counts"]
    require(declared == {
        "concept_specific_unique": 40,
        "shared_alpaca_unique": 4,
        "total_unique_strings": 44,
        "concept_prompt_assignments_per_model": 80,
    }, "declared prompt counts drift")
    return all_specific, shared


def validate_prompt_receipts(
    campaign: dict[str, Any], receipts: dict[str, Any]
) -> None:
    require(receipts["schema"] == "axbench-promptsteering-receipts/v1",
            "unexpected PromptSteering receipt schema")
    require(receipts["campaign_id"] == campaign["campaign_id"],
            "PromptSteering campaign identity mismatch")
    source = receipts["source"]
    require(source["axbench_revision"] == campaign["sources"]["axbench_code"]["revision"],
            "PromptSteering AxBench revision mismatch")
    template = source_constant(PROMPT_TEMPLATE_PATH, source["source_symbol"])
    require(sha256_text(template) == source["evaluated_template_sha256"],
            "PromptSteering template hash mismatch")

    generation = receipts["generation"]
    require(generation["requested_model"] == "gpt-4o-mini-2024-07-18",
            "PromptSteering requested model drift")
    require(generation["returned_model"] == "gpt-4o-mini-2024-07-18",
            "PromptSteering returned model drift")
    require(generation["temperature"] == 0, "PromptSteering temperature must be zero")

    concept_by_id = {int(item["concept_id"]): item for item in campaign["concepts"]}
    rows = receipts["receipts"]
    require(len(rows) == 10, "must have one PromptSteering receipt per concept")
    require({int(row["concept_id"]) for row in rows} == set(concept_by_id),
            "PromptSteering receipt concept set mismatch")
    for row in rows:
        concept = concept_by_id[int(row["concept_id"])]
        require(row["label"] == concept["label"], f"receipt {row['concept_id']} label drift")
        require(row["response_text_sha256"] == sha256_text(row["response_text"]),
                f"receipt {row['concept_id']} response hash mismatch")
        require(int(row["usage"]["prompt_tokens"]) > 0, "missing prompt token usage")
        require(int(row["usage"]["completion_tokens"]) > 0, "missing completion token usage")
        require(row["response_text"].strip() == row["response_text"],
                f"receipt {row['concept_id']} has surrounding whitespace")


def validate_method_and_design(campaign: dict[str, Any]) -> None:
    methods = campaign["methods"]
    require(set(methods["identities"]) == EXPECTED_METHODS, "method identity set drift")
    require(methods["random_controls"]["direction_seeds"] == EXPECTED_RANDOM_SEEDS,
            "random seeds drift")
    require(methods["random_controls"]["no_oracle_selection"] is True,
            "random controls must never be oracle-selected")
    require(methods["nla_av_edit_ar"]["av_rollouts"] == 3, "NLA rollout count drift")
    require(methods["nla_av_edit_ar"]["edit_location"] ==
            "immediately_before_first_closing_explanation_tag", "NLA edit location drift")
    require(methods["nla_av_edit_ar"]["prompt_processing_scope"] ==
            "final_user_token_only", "NLA prompt scope drift")
    require(methods["diffmean"]["prompt_processing_scope"] == "all_positions",
            "DiffMean prompt scope drift")
    require(methods["caa"]["prompt_processing_scope"] == "all_positions",
            "CAA prompt scope drift")

    require(campaign["models"]["qwen"]["alpha_grid"] == EXPECTED_QWEN_GRID,
            "Qwen alpha grid drift")
    require(campaign["models"]["gemma"]["alpha_grid"] == EXPECTED_GEMMA_GRID,
            "Gemma alpha grid drift")
    require(campaign["generation"]["target_temperature"] == 0.7,
            "target temperature drift")
    require(campaign["generation"]["max_new_tokens"] == 96,
            "target max token drift")
    require(campaign["generation"]["calibration_samples_per_assignment"] == 1,
            "calibration sample count drift")
    require(campaign["generation"]["evaluation_samples_per_assignment"] == 2,
            "evaluation sample count drift")
    require(campaign["calibration"]["run_arms"]["random_controls"] ==
            "not_run_or_optimized", "random controls may not be optimized")
    require(campaign["calibration"]["alpha_selection"]["evaluation_data_access"] ==
            "prohibited", "evaluation leakage is not prohibited")
    require(campaign["evaluation_metrics"]["primary"]["calls_per_generation"] == 3,
            "AxBench judge must use three calls per generation")

    cal_assignments = 10 * (2 + 2)
    cal_cells = cal_assignments * (2 + 3 * 7) * 1
    eval_assignments = 10 * (2 + 2)
    eval_cells = eval_assignments * 2 * (2 + 3 + 3 * 3)
    require(cal_cells == 920 and eval_cells == 1120, "internal cardinality arithmetic drift")
    expected = campaign["expected_cardinality"]
    require(expected["calibration_per_model"] == cal_cells,
            "declared calibration cardinality mismatch")
    require(expected["evaluation_per_model_worst_case"] == eval_cells,
            "declared evaluation cardinality mismatch")
    require(expected["total_per_model_worst_case"] == cal_cells + eval_cells,
            "declared per-model cardinality mismatch")
    require(expected["total_crossmodel_worst_case"] == 2 * (cal_cells + eval_cells),
            "declared cross-model cardinality mismatch")
    require(expected["judge_calls_worst_case"] == 3 * 2 * (cal_cells + eval_cells),
            "declared judge-call cardinality mismatch")

    serialized = json.dumps(campaign, ensure_ascii=False).casefold()
    require("j_lens" not in serialized and "jlens" not in serialized,
            "J-lens arm is outside this campaign")


def validate_human_review(
    campaign: dict[str, Any],
    specific: list[dict[str, Any]],
    shared: list[dict[str, Any]],
) -> None:
    review = HUMAN_REVIEW_PATH.read_text(encoding="utf-8")
    for prompt in specific + shared:
        require(prompt["text"] in review,
                f"human review omits exact carrier {prompt['prompt_id']}")
        require(prompt["prompt_id"] in review,
                f"human review omits prompt ID {prompt['prompt_id']}")
    for concept in campaign["concepts"]:
        require(concept["nla_hidden_addition"] in review,
                f"human review omits NLA addition for concept {concept['concept_id']}")
    for method in EXPECTED_METHODS:
        display_token = method if method.startswith("random_seed") else {
            "unsteered": "Unsteered",
            "axbench_prompt_steering": "PromptSteering",
            "diffmean": "DiffMean",
            "caa": "CAA",
            "nla_av_edit_ar": "NLA AV-edit-AR",
        }[method]
        if method.startswith("random_seed"):
            display_token = method.rsplit("_", 1)[-1]
        require(display_token in review, f"human review omits method marker {display_token}")


def validate_approval(
    campaign: dict[str, Any], receipts: dict[str, Any], approval: dict[str, Any]
) -> str:
    require(approval["schema"] == "nla-axbench-campaign-approval/v1",
            "unexpected approval schema")
    require(approval["campaign_id"] == campaign["campaign_id"],
            "approval campaign identity mismatch")
    require(approval["status"] == "approved_for_implementation_and_run",
            "campaign is not approved")
    payload_hash = canonical_payload_sha256(campaign, receipts)
    approved_hash = approval["approved_review_payload"]["canonical_combined_sha256"]
    require(payload_hash == approved_hash,
            "approved payload hash mismatch; explicit re-approval is required")
    authorization = approval["authorization"]
    required_permissions = {
        "freeze_exact_prompt_and_method_design",
        "implement_runtime",
        "build_model_specific_directions",
        "run_qwen_and_gemma_after_all_hard_gates_pass",
        "run_exact_axbench_judge_after_retrieval",
        "monitor_until_terminal_state",
        "retrieve_and_validate_all_artifacts",
        "automatically_stop_pods_after_success_or_failure",
    }
    require(required_permissions <= set(authorization), "approval permission set is incomplete")
    require(all(authorization[name] is True for name in required_permissions),
            "one or more required approval permissions are false")
    return payload_hash


def main() -> int:
    campaign = load_yaml(CAMPAIGN_PATH)
    receipts = load_yaml(PROMPT_RECEIPTS_PATH)
    approval = load_yaml(APPROVAL_PATH)
    parent = load_yaml(PARENT_PANEL_PATH)

    require(campaign["schema"] == "nla-axbench-openended-campaign/v1",
            "unexpected campaign schema")
    require(campaign["status"] == "review_pending_user_approval",
            "review validator expects a review-pending campaign")
    require(campaign["review_boundary"]["generation_authorized"] is False,
            "generation must remain unauthorized during review")

    validate_sources(campaign)
    specific, shared = validate_concepts_and_prompts(campaign, parent)
    validate_prompt_receipts(campaign, receipts)
    validate_method_and_design(campaign)
    validate_human_review(campaign, specific, shared)
    payload_hash = validate_approval(campaign, receipts, approval)
    print("VALIDATION PASS")
    print(f"campaign={campaign['campaign_id']}")
    print(f"concepts={len(campaign['concepts'])} (5 lexical/domain + 5 abstract/style)")
    print(f"unique_carrier_strings={len(specific) + len(shared)} (40 Concept500 negative + 4 AlpacaEval)")
    print("concept_prompt_assignments_per_model=80 (40 calibration + 40 evaluation)")
    print("methods=8 identities (unsteered, Prompt, DiffMean, CAA, NLA, 3 random seeds)")
    print("approval_status=APPROVED; operational hard gates still fail closed")
    print(f"review_payload_sha256={payload_hash}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, TypeError, ValueError, ValidationError, subprocess.CalledProcessError) as exc:
        print(f"VALIDATION FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
