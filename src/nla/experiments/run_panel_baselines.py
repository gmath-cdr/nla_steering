"""Run the locked 14-concept DiffMean/CAA comparison with crash-safe outputs."""
from __future__ import annotations

import argparse, json, sys
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pandas as pd
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.confirmation_metrics import jaccard, surface_quality, tokens
from experiments.discovery500 import matches
from contrast_steering.pipeline import extract_vector as extract_caa_vector


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def layers(model):
    for path in ("model.layers", "language_model.model.layers", "model.language_model.layers"):
        obj = model
        try:
            for part in path.split("."): obj = getattr(obj, part)
            return obj
        except AttributeError: pass
    raise TypeError(type(model))


def hidden(output): return output[0] if isinstance(output, tuple) else output
def replace(output, value): return (value, *output[1:]) if isinstance(output, tuple) else value


@contextmanager
def inject(module, direction, alpha):
    # Match the locked NLA convention: unit direction times current residual norm.
    direction = direction / direction.norm().clamp_min(1e-12)
    def hook(_m, _i, out):
        h = hidden(out)
        d = direction.to(h.device, h.dtype).view(1, 1, -1)
        scale = h.norm(dim=-1, keepdim=True)
        return replace(out, h + alpha * scale * d)
    handle = module.register_forward_hook(hook)
    try: yield
    finally: handle.remove()


def chat(tok, prompt, answer=None):
    messages = [{"role": "user", "content": prompt}]
    if answer is not None: messages.append({"role": "assistant", "content": answer})
    return tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=answer is None)


def forward_tokens(model, tok, text, module, last_only=False, trim=(0, 0)):
    found = []
    def hook(_m, _i, out): found.append(hidden(out).detach().float().cpu())
    h = module.register_forward_hook(hook)
    try:
        x = tok(text, return_tensors="pt", truncation=True, max_length=1024).to(model.device)
        with torch.inference_mode(): model(**x, use_cache=False)
    finally: h.remove()
    mask = x.attention_mask[0].bool().cpu()
    values = found[0][0][mask]
    left, right = trim
    values = values[left:len(values) - right if right else None]
    return values[-1:] if last_only else values


def axbench_trim(tok):
    # apply_chat_template(tokenize=True) returns a BatchEncoding on newer transformers
    # (iterating it yields dict keys, not token ids, and it isn't reversible) -- pull
    # out input_ids explicitly rather than relying on the return type.
    a = tok.apply_chat_template([{"role": "user", "content": "1"}], tokenize=True)
    b = tok.apply_chat_template([{"role": "user", "content": "2"}], tokenize=True)
    a = getattr(a, "input_ids", a)
    b = getattr(b, "input_ids", b)
    prefix = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), 0)
    suffix = next((i for i, (x, y) in enumerate(zip(reversed(a), reversed(b))) if x != y), 0)
    return prefix, suffix


def generate_many(model, tok, prompts, module=None, vector=None, alpha=0.0, batch_size=8,
                  do_sample=False):
    answers = []
    cm = inject(module, vector, alpha) if vector is not None else nullcontext()
    for start in range(0, len(prompts), batch_size):
        texts = [chat(tok, p) for p in prompts[start:start + batch_size]]
        x = tok(texts, return_tensors="pt", padding=True).to(model.device)
        sampling = {"do_sample": do_sample}
        if do_sample:
            sampling["temperature"] = 0.7
        with torch.inference_mode(), cm:
            y = model.generate(**x, max_new_tokens=96, **sampling,
                               pad_token_id=tok.pad_token_id or tok.eos_token_id)
        answers.extend(tok.batch_decode(y[:, x.input_ids.shape[1]:], skip_special_tokens=True))
    return answers


def generate(model, tok, prompt, module=None, vector=None, alpha=0.0):
    return generate_many(model, tok, [prompt], module, vector, alpha, 1)[0]


def hit(text, terms):
    return int(bool(matches(text, terms)))


