"""Create independent review sheets and merge a frozen emoji prompt bank.

Reviewers work without model output. Affordance is 0=inappropriate,
1=natural-but-optional, 2=strongly elicited/required. Calibration/evaluation
prompts must adjudicate to 1; transfer prompts must adjudicate to 0.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from typing import Any

import yaml


LAB = Path(__file__).resolve().parents[2]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.emoji_replication.harness import (  # noqa: E402
    HarnessError, _validate_prompts, load_study,
)
from nla_lab.study import normalize_prompts  # noqa: E402


REVIEW_SCHEMA = "emoji-replication-prompt-review/v1"
ADJUDICATION_SCHEMA = "emoji-replication-prompt-adjudication/v1"


def build_review_template(bundle: dict, reviewer: str) -> dict[str, Any]:
    name = reviewer.strip()
    if not name:
        raise HarnessError("reviewer name cannot be empty")
    return {
        "schema": REVIEW_SCHEMA,
        "reviewer": name,
        "instructions": (
            "Without viewing outputs, complete every boolean and score emoji "
            "affordance 0=inappropriate, 1=natural optional, 2=strongly elicited."
        ),
        "ratings": {
            prompt["id"]: {
                "target_absent": None,
                "behavior_not_required": None,
                "answerable": None,
                "category_fit": None,
                "emoji_affordance": None,
                "note": "",
            }
            for prompt in normalize_prompts(bundle["prompt_bank"]["prompts"])
        },
    }


def build_adjudication_template(bundle: dict, reviewer: str) -> dict[str, Any]:
    name = reviewer.strip()
    if not name:
        raise HarnessError("adjudicator name cannot be empty")
    return {
        "schema": ADJUDICATION_SCHEMA,
        "reviewer": name,
        "instructions": "Resolve every affordance score after comparing both reviews.",
        "ratings": {
            prompt["id"]: {"emoji_affordance": None, "note": ""}
            for prompt in normalize_prompts(bundle["prompt_bank"]["prompts"])
        },
    }


def _validate_sheet(value: dict, bundle: dict, *, adjudication: bool) -> None:
    schema = ADJUDICATION_SCHEMA if adjudication else REVIEW_SCHEMA
    if value.get("schema") != schema or not str(value.get("reviewer", "")).strip():
        raise HarnessError(f"review sheet must use {schema} and name its reviewer")
    prompts = normalize_prompts(bundle["prompt_bank"]["prompts"])
    expected_ids = {prompt["id"] for prompt in prompts}
    ratings = value.get("ratings")
    if not isinstance(ratings, dict) or set(ratings) != expected_ids:
        raise HarnessError("review sheet does not exactly cover the prompt bank")
    for prompt in prompts:
        row = ratings[prompt["id"]]
        if not isinstance(row, dict):
            raise HarnessError(f"malformed review for {prompt['id']}")
        score = row.get("emoji_affordance")
        if not isinstance(score, int) or score not in {0, 1, 2}:
            raise HarnessError(f"unfinished affordance for {prompt['id']}")
        if not adjudication and any(row.get(field) is not True for field in (
            "target_absent", "behavior_not_required", "answerable", "category_fit",
        )):
            raise HarnessError(f"review did not approve prompt {prompt['id']}")


def merge_reviews(
    bundle: dict, review_a: dict, review_b: dict, adjudication: dict,
) -> dict[str, Any]:
    _validate_sheet(review_a, bundle, adjudication=False)
    _validate_sheet(review_b, bundle, adjudication=False)
    _validate_sheet(adjudication, bundle, adjudication=True)
    if len({review_a["reviewer"].strip(), review_b["reviewer"].strip()}) != 2:
        raise HarnessError("the two reviewers must be distinct")
    output = deepcopy(bundle["prompt_bank"])
    output["status"] = "frozen"
    for prompt in output["prompts"]:
        prompt_id = prompt["id"]
        a, b = review_a["ratings"][prompt_id], review_b["ratings"][prompt_id]
        final = adjudication["ratings"][prompt_id]
        prompt["pre_output_review"] = {
            "reviews": [
                {"reviewer": review_a["reviewer"], **a},
                {"reviewer": review_b["reviewer"], **b},
            ],
            "adjudication": {
                "reviewer": adjudication["reviewer"], **final,
            },
        }
    _validate_prompts(bundle["study"], output)
    return output


def _load(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HarnessError(f"{path} must contain a mapping")
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as handle:
        yaml.safe_dump(value, handle, sort_keys=False, allow_unicode=True)
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    template = sub.add_parser("template")
    template.add_argument("--reviewer", required=True)
    template.add_argument("--adjudication", action="store_true")
    template.add_argument("--output", type=Path, required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--review-a", type=Path, required=True)
    freeze.add_argument("--review-b", type=Path, required=True)
    freeze.add_argument("--adjudication", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = load_study()
    if args.command == "template":
        value = (
            build_adjudication_template(bundle, args.reviewer)
            if args.adjudication else build_review_template(bundle, args.reviewer)
        )
    else:
        value = merge_reviews(
            bundle, _load(args.review_a), _load(args.review_b),
            _load(args.adjudication),
        )
    _write(args.output, value)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"prompt freeze failed: {exc}") from exc
