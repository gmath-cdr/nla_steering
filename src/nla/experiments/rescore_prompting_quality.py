"""Rescore direct-prompting completions against matched unsteered outputs.

The original prompting baseline used each prompted completion as its own
semantic reference.  This utility retroactively applies the frozen final-test
quality gate used for interventions: content-word Jaccard to the closest
unsteered completion with the same ``(concept_id, prompt_index)``.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


# Kept local so this CPU-only utility does not import the experiment runner and
# its model-serving dependencies.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "could", "did", "do", "does", "for", "from", "had", "has",
    "have", "how", "i", "if", "in", "into", "is", "it", "its", "may",
    "my", "of", "on", "or", "our", "should", "so", "than", "that", "the",
    "their", "then", "there", "these", "they", "this", "to", "was", "we",
    "were", "what", "when", "where", "which", "who", "why", "will", "with",
    "would", "you", "your",
}


def tokens(text: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9']+", text.lower())
        if len(word) > 2 and word not in STOPWORDS
    }


def jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(1, len(left | right))


def surface_quality(response: str) -> dict[str, float | int]:
    words = re.findall(r"[a-z0-9']+", response.lower())
    trigrams = list(zip(words, words[1:], words[2:]))
    return {
        "word_count": len(words),
        "repeated_trigram_rate": (len(trigrams) - len(set(trigrams))) / max(1, len(trigrams)),
    }


QUALITY_GATE = {
    "word_count_range": [8, 350],
    "maximum_repeated_trigram_rate": 0.10,
    "minimum_reference_similarity": 0.12,
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text())
    if not isinstance(rows, list):
        raise ValueError(f"Expected a JSON list in {path}")
    return rows


def key(row: dict[str, Any]) -> tuple[int, int]:
    return int(row["concept_id"]), int(row["prompt_index"])


def aggregate(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    if not rows:
        raise ValueError("No prompting rows to aggregate")
    n = len(rows)
    return {
        "n": n,
        "quality_pass_rate": sum(row["quality_pass"] for row in rows) / n,
        "mean_reference_similarity": sum(row["reference_similarity"] for row in rows) / n,
        "mean_repeated_trigram_rate": sum(row["repeated_trigram_rate"] for row in rows) / n,
        "mean_word_count": sum(row["word_count"] for row in rows) / n,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompting-results", required=True)
    parser.add_argument("--unsteered-results", required=True)
    parser.add_argument("--output-results", required=True)
    parser.add_argument("--output-summary", required=True)
    args = parser.parse_args()

    prompting_path = Path(args.prompting_results)
    unsteered_path = Path(args.unsteered_results)
    prompting = load_rows(prompting_path)
    unsteered = [row for row in load_rows(unsteered_path) if row.get("method") == "unsteered"]
    references: dict[tuple[int, int], list[str]] = {}
    for row in unsteered:
        references.setdefault(key(row), []).append(row["response"])

    rescored: list[dict[str, Any]] = []
    missing: list[tuple[int, int]] = []
    for row in prompting:
        matched = references.get(key(row), [])
        if not matched:
            missing.append(key(row))
            continue
        surface = surface_quality(row["response"])
        similarity = max(jaccard(tokens(row["response"]), tokens(ref)) for ref in matched)
        quality_pass = (
            QUALITY_GATE["word_count_range"][0] <= surface["word_count"] <= QUALITY_GATE["word_count_range"][1]
            and surface["repeated_trigram_rate"] <= QUALITY_GATE["maximum_repeated_trigram_rate"]
            and similarity >= QUALITY_GATE["minimum_reference_similarity"]
        )
        rescored.append({
            **row,
            **surface,
            "reference_similarity": similarity,
            "quality_pass": quality_pass,
        })
    if missing:
        unique = sorted(set(missing))
        raise ValueError(f"Missing unsteered references for {len(unique)} prompt keys: {unique[:10]}")
    if len(rescored) != len(prompting):
        raise AssertionError("Rescoring did not preserve all prompting rows")

    old_pass_rate = sum(bool(row.get("quality_pass")) for row in prompting) / len(prompting)
    output_results = Path(args.output_results)
    output_summary = Path(args.output_summary)
    output_results.parent.mkdir(parents=True, exist_ok=True)
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_results.write_text(json.dumps(rescored, indent=2) + "\n")
    summary = {
        "protocol": "prompting-matched-unsteered-quality-v1",
        "definition": "8-350 words; repeated-trigram rate <=0.10; content-word Jaccard >=0.12 to closest prompt-matched unsteered completion",
        "quality_gate": QUALITY_GATE,
        "prompting_rows": len(prompting),
        "unsteered_rows": len(unsteered),
        "matched_prompt_keys": len(references),
        "prior_self_referenced_quality_pass_rate": old_pass_rate,
        "rescored": aggregate(rescored),
    }
    output_summary.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"rescored {len(rescored)} prompting rows; quality "
        f"{old_pass_rate:.1%} -> {summary['rescored']['quality_pass_rate']:.1%}; "
        f"outputs: {output_results}, {output_summary}"
    )


if __name__ == "__main__":
    main()
