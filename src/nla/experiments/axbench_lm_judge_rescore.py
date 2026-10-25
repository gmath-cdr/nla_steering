"""TODO #3 (narrowed scope, see questions.txt 2026-08-15 update): AxBench's
published prompting/SAE/ReFT rows don't transfer "for free" -- their concept500
configs are Gemma-2-9B/2B + GemmaScope specific, zero Qwen/Gemma-3 support. What
IS still usable is their evaluator: re-score our own already-generated
completions with AxBench's own LM-judge rubric (three pairwise ratings --
concept relevance, instruction relevance, fluency -- combined via harmonic
mean, exactly as axbench/axbench/evaluators/lm_judge.py::LMJudgeEvaluator does),
to see whether their judgment on our completions agrees with, or diverges from,
this session's strict lexical metric. No new generations; scores the saved
confirmation-30 transcripts.json.

Prompt templates below are copied verbatim from
axbench/axbench/evaluators/prompt_templates.py (UNIDIRECTIONAL_PAIRWISE_EVALUATION_
{CONCEPT_RELEVANCE,INSTRUCTION_RELEVANCE,FLUENCY}_TEMPLATE) and the rating parse /
harmonic-mean aggregation below mirror LMJudgeEvaluator._get_rating_from_completion
and .compute_metrics exactly -- reimplemented against OpenRouter (httpx) instead of
importing the axbench package, since importing it pulls in its full torch/emoji/
langdetect model zoo for three prompt strings and a regex parse.

Usage:
    OPENROUTER_API_KEY=... python -m experiments.axbench_lm_judge_rescore \
        --transcripts artifacts/qwen2.5-7b/av_edit_confirmation30_2026-08-11/transcripts.json \
        --confirmation-summary artifacts/qwen2.5-7b/av_edit_confirmation30_2026-08-11/confirmation_summary.json \
        --out artifacts/qwen2.5-7b/av_edit_confirmation30_2026-08-11/axbench_lm_judge_rescore.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiments import openrouter_client, results

PROMPT_VERSION = "axbench-lm-judge-rescore-v1"

CONCEPT_RELEVANCE_TEMPLATE = """[System]
Please act as an impartial judge and evaluate whether the specified concept is \
presented in the sentence fragment provided below. Focus solely on whether the concept is \
clearly incorporated, without regard for grammar or logical coherence.

Begin your evaluation by providing a short explanation of whether the concept is \
clearly incorporated. Point out references to the concept in the fragment, noting any \
minimal or awkward incorporations. After providing your explanation, rate the concept's \
relevance on a scale from 0 to 2, where 0 indicates the concept is not present at all, \
1 indicates the concept is somewhat present but minimally or awkwardly incorporated, \
and 2 indicates the concept is more fully and effectively incorporated, with stronger and \
more natural integration. Provide your rating using this exact format: "Rating: [[score]]".

[Concept Start]
{concept}
[Concept End]

[Sentence Fragment Start]
{sentence}
[Sentence Fragment End]
"""

INSTRUCTION_RELEVANCE_TEMPLATE = """[System]
Please act as an impartial judge and evaluate whether the \
sentence fragment provided below is related to the instruction. Focus solely \
on the degree of relatedness in terms of topic, regardless of grammar, coherence, or \
informativeness.

Begin your evaluation by providing a brief explanation of whether the \
sentence is related to the instruction, and point out references \
related to the instruction. After providing your explanation, rate the instruction \
relevance on a scale from 0 to 2, where 0 indicates the sentence is unrelated to the \
instruction, 1 indicates it is somewhat related but only minimally or indirectly relevant in terms of topic, \
and 2 indicates it is more clearly and directly related to the instruction. Provide your rating \
using this exact format: "Rating: [[score]]".

[Instruction Start]
{instruction}
[Instruction End]

[Sentence Fragment Start]
{sentence}
[Sentence Fragment End]
"""

FLUENCY_TEMPLATE = """[System]
Please act as an impartial judge and evaluate the fluency of the \
sentence fragment provided below. Focus solely on fluency, disregarding \
its completeness, relevance, coherence with any broader context, or informativeness.

