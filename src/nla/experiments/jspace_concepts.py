"""Relate multi-concept steering outcomes to fitted J-space geometry."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import torch

from experiments import jspace, results, vectors


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    output = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + end - 1) / 2 + 1
        for index in order[start:end]:
            output[index] = rank
        start = end
    return output


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3:
        return None
    left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    denominator = math.sqrt(sum((x - left_mean) ** 2 for x in left) *
                            sum((y - right_mean) ** 2 for y in right))
    return numerator / denominator if denominator else None


def _strict(texts: list[str], terms: list[str]) -> tuple[float, float]:
    pattern = re.compile(r"(?i)(?<!\w)(?:" + "|".join(
        re.escape(term) for term in sorted(terms, key=len, reverse=True)) + r")(?!\w)")
    counts = [len(pattern.findall(text)) for text in texts]
    ordered = sorted(counts)
    median = (ordered[(len(ordered) - 1) // 2] + ordered[len(ordered) // 2]) / 2
    return sum(count > 0 for count in counts) / len(counts), median


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lens", required=True)
    parser.add_argument("--layer", required=True, type=int)
    parser.add_argument("--vectors-json", required=True,
                        help="JSON object mapping concept IDs to vector paths")
    parser.add_argument("--terms-json", required=True,
                        help="JSON object mapping concept IDs to term lists")
    parser.add_argument("--results", required=True, help="comma-separated result JSON files")
    parser.add_argument("--alpha", required=True, type=float)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    import jlens

    vector_paths = json.loads(Path(args.vectors_json).read_text())
    terms = json.loads(Path(args.terms_json).read_text())
    rows = []
    for path in args.results.split(","):
        rows.extend(results.load(path))
    by_label = {row["label"]: row for row in rows}
    lens = jlens.JacobianLens.load(args.lens)
    concepts = []
    for concept_id, path in vector_paths.items():
        direction = vectors.load(path)
        geometry = jspace.geometry(lens, args.layer, {concept_id: direction})[concept_id]
        label = f"c{concept_id}__ar_delta__ratio_{args.alpha}"
        baseline_label = f"c{concept_id}__unsteered"
        if label not in by_label or baseline_label not in by_label:
            raise KeyError(f"missing result arm {label!r} or {baseline_label!r}")
        steered = [item["response"] for item in by_label[label]["per_prompt"]]
        baseline = [item["response"] for item in by_label[baseline_label]["per_prompt"]]
        hit, median_terms = _strict(steered, terms[concept_id])
        baseline_hit, _ = _strict(baseline, terms[concept_id])
        concepts.append({"concept_id": int(concept_id), "vector": path, **geometry,
                         "strict_hit_rate": hit, "unsteered_hit_rate": baseline_hit,
                         "strict_lift": hit - baseline_hit,
                         "median_target_terms": median_terms,
                         "mean_concept_score": by_label[label]["mean_concept"],
                         "mean_fluency_proxy": by_label[label]["mean_fluency"]})
    lifts = [row["strict_lift"] for row in concepts]
    projection = [row["projection_fraction"] for row in concepts]
    alignment = [row["jacobian_alignment"] for row in concepts]
    output = {
        "layer": args.layer, "alpha": args.alpha, "n_concepts": len(concepts),
        "concepts": concepts,
        "spearman": {
            "projection_fraction_vs_strict_lift": _pearson(_ranks(projection), _ranks(lifts)),
            "jacobian_alignment_vs_strict_lift": _pearson(_ranks(alignment), _ranks(lifts)),
        },
        "caution": "Exploratory correlation over a small, selected concept set; not a general predictive claim."
    }
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(output, indent=2) + "\n")
    temporary.replace(target)
    print(f"concept analysis -> {target}")


if __name__ == "__main__":
    main()
