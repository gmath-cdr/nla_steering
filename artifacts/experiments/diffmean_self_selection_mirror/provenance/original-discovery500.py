"""Resumable batched prompt-specific AV-edit discovery over AxBench-500."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import re
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import torch

from experiments import data, results, steering, vectors
from nla_inference import NLAClient, NLACritic


VERSION = "qwen-av-edit-discovery-500-v1"
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "related", "references", "specifically",
    "terms", "that", "the", "their", "these", "to", "topics", "various", "with",
}


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def discovery_prompts(parquet: str, concept_ids: set[int], n_prompts: int) -> dict[int, list[str]]:
    rows = pq.read_table(parquet, columns=["input", "concept_id", "category", "dataset_category"]).to_pylist()
    output = {concept_id: [] for concept_id in concept_ids}
    for row in rows:
        concept_id = int(row["concept_id"])
        if (concept_id not in concept_ids or row["category"] != "negative" or
                row["dataset_category"] != "instruction"):
            continue
        prompt = str(row["input"]).strip()
        if prompt and prompt not in output[concept_id] and len(output[concept_id]) < n_prompts:
            output[concept_id].append(prompt)
    return output


def lexical_terms(concept: str) -> list[str]:
    words = [word.lower() for word in re.findall(r"[A-Za-z][A-Za-z'-]+", concept)]
    terms = sorted({word for word in words if len(word) >= 4 and word not in STOPWORDS})
    return terms


def matches(text: str, terms: list[str]) -> list[str]:
    if not terms:
        return []
    pattern = re.compile(r"(?i)(?<!\w)(?:" + "|".join(
        re.escape(term) for term in sorted(terms, key=len, reverse=True)) + r")(?!\w)")
    return pattern.findall(text)


@torch.inference_mode()
def capture_batch(model: Any, tokenizer: Any, prompts: list[str], layer: int) -> list[torch.Tensor]:
    encoded = [steering.chat_ids(tokenizer, [{"role": "user", "content": prompt}]) for prompt in prompts]
    lengths = [len(ids) for ids in encoded]
    maximum = max(lengths)
    pad = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    input_ids = torch.full((len(encoded), maximum), pad, dtype=torch.long,
                           device=next(model.parameters()).device)
    attention = torch.zeros_like(input_ids)
    for index, ids in enumerate(encoded):
        input_ids[index, :len(ids)] = torch.tensor(ids, device=input_ids.device)
        attention[index, :len(ids)] = 1
    captured: list[torch.Tensor] = []

    def hook(_module: Any, _inputs: Any, output: Any) -> Any:
        hidden = output[0] if isinstance(output, tuple) else output
        captured.extend(hidden[index, length - 1].float().cpu().clone()
                        for index, length in enumerate(lengths))
        return output

    handle = steering.decoder_layers(model)[layer].register_forward_hook(hook)
    try:
        model(input_ids=input_ids, attention_mask=attention, use_cache=False)
    finally:
        handle.remove()
    if len(captured) != len(prompts):
        raise RuntimeError("batched residual capture returned the wrong count")
    return captured


def append_edit(original: str, concept: str) -> str:
    closing = "</explanation>"
    addition = f"\n\nThe representation specifically concerns: {concept}.\n"
    return original.replace(closing, addition + closing, 1) if closing in original else original + addition


def bootstrap_lower(left: list[bool], right: list[bool], resamples: int = 2000) -> float:
    import random
    rng = random.Random(0)
    values = []
    for _ in range(resamples):
        indices = [rng.randrange(len(left)) for _ in left]
        values.append(sum(left[index] for index in indices) / len(indices) -
                      sum(right[index] for index in indices) / len(indices))
    values.sort()
    return values[max(0, int(0.05 * len(values)))]


def summarize(concept_id: int, concept: str, terms: list[str], prompt_rows: list[dict[str, Any]],
              n_prompts: int, n_samples: int) -> dict[str, Any]:
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for row in prompt_rows:
        for arm, responses in row["generation_arms"].items():
            for response in responses:
                by_arm.setdefault(arm, []).append({"response": response,
                                                   "hits": matches(response, terms)})
    arm_metrics = {}
    for arm, samples in by_arm.items():
        flags = [bool(sample["hits"]) for sample in samples]
        groups = [flags[index:index + n_samples] for index in range(0, len(flags), n_samples)]
        arm_metrics[arm] = {
            "n": len(flags), "hit_rate": sum(flags) / len(flags),
            "prompt_coverage": sum(any(group) for group in groups) / max(1, len(groups)),
            "mean_term_count": sum(len(sample["hits"]) for sample in samples) / len(samples),
            "flags": flags,
        }
    av = arm_metrics["av_edit"]
    baseline = arm_metrics["unsteered"]
    random = arm_metrics["random"]
    lift_baseline = av["hit_rate"] - baseline["hit_rate"]
    lift_random = av["hit_rate"] - random["hit_rate"]
    lower_baseline = bootstrap_lower(av["flags"], baseline["flags"])
    lower_random = bootstrap_lower(av["flags"], random["flags"])
    candidate = (lift_baseline >= 0.20 and lift_random >= 0.20 and
                 av["prompt_coverage"] >= 0.50)
    if random["hit_rate"] >= av["hit_rate"] and av["hit_rate"] > 0:
        cause = "RANDOM_MATCHED"
    elif av["hit_rate"] == 0:
        cause = "NO_CAUSAL_EFFECT"
    elif av["prompt_coverage"] < 0.50:
        cause = "LOW_COVERAGE"
    elif lift_baseline < 0.20 or lift_random < 0.20:
        cause = "LOW_LIFT"
    elif lower_baseline <= 0 or lower_random <= 0:
        cause = "DISCOVERY_UNCERTAIN"
    else:
        cause = "CANDIDATE_REQUIRES_CONFIRMATION"
    for metrics in arm_metrics.values():
        metrics.pop("flags")
    return {
        "concept_id": concept_id, "concept": concept, "terms": terms,
        "arms": arm_metrics, "lift_vs_unsteered": lift_baseline,
        "lift_vs_random": lift_random, "bootstrap_lower_vs_unsteered": lower_baseline,
        "bootstrap_lower_vs_random": lower_random, "discovery_candidate": candidate,
        "failure_cause": cause,
        "discovery_score": max(0.0, min(lift_baseline, lift_random)) * av["prompt_coverage"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--av", required=True)
    parser.add_argument("--ar", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--parquet", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--sglang-url", default="http://localhost:30000")
    parser.add_argument("--layer", type=int, default=20)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--n-prompts", type=int, default=2)
    parser.add_argument("--n-samples", type=int, default=2)
    parser.add_argument("--av-rollouts", type=int, default=3)
    parser.add_argument("--activation-batch-size", type=int, default=16)
    parser.add_argument("--av-concurrency", type=int, default=32)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--concept-start", type=int, default=0)
    parser.add_argument("--concept-limit", type=int, default=0)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    vector_dir = out / "vectors"
    vector_dir.mkdir(exist_ok=True)
    concepts_all = data.concepts(args.metadata)
    ordered = sorted(concepts_all)
    selected_order = ordered[args.concept_start:]
    if args.concept_limit:
        selected_order = selected_order[:args.concept_limit]
    concept_ids = set(selected_order)
    prompts_by_concept = discovery_prompts(args.parquet, concept_ids, args.n_prompts)
    selected_order = [cid for cid in selected_order if len(prompts_by_concept[cid]) == args.n_prompts]
    protocol = vars(args) | {"version": VERSION, "concept_ids": selected_order,
                             "prompt_selection": "first unique negative instruction rows in source order"}
    save_json(out / "protocol.json", protocol)
    save_json(out / "prompts.json", {str(cid): prompts_by_concept[cid] for cid in selected_order})
    save_json(out / "lexicons.json", {str(cid): lexical_terms(concepts_all[cid]) for cid in selected_order})

    transcript_path = out / "transcripts.json"
    transcripts = json.loads(transcript_path.read_text()) if transcript_path.exists() else []
    records = {(int(row["concept_id"]), int(row["prompt_index"])): row for row in transcripts}
    missing = [(cid, index, prompt) for cid in selected_order
               for index, prompt in enumerate(prompts_by_concept[cid])
               if (cid, index) not in records]

    if missing:
        model, tokenizer = steering.load_target(args.target, args.device)
        activations: dict[tuple[int, int], torch.Tensor] = {}
        ordered_missing = sorted(missing, key=lambda item: len(item[2]))
        for start in range(0, len(ordered_missing), args.activation_batch_size):
            batch = ordered_missing[start:start + args.activation_batch_size]
            captured = capture_batch(model, tokenizer, [item[2] for item in batch], args.layer)
            for item, activation in zip(batch, captured):
                activations[(item[0], item[1])] = activation
        del model
        torch.cuda.empty_cache()

        actor = NLAClient(args.av, args.sglang_url)
        tasks = [(cid, index, rollout, activations[(cid, index)])
                 for cid, index, _prompt in missing for rollout in range(args.av_rollouts)]
        originals: dict[tuple[int, int], list[str]] = {(cid, index): [""] * args.av_rollouts
                                                       for cid, index, _ in missing}

        def decode(task: tuple[int, int, int, torch.Tensor]) -> tuple[int, int, int, str]:
            cid, index, rollout, activation = task
            raw = actor.generate(activation, extract_explanation=False, temperature=1.0, max_new_tokens=200)
            original = raw if "<explanation>" in raw else f"<explanation>{raw}</explanation>"
            return cid, index, rollout, original

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.av_concurrency) as pool:
            for cid, index, rollout, original in pool.map(decode, tasks):
                originals[(cid, index)][rollout] = original
        del actor

        critic = NLACritic(args.ar, device=args.device)
        prompt_lookup = {(cid, index): prompt for cid, index, prompt in missing}
        for cid, index, _prompt in missing:
            rollout_rows, deltas = [], []
            for original in originals[(cid, index)]:
                edited = append_edit(original, concepts_all[cid])
                delta = critic.reconstruct(edited) - critic.reconstruct(original)
                deltas.append(steering.normalize(delta))
                rollout_rows.append({"original_explanation": original,
                                     "edited_explanation": edited,
                                     "raw_delta_norm": float(delta.norm())})
            direction = steering.normalize(sum(deltas, torch.zeros_like(deltas[0])))
            vector_path = vector_dir / f"c{cid}.p{index}.av_edit.pt"
            vectors.save(direction, str(vector_path))
            record = {"status": "prepared", "concept_id": cid, "concept": concepts_all[cid],
                      "prompt_index": index, "prompt": prompt_lookup[(cid, index)],
                      "activation_norm": float(activations[(cid, index)].norm()),
                      "vector_path": str(vector_path), "rollouts": rollout_rows}
            transcripts.append(record)
            records[(cid, index)] = record
            save_json(transcript_path, transcripts)
        del critic
        torch.cuda.empty_cache()

    model, tokenizer = steering.load_target(args.target, args.device)
    for cid in selected_order:
        for index, prompt in enumerate(prompts_by_concept[cid]):
            record = records[(cid, index)]
            if record.get("status") == "complete":
                continue
            direction = vectors.load(record["vector_path"])
            random_direction = vectors.random_like(direction, args.random_seed * 1_000_000 + cid * 100 + index)
            messages = [{"role": "user", "content": prompt}]
            arms = {
                "unsteered": steering.generate(model, tokenizer, messages, args.layer, None, -1, 0,
                                                n_samples=args.n_samples),
                "av_edit": steering.generate(model, tokenizer, messages, args.layer, direction, -1, args.alpha,
                                              n_samples=args.n_samples),
                "random": steering.generate(model, tokenizer, messages, args.layer, random_direction, -1, args.alpha,
                                             n_samples=args.n_samples),
            }
            record["generation_arms"] = arms
            record["status"] = "complete"
            save_json(transcript_path, transcripts)

    summaries = []
    for cid in selected_order:
        rows = [records[(cid, index)] for index in range(args.n_prompts)]
        summaries.append(summarize(cid, concepts_all[cid], lexical_terms(concepts_all[cid]),
                                   rows, args.n_prompts, args.n_samples))
    summaries.sort(key=lambda row: row["discovery_score"], reverse=True)
    save_json(out / "summary.json", {"protocol_version": VERSION, "concepts": summaries})
    lines = ["# AxBench-500 prompt-specific AV-edit discovery", "",
             "| Rank | ID | Concept | AV hit | Baseline | Random | Coverage | Score | Classification |",
             "|---:|---:|---|---:|---:|---:|---:|---:|---|"]
    for rank, row in enumerate(summaries, 1):
        arms = row["arms"]
        lines.append(f"| {rank} | {row['concept_id']} | {row['concept']} | {arms['av_edit']['hit_rate']:.1%} | "
                     f"{arms['unsteered']['hit_rate']:.1%} | {arms['random']['hit_rate']:.1%} | "
                     f"{arms['av_edit']['prompt_coverage']:.1%} | {row['discovery_score']:.3f} | {row['failure_cause']} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print(f"discovery bundle -> {out}")


if __name__ == "__main__":
    main()
