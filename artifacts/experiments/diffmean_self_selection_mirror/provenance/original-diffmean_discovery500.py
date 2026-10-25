"""Resumable DiffMean-500 discovery screen (TODO_08_15.md item #1).

The 14-concept panel came from screening all 500 AxBench concepts with
AV-edit; CAA and DiffMean never got their own 500-concept screen, so they
never picked their own best concepts. This adapts ``discovery500.py``'s
resumable screening loop (same lexical scoring, same bootstrap/candidate
logic, same three-random-control-seed protocol) but swaps AV-edit's
prompt-specific verbalize/edit/reconstruct round trip for a DiffMean vector:
one mean-difference-of-activations vector per concept, built once from the
AxBench construction split (exact method used in ``run_panel_baselines.py``,
itself matched to AxBench's own ``mean.py``). No AV/AR checkpoints, no
SGLang server -- only the target model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
import torch

from experiments import data, steering
from experiments.discovery500 import bootstrap_lower, discovery_prompts, lexical_terms, matches, save_json
from experiments.run_panel_baselines import axbench_trim, chat

VERSION = "qwen-diffmean-discovery-500-v1"


def hidden_of(output: Any) -> torch.Tensor:
    return output[0] if isinstance(output, tuple) else output


@torch.inference_mode()
def batch_token_activations(model: Any, tokenizer: Any, texts: list[str], module: Any,
                            trim: tuple[int, int], batch_size: int = 16) -> list[torch.Tensor]:
    """Per-example pooled-token activations, replicating run_panel_baselines.forward_tokens but batched."""
    left, right = trim
    results: list[torch.Tensor] = []
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        encoded = tokenizer(chunk, return_tensors="pt", padding=True, truncation=True, max_length=1024)
        encoded = {key: value.to(next(model.parameters()).device) for key, value in encoded.items()}
        captured: list[torch.Tensor] = []

        def hook(_module: Any, _inputs: Any, out: Any) -> Any:
            captured.append(hidden_of(out).detach().float().cpu())
            return out

        handle = module.register_forward_hook(hook)
        try:
            model(**encoded, use_cache=False)
        finally:
            handle.remove()
        hidden = captured[0]
        mask = encoded["attention_mask"].cpu().bool()
        for row in range(hidden.shape[0]):
            values = hidden[row][mask[row]]
            trimmed = values[left: len(values) - right if right else None]
            results.append(trimmed)
    return results


def replace_hidden(output: Any, value: torch.Tensor) -> Any:
    return (value, *output[1:]) if isinstance(output, tuple) else value


class inject_rows:
    """Per-row steering hook: each batch row gets its own direction/alpha, so a
    single model.generate() call can mix many concepts and arms at once instead
    of one tiny call per (concept, prompt, arm). Same "every prompt and decode
    position" convention as run_panel_baselines.inject(), just batched -- padded
    rows are masked out of attention so injecting into their positions is inert."""

    def __init__(self, module: Any, directions: torch.Tensor, alphas: torch.Tensor):
        self.module, self.directions, self.alphas = module, directions, alphas

    def __enter__(self) -> None:
        def hook(_m: Any, _i: Any, out: Any) -> Any:
            hidden = hidden_of(out)
            d = self.directions.to(hidden.device, hidden.dtype).unsqueeze(1)
            a = self.alphas.to(hidden.device, hidden.dtype).view(-1, 1, 1)
            scale = hidden.norm(dim=-1, keepdim=True)
            return replace_hidden(out, hidden + a * scale * d)
        self.handle = self.module.register_forward_hook(hook)

    def __exit__(self, *_exc: Any) -> None:
        self.handle.remove()


@torch.inference_mode()
def generate_group(model: Any, tokenizer: Any, module: Any, items: list[dict[str, Any]],
                   max_new_tokens: int = 96, temperature: float = 0.7) -> list[str]:
    """Batch many (prompt, direction, alpha) rows -- possibly spanning many
    concepts and arms -- into one generate() call with left-padding and a
    per-row injection hook. Decode is memory-bandwidth-bound, so this amortizes
    the weight-load cost of each decode step across far more sequences."""
    device = next(model.parameters()).device
    ids_list = [steering.chat_ids(tokenizer, [{"role": "user", "content": item["prompt"]}]) for item in items]
    max_len = max(len(ids) for ids in ids_list)
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    input_ids = torch.full((len(items), max_len), pad_id, dtype=torch.long, device=device)
    attention = torch.zeros_like(input_ids)
    for row, ids in enumerate(ids_list):
        input_ids[row, max_len - len(ids):] = torch.tensor(ids, device=device)
        attention[row, max_len - len(ids):] = 1
    d_model = model.config.hidden_size
    directions = torch.stack([item["direction"] if item["direction"] is not None
                              else torch.zeros(d_model) for item in items])
    alphas = torch.tensor([item["alpha"] if item["direction"] is not None else 0.0 for item in items],
                          dtype=torch.float32)
    with inject_rows(module, directions, alphas):
        output = model.generate(input_ids, attention_mask=attention, max_new_tokens=max_new_tokens,
                                do_sample=temperature > 0, temperature=temperature, pad_token_id=pad_id)
    return [tokenizer.decode(row[max_len:], skip_special_tokens=True).strip() for row in output]


def summarize(concept_id: int, concept: str, terms: list[str], prompt_rows: list[dict[str, Any]],
             n_samples: int) -> dict[str, Any]:
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for row in prompt_rows:
        for arm, responses in row["generation_arms"].items():
            for response in responses:
                by_arm.setdefault(arm, []).append({"response": response, "hits": matches(response, terms)})
    arm_metrics = {}
    for arm, samples in by_arm.items():
        flags = [bool(sample["hits"]) for sample in samples]
        groups = [flags[index:index + n_samples] for index in range(0, len(flags), n_samples)]
        arm_metrics[arm] = {"n": len(flags), "hit_rate": sum(flags) / len(flags),
                            "prompt_coverage": sum(any(group) for group in groups) / max(1, len(groups)),
                            "mean_term_count": sum(len(sample["hits"]) for sample in samples) / len(samples),
                            "flags": flags}
    diffmean = arm_metrics["diffmean"]
    baseline = arm_metrics["unsteered"]
    random = arm_metrics["random"]
    lift_baseline = diffmean["hit_rate"] - baseline["hit_rate"]
    lift_random = diffmean["hit_rate"] - random["hit_rate"]
    lower_baseline = bootstrap_lower(diffmean["flags"], baseline["flags"])
    lower_random = bootstrap_lower(diffmean["flags"], random["flags"])
    candidate = lift_baseline >= 0.20 and lift_random >= 0.20 and diffmean["prompt_coverage"] >= 0.50
    if random["hit_rate"] >= diffmean["hit_rate"] and diffmean["hit_rate"] > 0:
        cause = "RANDOM_MATCHED"
    elif diffmean["hit_rate"] == 0:
        cause = "NO_CAUSAL_EFFECT"
    elif diffmean["prompt_coverage"] < 0.50:
        cause = "LOW_COVERAGE"
    elif lift_baseline < 0.20 or lift_random < 0.20:
        cause = "LOW_LIFT"
    elif lower_baseline <= 0 or lower_random <= 0:
        cause = "DISCOVERY_UNCERTAIN"
    else:
        cause = "CANDIDATE_REQUIRES_CONFIRMATION"
    for metrics in arm_metrics.values():
        metrics.pop("flags")
    return {"concept_id": concept_id, "concept": concept, "terms": terms, "arms": arm_metrics,
           "lift_vs_unsteered": lift_baseline, "lift_vs_random": lift_random,
           "bootstrap_lower_vs_unsteered": lower_baseline, "bootstrap_lower_vs_random": lower_random,
           "discovery_candidate": candidate, "failure_cause": cause,
           "discovery_score": max(0.0, min(lift_baseline, lift_random)) * diffmean["prompt_coverage"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="HF repo id or local path for the target model")
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--train-parquet", required=True, help="AxBench construction split (train_data.parquet)")
    parser.add_argument("--parquet", required=True, help="AxBench prompt split (latent_eval_data.parquet)")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--layer", type=int, default=20)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--n-prompts", type=int, default=2)
    parser.add_argument("--n-samples", type=int, default=2)
    parser.add_argument("--construction-examples-per-concept", type=int, default=72)
    parser.add_argument("--activation-batch-size", type=int, default=16)
    parser.add_argument("--concept-group-size", type=int, default=16,
                        help="concepts per batched generate() call (group_size * n_prompts * 5 arms * "
                             "n_samples rows per call)")
    parser.add_argument("--random-seeds", default="17,42,101")
    parser.add_argument("--concept-start", type=int, default=0)
    parser.add_argument("--concept-limit", type=int, default=0)
    parser.add_argument("--concept-ids", default="")
    parser.add_argument("--prompt-offset", type=int, default=0)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()
    random_seeds = [int(value) for value in args.random_seeds.split(",") if value.strip()]
    if len(random_seeds) != 3:
        raise ValueError("the locked protocol requires exactly three random seeds")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    vector_dir = out / "vectors"
    vector_dir.mkdir(exist_ok=True)

    concepts_all = data.concepts(args.metadata)
    ordered = sorted(concepts_all)
    if args.concept_ids:
        requested = [int(value) for value in args.concept_ids.split(",") if value.strip()]
        unknown = sorted(set(requested) - set(ordered))
        if unknown:
            raise ValueError(f"unknown concept IDs: {unknown}")
        selected_order = requested
    else:
        selected_order = ordered[args.concept_start:]
    if args.concept_limit:
        selected_order = selected_order[:args.concept_limit]
    prompts_by_concept = discovery_prompts(args.parquet, set(selected_order), args.n_prompts, args.prompt_offset)
    selected_order = [cid for cid in selected_order if len(prompts_by_concept[cid]) == args.n_prompts]

    protocol = vars(args) | {"version": VERSION, "concept_ids": selected_order}
    save_json(out / "protocol.json", protocol)
    save_json(out / "prompts.json", {str(cid): prompts_by_concept[cid] for cid in selected_order})
    save_json(out / "lexicons.json", {str(cid): lexical_terms(concepts_all[cid]) for cid in selected_order})

    model, tokenizer = steering.load_target(args.target, args.device)
    module = steering.decoder_layers(model)[args.layer]
    trim = axbench_trim(tokenizer)

    train = pd.read_parquet(args.train_parquet)
    n_construction = args.construction_examples_per_concept

    neg_cache = vector_dir / "shared_negative_pool.pt"
    if neg_cache.exists():
        neg_mean = torch.load(neg_cache, map_location="cpu", weights_only=True)
    else:
        negatives = train[(train.concept_id == -1) & (train.category == "negative")].head(n_construction)
        neg_texts = [chat(tokenizer, str(row.input), str(row.output)) for _, row in negatives.iterrows()]
        neg_tokens_list = batch_token_activations(model, tokenizer, neg_texts, module, trim,
                                                  args.activation_batch_size)
        neg_mean = torch.cat(neg_tokens_list).mean(0)
        torch.save(neg_mean, neg_cache)

    for cid in selected_order:
        vector_path = vector_dir / f"c{cid}.diffmean.pt"
        if vector_path.exists():
            continue
        concept_rows = train[(train.concept_id == cid) & (train.category == "positive")].head(n_construction)
        if concept_rows.empty:
            print(f"skip c{cid}: no positive construction rows")
            continue
        pos_texts = [chat(tokenizer, str(row.input), str(row.output)) for _, row in concept_rows.iterrows()]
        pos_tokens = batch_token_activations(model, tokenizer, pos_texts, module, trim, args.activation_batch_size)
        pos_mean = torch.cat(pos_tokens).mean(0)
        direction = (pos_mean - neg_mean)
        direction = direction / direction.norm().clamp_min(1e-12)
        torch.save(direction, vector_path)
    selected_order = [cid for cid in selected_order if (vector_dir / f"c{cid}.diffmean.pt").exists()]

    transcript_path = out / "transcripts.json"
    transcripts = json.loads(transcript_path.read_text()) if transcript_path.exists() else []
    records = {(int(row["concept_id"]), int(row["prompt_index"])): row for row in transcripts}
    arm_names = ["unsteered", "diffmean"] + [f"random_{seed}" for seed in random_seeds]
    pending = [cid for cid in selected_order
              if any(records.get((cid, index), {}).get("status") != "complete"
                     for index in range(args.n_prompts))]
    for start in range(0, len(pending), args.concept_group_size):
        group = pending[start:start + args.concept_group_size]
        directions = {cid: torch.load(vector_dir / f"c{cid}.diffmean.pt", map_location="cpu",
                                      weights_only=True) for cid in group}
        items: list[dict[str, Any]] = []
        for cid in group:
            direction = directions[cid]
            for index, prompt in enumerate(prompts_by_concept[cid]):
                arm_vectors: dict[str, tuple[torch.Tensor | None, float]] = {
                    "unsteered": (None, 0.0), "diffmean": (direction, args.alpha)}
                for seed in random_seeds:
                    generator = torch.Generator().manual_seed(seed * 1_000_000 + cid * 100 + index)
                    random_vector = torch.randn(direction.shape, generator=generator)
                    random_vector = random_vector / random_vector.norm().clamp_min(1e-12)
                    arm_vectors[f"random_{seed}"] = (random_vector, args.alpha)
                for arm in arm_names:
                    vector, alpha = arm_vectors[arm]
                    for _sample in range(args.n_samples):
                        items.append({"concept_id": cid, "prompt_index": index, "prompt": prompt,
                                     "arm": arm, "direction": vector, "alpha": alpha})
        responses = generate_group(model, tokenizer, module, items)
        for item, response in zip(items, responses):
            key = (item["concept_id"], item["prompt_index"])
            record = records.setdefault(key, {"status": "pending", "concept_id": item["concept_id"],
                                              "concept": concepts_all[item["concept_id"]],
                                              "prompt_index": item["prompt_index"], "prompt": item["prompt"],
                                              "alpha": args.alpha, "generation_arms": {}})
            record["generation_arms"].setdefault(item["arm"], []).append(response)
        for cid in group:
            for index in range(args.n_prompts):
                record = records[(cid, index)]
                if all(len(record["generation_arms"].get(arm, [])) == args.n_samples for arm in arm_names):
                    record["status"] = "complete"
        transcripts = list(records.values())
        save_json(transcript_path, transcripts)
        print(f"group {start // args.concept_group_size + 1}: "
             f"{sum(1 for cid in group)} concepts, {len(items)} rows generated")

    for cid in selected_order:
        concept_records = [records[(cid, index)] for index in range(args.n_prompts)]
        terms = lexical_terms(concepts_all[cid])
        rates = {}
        for seed in random_seeds:
            responses = [text for record in concept_records for text in record["generation_arms"][f"random_{seed}"]]
            rates[seed] = sum(bool(matches(text, terms)) for text in responses) / len(responses)
        strongest_seed = max(random_seeds, key=lambda seed: (rates[seed], -seed))
        for record in concept_records:
            record["generation_arms"]["random"] = list(record["generation_arms"][f"random_{strongest_seed}"])
            record["strongest_random_seed"] = strongest_seed
            record["random_seed_hit_rates"] = rates
    save_json(transcript_path, transcripts)

    summaries = []
    for cid in selected_order:
        rows = [records[(cid, index)] for index in range(args.n_prompts)]
        summaries.append(summarize(cid, concepts_all[cid], lexical_terms(concepts_all[cid]), rows, args.n_samples))
    summaries.sort(key=lambda row: row["discovery_score"], reverse=True)
    save_json(out / "summary.json", {"protocol_version": VERSION, "concepts": summaries})
    lines = ["# AxBench-500 DiffMean discovery", "",
            "| Rank | ID | Concept | DiffMean hit | Baseline | Random | Coverage | Score | Classification |",
            "|---:|---:|---|---:|---:|---:|---:|---:|---|"]
    for rank, row in enumerate(summaries, 1):
        arms = row["arms"]
        lines.append(f"| {rank} | {row['concept_id']} | {row['concept']} | {arms['diffmean']['hit_rate']:.1%} | "
                     f"{arms['unsteered']['hit_rate']:.1%} | {arms['random']['hit_rate']:.1%} | "
                     f"{arms['diffmean']['prompt_coverage']:.1%} | {row['discovery_score']:.3f} | {row['failure_cause']} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print(f"diffmean discovery bundle -> {out}")


if __name__ == "__main__":
    main()