Begin your evaluation by briefly describing the fluency of the sentence, noting any \
unnatural phrasing, awkward transitions, grammatical errors, or repetitive structures that \
may hinder readability. After providing your explanation, rate the sentence's fluency \
on a scale from 0 to 2, where 0 indicates the sentence is not fluent and highly unnatural \
(e.g., incomprehensible or repetitive), 1 indicates it is somewhat fluent but contains noticeable \
errors or awkward phrasing, and 2 indicates the sentence is fluent and almost perfect. \
Provide your rating using this exact format: "Rating: [[score]]".

[Sentence Fragment Start]
{sentence}
[Sentence Fragment End]
"""


def _parse_rating(completion: str) -> float:
    """Mirrors LMJudgeEvaluator._get_rating_from_completion exactly."""
    if "Rating:" not in completion:
        return 0.0
    text = completion.split("Rating:")[-1].strip().split("\n")[0].strip()
    text = text.replace("[", "").replace("]", "").rstrip(".").strip('"').strip("'").strip("*").strip()
    try:
        rating = float(text)
    except ValueError:
        return 0.0
    return rating if 0.0 <= rating <= 2.0 else 0.0


def _harmonic_mean(scores: list[float]) -> float:
    """Mirrors LMJudgeEvaluator.compute_metrics's inner harmonic_mean: 0 if any score is 0."""
    if 0 in scores:
        return 0.0
    return len(scores) / sum(1 / s for s in scores)


