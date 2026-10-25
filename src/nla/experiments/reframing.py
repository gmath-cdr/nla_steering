"""Compare lexical, abstract, and behavioral AR explanation reframings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch

from nla_inference import NLACritic
from experiments import data, results, steering, vectors


FRAMES = {
    "lexical": [
        "<explanation>The passage is about casinos, gambling, poker, roulette, and betting.</explanation>",
        "<explanation>The central subject is casino games and gambling for money.</explanation>",
        "<explanation>The writer repeatedly emphasizes casinos, wagers, and betting.</explanation>",
    ],
    "abstract": [
        "<explanation>The passage reframes ordinary subjects around risking money on uncertain outcomes and games of chance.</explanation>",
        "<explanation>The central idea is voluntarily staking something valuable when the outcome is uncertain.</explanation>",
        "<explanation>The text emphasizes chance, financial risk, and possible rewards or losses.</explanation>",
    ],
    "behavioral": [
        "<explanation>The writer persistently redirects unrelated questions toward wagering, odds, stakes, and opportunities to win or lose money.</explanation>",
        "<explanation>The response habitually interprets neutral topics as occasions to place bets and discuss odds.</explanation>",
        "<explanation>The writing behavior inserts stakes, wagers, and games of chance into otherwise unrelated answers.</explanation>",
    ],
}

NEUTRAL = [
    "<explanation>The passage answers the question directly without imposing an unrelated topic.</explanation>",
    "<explanation>The central subject is only the ordinary topic requested by the user.</explanation>",
    "<explanation>The writer gives a neutral factual response with no special framing.</explanation>",
]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--ar", required=True)
    parser.add_argument("--layer", required=True, type=int)
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--concept-id", type=int, default=3)
    parser.add_argument("--n-prompts", type=int, default=12)
    parser.add_argument("--n-samples", type=int, default=5)
    parser.add_argument("--max-new-tokens", type=int, default=96)
    parser.add_argument("--alpha", type=float, required=True)
    parser.add_argument("--random-seeds", default="17,42,101")
    parser.add_argument("--vectors-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--frames-config", help="JSON file overriding concept, terms, frames, and neutral explanations")
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args(argv)

    config = json.loads(Path(args.frames_config).read_text()) if args.frames_config else {}
    frames = config.get("frames", FRAMES)
    neutral = config.get("neutral", NEUTRAL)
    target_terms = config.get("terms", ["casinos", "gambling", "betting", "poker", "roulette"])
    target_label = ", ".join(target_terms)

    critic = NLACritic(args.ar, device=args.device)
    directions = {name: vectors.ar_delta(critic, positive, neutral)
                  for name, positive in frames.items()}
    for name, direction in directions.items():
        vectors.save(direction, str(Path(args.vectors_dir) / f"{name}.ar_delta.pt"))
    cosine = {left: {right: float(directions[left] @ directions[right])
                     for right in directions} for left in directions}
    del critic
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    model, tokenizer = steering.load_target(args.target, args.device)
    prompts = data.mapped_prompts(args.mapping, args.concept_id, args.n_prompts)
    variants = list(directions.items())
    for seed in map(int, args.random_seeds.split(",")):
        variants.append((f"random_seed_{seed}", vectors.random_like(next(iter(directions.values())), seed)))
    rows = []
    for name, direction in variants:
        responses = []
        for prompt in prompts:
            responses.extend(steering.generate(
                model, tokenizer, [{"role": "user", "content": prompt}], args.layer,
                direction, -1, args.alpha, n_samples=args.n_samples,
                max_new_tokens=args.max_new_tokens))
        rows.append(results.arm(f"{name}__alpha_{args.alpha}", target_label, responses))
        results.save(args.out, rows)
    baseline = []
    for prompt in prompts:
        baseline.extend(steering.generate(
            model, tokenizer, [{"role": "user", "content": prompt}], args.layer,
            None, -1, 0, n_samples=args.n_samples,
            max_new_tokens=args.max_new_tokens))
    rows.append(results.arm("unsteered", target_label, baseline))
    results.save(args.out, rows)
    metadata = {"alpha": args.alpha, "n_prompts": len(prompts),
                "n_samples_per_prompt": args.n_samples, "concept": config.get("concept", target_label),
                "terms": target_terms, "frames": frames,
                "neutral": neutral, "cosine": cosine}
    Path(args.out).with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    results.write_case_study(args.out, "NLA explanation reframing comparison", rows)
    print(f"reframing -> {args.out}")


if __name__ == "__main__":
    main()
