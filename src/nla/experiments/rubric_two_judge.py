"""Score the 112-item panel-14 calibration rubric with two independent LM judges.

Purpose: the rubric at artifacts/baseline_panel14/reliability_v1/calibration_review.json
was built to validate the automated quality gate and never scored. This runs two
frontier judges from different labs over it and reports:

  * per-judge label distributions
  * judge-vs-judge agreement (Cohen's kappa on `usable`)
  * the disagreement set, which localises where the gate is least trustworthy

This is NOT human validation. It is a second and third automated measure, and
must be reported as such. The paper's "automated rather than human-judged"
limitation stands regardless of what this produces.

Usage:
  export OPENROUTER_API_KEY=...
  python -m experiments.rubric_two_judge --limit 112
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from experiments import openrouter_client

RUBRIC = Path("artifacts/baseline_panel14/reliability_v1/calibration_review.json")
OUT = Path("artifacts/baseline_panel14/reliability_v1/two_judge_scores.json")

# Different labs from each other, from the models under test (Qwen/Alibaba,
# Gemma/Google), and from the incumbent judge (openai/gpt-4o-mini).
JUDGE_A = "anthropic/claude-opus-5"
JUDGE_B = "openai/gpt-5.6-sol-pro"

SYSTEM = (
    "You are grading model completions for a research paper. Judge only what is "
    "in front of you. Do not speculate about how the text was produced."
)

TEMPLATE = """Below is a prompt given to a language model and the completion it produced.

IMPORTANT: these completions were generated with a low token cap, so nearly all of
them stop mid-sentence. That truncation is an artefact of how the data was collected,
not a property of the model being studied. Judge only the text that is present, and do
not lower any score because the completion ends abruptly or is incomplete.

PROMPT:
{prompt}

COMPLETION:
{output}

Rate the completion on each axis. Answer with a JSON object only, no prose.

- relevance: 0-2. Does it address the prompt? 0 = ignores it, 1 = partially, 2 = directly.
- factual_correctness: 0-2. 0 = clear errors, 1 = minor or unverifiable, 2 = no apparent errors.
- repetition: 0-2. 0 = severely repetitive or looping, 1 = some redundancy, 2 = clean.
- fluency: 0-2. 0 = broken English, 1 = awkward, 2 = natural.
- usable: 0 or 1. Ignoring the truncated ending, is the text that IS present acceptable as the start of an answer to the prompt?

Return exactly: {{"relevance": _, "factual_correctness": _, "repetition": _, "fluency": _, "usable": _}}"""


def parse_labels(text: str) -> dict | None:
    """Pull the JSON object out of a judge response. Returns None if unparseable."""
    if not text:
        return None
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    keys = {"relevance", "factual_correctness", "repetition", "fluency", "usable"}
    if not keys.issubset(d):
        return None
    out = {}
    for k in keys:
        try:
            out[k] = int(d[k])
        except (TypeError, ValueError):
            return None
    return out


def cohens_kappa(a: list[int], b: list[int]) -> float | None:
    """Cohen's kappa for two binary raters. None if either rater is constant."""
    n = len(a)
    if n == 0:
        return None
    agree = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1, pb1 = sum(a) / n, sum(b) / n
    chance = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if abs(1 - chance) < 1e-12:
        return None
    return (agree - chance) / (1 - chance)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = all records")
    ap.add_argument("--judge-a", default=JUDGE_A)
    ap.add_argument("--judge-b", default=JUDGE_B)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-tokens", type=int, default=1500,
                    help="reasoning models consume budget before emitting JSON")
    ap.add_argument("--dry-run", action="store_true", help="print one prompt and exit")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY") and not args.dry_run:
        raise SystemExit("set OPENROUTER_API_KEY")

    doc = json.loads(RUBRIC.read_text())
    records = doc["records"]
    if args.limit:
        records = records[: args.limit]
    prompts = [(SYSTEM, TEMPLATE.format(prompt=r["prompt"], output=r["output"]))
               for r in records]

    if args.dry_run:
        print(prompts[0][1][:1200])
        print(f"\n[dry-run] {len(records)} records, judges: {args.judge_a} / {args.judge_b}")
        return

    results: dict[str, list] = {}
    for tag, model in (("a", args.judge_a), ("b", args.judge_b)):
        print(f"judging with {model} ...", flush=True)
        raw = openrouter_client.complete_batch(
            prompts, model=model, max_tokens=args.max_tokens, concurrency=args.concurrency
        )
        results[tag] = [parse_labels(t) for t in raw]
        ok = sum(1 for x in results[tag] if x)
        print(f"  parsed {ok}/{len(raw)}", flush=True)

    scored = []
    for rec, la, lb in zip(records, results["a"], results["b"]):
        scored.append({
            "review_id": rec["review_id"],
            "concept_id": rec["concept_id"],
            "prompt_index": rec["prompt_index"],
            "sample_index": rec["sample_index"],
            "judge_a": la,
            "judge_b": lb,
        })

    both = [s for s in scored if s["judge_a"] and s["judge_b"]]
    ua = [s["judge_a"]["usable"] for s in both]
    ub = [s["judge_b"]["usable"] for s in both]
    kappa = cohens_kappa(ua, ub)
    agree = sum(1 for x, y in zip(ua, ub) if x == y)
    disagreements = [s["review_id"] for s in both
                     if s["judge_a"]["usable"] != s["judge_b"]["usable"]]

    summary = {
        "rubric": str(RUBRIC),
        "n_records": len(records),
        "n_both_parsed": len(both),
        "judge_a": args.judge_a,
        "judge_b": args.judge_b,
        "usable_rate_a": (sum(ua) / len(ua)) if ua else None,
        "usable_rate_b": (sum(ub) / len(ub)) if ub else None,
        "usable_agreement": (agree / len(both)) if both else None,
        "usable_cohens_kappa": kappa,
        "disagreement_review_ids": disagreements,
        "source_truncation": ("All rubric completions are truncated mid-sentence by a "
                              "generation-time token cap (max 584 chars, only 9/112 end in "
                              "terminal punctuation). Judges were instructed to disregard "
                              "the truncated ending. Fluency and usable remain the axes "
                              "most exposed to this artefact."),
        "caveat": ("Two automated judges. NOT human validation. The paper's "
                   "'automated rather than human-judged' limitation is unchanged. "
                   "Covers panel-14 concepts only; Gemma's panel-10 shares just "
                   "concept 287."),
    }

    OUT.write_text(json.dumps({"summary": summary, "scores": scored}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
