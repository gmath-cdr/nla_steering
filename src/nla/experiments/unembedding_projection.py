"""Project saved AV-edit steering vectors onto the target model's own unembedding rows.

Answers TODO_08_15.md item #4: does an AV-edit vector work by boosting the
unembedding directions of the concept's own words, or is something deeper
happening? For each concept we rank every vocabulary token by its dot product
with the (unit-normalized) steering vector, then ask where the concept's own
strict-lexicon tokens land in that ranking -- both for the real vector and for
matched random directions. No generation, no AV/AR checkpoints: just the
already-saved centroid vectors and the target model's own weights, read
directly off the safetensors shards (CPU-only, no CUDA needed).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import torch
from safetensors import safe_open
from transformers import AutoTokenizer

from experiments import data, vectors

VERSION = "unembedding-row-projection-v1"


def unembedding_matrix(model_dir: Path, weight_key: str) -> torch.Tensor:
    index_path = model_dir / "model.safetensors.index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text())
        shard = index["weight_map"][weight_key]
    else:
        shard = "model.safetensors"
    with safe_open(str(model_dir / shard), framework="pt") as handle:
        return handle.get_tensor(weight_key).float()


def term_token_ids(tokenizer: Any, term: str) -> set[int]:
    ids = set()
    for variant in (term, " " + term, term.capitalize(), " " + term.capitalize()):
        encoded = tokenizer.encode(variant, add_special_tokens=False)
        if len(encoded) == 1:
            ids.add(encoded[0])
    return ids


def percentiles(projection: torch.Tensor, token_ids: set[int]) -> list[float]:
    vocab_size = projection.numel()
    order = torch.argsort(projection, descending=True)
    rank_of = {int(token): position for position, token in enumerate(order.tolist())}
    return [1.0 - rank_of[token] / (vocab_size - 1) for token in token_ids if token in rank_of]


def evaluate_concept(unembed: torch.Tensor, direction: torch.Tensor, token_ids: set[int],
                     random_seeds: list[int]) -> dict[str, Any]:
    direction = direction.float() / direction.float().norm().clamp_min(1e-12)
    projection = unembed @ direction
    real_percentiles = percentiles(projection, token_ids)
    random_percentiles = []
    for seed in random_seeds:
        generator = torch.Generator().manual_seed(seed)
        random_direction = torch.randn(direction.shape, generator=generator)
        random_direction = random_direction / random_direction.norm().clamp_min(1e-12)
        random_percentiles.extend(percentiles(unembed @ random_direction, token_ids))
    mean_real = sum(real_percentiles) / len(real_percentiles) if real_percentiles else 0.0
    mean_random = sum(random_percentiles) / len(random_percentiles) if random_percentiles else 0.0
    cosine_to_terms = [float(projection[token].item() / unembed[token].norm().clamp_min(1e-12))
                       for token in token_ids if token < unembed.shape[0]]
    return {
        "n_terms_resolved": len(real_percentiles),
        "mean_percentile_real": mean_real,
        "mean_percentile_random": mean_random,
        "percentile_lift": mean_real - mean_random,
        "max_percentile_real": max(real_percentiles, default=0.0),
        "mean_cosine_to_terms": sum(cosine_to_terms) / len(cosine_to_terms) if cosine_to_terms else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, help="local HF snapshot directory")
    parser.add_argument("--tokenizer", required=True, help="HF repo id or local path for the tokenizer")
    parser.add_argument("--weight-key", required=True,
                        help="safetensors key for the unembedding matrix, e.g. lm_head.weight")
    parser.add_argument("--vectors-dir", required=True,
                        help="directory of c{concept_id}.mean_av_edit.pt centroid vectors")
    parser.add_argument("--terms", required=True, help="path to the strict-lexicon terms JSON")
    parser.add_argument("--metadata", required=True, help="AxBench metadata.jsonl for concept names")
    parser.add_argument("--random-seeds", default="1,2,3,4,5")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    unembed = unembedding_matrix(Path(args.model_dir), args.weight_key)
    terms = json.loads(Path(args.terms).read_text())
    concept_names = data.concepts(args.metadata)
    random_seeds = [int(value) for value in args.random_seeds.split(",")]

    concepts = []
    for concept_id_str, term_list in terms.items():
        concept_id = int(concept_id_str)
        vector_path = Path(args.vectors_dir) / f"c{concept_id}.mean_av_edit.pt"
        if not vector_path.exists():
            print(f"skip c{concept_id}: no saved vector at {vector_path}")
            continue
        direction = vectors.load(str(vector_path))
        token_ids: set[int] = set()
        for term in term_list:
            token_ids |= term_token_ids(tokenizer, term)
        result = evaluate_concept(unembed, direction, token_ids, random_seeds)
        concepts.append({"concept_id": concept_id, "concept": concept_names.get(concept_id, ""),
                         "terms": term_list, **result})

    concepts.sort(key=lambda row: row["percentile_lift"], reverse=True)
    overall_lift = (sum(row["percentile_lift"] for row in concepts) / len(concepts)) if concepts else 0.0
    payload = {
        "version": VERSION, "model_dir": args.model_dir, "weight_key": args.weight_key,
        "vectors_dir": args.vectors_dir, "random_seeds": random_seeds,
        "mean_percentile_lift": overall_lift, "concepts": concepts,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    lines = ["# Unembedding-row projection", "", f"Mean percentile lift (real vs. random): {overall_lift:.3f}", "",
             "| Concept | ID | Terms resolved | Mean pctl (real) | Mean pctl (random) | Lift | Mean cosine |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for row in concepts:
        lines.append(f"| {row['concept']} | {row['concept_id']} | {row['n_terms_resolved']} | "
                     f"{row['mean_percentile_real']:.3f} | {row['mean_percentile_random']:.3f} | "
                     f"{row['percentile_lift']:+.3f} | {row['mean_cosine_to_terms']:+.4f} |")
    out_path.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(f"unembedding projection -> {out_path}")


if __name__ == "__main__":
    main()
