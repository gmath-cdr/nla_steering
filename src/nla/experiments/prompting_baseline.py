"""Direct-prompting baseline for the locked 14-concept panel (TODO_08_15.md item #2).

No vectors, no hooks, no AR/AV pass: prompt the target model directly to
write about the concept and score with the same strict lexicon used
elsewhere. Reuses the exact chat template, sampling settings, and held-out
test prompts from ``run_panel_baselines.py`` so the new "prompting" arm is
directly comparable to the "unsteered" rows already saved in
``artifacts/baseline_panel14/{qwen,gemma}/results.json``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.data import concepts as load_concepts
from experiments.run_panel_baselines import chat, generate_many, hit, quality


VERSION = "prompting-baseline-v1"
INSTRUCTION = "Write a response that touches on: {concept}."


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-key", choices=["qwen", "gemma"], required=True)
    ap.add_argument("--config", default="experiments/configs/qwen_selected14_baseline_protocol.json")
    ap.add_argument("--metadata", required=True, help="AxBench metadata.jsonl for concept names")
    ap.add_argument("--output", required=True)
    ap.add_argument("--unsteered-results", required=True,
                    help="Final-test baseline results used for prompt-matched quality references")
    ap.add_argument("--batch-size", type=int, default=8)
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    cfg = json.loads((root / args.config).read_text())
    spec = cfg["models"][args.model_key]
    torch.manual_seed(42)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    unsteered = [
        row for row in json.loads(Path(args.unsteered_results).read_text())
        if row.get("method") == "unsteered"
    ]
    references: dict[tuple[int, int], list[str]] = {}
    for row in unsteered:
        references.setdefault((int(row["concept_id"]), int(row["prompt_index"])), []).append(row["response"])

    terms_path = cfg.get(
        "terms_source", "experiments/configs/gemma_qwen_positive14_terms.json"
    )
    terms = json.loads((root / terms_path).read_text())
    concept_names = load_concepts(args.metadata)
    latent = pd.read_parquet(root.parent / cfg["prompt_source"])

    tok = AutoTokenizer.from_pretrained(spec["model"])
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token_id = tok.eos_token_id
    model = AutoModelForCausalLM.from_pretrained(
        spec["model"], torch_dtype=torch.bfloat16, device_map={"": 0}, low_cpu_mem_usage=True,
    ).eval()

    tasks = []
    for cid in cfg["concept_ids"]:
        concept = concept_names[cid]
        target_terms = terms.get(str(cid), [concept])
        prompts = latent[(latent.concept_id == cid) & (latent.category == "negative")].input.drop_duplicates().tolist()
        test = prompts[cfg["final_test_prompt_indices"][0]:cfg["final_test_prompt_indices"][1] + 1]
        for pi, prompt in enumerate(test):
            augmented = f"{prompt}\n\n{INSTRUCTION.format(concept=concept)}"
            tasks.append({"concept_id": cid, "concept": concept, "prompt_index": 14 + pi,
                         "prompt": prompt, "augmented_prompt": augmented, "target_terms": target_terms})

    expanded = [item for item in tasks for _ in range(2)]
    responses = generate_many(model, tok, [item["augmented_prompt"] for item in expanded],
                              batch_size=args.batch_size, do_sample=True)

    rows = []
    for sample_index, (item, response) in enumerate(zip(expanded, responses)):
        matched = references.get((item["concept_id"], item["prompt_index"]), [])
        if not matched:
            raise ValueError(
                "No unsteered quality reference for "
                f"concept {item['concept_id']}, prompt {item['prompt_index']}"
            )
        rows.append({"model": args.model_key, "concept_id": item["concept_id"], "concept": item["concept"],
                    "prompt_index": item["prompt_index"], "sample_index": sample_index % 2,
                    "prompt": item["prompt"], "method": "prompting_baseline", "alpha": None,
                    "response": response, "hit": hit(response, item["target_terms"]),
                    **quality(response, matched, item["target_terms"])})
        if sample_index % 20 == 0:
            dump(out / "results.json", rows)
    dump(out / "results.json", rows)

    by_concept: dict[int, list[dict]] = {}
    for row in rows:
        by_concept.setdefault(row["concept_id"], []).append(row)
    summary = []
    for cid, concept_rows in by_concept.items():
        hits = [row["hit"] for row in concept_rows]
        summary.append({"concept_id": cid, "concept": concept_rows[0]["concept"],
                       "n": len(hits), "strict_hit_rate": sum(hits) / len(hits)})
    overall_hit_rate = sum(row["hit"] for row in rows) / len(rows)
    dump(out / "summary.json", {"version": VERSION, "model_key": args.model_key,
                                "overall_hit_rate": overall_hit_rate, "concepts": summary})
    print(f"prompting baseline ({args.model_key}) -> {out}, overall hit rate {overall_hit_rate:.1%}")


if __name__ == "__main__":
    main()
