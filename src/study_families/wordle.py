"""Deterministic reanalysis of the exploratory Qwen and Gemma Wordle runs.

Every reported Wordle direction is an authored AR text contrast. This module
uses the exact archived answer-extraction preference order and keeps the early
single-prompt sweeps separate from the later four-prompt administrations.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any, Iterable

from study_families.common import ArtifactError, DATA_ROOT


FAMILY = DATA_ROOT / "freeform_wordle"
RAW = FAMILY / "raw"

_ALPHA_TOKEN = re.compile(r"[A-Za-z]+")
_QUOTED_ANY = re.compile(r"[\"'“‘]([A-Za-z]{3,9})[.,!?]?[\"'”’]")
_SPELLED_ANY = re.compile(r"\b[A-Za-z](?:\s*-\s*[A-Za-z]){2,8}\b")
_LEADING_MARKUP = re.compile(r"^[^A-Za-z]*([A-Za-z]{3,9})\b")


def extract_answer(text: str) -> str:
    """Extract a 3--9 letter answer using the archived probe's preference order."""

    spelled = _SPELLED_ANY.search(text)
    if spelled:
        return re.sub(r"[\s-]+", "", spelled.group(0)).lower()
    quoted = _QUOTED_ANY.search(text)
    if quoted:
        return quoted.group(1).lower()
    tokens = _ALPHA_TOKEN.findall(text)
    for token in tokens:
        if 3 <= len(token) <= 9 and token.isupper():
            return token.lower()
    for token in reversed(tokens):
        if 3 <= len(token) <= 9:
            return token.lower()
    return ""


def extract_leading_answer(text: str) -> str:
    """Return the first answer-like word, ignoring leading Markdown markup.

    The early exact-HOUSE experiment reported whether the response *led* with
    HOUSE.  That claim is slightly different from the later generic Wordle
    probe, which deliberately falls back to the final 3--9 letter word.  We
    expose both measures so the archived claim is reproducible without
    silently changing the generic extractor.
    """

    match = _LEADING_MARKUP.search(text)
    return match.group(1).lower() if match else ""


def answer_bucket(answer: str) -> str:
    """Return the mutually exclusive categories used in the submitted figure."""

    if answer == "crane":
        return "crane"
    if answer == "home":
        return "home"
    if answer in {"house", "homes"}:
        return "house_or_homes"
    if answer:
        return "other"
    return "no_answer"