def _flatten(transcript_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per (concept, prompt, arm, sample) generation."""
    flat = []
    for row in transcript_rows:
        if row.get("status") != "complete":
            continue
        for arm, samples in row["generation_arms"].items():
            for sample_idx, response in enumerate(samples):
                flat.append({"concept_id": row["concept_id"], "concept": row["concept"],
                             "prompt_index": row["prompt_index"], "prompt": row["prompt"],
                             "arm": arm, "sample_idx": sample_idx, "response": response})
    return flat


def _key(entry: dict[str, Any]) -> tuple:
    return (entry["concept_id"], entry["prompt_index"], entry["arm"], entry["sample_idx"])


def evaluate(transcript_rows: list[dict[str, Any]], judged: list[dict[str, Any]], *, model: str,
             api_key_env: str, concurrency: int, output: str) -> list[dict[str, Any]]:
    """Extends `judged` in place (list of already-scored entries, possibly empty), checkpointing per concept."""
    protocol = {"provider": "openrouter", "api_endpoint": openrouter_client.API_ENDPOINT, "model": model,
                "prompt_version": PROMPT_VERSION, "api_key_env": api_key_env,
                "evaluated_at": datetime.now(timezone.utc).isoformat()}
    protocol_key = {k: protocol[k] for k in ("provider", "api_endpoint", "model", "prompt_version")}

    done = {_key(e): e for e in judged if e.get("protocol") == protocol_key}
    flat = _flatten(transcript_rows)
    by_concept: dict[int, list[dict[str, Any]]] = {}
    for entry in flat:
        by_concept.setdefault(entry["concept_id"], []).append(entry)

    for concept_id, entries in sorted(by_concept.items()):
        pending = [e for e in entries if _key(e) not in done]
        if not pending:
            continue
        prompts: list[tuple[str | None, str]] = []
        for e in pending:
            prompts.append((None, CONCEPT_RELEVANCE_TEMPLATE.format(concept=e["concept"], sentence=e["response"])))
            prompts.append((None, INSTRUCTION_RELEVANCE_TEMPLATE.format(instruction=e["prompt"], sentence=e["response"])))
            prompts.append((None, FLUENCY_TEMPLATE.format(sentence=e["response"])))
        completions = openrouter_client.complete_batch(prompts, model=model, api_key_env=api_key_env,
                                                         concurrency=concurrency, max_tokens=200)
        for i, e in enumerate(pending):
            concept_raw, instruction_raw, fluency_raw = completions[3 * i:3 * i + 3]
            concept_r, instruction_r, fluency_r = (_parse_rating(concept_raw), _parse_rating(instruction_raw),
                                                     _parse_rating(fluency_raw))
            new_entry = {**e,
                         "ratings": {"concept_relevance": concept_r, "instruction_relevance": instruction_r,
                                     "fluency": fluency_r,
                                     "lm_judge_rating": _harmonic_mean([concept_r, instruction_r, fluency_r])},
                         "raw_completions": {"concept": concept_raw, "instruction": instruction_raw, "fluency": fluency_raw},
                         "protocol": protocol_key}
            judged.append(new_entry)
            done[_key(e)] = new_entry
        results.save(output, judged)
        print(f"  concept {concept_id}: scored {len(pending)} generations ({len(done)}/{len(flat)} total)")

    return judged


def summarize(judged: list[dict[str, Any]], confirmation_summary: dict[str, Any] | None) -> dict[str, Any]:
    by_arm: dict[str, list[dict[str, Any]]] = {}
    by_concept_arm: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for e in judged:
        by_arm.setdefault(e["arm"], []).append(e)
        by_concept_arm.setdefault((e["concept_id"], e["arm"]), []).append(e)

    def _mean(entries: list[dict[str, Any]], field: str) -> float:
        vals = [entry["ratings"][field] for entry in entries]
        return sum(vals) / len(vals) if vals else 0.0

    arms_summary = [{"arm": arm, "n": len(entries),
                     "mean_concept_relevance": _mean(entries, "concept_relevance"),
                     "mean_instruction_relevance": _mean(entries, "instruction_relevance"),
                     "mean_fluency": _mean(entries, "fluency"),
                     "mean_lm_judge_rating": _mean(entries, "lm_judge_rating"),
                     "concept_relevance_hit_rate (>=1)": sum(e["ratings"]["concept_relevance"] >= 1 for e in entries) / len(entries) if entries else 0.0}
                    for arm, entries in sorted(by_arm.items())]

    strict_hit_by_concept_arm = {}
    if confirmation_summary:
        for c in confirmation_summary["concepts"]:
            for arm, stats in c["arms"].items():
                strict_hit_by_concept_arm[(c["concept_id"], arm)] = stats["hit_rate"]

    divergence = []
    for (concept_id, arm), entries in sorted(by_concept_arm.items()):
        axbench_hit = sum(e["ratings"]["concept_relevance"] >= 1 for e in entries) / len(entries) if entries else 0.0
        strict_hit = strict_hit_by_concept_arm.get((concept_id, arm))
        divergence.append({"concept_id": concept_id, "arm": arm, "n": len(entries),
                           "axbench_concept_relevance_hit_rate": axbench_hit,
                           "strict_lexical_hit_rate": strict_hit,
                           "delta (axbench - strict)": (axbench_hit - strict_hit) if strict_hit is not None else None})

    return {"arms": arms_summary, "per_concept_per_arm_divergence": divergence}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcripts", required=True)
    parser.add_argument("--confirmation-summary", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default=openrouter_client.DEFAULT_MODEL)
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--concurrency", type=int, default=16)
    args = parser.parse_args(argv)

    transcript_rows = json.loads(Path(args.transcripts).read_text())
    judged = results.load(args.out)
    judged = evaluate(transcript_rows, judged, model=args.model, api_key_env=args.api_key_env,
                       concurrency=args.concurrency, output=args.out)

    confirmation_summary = None
    if args.confirmation_summary:
        confirmation_summary = json.loads(Path(args.confirmation_summary).read_text())
    summary = summarize(judged, confirmation_summary)
    summary_path = Path(args.out).with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"axbench lm-judge rescore -> {args.out} (summary: {summary_path})")
    for arm in summary["arms"]:
        print(f"  {arm['arm']:12s} n={arm['n']:4d} lm_judge_rating={arm['mean_lm_judge_rating']:.3f} "
              f"concept={arm['mean_concept_relevance']:.3f} instruction={arm['mean_instruction_relevance']:.3f} "
              f"fluency={arm['mean_fluency']:.3f}")


if __name__ == "__main__":
    main()
