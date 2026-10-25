"""TODO #5: semantic/paraphrase-drift judge.

Scores already-saved reframing-arm transcripts (experiments/reframing.py output)
for whether a response's *theme* drifted toward the target topic without using
its literal vocabulary -- the case the strict lexical metric and the concept-
presence judge (axbench_judge.py) both miss by construction. No new generations.

Usage:
    OPENROUTER_API_KEY=... python -m experiments.paraphrase_drift_judge \
        --transcripts artifacts/cross_model_extended_2026-08-08/qwen/results/reframing_c3.json \
        --out artifacts/cross_model_extended_2026-08-08/qwen/results/reframing_c3.drift_judge.json
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiments import openrouter_client, results

PROMPT_VERSION = "paraphrase-drift-v1"

SYSTEM_PROMPT = """You are evaluating whether a piece of text has thematically drifted toward a target topic, even if it never uses the topic's own literal vocabulary. This is different from simple keyword matching.

Given a TARGET TOPIC, its literal vocabulary, and a RESPONSE TEXT, judge two independent things:
1. literal_mention: true if the response contains, describes, or clearly names any of the target topic's own literal terms (the words listed as "Literal terms").
2. thematic_drift: true if the response's framing, imagery, or word choice has shifted toward the underlying theme of the target topic (e.g. risking something of value on an uncertain outcome, odds, chance, stakes) WITHOUT using its literal terms. This must be a real shift in emphasis compared to a neutral, on-topic answer to the same prompt -- not just one incidental word.

Return JSON only: {"literal_mention": boolean, "thematic_drift": boolean, "drift_strength": integer, "rationale": string}.
drift_strength: 0 = no discernible pull toward the target topic's theme; 1 = a mild, occasional pull; 2 = the response's central framing has clearly shifted toward the target topic's theme."""

DEFAULT_TERMS = ["casino", "casinos", "gambling", "gamble", "gambled", "gambler",
                  "bet", "bets", "betting", "wager", "wagering", "wagered",
                  "poker", "roulette", "stake", "stakes", "staking", "odds", "jackpot"]


def _user_prompt(concept: str, terms: list[str], response: str) -> str:
    return f"Target topic: {concept}\nLiteral terms: {', '.join(terms)}\n\nResponse text:\n{response}"


def _parse(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"judge did not return JSON: {text[:200]!r}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed.get("literal_mention"), bool) or not isinstance(parsed.get("thematic_drift"), bool):
        raise ValueError(f"judge response missing boolean fields: {text[:200]!r}")
    return {"literal_mention": parsed["literal_mention"], "thematic_drift": parsed["thematic_drift"],
            "drift_strength": int(parsed.get("drift_strength", 0)), "rationale": str(parsed.get("rationale", "")),
            "raw_response": text}


def evaluate(rows: list[dict[str, Any]], concept: str, *, terms: list[str], model: str, api_key_env: str,
             concurrency: int, output: str) -> dict[str, Any]:
    """Annotate result rows in place with sample['drift_judge'], checkpointing after every arm."""
    protocol = {"provider": "openrouter", "api_endpoint": openrouter_client.API_ENDPOINT, "model": model,
                "prompt_version": PROMPT_VERSION, "api_key_env": api_key_env,
                "evaluated_at": datetime.now(timezone.utc).isoformat()}
    protocol_key = {k: protocol[k] for k in ("provider", "api_endpoint", "model", "prompt_version")}

    for row in rows:
        pending = [sample for sample in row["per_prompt"]
                   if not (sample.get("drift_judge") and sample["drift_judge"].get("protocol") == protocol_key)]
        if not pending:
            continue
        prompts = [(SYSTEM_PROMPT, _user_prompt(concept, terms, sample["response"])) for sample in pending]
        completions = openrouter_client.complete_batch(prompts, model=model, api_key_env=api_key_env,
                                                         concurrency=concurrency, json_mode=True, max_tokens=220)
        for sample, text in zip(pending, completions):
            sample["drift_judge"] = {**_parse(text), "protocol": protocol_key}
        results.save(output, rows)

    summary_rows = []
    for row in rows:
        judged = [sample["drift_judge"] for sample in row["per_prompt"] if "drift_judge" in sample]
        n = len(judged)
        literal_rate = sum(j["literal_mention"] for j in judged) / n if n else 0.0
        drift_rate = sum(j["thematic_drift"] for j in judged) / n if n else 0.0
        pure_drift_rate = sum(j["thematic_drift"] and not j["literal_mention"] for j in judged) / n if n else 0.0
        mean_strength = sum(j["drift_strength"] for j in judged) / n if n else 0.0
        summary_rows.append({"label": row["label"], "n_samples": n, "literal_mention_rate": literal_rate,
                             "thematic_drift_rate": drift_rate,
                             "pure_drift_rate (drift without literal terms)": pure_drift_rate,
                             "mean_drift_strength": mean_strength})
    return {"judge_protocol": protocol, "concept": concept, "terms": terms, "arms": summary_rows}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcripts", required=True, help="path to a reframing.py results.arm() JSON file")
    parser.add_argument("--out", required=True)
    parser.add_argument("--concept", default="casinos, gambling, betting, poker, roulette")
    parser.add_argument("--terms", default=None, help="comma-separated literal terms; defaults to DEFAULT_TERMS")
    parser.add_argument("--model", default=openrouter_client.DEFAULT_MODEL)
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--concurrency", type=int, default=16)
    args = parser.parse_args(argv)

    rows = results.load(args.transcripts)
    if not rows:
        raise SystemExit(f"no rows found in {args.transcripts}")
    terms = args.terms.split(",") if args.terms else DEFAULT_TERMS

    summary = evaluate(rows, args.concept, terms=terms, model=args.model, api_key_env=args.api_key_env,
                        concurrency=args.concurrency, output=args.out)
    summary_path = Path(args.out).with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"paraphrase-drift judge -> {args.out} (summary: {summary_path})")
    for arm in summary["arms"]:
        print(f"  {arm['label']:28s} literal={arm['literal_mention_rate']:.2f} "
              f"drift={arm['thematic_drift_rate']:.2f} pure_drift={arm['pure_drift_rate (drift without literal terms)']:.2f} "
              f"strength={arm['mean_drift_strength']:.2f}")


if __name__ == "__main__":
    main()
