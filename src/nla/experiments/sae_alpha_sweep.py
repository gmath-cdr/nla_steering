"""Lightweight alpha-sweep diagnostic for the SAE baseline (follow-up to
sae_baseline.py). The full baseline run reused DiffMean's calibrated alpha as
a starting point per SAE_REFT_BASELINE_PLAN.md's own suggestion ("or sweep a
small grid if time allows") -- results came back almost universally null, so
this checks whether that's a real ceiling or just the wrong alpha for a
differently-scaled vector (an SAE decoder row isn't the same kind of object
as a DiffMean mean-difference vector).

Reuses the already-selected feature vectors from a prior sae_baseline.py run
(--vectors-dir) -- no recomputation of feature selection, only new generation
at each alpha. Deliberately lighter than sae_baseline.py's full protocol: no
random-direction control, no bootstrap CI, single sample per prompt. This is
a fast "does hit-rate respond to alpha at all" diagnostic, not a replacement
confirmation run -- if a concept looks promising here, rerun it properly
through sae_baseline.py with --alpha-source pointed at the winning value.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
import torch

from experiments import steering
from experiments.diffmean_discovery500 import generate_group
from experiments.discovery500 import matches, save_json

PROFILES = {
    "qwen2.5-7b": {"model_key": "qwen", "model": "Qwen/Qwen2.5-7B-Instruct", "layer": 20},
    "gemma3-12b": {"model_key": "gemma", "model": "google/gemma-3-12b-it", "layer": 32},
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--layer", type=int, default=0)
    parser.add_argument("--model", default=None, help="optional local target-model path")
    parser.add_argument("--vectors-dir", required=True, help="vectors/ dir from a prior sae_baseline.py run")
    parser.add_argument("--concept-ids", required=True, help="comma-separated subset to sweep")
    parser.add_argument("--alphas", default="0.1,0.2,0.4,0.6,0.8,1.0")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--protocol", default="experiments/configs/qwen_selected14_baseline_protocol.json")
    parser.add_argument("--n-samples", type=int, default=2)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    profile = PROFILES[args.profile]
    layer = args.layer or profile["layer"]
    root = Path(__file__).resolve().parents[1]
    protocol = json.loads((root / args.protocol).read_text())
    terms_path = protocol.get("terms_source", "experiments/configs/gemma_qwen_positive14_terms.json")
    terms_all = json.loads((root / terms_path).read_text())
    concept_ids = [int(v) for v in args.concept_ids.split(",") if v.strip()]
    alphas = [float(v) for v in args.alphas.split(",") if v.strip()]

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    model, tokenizer = steering.load_target(args.model or profile["model"], args.device)
    module = steering.decoder_layers(model)[layer]

    latent = pd.read_parquet(root.parent / protocol["prompt_source"])
    start, end = protocol["final_test_prompt_indices"]
    vectors_dir = Path(args.vectors_dir)

    results = []
    for cid in concept_ids:
        direction = torch.load(vectors_dir / f"c{cid}.sae.pt", map_location="cpu", weights_only=True)
        prompts = latent[(latent.concept_id == cid) & (latent.category == "negative")].input.drop_duplicates().tolist()
        prompts = prompts[start:end + 1]
        target_terms = terms_all.get(str(cid), terms_all.get(cid, [str(cid)]))

        items: list[dict[str, Any]] = []
        for index, prompt in enumerate(prompts):
            items.append({"concept_id": cid, "prompt_index": index, "prompt": prompt,
                         "arm": "unsteered", "alpha": 0.0, "direction": None})
            for alpha in alphas:
                for _sample in range(args.n_samples):
                    items.append({"concept_id": cid, "prompt_index": index, "prompt": prompt,
                                 "arm": f"alpha_{alpha}", "alpha": alpha, "direction": direction})
        responses = generate_group(model, tokenizer, module, items)

        by_arm: dict[str, list[str]] = {}
        for item, response in zip(items, responses):
            by_arm.setdefault(item["arm"], []).append(response)
        unsteered_hit_rate = sum(bool(matches(r, target_terms)) for r in by_arm["unsteered"]) / len(by_arm["unsteered"])
        row = {"concept_id": cid, "unsteered_hit_rate": unsteered_hit_rate, "by_alpha": {}}
        for alpha in alphas:
            responses_here = by_arm[f"alpha_{alpha}"]
            hits = [bool(matches(r, target_terms)) for r in responses_here]
            row["by_alpha"][alpha] = {"hit_rate": sum(hits) / len(hits), "n": len(hits),
                                       "sample_responses": responses_here[:2]}
        results.append(row)
        save_json(out / "sweep_results.json", results)
        best_alpha, best = max(row["by_alpha"].items(), key=lambda kv: kv[1]["hit_rate"])
        all_rates = ", ".join(
            f"{alpha}={row['by_alpha'][alpha]['hit_rate']:.0%}" for alpha in alphas
        )
        print(f"c{cid}: unsteered={unsteered_hit_rate:.1%}  best alpha={best_alpha} -> {best['hit_rate']:.1%}  "
             f"(all: {all_rates})")

    print(f"sweep complete -> {out}")


if __name__ == "__main__":
    main()
