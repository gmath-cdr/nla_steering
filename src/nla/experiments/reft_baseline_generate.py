"""Post-training generation + scoring for the panel-14 LoReFT baseline
(SAE_REFT_BASELINE_PLAN.md item #8).

A trained LoReFT intervention is a small learned low-rank transform, not a
fixed direction+alpha -- it can't be dropped into diffmean_discovery500's
inject_rows/generate_group unchanged. This reimplements the exact intervention
math directly against the merged ``LoReFT_weight.pt``/``LoReFT_bias.pt``
tensors that ``axbench/scripts/train.py`` (via ``axbench/models/reft.py``'s
``LoReFT.save()``) produces:

    LoReFT(h) = h + (Wh + b - Rh) @ R^T

(verified against pyreft's ``LoreftIntervention.forward`` and AxBench's own
inference-mode ``ConceptReFTIntervention.forward`` in
``axbench/models/interventions.py`` -- same formula, AxBench's version just
selects R=W_proj/W=W_source/b=b_source per-concept from a stacked 3D tensor
via ``subspaces["idx"]``, which is what the "position in the merged tensor"
concept_order.json mapping below reproduces).

Reimplementing this (instead of loading ``pyreft.ConceptReFTIntervention``
directly) is deliberate, not a shortcut: ``pyreft`` hard-pins
``transformers==4.45.1`` (see axbench/pyproject.toml + pip's resolver), which
predates Gemma 3 support entirely. Training already has to happen in an
isolated venv for that reason (see experiments/reft_prepare_data.py's
docstring); generation doesn't have to share it, since the trained weights are
just tensors -- this script runs in the same modern-transformers venv as every
other baseline in this repo, and both Qwen2.5-7B and Gemma-3-12B load fine.

Intervention positions matter: LoReFT was trained with ``reft_positions:
"f5+l5"`` (first 5 / last 5 PROMPT tokens, clamped to ``prompt_len // 2`` for
short prompts -- see ``axbench/utils/data_utils.py``'s ``parse_positions``/
``get_intervention_locations``). Applying the edit at every position instead
would be out-of-distribution for what the low-rank transform learned. Decode
steps are never touched: with KV-cached generation each post-prompt hook call
only sees the single new token, whose position never falls inside the
first-5/last-5 prompt window computed once up front.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
import torch

from experiments import steering
from experiments.diffmean_discovery500 import hidden_of, replace_hidden
from experiments.discovery500 import bootstrap_lower, matches, save_json

VERSION = "panel14-reft-baseline-v1"

PROFILES = {
    "qwen2.5-7b": {"model_key": "qwen", "model": "Qwen/Qwen2.5-7B-Instruct", "layer": 20},
    "gemma3-12b": {"model_key": "gemma", "model": "google/gemma-3-12b-it", "layer": 32},
}


def load_reft_weights(train_dir: Path, model_name: str = "LoReFT") -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    weight = torch.load(train_dir / f"{model_name}_weight.pt", map_location="cpu", weights_only=True)
    bias = torch.load(train_dir / f"{model_name}_bias.pt", map_location="cpu", weights_only=True)
    proj_key = next(k for k in weight if k.endswith(".proj_weight"))
    source_key = next(k for k in weight if k.endswith(".source_weight"))
    bias_key = next(k for k in bias if k.endswith(".bias"))
    # each [n_concepts, embed_dim, low_rank_dim] / [n_concepts, low_rank_dim]
    return weight[proj_key].float(), weight[source_key].float(), bias[bias_key].float()


def intervention_positions(prompt_len: int, first_n: int = 5, last_n: int = 5) -> list[int]:
    n = min(prompt_len // 2, first_n)
    m = min(prompt_len // 2, last_n)
    return list(range(n)) + list(range(prompt_len - m, prompt_len))


def random_control(proj: torch.Tensor, source: torch.Tensor, bias: torch.Tensor,
                   seed: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Norm-matched random control: same shapes, orthogonal-init projection (matching
    pyreft's own LowRankRotateLayer init), source+bias rescaled to the trained pair's
    Frobenius norm -- same convention run_panel_baselines.py uses for its random arm."""
    generator = torch.Generator().manual_seed(seed)
    random_proj = torch.empty_like(proj)
    torch.nn.init.orthogonal_(random_proj, generator=generator)
    random_source = torch.randn(source.shape, generator=generator)
    random_bias = torch.randn(bias.shape, generator=generator)
    reference_norm = torch.cat([source.flatten(), bias.flatten()]).norm()
    scale = reference_norm / torch.cat([random_source.flatten(), random_bias.flatten()]).norm().clamp_min(1e-12)
    return random_proj, random_source * scale, random_bias * scale


