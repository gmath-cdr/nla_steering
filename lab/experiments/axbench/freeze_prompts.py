"""Create review worksheets and merge signed prompt reviews fail-closed.

Two reviewers complete separate templates without seeing model output. A named
adjudicator then resolves the 0/1/2 affordance score for each prompt/concept.
The merge command embeds those receipts into a new frozen prompt bank; it never
overwrites the candidate bank.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import tempfile
from typing import Any

import yaml

from harness import HarnessError, load_study, validate_prompt_bank
from nla_lab.study import normalize_prompts


REVIEW_SCHEMA = "axbench-prompt-review/v1"
ADJUDICATION_SCHEMA = "axbench-prompt-adjudication/v1"


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(path)


def build_review_template(bundle: dict, reviewer: str) -> dict[str, Any]:
    if not reviewer.strip():
        raise HarnessError("reviewer name cannot be empty")
    ratings = {}
    for prompt in normalize_prompts(bundle["prompt_bank"]["prompts"]):
        applies = [
            str(key) for key in prompt["concepts"]
            if key in bundle["study"]["pilot_concepts"]
        ]
        ratings[prompt["id"]] = {
            "target_absent": None,
            "behavior_optional": None,
            "affordance": {key: None for key in applies},
        }
    return {
        "schema": REVIEW_SCHEMA,
        "reviewer": reviewer.strip(),
        "instructions": (
            "Set both booleans and every affordance score (0=derails, "
            "1=natural optional, 2=strongly elicited) without viewing outputs."
        ),
        "ratings": ratings,
    }


def build_adjudication_template(bundle: dict, reviewer: str) -> dict[str, Any]:
    if not reviewer.strip():
        raise HarnessError("adjudicator name cannot be empty")
    ratings = {}
    for prompt in normalize_prompts(bundle["prompt_bank"]["prompts"]):
        applies = [
            str(key) for key in prompt["concepts"]
            if key in bundle["study"]["pilot_concepts"]
        ]
        ratings[prompt["id"]] = {
            "affordance": {key: None for key in applies},
        }
    return {
        "schema": ADJUDICATION_SCHEMA,
        "reviewer": reviewer.strip(),
        "instructions": "Resolve every affordance score after comparing both reviews.",
        "ratings": ratings,
    }


def _validate_review(
    value: dict[str, Any], bundle: dict, *, adjudication: bool
) -> None:
    schema = ADJUDICATION_SCHEMA if adjudication else REVIEW_SCHEMA
    if value.get("schema") != schema or not str(value.get("reviewer", "")).strip():
        raise HarnessError(f"review file must use {schema} and name its reviewer")
    ratings = value.get("ratings")
    prompts = normalize_prompts(bundle["prompt_bank"]["prompts"])
    expected_ids = {prompt["id"] for prompt in prompts}
    if not isinstance(ratings, dict) or set(ratings) != expected_ids:
        raise HarnessError("review ratings do not exactly cover the candidate prompt bank")
    selected = set(bundle["study"]["pilot_concepts"])
    for prompt in prompts:
        row = ratings[prompt["id"]]
        if not isinstance(row, dict):
            raise HarnessError(f"malformed review row for {prompt['id']}")
        expected_concepts = set(prompt["concepts"]) & selected
        affordance = row.get("affordance")
        if not isinstance(affordance, dict) or set(affordance) != expected_concepts:
            raise HarnessError(f"wrong affordance concepts for {prompt['id']}")
        if any(not isinstance(score, int) or score not in {0, 1, 2}
               for score in affordance.values()):
            raise HarnessError(f"unfinished affordance score for {prompt['id']}")
        if not adjudication and (
            row.get("target_absent") is not True
            or row.get("behavior_optional") is not True
        ):
            raise HarnessError(
                f"review did not affirm target absence/optionality for {prompt['id']}"
            )


def merge_reviews(
    bundle: dict,
    review_a: dict[str, Any],
    review_b: dict[str, Any],
    adjudication: dict[str, Any],
) -> dict[str, Any]:
    _validate_review(review_a, bundle, adjudication=False)
    _validate_review(review_b, bundle, adjudication=False)
    _validate_review(adjudication, bundle, adjudication=True)
    names = {
        str(review_a["reviewer"]).strip(),
        str(review_b["reviewer"]).strip(),
    }
    if len(names) != 2:
        raise HarnessError("the two prompt reviewers must be distinct")
    output = deepcopy(bundle["prompt_bank"])
    output["status"] = "frozen"
    for prompt in output["prompts"]:
        prompt_id = prompt["id"]
        a = review_a["ratings"][prompt_id]
        b = review_b["ratings"][prompt_id]
        final = adjudication["ratings"][prompt_id]
        prompt["pre_output_review"] = {
            "reviews": [
                {
                    "reviewer": review_a["reviewer"],
                    "target_absent": a["target_absent"],
                    "behavior_optional": a["behavior_optional"],
                    "affordance": a["affordance"],
                },
                {
                    "reviewer": review_b["reviewer"],
                    "target_absent": b["target_absent"],
                    "behavior_optional": b["behavior_optional"],
                    "affordance": b["affordance"],
                },
            ],
            "adjudication": {
                "reviewer": adjudication["reviewer"],
                "affordance": final["affordance"],
            },
        }
    validate_prompt_bank(output, bundle["concepts"], bundle["study"]["pilot_concepts"])
    return output


def _load(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HarnessError(f"{path} must contain a YAML mapping")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    template = subparsers.add_parser("template")
    template.add_argument("--reviewer", required=True)
    template.add_argument("--adjudication", action="store_true")
    template.add_argument("--output", type=Path, required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--review-a", type=Path, required=True)
    freeze.add_argument("--review-b", type=Path, required=True)
    freeze.add_argument("--adjudication", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    bundle = load_study()
    if args.command == "template":
        value = (
            build_adjudication_template(bundle, args.reviewer)
            if args.adjudication
            else build_review_template(bundle, args.reviewer)
        )
    else:
        value = merge_reviews(
            bundle,
            _load(args.review_a),
            _load(args.review_b),
            _load(args.adjudication),
        )
    _write_yaml(args.output, value)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"prompt freeze failed: {exc}") from exc
