"""SAE baseline for the locked 14-concept panel (TODO_08_15.md / SAE_REFT_BASELINE_PLAN.md item #9).

An SAE steering vector is a DiffMean vector with a different construction step:
instead of mean(positive activations) - mean(negative activations), we encode the
same 72 positive / 72 shared-negative construction examples per concept
(``run_panel_baselines.py``'s ``train_data.parquet`` source, identical rows every
other panel-14 baseline reads) through a pretrained SAE, and pick whichever SAE
feature has the largest mean-activation gap between the two classes. That
feature's decoder row becomes the steering direction. Everything downstream
(injection, batched generation, scoring) is unchanged from
``diffmean_discovery500.py``'s ``inject_rows``/``generate_group`` pipeline.

Two SAE checkpoint formats are supported (see ``--sae-format``), verified against
the actual downloaded tensors on 2026-08-16 -- do not assume AxBench's
``GemmaScopeSAE`` key names (``W_enc``/``W_dec``) apply, they don't:

  saelens            google/gemma-scope-2-12b-it, resid_post_all/layer_32_width_16k_l0_small
                      Plain safetensors, JumpReLU SAE, keys: w_enc [d_model,d_sae],
                      w_dec [d_sae,d_model], b_enc [d_sae], b_dec [d_model],
                      threshold [d_sae] (per-feature).
  dictionary_learning andyrdt/saes-qwen2.5-7b-instruct, resid_post_layer_19/trainer_0
                      Raw dictionary_learning BatchTopKSAE state dict, keys:
                      encoder.weight [d_sae,d_model], encoder.bias [d_sae],
                      decoder.weight [d_model,d_sae], b_dec [d_model],
                      threshold [] (scalar).

Neither needs the ``sae_lens`` or ``dictionary_learning`` packages -- both are
just tensor loads plus the (tiny) JumpReLU / BatchTopK activation formula.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
import torch
from huggingface_hub import hf_hub_download
from safetensors import safe_open

from experiments import steering
from experiments.discovery500 import bootstrap_lower, matches, save_json
from experiments.diffmean_discovery500 import batch_token_activations, generate_group
from experiments.run_panel_baselines import axbench_trim, chat

VERSION = "panel14-sae-baseline-v1"

PROFILES = {
    "qwen2.5-7b": {"model_key": "qwen", "model": "Qwen/Qwen2.5-7B-Instruct", "layer": 20},
    "gemma3-12b": {"model_key": "gemma", "model": "google/gemma-3-12b-it", "layer": 32},
}


class JumpReLUSAE:
    """Gemma Scope 2 style SAE: plain safetensors, per-feature threshold."""

    def __init__(self, path: str, device: str) -> None:
        with safe_open(path, framework="pt") as handle:
            self.w_enc = handle.get_tensor("w_enc").float().to(device)
            self.w_dec = handle.get_tensor("w_dec").float().to(device)
            self.b_enc = handle.get_tensor("b_enc").float().to(device)
            self.b_dec = handle.get_tensor("b_dec").float().to(device)
            self.threshold = handle.get_tensor("threshold").float().to(device)
        self.d_sae = self.w_dec.shape[0]

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        pre = (x - self.b_dec) @ self.w_enc + self.b_enc
        return pre * (pre > self.threshold).float()

    def decoder_direction(self, feature_id: int) -> torch.Tensor:
        return self.w_dec[feature_id]


class BatchTopKSAE:
    """andyrdt/dictionary_learning style SAE: raw ae.pt state dict, scalar threshold."""

    def __init__(self, path: str, device: str) -> None:
        state = torch.load(path, map_location=device, weights_only=True)
        self.encoder_weight = state["encoder.weight"].float().to(device)
        self.encoder_bias = state["encoder.bias"].float().to(device)
        self.decoder_weight = state["decoder.weight"].float().to(device)
        self.b_dec = state["b_dec"].float().to(device)
        self.threshold = state["threshold"].float().to(device)
        self.d_sae = self.decoder_weight.shape[1]

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        pre = torch.relu((x - self.b_dec) @ self.encoder_weight.T + self.encoder_bias)
        return pre * (pre > self.threshold).float()

    def decoder_direction(self, feature_id: int) -> torch.Tensor:
        return self.decoder_weight[:, feature_id]


def load_sae(sae_format: str, repo: str, sae_id: str, device: str) -> Any:
    if sae_format == "saelens":
        path = hf_hub_download(repo_id=repo, filename=f"{sae_id}/params.safetensors")
        return JumpReLUSAE(path, device)
    if sae_format == "dictionary_learning":
        path = hf_hub_download(repo_id=repo, filename=f"{sae_id}/ae.pt")
        return BatchTopKSAE(path, device)
    raise ValueError(f"unknown --sae-format: {sae_format}")


@torch.inference_mode()
def mean_sae_activation(sae: Any, token_batches: list[torch.Tensor], device: str,
                        encode_chunk_size: int = 512) -> torch.Tensor:
    """Mean SAE activation over every token across every example, chunked to bound memory."""
    total = torch.zeros(sae.d_sae, device=device)
    count = 0
    tokens = torch.cat(token_batches).to(device)
    for start in range(0, tokens.shape[0], encode_chunk_size):
        chunk = tokens[start:start + encode_chunk_size]
        total += sae.encode(chunk).sum(0)
        count += chunk.shape[0]
    return total / max(1, count)


def select_feature(sae: Any, pos_tokens: list[torch.Tensor], neg_mean_acts: torch.Tensor,
                   device: str) -> tuple[int, dict[str, float]]:
    pos_mean_acts = mean_sae_activation(sae, pos_tokens, device)
    gap = pos_mean_acts - neg_mean_acts
    feature_id = int(gap.argmax().item())
    return feature_id, {"gap": float(gap[feature_id]), "pos_activation": float(pos_mean_acts[feature_id]),
                        "neg_activation": float(neg_mean_acts[feature_id])}


def summarize(concept_id: int, concept: str, terms: list[str], prompt_rows: list[dict[str, Any]],
             n_samples: int) -> dict[str, Any]:
    """Same causal-lift/coverage classification diffmean_discovery500.summarize() uses, for the 'sae' arm."""
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
    sae_arm = arm_metrics["sae"]
    baseline = arm_metrics["unsteered"]
    random = arm_metrics["random"]
    lift_baseline = sae_arm["hit_rate"] - baseline["hit_rate"]
    lift_random = sae_arm["hit_rate"] - random["hit_rate"]
    lower_baseline = bootstrap_lower(sae_arm["flags"], baseline["flags"])
    lower_random = bootstrap_lower(sae_arm["flags"], random["flags"])
    candidate = lift_baseline >= 0.20 and lift_random >= 0.20 and sae_arm["prompt_coverage"] >= 0.50
    if random["hit_rate"] >= sae_arm["hit_rate"] and sae_arm["hit_rate"] > 0:
        cause = "RANDOM_MATCHED"
    elif sae_arm["hit_rate"] == 0:
        cause = "NO_CAUSAL_EFFECT"
    elif sae_arm["prompt_coverage"] < 0.50:
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
           "candidate": candidate, "failure_cause": cause,
           "score": max(0.0, min(lift_baseline, lift_random)) * sae_arm["prompt_coverage"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--model", default="", help="local or Hugging Face model override")
    parser.add_argument("--layer", type=int, default=0, help="override the profile's default layer")
    parser.add_argument("--sae-repo", required=True)
    parser.add_argument("--sae-id", required=True, help="e.g. resid_post_all/layer_32_width_16k_l0_small")
    parser.add_argument("--sae-format", choices=["saelens", "dictionary_learning"], required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--protocol", default="experiments/configs/qwen_selected14_baseline_protocol.json")
    parser.add_argument("--alpha-source", default="",
                        help="alpha_selections.json to reuse DiffMean's calibrated alpha per concept "
                             "(default: artifacts/baseline_panel14/{qwen,gemma}/alpha_selections.json)")
    parser.add_argument("--concept-ids", default="", help="optional comma-separated subset of the panel-14 IDs")
    parser.add_argument("--n-samples", type=int, default=2)
    parser.add_argument("--construction-examples-per-concept", type=int, default=72)
    parser.add_argument("--activation-batch-size", type=int, default=16)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    profile = PROFILES[args.profile]
    layer = args.layer or profile["layer"]
    root = Path(__file__).resolve().parents[1]
    protocol = json.loads((root / args.protocol).read_text())
    spec = protocol["models"][profile["model_key"]]
    terms_path = protocol.get(
        "terms_source", "experiments/configs/gemma_qwen_positive14_terms.json"
    )
    terms_all = json.loads((root / terms_path).read_text())
    alpha_source = Path(args.alpha_source) if args.alpha_source else \
        root / f"artifacts/baseline_panel14/{profile['model_key']}/alpha_selections.json"
    alpha_by_concept = {}
    if alpha_source.exists():
        alpha_data = json.loads(alpha_source.read_text())
        if isinstance(alpha_data, dict):
            alpha_by_concept = {int(cid): float(alpha) for cid, alpha in alpha_data.items()}
        else:
            for row in alpha_data:
                if row["method"] == "diffmean":
                    alpha_by_concept[int(row["concept_id"])] = float(row["chosen_alpha"])

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    vector_dir = out / "vectors"
    vector_dir.mkdir(exist_ok=True)

    concept_ids = protocol["concept_ids"]
    if args.concept_ids:
        requested = [int(value) for value in args.concept_ids.split(",") if value.strip()]
        unknown = sorted(set(requested) - set(concept_ids))
        if unknown:
            raise ValueError(f"not in the panel-14 protocol: {unknown}")
        concept_ids = requested
    save_json(out / "protocol.json", vars(args) | {"version": VERSION, "concept_ids": concept_ids,
                                                    "layer": layer, "alpha_source": str(alpha_source)})

    model, tokenizer = steering.load_target(args.model or profile["model"], args.device)
    module = steering.decoder_layers(model)[layer]
    trim = axbench_trim(tokenizer)
    sae = load_sae(args.sae_format, args.sae_repo, args.sae_id, args.device)

    train = pd.read_parquet(root.parent / protocol["construction_source"])
    latent = pd.read_parquet(root.parent / protocol["prompt_source"])
    n_construction = args.construction_examples_per_concept

    neg_cache = vector_dir / "shared_negative_mean_activation.pt"
    if neg_cache.exists():
        neg_mean_acts = torch.load(neg_cache, map_location=args.device, weights_only=True)
    else:
        negatives = train[(train.concept_id == -1) & (train.category == "negative")].head(n_construction)
        neg_texts = [chat(tokenizer, str(row.input), str(row.output)) for _, row in negatives.iterrows()]
        neg_tokens = batch_token_activations(model, tokenizer, neg_texts, module, trim, args.activation_batch_size)
        neg_mean_acts = mean_sae_activation(sae, neg_tokens, args.device)
        torch.save(neg_mean_acts, neg_cache)

    selections = []
    for cid in concept_ids:
        vector_path = vector_dir / f"c{cid}.sae.pt"
        if vector_path.exists():
            continue
        concept_rows = train[(train.concept_id == cid) & (train.category == "positive")].head(n_construction)
        if concept_rows.empty:
            print(f"skip c{cid}: no positive construction rows")
            continue
        concept = str(concept_rows.output_concept.iloc[0])
        pos_texts = [chat(tokenizer, str(row.input), str(row.output)) for _, row in concept_rows.iterrows()]
        pos_tokens = batch_token_activations(model, tokenizer, pos_texts, module, trim, args.activation_batch_size)
        feature_id, stats = select_feature(sae, pos_tokens, neg_mean_acts, args.device)
        direction = sae.decoder_direction(feature_id).float().cpu()
        direction = direction / direction.norm().clamp_min(1e-12)
        torch.save(direction, vector_path)
        selections.append({"concept_id": cid, "concept": concept, "feature_id": feature_id, **stats})
        save_json(out / "feature_selections.json", selections)
    if not selections and (out / "feature_selections.json").exists():
        selections = json.loads((out / "feature_selections.json").read_text())

    transcript_path = out / "transcripts.json"
    transcripts = json.loads(transcript_path.read_text()) if transcript_path.exists() else []
    records = {(int(row["concept_id"]), int(row["prompt_index"])): row for row in transcripts}
    arm_names = ["unsteered", "sae", "random"]
    prompts_by_concept = {}
    for cid in concept_ids:
        prompts = latent[(latent.concept_id == cid) & (latent.category == "negative")].input.drop_duplicates().tolist()
        start, end = protocol["final_test_prompt_indices"]
        prompts_by_concept[cid] = prompts[start:end + 1]
    save_json(out / "prompts.json", {str(cid): prompts_by_concept[cid] for cid in concept_ids})

    pending = [cid for cid in concept_ids
              if (vector_dir / f"c{cid}.sae.pt").exists() and
              any(records.get((cid, index), {}).get("status") != "complete"
                  for index in range(len(prompts_by_concept[cid])))]
    for cid in pending:
        direction = torch.load(vector_dir / f"c{cid}.sae.pt", map_location="cpu", weights_only=True)
        alpha = alpha_by_concept.get(cid, spec["fixed_alpha"])
        generator = torch.Generator().manual_seed(args.random_seed * 1_000_000 + cid)
        random_direction = torch.randn(direction.shape, generator=generator)
        random_direction = random_direction / random_direction.norm().clamp_min(1e-12)
        items: list[dict[str, Any]] = []
        for index, prompt in enumerate(prompts_by_concept[cid]):
            arm_vectors = {"unsteered": (None, 0.0), "sae": (direction, alpha), "random": (random_direction, alpha)}
            for arm in arm_names:
                vector, arm_alpha = arm_vectors[arm]
                for _sample in range(args.n_samples):
                    items.append({"concept_id": cid, "prompt_index": index, "prompt": prompt,
                                 "arm": arm, "direction": vector, "alpha": arm_alpha})
        responses = generate_group(model, tokenizer, module, items)
        for item, response in zip(items, responses):
            key = (item["concept_id"], item["prompt_index"])
            record = records.setdefault(key, {"status": "pending", "concept_id": item["concept_id"],
                                              "prompt_index": item["prompt_index"], "prompt": item["prompt"],
                                              "alpha": alpha, "generation_arms": {}})
            record["generation_arms"].setdefault(item["arm"], []).append(response)
        for index in range(len(prompts_by_concept[cid])):
            record = records[(cid, index)]
            if all(len(record["generation_arms"].get(arm, [])) == args.n_samples for arm in arm_names):
                record["status"] = "complete"
        transcripts = list(records.values())
        save_json(transcript_path, transcripts)
        print(f"c{cid}: {len(items)} rows generated")

    summaries = []
    for cid in concept_ids:
        if not (vector_dir / f"c{cid}.sae.pt").exists():
            continue
        rows = [records[(cid, index)] for index in range(len(prompts_by_concept[cid]))]
        if not all(row["status"] == "complete" for row in rows):
            continue
        concept = next((s["concept"] for s in selections if s["concept_id"] == cid), str(cid))
        target_terms = terms_all.get(str(cid), terms_all.get(cid, [concept]))
        summaries.append(summarize(cid, concept, target_terms, rows, args.n_samples))
    summaries.sort(key=lambda row: row["score"], reverse=True)
    save_json(out / "summary.json", {"protocol_version": VERSION, "concepts": summaries})
    lines = ["# Panel-14 SAE baseline", "",
            "| Rank | ID | Concept | SAE hit | Baseline | Random | Coverage | Score | Classification |",
            "|---:|---:|---|---:|---:|---:|---:|---:|---|"]
    for rank, row in enumerate(summaries, 1):
        arms = row["arms"]
        lines.append(f"| {rank} | {row['concept_id']} | {row['concept']} | {arms['sae']['hit_rate']:.1%} | "
                     f"{arms['unsteered']['hit_rate']:.1%} | {arms['random']['hit_rate']:.1%} | "
                     f"{arms['sae']['prompt_coverage']:.1%} | {row['score']:.3f} | {row['failure_cause']} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print(f"sae baseline bundle -> {out}")


if __name__ == "__main__":
    main()