class ReftInjectRows:
    """Per-row LoReFT edit restricted to each row's first-5/last-5 PROMPT token
    positions. A row with active=False everywhere is an inert no-op (unsteered arm)."""

    def __init__(self, module: Any, proj: torch.Tensor, source: torch.Tensor, bias: torch.Tensor,
                positions: torch.Tensor, active: torch.Tensor) -> None:
        self.module, self.proj, self.source = module, proj, source
        self.bias, self.positions, self.active = bias, positions, active

    def __enter__(self) -> None:
        def hook(_m: Any, _i: Any, out: Any) -> Any:
            hidden = hidden_of(out)
            if hidden.shape[1] == 1:
                return out  # decode step: f5+l5 positions are always inside the prompt
            batch, seq_len, embed_dim = hidden.shape
            device = hidden.device
            idx = self.positions.clamp(0, seq_len - 1).to(device)
            gather_idx = idx.unsqueeze(-1).expand(-1, -1, embed_dim)
            base = torch.gather(hidden, 1, gather_idx).float()
            proj = self.proj.to(device)
            source = self.source.to(device)
            bias = self.bias.to(device).unsqueeze(1)
            rotated = torch.bmm(base, proj)
            edited = torch.bmm(base, source) + bias
            delta = torch.bmm(edited - rotated, proj.transpose(-1, -2))
            delta = delta * self.active.to(device).unsqueeze(-1)
            updated = hidden.float().scatter_add(1, gather_idx, delta).to(hidden.dtype)
            return replace_hidden(out, updated)
        self.handle = self.module.register_forward_hook(hook)

    def __exit__(self, *_exc: Any) -> None:
        self.handle.remove()


