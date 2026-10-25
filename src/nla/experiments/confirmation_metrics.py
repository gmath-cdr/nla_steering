"""Apply frozen causal and semantic-quality gates to AV-edit confirmation output."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from experiments.discovery500 import bootstrap_lower, matches


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
    return {word for word in re.findall(r"[a-z0-9']+", text.lower())
            if len(word) > 2 and word not in STOPWORDS}


def jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(1, len(left | right))


def surface_quality(response: str) -> dict[str, Any]:
    words = re.findall(r"[a-z0-9']+", response.lower())
    trigrams = list(zip(words, words[1:], words[2:]))
    repeated = len(trigrams) - len(set(trigrams))
    return {
        "word_count": len(words),
        "repeated_trigram_rate": repeated / max(1, len(trigrams)),
    }


def evaluate_prompt(row: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    arms = row["generation_arms"]
    references = [tokens(response) for response in arms["unsteered"]]
    output: dict[str, Any] = {"prompt_index": row["prompt_index"], "arms": {}}
    for arm, responses in arms.items():
        samples = []
        for response in responses:
            surface = surface_quality(response)
            similarity = max((jaccard(tokens(response), ref) for ref in references), default=0.0)
            hits = matches(response, terms)
            quality_pass = (
                8 <= surface["word_count"] <= 350
                and surface["repeated_trigram_rate"] <= 0.10
                and similarity >= 0.12
            )
            samples.append({
                "concept_hit": bool(hits),
                "matched_terms": hits,
                "target_term_count": len(hits),
                "reference_similarity": similarity,
                "quality_pass": quality_pass,
                **surface,
            })
        output["arms"][arm] = samples
    return output


def aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(samples)
    return {
        "n": n,
        "hit_rate": sum(sample["concept_hit"] for sample in samples) / n,
        "quality_pass_rate": sum(sample["quality_pass"] for sample in samples) / n,
        "mean_reference_similarity": sum(sample["reference_similarity"] for sample in samples) / n,
        "mean_repeated_trigram_rate": sum(sample["repeated_trigram_rate"] for sample in samples) / n,
        "mean_target_term_count": sum(sample["target_term_count"] for sample in samples) / n,
        "mean_word_count": sum(sample["word_count"] for sample in samples) / n,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--target-count", type=int, default=20)
    args = parser.parse_args()
    run = Path(args.run_dir)
    protocol = json.loads((run / "protocol.json").read_text())
    summaries = json.loads((run / "summary.json").read_text())["concepts"]
    transcripts = json.loads((run / "transcripts.json").read_text())
    lexicons = json.loads((run / "lexicons.json").read_text())
    by_concept = {
        cid: [row for row in transcripts if int(row["concept_id"]) == cid]
        for cid in protocol["concept_ids"]
    }
    discovery_summary = {int(row["concept_id"]): row for row in summaries}
    evaluated = []
    for cid in protocol["concept_ids"]:
        terms = lexicons[str(cid)]
        prompt_rows = [evaluate_prompt(row, terms) for row in by_concept[cid]]
        flat = {
            arm: [sample for prompt in prompt_rows for sample in prompt["arms"][arm]]
            for arm in ("unsteered", "av_edit", "random")
        }
        arms = {arm: aggregate(samples) for arm, samples in flat.items()}
        av_flags = [sample["concept_hit"] for sample in flat["av_edit"]]
        base_flags = [sample["concept_hit"] for sample in flat["unsteered"]]
        random_flags = [sample["concept_hit"] for sample in flat["random"]]
        prompt_coverage = sum(
            any(sample["concept_hit"] for sample in prompt["arms"]["av_edit"])
            for prompt in prompt_rows
        ) / len(prompt_rows)
        lift_base = arms["av_edit"]["hit_rate"] - arms["unsteered"]["hit_rate"]
        lift_random = arms["av_edit"]["hit_rate"] - arms["random"]["hit_rate"]
        lower_base = bootstrap_lower(av_flags, base_flags)
        lower_random = bootstrap_lower(av_flags, random_flags)
        causal_pass = (
            lift_base >= 0.20 and lift_random >= 0.20 and prompt_coverage >= 0.50
            and lower_base > 0 and lower_random > 0
        )
        quality_pass = (
            arms["av_edit"]["quality_pass_rate"] >= 0.75
            and arms["av_edit"]["mean_reference_similarity"] >= 0.12
            and arms["av_edit"]["mean_repeated_trigram_rate"] <= 0.10
            and arms["av_edit"]["mean_target_term_count"] <= 4.0
        )
        confirmed = causal_pass and quality_pass
        evaluated.append({
            "concept_id": cid,
            "concept": discovery_summary[cid]["concept"],
            "terms": terms,
            "arms": arms,
            "prompt_coverage": prompt_coverage,
            "lift_vs_unsteered": lift_base,
            "lift_vs_random": lift_random,
            "bootstrap_lower_vs_unsteered": lower_base,
            "bootstrap_lower_vs_random": lower_random,
            "causal_pass": causal_pass,
            "semantic_quality_pass": quality_pass,
            "confirmed": confirmed,
            "score": min(lift_base, lift_random) * prompt_coverage
                     * arms["av_edit"]["quality_pass_rate"],
            "prompt_metrics": prompt_rows,
        })
    evaluated.sort(key=lambda row: (row["confirmed"], row["score"]), reverse=True)
    confirmed = [row["concept_id"] for row in evaluated if row["confirmed"]][:args.target_count]
    bundle = {
        "protocol_version": "qwen-av-edit-confirmation-metrics-v1",
        "quality_gate": {
            "reference_similarity": "content-word Jaccard to closest prompt-matched unsteered answer",
            "minimum_per_response_similarity": 0.12,
            "minimum_quality_pass_rate": 0.75,
            "maximum_repeated_trigram_rate": 0.10,
            "word_count_range": [8, 350],
            "maximum_mean_target_term_count": 4.0,
            "limitation": "Automated semantic-preservation proxy; blind human or independent LLM audit is still recommended.",
        },
        "confirmed_concept_ids": confirmed,
        "confirmed_count": len(confirmed),
        "target_count": args.target_count,
        "target_reached": len(confirmed) >= args.target_count,
        "concepts": evaluated,
    }
    (run / "confirmation_summary.json").write_text(json.dumps(bundle, indent=2) + "\n")
    lines = [
        "# AV-edit held-out confirmation", "",
        "| Rank | ID | Concept | AV hit | Lift/base | Lift/random | Coverage | Quality | Terms/output | Decision |",
        "|---:|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for rank, row in enumerate(evaluated, 1):
        av = row["arms"]["av_edit"]
        decision = "CONFIRMED" if row["confirmed"] else (
            "QUALITY_FAIL" if row["causal_pass"] else "CAUSAL_FAIL")
        lines.append(
            f"| {rank} | {row['concept_id']} | {row['concept']} | {av['hit_rate']:.1%} | "
            f"{row['lift_vs_unsteered']:.1%} | {row['lift_vs_random']:.1%} | "
            f"{row['prompt_coverage']:.1%} | {av['quality_pass_rate']:.1%} | "
            f"{av['mean_target_term_count']:.2f} | {decision} |"
        )
    (run / "confirmation_report.md").write_text("\n".join(lines) + "\n")
    print(f"confirmed {len(confirmed)}/{args.target_count} -> {run}")


if __name__ == "__main__":
    main()
