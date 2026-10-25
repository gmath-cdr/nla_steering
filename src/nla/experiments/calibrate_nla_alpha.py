"""Held-out alpha sweep over already-constructed prompt-specific NLA vectors."""
from __future__ import annotations

import argparse, json
from pathlib import Path

from experiments import steering, vectors
from experiments.confirmation_metrics import jaccard, surface_quality, tokens
from experiments.discovery500 import matches


def save(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--layer", type=int, required=True)
    p.add_argument("--alphas", required=True)
    p.add_argument("--fallback-alpha", type=float, required=True)
    p.add_argument("--device", default="cuda:0")
    args = p.parse_args()
    run = Path(args.run_dir)
    alphas = [float(x) for x in args.alphas.split(",")]
    records = json.loads((run / "transcripts.json").read_text())
    lexicons = json.loads((run / "lexicons.json").read_text())
    model, tokenizer = steering.load_target(args.target, args.device)
    details, selections, all_summaries = [], {}, {}
    for cid in sorted({int(x["concept_id"]) for x in records}):
        concept_rows = [x for x in records if int(x["concept_id"]) == cid]
        terms = lexicons[str(cid)]
        candidates = []
        for alpha in alphas:
            metrics = []
            for record in concept_rows:
                refs = record["generation_arms"]["unsteered"]
                direction = vectors.load(record["vector_path"])
                responses = steering.generate(
                    model, tokenizer, [{"role": "user", "content": record["prompt"]}],
                    args.layer, direction, -1, alpha, n_samples=2)
                for sample_index, response in enumerate(responses):
                    surface = surface_quality(response)
                    similarity = max(jaccard(tokens(response), tokens(ref)) for ref in refs)
                    hits = matches(response, terms)
                    item = {"concept_id": cid, "prompt_index": record["prompt_index"],
                            "sample_index": sample_index, "alpha": alpha,
                            "prompt": record["prompt"], "response": response,
                            "hit": bool(hits), "matched_terms": hits,
                            "reference_similarity": similarity,
                            "target_term_count": len(hits), **surface}
                    item["quality_pass"] = (8 <= surface["word_count"] <= 350 and
                                             surface["repeated_trigram_rate"] <= .10 and
                                             similarity >= .12)
                    details.append(item); metrics.append(item)
                save(run / "alpha_calibration.json", details)
            n = len(metrics)
            summary = {"alpha": alpha,
                       "hit_rate": sum(x["hit"] for x in metrics) / n,
                       "quality_pass_rate": sum(x["quality_pass"] for x in metrics) / n,
                       "mean_reference_similarity": sum(x["reference_similarity"] for x in metrics) / n,
                       "mean_repeated_trigram_rate": sum(x["repeated_trigram_rate"] for x in metrics) / n,
                       "mean_target_term_count": sum(x["target_term_count"] for x in metrics) / n}
            summary["eligible"] = (summary["quality_pass_rate"] >= .75 and
                                   summary["mean_reference_similarity"] >= .12 and
                                   summary["mean_repeated_trigram_rate"] <= .10 and
                                   summary["mean_target_term_count"] <= 4.0)
            candidates.append(summary)
        eligible = [x for x in candidates if x["eligible"]]
        selected = max(eligible, key=lambda x: (x["hit_rate"], -x["alpha"])) if eligible else None
        selections[str(cid)] = selected["alpha"] if selected else args.fallback_alpha
        all_summaries[str(cid)] = candidates
        save(run / "alpha_summaries.json", {"by_concept": selections,
                                             "summaries": all_summaries})
        save(run / "selected_alphas.json", selections)


if __name__ == "__main__":
    main()