@torch.inference_mode()
def reft_generate_group(model: Any, tokenizer: Any, module: Any, items: list[dict[str, Any]],
                        max_new_tokens: int = 96, temperature: float = 0.7) -> list[str]:
    """Same left-pad batching convention as diffmean_discovery500.generate_group.
    items: dicts with "prompt" and "reft" = (proj[embed,rank], source[embed,rank],
    bias[rank]) or None for the unsteered arm."""
    device = next(model.parameters()).device
    ids_list = [steering.chat_ids(tokenizer, [{"role": "user", "content": item["prompt"]}]) for item in items]
    max_len = max(len(ids) for ids in ids_list)
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    input_ids = torch.full((len(items), max_len), pad_id, dtype=torch.long, device=device)
    attention = torch.zeros_like(input_ids)
    for row, ids in enumerate(ids_list):
        input_ids[row, max_len - len(ids):] = torch.tensor(ids, device=device)
        attention[row, max_len - len(ids):] = 1

    # Composite configs (e.g. Gemma3Config) nest hidden_size under text_config.
    d_model = getattr(model.config, "hidden_size", None) or model.config.text_config.hidden_size
    low_rank = next((item["reft"][0].shape[-1] for item in items if item["reft"] is not None), 1)
    proj = torch.zeros(len(items), d_model, low_rank)
    source = torch.zeros(len(items), d_model, low_rank)
    bias = torch.zeros(len(items), low_rank)
    max_positions = 10
    positions = torch.zeros(len(items), max_positions, dtype=torch.long)
    active = torch.zeros(len(items), max_positions, dtype=torch.bool)
    for row, (ids, item) in enumerate(zip(ids_list, items)):
        offset = max_len - len(ids)
        for slot, pos in enumerate(intervention_positions(len(ids))):
            positions[row, slot] = offset + pos
            active[row, slot] = item["reft"] is not None
        if item["reft"] is not None:
            proj[row], source[row], bias[row] = item["reft"]

    with ReftInjectRows(module, proj, source, bias, positions, active):
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
    reft_arm = arm_metrics["reft"]
    baseline = arm_metrics["unsteered"]
    random = arm_metrics["random"]
    lift_baseline = reft_arm["hit_rate"] - baseline["hit_rate"]
    lift_random = reft_arm["hit_rate"] - random["hit_rate"]
    lower_baseline = bootstrap_lower(reft_arm["flags"], baseline["flags"])
    lower_random = bootstrap_lower(reft_arm["flags"], random["flags"])
    candidate = lift_baseline >= 0.20 and lift_random >= 0.20 and reft_arm["prompt_coverage"] >= 0.50
    if random["hit_rate"] >= reft_arm["hit_rate"] and reft_arm["hit_rate"] > 0:
        cause = "RANDOM_MATCHED"
    elif reft_arm["hit_rate"] == 0:
        cause = "NO_CAUSAL_EFFECT"
    elif reft_arm["prompt_coverage"] < 0.50:
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
           "score": max(0.0, min(lift_baseline, lift_random)) * reft_arm["prompt_coverage"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--model", default="", help="local or Hugging Face model override")
    parser.add_argument("--layer", type=int, default=0, help="override the profile's default layer")
    parser.add_argument("--train-dir", required=True,
                        help="the yaml config's train.dump_dir (this script reads {train-dir}/train/LoReFT_*.pt)")
    parser.add_argument("--out-dir", default="", help="default: same as --train-dir")
    parser.add_argument("--concept-order", default="../axbench/axbench/concept500/panel14/generate/concept_order.json")
    parser.add_argument("--protocol", default="experiments/configs/qwen_selected14_baseline_protocol.json")
    parser.add_argument("--concept-ids", default="", help="optional comma-separated subset of the panel-14 IDs")
    parser.add_argument("--n-samples", type=int, default=2)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    profile = PROFILES[args.profile]
    layer = args.layer or profile["layer"]
    root = Path(__file__).resolve().parents[1]
    protocol = json.loads((root / args.protocol).read_text())
    terms_path = protocol.get(
        "terms_source", "experiments/configs/gemma_qwen_positive14_terms.json"
    )
    terms_all = json.loads((root / terms_path).read_text())
    metadata_lines = (root.parent / "axbench/axbench/concept500/prod_9b_l20_v1/generate/metadata.jsonl").read_text().splitlines()
    concept_names = {int(row["concept_id"]): row["concept"]
                     for row in (json.loads(line) for line in metadata_lines)}

    train_dir = Path(args.train_dir)
    out = Path(args.out_dir) if args.out_dir else train_dir
    out.mkdir(parents=True, exist_ok=True)

    concept_order = json.loads((root / args.concept_order).read_text())
    proj_all, source_all, bias_all = load_reft_weights(train_dir / "train")

    concept_ids = protocol["concept_ids"]
    if args.concept_ids:
        requested = [int(value) for value in args.concept_ids.split(",") if value.strip()]
        unknown = sorted(set(requested) - set(concept_ids))
        if unknown:
            raise ValueError(f"not in the panel-14 protocol: {unknown}")
        concept_ids = requested
    missing_trained = [cid for cid in concept_ids if cid not in concept_order]
    if missing_trained:
        raise ValueError(f"no trained LoReFT weights for concept(s) {missing_trained}; "
                         f"check {args.train_dir}/train and concept_order.json")

    model, tokenizer = steering.load_target(args.model or profile["model"], args.device)
    module = steering.decoder_layers(model)[layer]

    latent = pd.read_parquet(root.parent / protocol["prompt_source"])
    start, end = protocol["final_test_prompt_indices"]

    save_json(out / "protocol.json", vars(args) | {"version": VERSION, "concept_ids": concept_ids, "layer": layer})

    transcript_path = out / "transcripts.json"
    transcripts = json.loads(transcript_path.read_text()) if transcript_path.exists() else []
    records = {(int(row["concept_id"]), int(row["prompt_index"])): row for row in transcripts}
    arm_names = ["unsteered", "reft", "random"]

    for cid in concept_ids:
        position = concept_order.index(cid)
        proj, source, bias = proj_all[position], source_all[position], bias_all[position]
        random_proj, random_source, random_bias = random_control(proj, source, bias,
                                                                  args.random_seed * 1_000_000 + cid)
        prompts = latent[(latent.concept_id == cid) & (latent.category == "negative")].input.drop_duplicates().tolist()
        prompts = prompts[start:end + 1]
        if all(records.get((cid, index), {}).get("status") == "complete" for index in range(len(prompts))):
            continue
        items: list[dict[str, Any]] = []
        for index, prompt in enumerate(prompts):
            arm_reft = {"unsteered": None, "reft": (proj, source, bias),
                       "random": (random_proj, random_source, random_bias)}
            for arm in arm_names:
                for _sample in range(args.n_samples):
                    items.append({"concept_id": cid, "prompt_index": index, "prompt": prompt,
                                 "arm": arm, "reft": arm_reft[arm]})
        responses = reft_generate_group(model, tokenizer, module, items)
        for item, response in zip(items, responses):
            key = (item["concept_id"], item["prompt_index"])
            record = records.setdefault(key, {"status": "pending", "concept_id": item["concept_id"],
                                              "prompt_index": item["prompt_index"], "prompt": item["prompt"],
                                              "generation_arms": {}})
            record["generation_arms"].setdefault(item["arm"], []).append(response)
        for index in range(len(prompts)):
            record = records[(cid, index)]
            if all(len(record["generation_arms"].get(arm, [])) == args.n_samples for arm in arm_names):
                record["status"] = "complete"
        transcripts = list(records.values())
        save_json(transcript_path, transcripts)
        print(f"c{cid}: {len(items)} rows generated")

    summaries = []
    for cid in concept_ids:
        prompts = latent[(latent.concept_id == cid) & (latent.category == "negative")].input.drop_duplicates().tolist()
        prompts = prompts[start:end + 1]
        rows = [records[(cid, index)] for index in range(len(prompts))]
        if not all(row["status"] == "complete" for row in rows):
            continue
        concept = concept_names.get(cid, str(cid))
        target_terms = terms_all.get(str(cid), terms_all.get(cid, [concept]))
        summaries.append(summarize(cid, concept, target_terms, rows, args.n_samples))
    summaries.sort(key=lambda row: row["score"], reverse=True)
    save_json(out / "summary.json", {"protocol_version": VERSION, "concepts": summaries})
    lines = ["# Panel-14 LoReFT baseline", "",
            "| Rank | ID | ReFT hit | Baseline | Random | Coverage | Score | Classification |",
            "|---:|---:|---:|---:|---:|---:|---:|---|"]
    for rank, row in enumerate(summaries, 1):
        arms = row["arms"]
        lines.append(f"| {rank} | {row['concept_id']} | {arms['reft']['hit_rate']:.1%} | "
                     f"{arms['unsteered']['hit_rate']:.1%} | {arms['random']['hit_rate']:.1%} | "
                     f"{arms['reft']['prompt_coverage']:.1%} | {row['score']:.3f} | {row['failure_cause']} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print(f"reft baseline bundle -> {out}")


if __name__ == "__main__":
    main()
