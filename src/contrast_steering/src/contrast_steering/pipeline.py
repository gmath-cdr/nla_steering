from __future__ import annotations

import json
from pathlib import Path

from .data import write_jsonl
from .model import add_vector, capture_last_token, resolve_layer
from .scoring import choose_alpha, score_text


def tokenize(tokenizer, text: str, device, max_length: int):
    return tokenizer(text, return_tensors="pt", truncation=True, max_length=max_length).to(device)


def extract_vector(model, tokenizer, pairs: list[dict], layer: int, one_based: bool,
                   max_length: int, normalize: bool):
    import torch
    module, index, path = resolve_layer(model, layer, one_based)
    positive, neutral = [], []
    with torch.inference_mode():
        for row in pairs:
            with capture_last_token(module, positive):
                model(**tokenize(tokenizer, row["positive"], model.device, max_length), use_cache=False)
            with capture_last_token(module, neutral):
                model(**tokenize(tokenizer, row["neutral"], model.device, max_length), use_cache=False)
    pos = torch.cat(positive, dim=0)
    neg = torch.cat(neutral, dim=0)
    vector = (pos - neg).mean(0)
    raw_norm = float(vector.norm())
    if normalize:
        vector = vector / vector.norm().clamp_min(1e-12)
    meta = {"method": "caa", "layer_requested": layer, "layer_index": index,
            "one_based": one_based, "hook_path": path, "pairs": len(pairs),
            "raw_norm": raw_norm, "normalized": normalize}
    return vector, meta


def generate(model, tokenizer, module, vector, prompt: str, alpha: float,
             max_length: int, max_new_tokens: int) -> str:
    import torch
    inputs = tokenize(tokenizer, prompt, model.device, max_length)
    prompt_tokens = inputs["input_ids"].shape[1]
    with torch.inference_mode(), add_vector(module, vector, alpha):
        output = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False,
                                pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id)
    return tokenizer.decode(output[0, prompt_tokens:], skip_special_tokens=True)


def evaluate_alphas(model, tokenizer, vector, rows: list[dict], layer: int, one_based: bool,
                    alphas: list[float], max_length: int, max_new_tokens: int,
                    hit_weight: float, quality_weight: float, output_path: Path):
    module, _, _ = resolve_layer(model, layer, one_based)
    details, summaries = [], []
    for alpha in alphas:
        alpha_rows = []
        for i, row in enumerate(rows):
            text = generate(model, tokenizer, module, vector, row["prompt"], alpha,
                            max_length, max_new_tokens)
            score = score_text(text, row["target_terms"], hit_weight, quality_weight)
            item = {"example": i, "alpha": alpha, "prompt": row["prompt"], "generation": text,
                    "hit": score.hit, "quality": score.quality, "combined": score.combined}
            details.append(item)
            alpha_rows.append(item)
            write_jsonl(output_path, details)  # crash-safe progress
        summaries.append({"alpha": alpha, **{k: sum(x[k] for x in alpha_rows) / len(alpha_rows)
                                             for k in ("hit", "quality", "combined")}})
    return choose_alpha(summaries), summaries


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

