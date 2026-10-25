from __future__ import annotations

import argparse
import json
from pathlib import Path

from .data import read_jsonl, validate_eval, validate_pairs


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extract and evaluate CAA vectors, with optional held-out calibration")
    p.add_argument("--model", required=True)
    p.add_argument("--pairs", required=True, help="JSONL with positive and neutral fields")
    p.add_argument("--calibration", help="held-out JSONL; enables calibrated CAA")
    p.add_argument("--test", help="optional held-out JSONL with prompt and target_terms fields")
    p.add_argument("--alpha", type=float, help="fixed alpha for plain CAA (required without --calibration)")
    p.add_argument("--layer", required=True, type=int)
    p.add_argument("--zero-based", action="store_true")
    p.add_argument("--alphas", nargs="+", type=float, default=[-2, -1, -0.5, 0, 0.5, 1, 2])
    p.add_argument("--output", default="outputs/run")
    p.add_argument("--max-length", type=int, default=2048)
    p.add_argument("--max-new-tokens", type=int, default=128)
    p.add_argument("--hit-weight", type=float, default=1.0)
    p.add_argument("--quality-weight", type=float, default=0.25)
    p.add_argument("--no-normalize", action="store_true")
    p.add_argument("--revision", default="main")
    p.add_argument("--token", default=None, help="HF token; prefer HF_TOKEN environment variable")
    return p


def main(argv=None) -> None:
    args = parser().parse_args(argv)
    import torch
    from safetensors.torch import save_file
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
    from .pipeline import evaluate_alphas, extract_vector, save_manifest

    if not torch.cuda.is_available():
        raise SystemExit("CUDA GPU required for the target models")
    if (args.calibration is None) == (args.alpha is None):
        raise SystemExit("choose exactly one: --calibration for calibrated CAA, or --alpha for plain CAA")
    pairs = read_jsonl(args.pairs)
    calibration = read_jsonl(args.calibration) if args.calibration else None
    validate_pairs(pairs)
    if calibration: validate_eval(calibration)
    test = read_jsonl(args.test) if args.test else None
    if test: validate_eval(test)
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model, revision=args.revision, token=args.token,
                                               trust_remote_code=True)
    load_kwargs = dict(revision=args.revision, token=args.token, torch_dtype=torch.bfloat16,
                       device_map={"": 0}, low_cpu_mem_usage=True, trust_remote_code=True,
                       attn_implementation="sdpa")
    config = AutoConfig.from_pretrained(args.model, revision=args.revision, token=args.token,
                                        trust_remote_code=True)
    if getattr(config, "model_type", "") == "gemma3":
        from transformers import Gemma3ForConditionalGeneration
        model = Gemma3ForConditionalGeneration.from_pretrained(args.model, **load_kwargs)
    else:
        model = AutoModelForCausalLM.from_pretrained(args.model, **load_kwargs)
    model.eval()
    vector, metadata = extract_vector(model, tokenizer, pairs, args.layer, not args.zero_based,
                                      args.max_length, not args.no_normalize)
    save_file({"steering_vector": vector.contiguous()}, out / "vector.safetensors")
    if calibration:
        chosen, calibration_summary = evaluate_alphas(
            model, tokenizer, vector, calibration, args.layer, not args.zero_based, args.alphas,
            args.max_length, args.max_new_tokens, args.hit_weight, args.quality_weight,
            out / "calibration_generations.jsonl")
    else:
        chosen, calibration_summary = float(args.alpha), None
    manifest = {**metadata, "model": args.model, "revision": args.revision,
                "alphas": args.alphas if calibration else [chosen], "chosen_alpha": chosen,
                "weights": {"hit": args.hit_weight, "quality": args.quality_weight},
                "mode": "calibrated_caa" if calibration else "caa",
                "calibration": calibration_summary}
    if test:
        _, test_summary = evaluate_alphas(
            model, tokenizer, vector, test, args.layer, not args.zero_based, [chosen],
            args.max_length, args.max_new_tokens, args.hit_weight, args.quality_weight,
            out / "test_generations.jsonl")
        manifest["test"] = test_summary[0]
    save_manifest(out / "manifest.json", manifest)
    print(json.dumps({"chosen_alpha": chosen, "output": str(out)}, indent=2))


if __name__ == "__main__":
    main()