def _load_result(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ArtifactError(f"malformed Wordle result: {path}")
    spec = payload.get("spec", {})
    # Unsteered census specs predate the explicit ``alphas: [0]`` convention.
    # Their records still carry alpha zero, so treat an omitted ladder as one
    # baseline cell rather than weakening the cardinality check.
    alpha_count = len(spec.get("alphas", [])) or 1
    expected = alpha_count * len(spec.get("prompts", [])) * int(
        spec.get("samples_per_prompt", 0)
    )
    if len(payload["records"]) != expected:
        raise ArtifactError(
            f"Wordle result cardinality mismatch for {path.name}: "
            f"expected {expected}, got {len(payload['records'])}"
        )
    cells = [
        (float(row["alpha"]), str(row["prompt"]), int(row["sample"]))
        for row in payload["records"]
    ]
    if len(cells) != len(set(cells)):
        raise ArtifactError(f"duplicate Wordle cells in {path.name}")
    return payload


def _find(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise ArtifactError(f"expected one Wordle file for {pattern}, found {len(matches)}")
    return matches[0]


def _rows_at(payload: dict[str, Any], alpha: float) -> list[dict[str, Any]]:
    rows = [row for row in payload["records"] if abs(float(row["alpha"]) - alpha) < 1e-12]
    if not rows:
        raise ArtifactError(f"Wordle result has no rows at alpha={alpha}")
    return rows


def _answer_counts(rows: Iterable[dict[str, Any]]) -> Counter[str]:
    return Counter(extract_answer(str(row["text"])) for row in rows)


def _figure_ladder(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
    ladder: dict[str, dict[str, int]] = {}
    for alpha in payload["spec"]["alphas"]:
        rows = _rows_at(payload, float(alpha))
        buckets = Counter(answer_bucket(extract_answer(str(row["text"]))) for row in rows)
        ladder[str(float(alpha))] = {
            key: int(buckets[key])
            for key in ("crane", "home", "house_or_homes", "other", "no_answer")
        }
    return ladder


def _mean_answer_length(payload: dict[str, Any], alpha: float) -> float:
    answers = [extract_answer(str(row["text"])) for row in _rows_at(payload, alpha)]
    return sum(len(answer) for answer in answers) / len(answers)


def rebuild_summary() -> dict[str, Any]:
    """Recompute all Wordle counts used in the main text and appendix."""

    qwen_wordle2 = RAW / "qwen_wordle2"
    qwen_early = RAW / "qwen_early"
    gemma = RAW / "gemma"

    # Verify the complete curated raw inventory before selecting headline arms.
    early_paths = sorted(qwen_early.glob("*.json"))
    wordle2_paths = sorted(qwen_wordle2.glob("*.json"))
    gemma_paths = sorted(gemma.glob("*.json"))
    if (len(early_paths), len(wordle2_paths), len(gemma_paths)) != (19, 11, 4):
        raise ArtifactError(
            "unexpected Wordle file inventory: "
            f"early={len(early_paths)}, wordle2={len(wordle2_paths)}, gemma={len(gemma_paths)}"
        )
    all_payloads = {path.name: _load_result(path) for path in early_paths + wordle2_paths + gemma_paths}

    qwen_dwell = all_payloads[_find(qwen_wordle2, "wordle2-ff-dwell_*.json").name]
    qwen_random = all_payloads[_find(qwen_wordle2, "wordle2-random_*.json").name]
    qwen_census = all_payloads[_find(qwen_wordle2, "wordle2-census_*.json").name]
    early_dwell = all_payloads[_find(qwen_early, "wcrank-semantic_*.json").name]
    early_exact = all_payloads[_find(qwen_early, "wcrank-verb_*.json").name]
    early_random = all_payloads[_find(qwen_early, "wcrank-control_*.json").name]
    gemma_dwell = all_payloads[_find(gemma, "gcore-g-dwell_*.json").name]
    gemma_random = all_payloads[_find(gemma, "gcore-random-wordle_*.json").name]
    gemma_census = all_payloads[_find(gemma, "gcore-census-wordle_*.json").name]

    qwen_census_rows = qwen_census["records"]
    gemma_census_rows = gemma_census["records"]

    # Length controls are summarized descriptively because the tested arms and
    # random direction shift together as generations degrade.
    qwen_length_names = (
        "wordle2-len-four-b_*.json",
        "wordle2-len-six-b_*.json",
        "wordle2-len-seven-b_*.json",
        "wordle2-len-digit-six-b_*.json",
    )
    qwen_length_means: dict[str, dict[str, float]] = {}
    for pattern in qwen_length_names:
        path = _find(qwen_wordle2, pattern)
        payload = all_payloads[path.name]
        qwen_length_means[payload["spec"]["name"]] = {
            str(float(alpha)): _mean_answer_length(payload, float(alpha))
            for alpha in payload["spec"]["alphas"]
        }
    gemma_length = all_payloads[_find(gemma, "gcore-g-len-seven_*.json").name]

    return {
        "schema": "paper-wordle-reanalysis/v1",
        "scope": {
            "qwen_early_files": len(early_paths),
            "qwen_wordle2_files": len(wordle2_paths),
            "gemma_files": len(gemma_paths),
            "total_records": sum(len(payload["records"]) for payload in all_payloads.values()),
            "method": "authored AR text contrasts",
            "evidence_intent": "exploratory",
        },
        "qwen_natural_baseline": {
            "n": len(qwen_census_rows),
            "crane": _answer_counts(qwen_census_rows)["crane"],
            "five_letter": sum(len(extract_answer(str(row["text"]))) == 5 for row in qwen_census_rows),
        },
        "gemma_natural_baseline": {
            "n": len(gemma_census_rows),
            "crane": _answer_counts(gemma_census_rows)["crane"],
        },
        "qwen_early_dwelling": {
            "home_by_alpha": {
                str(alpha): _answer_counts(_rows_at(early_dwell, alpha))["home"]
                for alpha in (0.4, 0.5, 0.65)
            },
            "random_home_or_house_all_alphas": sum(
                count
                for answer, count in _answer_counts(early_random["records"]).items()
                if answer in {"home", "house"}
            ),
        },
        "qwen_early_exact_house": {
            "leading_house_at_0.8": sum(
                extract_leading_answer(str(row["text"])) == "house"
                for row in _rows_at(early_exact, 0.8)
            ),
            # The later generic extractor returns the last short word for one
            # rambling response that starts with House but ends in "like".
            "generic_probe_house_at_0.8": _answer_counts(
                _rows_at(early_exact, 0.8)
            )["house"],
        },
        "qwen_natural_dwelling": {
            "home_at_0.4": _answer_counts(_rows_at(qwen_dwell, 0.4))["home"],
            "home_at_0.6": _answer_counts(_rows_at(qwen_dwell, 0.6))["home"],
            "home_across_headline_doses": sum(
                _answer_counts(_rows_at(qwen_dwell, alpha))["home"] for alpha in (0.4, 0.6)
            ),
            "random_home_all_alphas": _answer_counts(qwen_random["records"])["home"],
            "figure_ladder": _figure_ladder(qwen_dwell),
        },
        "gemma_natural_dwelling": {
            "house_at_0.2": _answer_counts(_rows_at(gemma_dwell, 0.2))["house"],
            "home_at_0.2": _answer_counts(_rows_at(gemma_dwell, 0.2))["home"],
            "random_house_or_home_all_alphas": sum(
                count
                for answer, count in _answer_counts(gemma_random["records"]).items()
                if answer in {"home", "house"}
            ),
            "figure_ladder": _figure_ladder(gemma_dwell),
        },
        "length_controls_descriptive": {
            "qwen_treatments": qwen_length_means,
            "qwen_random": {
                str(float(alpha)): _mean_answer_length(qwen_random, float(alpha))
                for alpha in qwen_random["spec"]["alphas"]
            },
            "gemma_seven": {
                str(float(alpha)): _mean_answer_length(gemma_length, float(alpha))
                for alpha in gemma_length["spec"]["alphas"]
            },
            "gemma_random": {
                str(float(alpha)): _mean_answer_length(gemma_random, float(alpha))
                for alpha in gemma_random["spec"]["alphas"]
            },
        },
    }
