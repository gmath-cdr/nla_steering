"""Validate Jacobian-transported steering directions against model logit shifts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import torch

from experiments import steering, vectors


def _atomic_json(path: str, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(target)


def _tokens(tokenizer: Any, scores: torch.Tensor, count: int) -> list[dict[str, Any]]:
    values, ids = scores.topk(count)
    return [{"token_id": int(token_id), "token": tokenizer.decode([int(token_id)]),
             "score": float(value)} for value, token_id in zip(values, ids, strict=True)]


def _term_ranks(tokenizer: Any, scores: torch.Tensor, terms: list[str]) -> list[dict[str, Any]]:
    rows = []
    for term in terms:
        ids = set()
        for text in (term, " " + term):
            ids.update(tokenizer.encode(text, add_special_tokens=False))
        candidates = []
        for token_id in ids:
            score = scores[token_id]
            candidates.append({"token_id": int(token_id),
                               "token": tokenizer.decode([int(token_id)]),
                               "score": float(score),
                               "rank": int((scores > score).sum()) + 1})
        rows.append({"term": term, "tokens": sorted(candidates, key=lambda row: row["rank"])})
    return rows


@torch.inference_mode()
def _baseline(model: Any, tokenizer: Any, messages: list[dict[str, str]], layer: int):
    ids = steering.chat_ids(tokenizer, messages)
    blocks = steering.decoder_layers(model)
    captured: dict[str, torch.Tensor] = {}

    def source_hook(_module: Any, _inputs: Any, output: Any) -> Any:
        hidden = output[0] if isinstance(output, tuple) else output
        captured["source"] = hidden[:, -1].float().detach().clone()
        return output

    def final_hook(_module: Any, _inputs: Any, output: Any) -> Any:
        hidden = output[0] if isinstance(output, tuple) else output
        captured["final"] = hidden[:, -1].float().detach().clone()
        return output

    handles = [blocks[layer].register_forward_hook(source_hook),
               blocks[-1].register_forward_hook(final_hook)]
    try:
        output = model(torch.tensor([ids], device=next(model.parameters()).device), use_cache=False)
    finally:
        for handle in handles:
            handle.remove()
    return ids, captured["source"], captured["final"], output.logits[:, -1].float()


@torch.inference_mode()
def _actual(model: Any, ids: list[int], layer: int, direction: torch.Tensor,
            alpha: float) -> torch.Tensor:
    blocks = steering.decoder_layers(model)

    def hook(_module: Any, _inputs: Any, output: Any) -> Any:
        hidden = output[0] if isinstance(output, tuple) else output
        norm = hidden[:, -1].float().norm(dim=-1)
        hidden[:, -1] += (alpha * norm[:, None] *
                          direction.to(hidden.device, hidden.dtype))
        return output

    handle = blocks[layer].register_forward_hook(hook)
    try:
        output = model(torch.tensor([ids], device=next(model.parameters()).device), use_cache=False)
    finally:
        handle.remove()
    return output.logits[:, -1].float()


def run(args: argparse.Namespace) -> None:
    import jlens
    from jlens.hf import from_hf

    paths = args.vectors.split(",")
    names = args.names.split(",")
    if len(paths) != len(names):
        raise ValueError("--vectors and --names must have the same number of entries")
    terms = json.loads(Path(args.terms_json).read_text())
    lens = jlens.JacobianLens.load(args.lens)
    model, tokenizer = steering.load_target(args.target, args.device)
    lens_model = from_hf(model, tokenizer, force_bos=True)
    prompts = args.prompts.split("|")
    baselines = []
    for prompt in prompts:
        messages = [{"role": "user", "content": prompt}]
        baselines.append(_baseline(model, tokenizer, messages, args.layer))

    output = {"layer": args.layer, "alpha": args.alpha, "top_k": args.top_k,
              "n_lens_prompts": lens.n_prompts, "prompts": prompts, "vectors": {}}
    for name, path in zip(names, paths, strict=True):
        direction = vectors.load(path)
        transported = lens.transport(direction, args.layer).float()
        prompt_rows, predicted, actual = [], [], []
        for prompt, (ids, source, final, baseline_logits) in zip(prompts, baselines, strict=True):
            scale = args.alpha * source.norm()
            predicted_logits = lens_model.unembed(
                final + scale * transported.to(final.device)).float()
            actual_logits = _actual(model, ids, args.layer, direction, args.alpha)
            predicted_delta = (predicted_logits - baseline_logits).cpu()[0]
            actual_delta = (actual_logits - baseline_logits).cpu()[0]
            cosine = float(torch.nn.functional.cosine_similarity(
                predicted_delta[None], actual_delta[None]))
            overlap = len(set(predicted_delta.topk(args.top_k).indices.tolist()) &
                          set(actual_delta.topk(args.top_k).indices.tolist()))
            prompt_rows.append({"prompt": prompt, "cosine": cosine,
                                "top_k_overlap": overlap,
                                "predicted_norm": float(predicted_delta.norm()),
                                "actual_norm": float(actual_delta.norm())})
            predicted.append(predicted_delta)
            actual.append(actual_delta)
        mean_predicted = torch.stack(predicted).mean(0)
        mean_actual = torch.stack(actual).mean(0)
        output["vectors"][name] = {
            "path": path,
            "direction_norm": float(direction.norm()),
            "transported_norm": float(transported.norm()),
            "mean_cosine": sum(row["cosine"] for row in prompt_rows) / len(prompt_rows),
            "mean_top_k_overlap": sum(row["top_k_overlap"] for row in prompt_rows) / len(prompt_rows),
            "per_prompt": prompt_rows,
            "predicted_top_positive": _tokens(tokenizer, mean_predicted, args.report_tokens),
            "predicted_top_negative": _tokens(tokenizer, -mean_predicted, args.report_tokens),
            "actual_top_positive": _tokens(tokenizer, mean_actual, args.report_tokens),
            "target_term_predicted_ranks": _term_ranks(tokenizer, mean_predicted, terms[name]),
            "target_term_actual_ranks": _term_ranks(tokenizer, mean_actual, terms[name]),
        }
        _atomic_json(args.out, output)
    print(f"readout -> {args.out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--lens", required=True)
    parser.add_argument("--layer", required=True, type=int)
    parser.add_argument("--vectors", required=True, help="comma-separated vector paths")
    parser.add_argument("--names", required=True, help="comma-separated vector names")
    parser.add_argument("--terms-json", required=True, help="JSON object mapping names to target terms")
    parser.add_argument("--prompts", required=True, help="pipe-separated held-out prompts")
    parser.add_argument("--alpha", type=float, default=0.01)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--report-tokens", type=int, default=25)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--out", required=True)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
