"""Predeclared lexicon-based strict metrics for AxBench steering runs."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "axbench-lexicon-strict-v1"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def _hits(text: str, terms: list[str]) -> list[str]:
    pattern = re.compile(r"(?i)(?<!\w)(?:" + "|".join(
        re.escape(term) for term in sorted(terms, key=len, reverse=True)) + r")(?!\w)")
    return pattern.findall(text)


def _bootstrap_lower(left: list[bool], right: list[bool], *, confidence: float, resamples: int) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    rng = random.Random(0)
    differences = []
    for _ in range(resamples):
        indices = [rng.randrange(len(left)) for _ in left]
        differences.append(sum(left[index] for index in indices) / len(indices) -
                           sum(right[index] for index in indices) / len(indices))
    differences.sort()
    return differences[max(0, int((1 - confidence) * len(differences)))]


def _arm(row: dict[str, Any], terms: list[str], n_samples: int) -> dict[str, Any]:
    hits = []
    prompt_hits: list[list[bool]] = []
    for index, sample in enumerate(row["per_prompt"]):
        matches = _hits(sample["response"], terms)
        sample["strict"] = {"strict_hit": bool(matches), "matched_terms": matches,
                            "target_term_count": len(matches), "protocol_version": PROTOCOL_VERSION}
        hits.append(bool(matches))
        if index % n_samples == 0:
            prompt_hits.append([])
        prompt_hits[-1].append(bool(matches))
    counts = sorted(sample["strict"]["target_term_count"] for sample in row["per_prompt"])
    median = (counts[(len(counts) - 1) // 2] + counts[len(counts) // 2]) / 2 if counts else 0.0
    return {"label": row["label"], "n_samples": len(hits), "strict_hit_rate": sum(hits) / len(hits) if hits else 0.0,
            "median_target_terms": median, "prompts_any_hit": sum(any(group) for group in prompt_hits),
            "prompts_majority_hit": sum(sum(group) > len(group) / 2 for group in prompt_hits),
            "n_prompts": len(prompt_hits), "_hits": hits}


def evaluate(rows: list[dict[str, Any]], *, terms_path: str, n_samples_per_prompt: int,
             min_lift: float, confidence: float, resamples: int, min_prompt_coverage: float,
             output: str) -> dict[str, Any]:
    terms = json.loads(Path(terms_path).read_text())
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        concept_id = int(row["label"].split("__", 1)[0][1:])
        if str(concept_id) not in terms:
            raise KeyError(f"missing strict lexicon for concept {concept_id}")
        grouped.setdefault(concept_id, []).append(row)
    concepts = []
    for concept_id, concept_rows in sorted(grouped.items()):
        arms = [_arm(row, terms[str(concept_id)], n_samples_per_prompt) for row in concept_rows]
        by_label = {arm["label"]: arm for arm in arms}
        baseline = by_label.get(f"c{concept_id}__unsteered")
        decisions = []
        for arm in arms:
            if "__av_edit__ratio_" not in arm["label"] or not baseline:
                continue
            alpha = arm["label"].rsplit("_", 1)[-1]
            random_arms = [candidate for candidate in arms
                           if candidate["label"].endswith(f"__ratio_{alpha}") and "__random_seed_" in candidate["label"]]
            best_random = max(random_arms, key=lambda candidate: candidate["strict_hit_rate"], default=None)
            lift_baseline = arm["strict_hit_rate"] - baseline["strict_hit_rate"]
            lift_random = arm["strict_hit_rate"] - (best_random["strict_hit_rate"] if best_random else 0.0)
            lower_baseline = _bootstrap_lower(arm["_hits"], baseline["_hits"], confidence=confidence, resamples=resamples)
            lower_random = _bootstrap_lower(arm["_hits"], best_random["_hits"], confidence=confidence, resamples=resamples) if best_random else lift_random
            coverage = arm["prompts_any_hit"] / max(1, arm["n_prompts"])
            supported = (lift_baseline >= min_lift and lift_random >= min_lift and
                         lower_baseline > 0 and lower_random > 0 and coverage >= min_prompt_coverage)
            decisions.append({"alpha": float(alpha), "av_edit_hit_rate": arm["strict_hit_rate"],
                              "unsteered_hit_rate": baseline["strict_hit_rate"],
                              "highest_random_hit_rate": best_random["strict_hit_rate"] if best_random else None,
                              "strict_lift_vs_unsteered": lift_baseline, "strict_lift_vs_best_random": lift_random,
                              "bootstrap_lower_vs_unsteered": lower_baseline,
                              "bootstrap_lower_vs_best_random": lower_random,
                              "prompt_any_hit_coverage": coverage,
                              "decision": "supported" if supported else "not_supported"})
        for arm in arms:
            arm.pop("_hits")
        concepts.append({"concept_id": concept_id, "arms": arms, "decisions": decisions})
    payload = {"protocol": {"version": PROTOCOL_VERSION, "terms_path": terms_path,
                              "minimum_lift": min_lift, "confidence": confidence,
                              "bootstrap_resamples": resamples,
                              "minimum_prompt_any_hit_coverage": min_prompt_coverage},
               "concepts": concepts}
    _atomic_json(Path(output), payload)
    return payload
