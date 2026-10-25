"""Regenerate only baseline random arms with per-prompt CAA-norm-matched vectors."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from experiments.run_panel_baselines import dump, generate_many, hit, layers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--config", default="experiments/configs/qwen_selected14_baseline_protocol.json")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    run = Path(args.run_dir)
    config = json.loads((root / args.config).read_text())
    spec = config["models"][args.model_key]
    terms = json.loads((root / "experiments/configs/gemma_qwen_positive14_terms.json").read_text())
    source_rows = json.loads((run / "results.json").read_text())
    retained = [row for row in source_rows if not row["method"].startswith("random_")]
    prompt_rows = {}
    for row in retained:
        prompt_rows[(row["concept_id"], row["prompt_index"])] = row

    repair_path = run / "random_rows.norm_matched.json"
    repaired = json.loads(repair_path.read_text()) if repair_path.exists() else []
    complete_ids = {
        cid for cid in config["concept_ids"]
        if len([row for row in repaired if row["concept_id"] == cid]) == 48
    }

    tokenizer = AutoTokenizer.from_pretrained(spec["model"])
    tokenizer.padding_side = "left"
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id
    model = AutoModelForCausalLM.from_pretrained(
        spec["model"], torch_dtype=torch.bfloat16, device_map={"": 0},
        low_cpu_mem_usage=True).eval()
    module = layers(model)[spec["layer_index"]]
    torch.manual_seed(42)

    for concept_index, cid in enumerate(config["concept_ids"]):
        if cid in complete_ids:
            continue
        caa = torch.load(run / f"c{cid}.caa.pt", map_location="cpu", weights_only=True)
        reference_norm = float(caa.float().norm())
        concept_rows = []
        for prompt_index in range(14, 22):
            source = prompt_rows[(cid, prompt_index)]
            for seed in config["random_seeds"]:
                generator = torch.Generator().manual_seed(
                    seed * 100000 + cid * 100 + prompt_index - 14)
                random_vector = torch.randn(caa.shape, generator=generator, dtype=torch.float32)
                random_vector = (
                    random_vector / random_vector.norm().clamp_min(1e-12) * reference_norm
                )
                responses = generate_many(
                    model, tokenizer, [source["prompt"], source["prompt"]], module,
                    random_vector, spec["fixed_alpha"], batch_size=2, do_sample=True)
                for sample_index, response in enumerate(responses):
                    concept_rows.append({
                        "model": args.model_key,
                        "concept_id": cid,
                        "concept": source["concept"],
                        "prompt_index": prompt_index,
                        "sample_index": sample_index,
                        "prompt": source["prompt"],
                        "method": f"random_{seed}",
                        "alpha": spec["fixed_alpha"],
                        "response": response,
                        "hit": hit(response, terms[str(cid)]),
                        "vector_norm": float(random_vector.norm()),
                        "reference_vector_norm": reference_norm,
                        "norm_matched": True,
                    })
        repaired = [row for row in repaired if row["concept_id"] != cid] + concept_rows
        dump(repair_path, repaired)
        dump(run / "random_repair_progress.json", {
            "completed_concepts": len({
                row["concept_id"] for row in repaired
                if len([x for x in repaired if x["concept_id"] == row["concept_id"]]) == 48
            }),
            "total": len(config["concept_ids"]),
            "last_concept_id": cid,
            "source": "per-prompt seeded Gaussian scaled to concept CAA norm",
        })

    assert len(repaired) == 14 * 8 * 3 * 2
    backup = run / "results.pre_norm_match.json"
    if not backup.exists():
        shutil.copy2(run / "results.json", backup)
    final_rows = retained + repaired
    assert len(final_rows) == 1568
    temporary = run / "results.norm_matched.tmp.json"
    dump(temporary, final_rows)
    temporary.replace(run / "results.json")

    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["random_controls"] = {
        "seeds": config["random_seeds"],
        "scope": "three independently seeded vectors per final-test prompt",
        "norm_reference": "concept CAA vector",
        "norm_matched": True,
    }
    dump(manifest_path, manifest)


if __name__ == "__main__":
    main()