def quality(text, references, terms):
    surface = surface_quality(text)
    similarity = max((jaccard(tokens(text), tokens(r)) for r in references), default=0.0)
    term_count = len(matches(text, terms))
    passed = (8 <= surface["word_count"] <= 350 and
              surface["repeated_trigram_rate"] <= .10 and similarity >= .12)
    return {**surface, "reference_similarity": similarity, "target_term_count": term_count,
            "quality_pass": passed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-key", choices=["qwen", "gemma"], required=True)
    ap.add_argument("--config", default="experiments/configs/qwen_selected14_baseline_protocol.json")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    cfg = json.loads((root / args.config).read_text())
    spec = cfg["models"][args.model_key]
    torch.manual_seed(42)
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    terms_path = cfg.get(
        "terms_source", "experiments/configs/gemma_qwen_positive14_terms.json"
    )
    terms = json.loads((root / terms_path).read_text())
    train = pd.read_parquet(root.parent / cfg["construction_source"])
    latent = pd.read_parquet(root.parent / cfg["prompt_source"])
    tok = AutoTokenizer.from_pretrained(spec["model"])
    tok.padding_side = "left"
    if tok.pad_token_id is None: tok.pad_token_id = tok.eos_token_id
    model = AutoModelForCausalLM.from_pretrained(spec["model"], torch_dtype=torch.bfloat16,
                                                 device_map={"": 0}, low_cpu_mem_usage=True).eval()
    module = layers(model)[spec["layer_index"]]
    diffmean_trim = axbench_trim(tok)
    rows, calibration_rows, selections, vectors, caa_metadata = [], [], [], {}, {}
    for ci, cid in enumerate(cfg["concept_ids"]):
        concept_rows = train[(train.concept_id == cid) & (train.category == "positive")].head(72)
        concept = str(concept_rows.output_concept.iloc[0])
        negatives = train[(train.concept_id == -1) & (train.category == "negative")].head(72)
        pos_all, neg_all, caa_pairs = [], [], []
        neutral_answers = generate_many(model, tok, [str(x) for x in concept_rows.input], batch_size=8)
        for (_, r), neutral in zip(concept_rows.iterrows(), neutral_answers):
            pos_text, neutral_text = chat(tok, str(r.input), str(r.output)), chat(tok, str(r.input), neutral)
            pos_all.append(forward_tokens(model, tok, pos_text, module, trim=diffmean_trim))
            caa_pairs.append({"positive": pos_text, "neutral": neutral_text})
        for _, r in negatives.iterrows():
            neg_all.append(forward_tokens(model, tok, chat(tok, str(r.input), str(r.output)),
                                          module, trim=diffmean_trim))
        # Exact official AxBench construction: mean across eligible positive tokens minus negative tokens.
        caa_vector, caa_meta = extract_caa_vector(
            model, tok, caa_pairs, spec["layer_index"], False, 1024, False)
        caa_metadata[str(cid)] = caa_meta
        vectors[cid] = {"diffmean": torch.cat(pos_all).mean(0) - torch.cat(neg_all).mean(0),
                        "caa": caa_vector, "concept": concept}
        torch.save(vectors[cid]["diffmean"], out / f"c{cid}.diffmean.pt")
        torch.save(vectors[cid]["caa"], out / f"c{cid}.caa.pt")
        prompts = latent[(latent.concept_id == cid) & (latent.category == "negative")].input.drop_duplicates().tolist()
        cal = prompts[cfg["calibration_prompt_indices"][0]:cfg["calibration_prompt_indices"][1]+1]
        test = prompts[cfg["final_test_prompt_indices"][0]:cfg["final_test_prompt_indices"][1]+1]
        target_terms = terms.get(str(cid), terms.get(cid, [concept]))
        chosen = {}
        cal_expanded = [prompt for prompt in cal for _ in range(2)]
        cal_references = generate_many(model, tok, cal_expanded, batch_size=8, do_sample=True)
        for sample_index, (prompt, response) in enumerate(zip(cal_expanded, cal_references)):
            calibration_rows.append({"concept_id": cid, "concept": concept, "method": "unsteered",
                                     "alpha": 0.0, "prompt": prompt, "response": response,
                                     "sample_index": sample_index % 2,
                                     "hit": hit(response, target_terms),
                                     **quality(response, [response], target_terms)})
        for method in ("diffmean", "caa"):
            scores = []
            for alpha in spec["alpha_grid"]:
                generated = generate_many(model, tok, cal_expanded, module, vectors[cid][method],
                                          alpha, batch_size=8, do_sample=True)
                metrics = []
                for sample_index, (prompt, response, reference) in enumerate(
                        zip(cal_expanded, generated, cal_references)):
                    item = {"concept_id": cid, "concept": concept, "method": method,
                            "alpha": alpha, "prompt": prompt, "response": response,
                            "sample_index": sample_index % 2,
                            "hit": hit(response, target_terms),
                            **quality(response, [reference], target_terms)}
                    calibration_rows.append(item); metrics.append(item)
                pass_rate = sum(x["quality_pass"] for x in metrics) / len(metrics)
                mean_terms = sum(x["target_term_count"] for x in metrics) / len(metrics)
                eligible = pass_rate >= .75 and mean_terms <= 4.0
                scores.append((eligible, sum(x["hit"] for x in metrics) / len(metrics), -alpha, alpha))
                dump(out / "calibration.json", calibration_rows)
            eligible_scores = [score for score in scores if score[0]]
            if eligible_scores:
                chosen[method] = max(eligible_scores)[3]
                status = "quality_eligible"
            else:
                chosen[method] = spec["fixed_alpha"]
                status = "fixed_fallback_no_quality_eligible_alpha"
            selections.append({"concept_id": cid, "method": method,
                               "chosen_alpha": chosen[method], "status": status})
            dump(out / "alpha_selections.json", selections)
        methods = {"unsteered": (None, 0.0), "diffmean": (vectors[cid]["diffmean"], chosen["diffmean"]),
                   "caa_fixed": (vectors[cid]["caa"], spec["fixed_alpha"]),
                   "caa_calibrated": (vectors[cid]["caa"], chosen["caa"])}
        for pi, prompt in enumerate(test):
            for name, (vec, alpha) in methods.items():
                responses = generate_many(model, tok, [prompt, prompt], module, vec, alpha,
                                          batch_size=2, do_sample=True)
                for sample_index, response in enumerate(responses):
                    rows.append({"model": args.model_key, "concept_id": cid, "concept": concept,
                                 "prompt_index": 14+pi, "sample_index": sample_index,
                                 "prompt": prompt, "method": name, "alpha": alpha,
                                 "response": response, "hit": hit(response, target_terms)})
            for seed in cfg["random_seeds"]:
                gen = torch.Generator().manual_seed(seed * 100000 + cid * 100 + pi)
                reference_norm = vectors[cid]["caa"].float().norm()
                rand = torch.randn(vectors[cid]["caa"].shape, generator=gen, dtype=torch.float32)
                rand = rand / rand.norm().clamp_min(1e-12) * reference_norm
                responses = generate_many(model, tok, [prompt, prompt], module, rand,
                                          spec["fixed_alpha"], batch_size=2, do_sample=True)
                for sample_index, response in enumerate(responses):
                    rows.append({"model": args.model_key, "concept_id": cid, "concept": concept,
                                 "prompt_index": 14+pi, "sample_index": sample_index,
                                 "prompt": prompt, "method": f"random_{seed}",
                                 "alpha": spec["fixed_alpha"], "response": response,
                                 "hit": hit(response, target_terms),
                                 "vector_norm": float(rand.norm()),
                                 "reference_vector_norm": float(reference_norm),
                                 "norm_matched": True})
            dump(out / "results.json", rows)
        dump(out / "progress.json", {"completed_concepts": ci+1, "total": len(cfg["concept_ids"])})
    dump(out / "manifest.json", {"protocol": cfg, "model": spec, "rows": len(rows),
                                  "diffmean_prefix_suffix_trim": diffmean_trim,
                                  "caa_extraction_metadata": caa_metadata,
                                  "random_controls": {"seeds": cfg["random_seeds"],
                                                      "scope": "per final-test prompt",
                                                      "norm_reference": "concept CAA vector",
                                                      "norm_matched": True},
                                  "note": "DiffMean follows AxBench mean.py; CAA is returned by contrast_steering.pipeline.extract_vector."})


if __name__ == "__main__": main()
