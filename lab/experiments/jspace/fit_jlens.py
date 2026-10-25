"""Fit the averaged Jacobian J_20 for Qwen2.5-7B-Instruct. Pod, ~1.5-2.5 h.

    /venv_jlens/bin/python -u experiments/jspace/fit_jlens.py

Runs Anthropic's reference implementation (jlens, pip-installed from
external/jacobian-lens into /venv_jlens; needs transformers>=5.5, hence
the separate venv from the NLA stack). Fits the lens in two halves of
data/fit_prompts.json, merges, then derives J-lens token vectors:

    v_t = J_20^T (effective_final_norm_scale * W_U[t])

where W_U[t] is token t's unembedding row and the scale is recovered by
evaluating the actual final RMSNorm module on an all-ones vector. This handles
both raw-weight (Qwen) and one-plus-weight (Gemma) conventions, up to the
positive per-position RMS scalar that never changes token ranking.

Saves to data/: lens_L20{,_A,_B}.pt, jlens_tokens.npz (curated vectors,
fp32), jlens_vocab_norms.npy (all-vocab |v_t|), jlens_fit_report.json.
Checkpoints under CKPT_DIR after every prompt; safe to kill and rerun.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import torch

from jlens.fitting import fit
from jlens.hf import from_hf
from jlens.lens import JacobianLens

# JLENS_MODEL overrides (e.g. a copy on fast local disk; the network
# volume's mfs can stall shard-by-shard weight loading for 30+ minutes).
# JLENS_LAYER selects the source layer (20 = Qwen NLA site, 32 = Gemma's).
_LOCAL_MODEL = Path("/workspace/models/qwen2.5-7b-instruct")
MODEL_ID = os.environ.get("JLENS_MODEL") or (
    str(_LOCAL_MODEL) if _LOCAL_MODEL.exists() else "Qwen/Qwen2.5-7B-Instruct")
LAYER = int(os.environ.get("JLENS_LAYER", "20"))
DIM_BATCH = int(os.environ.get("JLENS_DIM_BATCH", "16"))
CKPT_DIR = Path(os.environ.get("JLENS_CKPT_DIR", "/workspace/jlens"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S")


def cos(a: torch.Tensor, b: torch.Tensor) -> float:
    return float(torch.nn.functional.cosine_similarity(
        a.flatten().float(), b.flatten().float(), dim=0))


def resolve_tokens(tokenizer, words: list[str]) -> dict[str, dict]:
    """Map each word to its single-token id(s): bare and space-prefixed."""
    table: dict[str, dict] = {}
    for word in words:
        entry = {}
        for label, variant in (("bare", word), ("space", " " + word)):
            ids = tokenizer.encode(variant, add_special_tokens=False)
            entry[label] = {"ids": ids, "single": len(ids) == 1}
        table[word] = entry
    return table


def main() -> None:
    here = Path(__file__).resolve().parent
    data_dir = here / "data"
    CKPT_DIR.mkdir(parents=True, exist_ok=True)

    prompts = [p["text"] for p in json.loads(
        (data_dir / "fit_prompts.json").read_text(encoding="utf-8"))["prompts"]]
    token_groups = json.loads(
        (data_dir / "tokens_of_interest.json").read_text(encoding="utf-8"))
    half = len(prompts) // 2

    from transformers import AutoModelForCausalLM, AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID, torch_dtype=torch.bfloat16, device_map="cuda")
    lm = from_hf(model, tokenizer)
    assert lm.n_layers > LAYER, repr(lm)
    print(f"model: {lm!r}, fitting layer {LAYER}")

    t0 = time.time()
    lens_a = fit(lm, prompts[:half], source_layers=[LAYER],
                 dim_batch=DIM_BATCH,
                 checkpoint_path=str(CKPT_DIR / "ckpt_A.pt"))
    lens_b = fit(lm, prompts[half:], source_layers=[LAYER],
                 dim_batch=DIM_BATCH,
                 checkpoint_path=str(CKPT_DIR / "ckpt_B.pt"))
    fit_minutes = (time.time() - t0) / 60
    merged = JacobianLens.merge([lens_a, lens_b])
    lens_a.save(str(data_dir / f"lens_L{LAYER}_A.pt"))
    lens_b.save(str(data_dir / f"lens_L{LAYER}_B.pt"))
    merged.save(str(data_dir / f"lens_L{LAYER}.pt"))

    split_half_matrix_cos = cos(lens_a.jacobians[LAYER], lens_b.jacobians[LAYER])

    # Token vectors: rows of (effective_scale * W_U) @ J are v_t^T.
    # Do not read ``final_norm.weight`` directly: Qwen applies ``weight`` but
    # Gemma/Gemma3 applies ``1 + weight``. Evaluating the actual norm module on
    # ones captures both conventions; the near-one global RMS scalar is
    # irrelevant to token ranking and unit-normalized atoms.
    # layout-resolved by the jlens wrapper: works for Qwen's single tower,
    # Gemma's multimodal wrapper, and tied-embedding unembeddings alike
    W_U = lm._lm_head.weight.detach().float().cuda()            # [vocab, d]
    with torch.no_grad():
        effective_scale = lm._final_norm(
            torch.ones((1, W_U.shape[1]), device="cuda", dtype=torch.float32)
        ).detach().float().reshape(-1)
    def token_matrix(lens: JacobianLens) -> torch.Tensor:
        return (W_U * effective_scale) @ lens.jacobians[LAYER].cuda().float()
    V = token_matrix(merged)
    vocab_norms = V.norm(dim=1).cpu().numpy().astype(np.float32)
    np.save(data_dir / "jlens_vocab_norms.npy", vocab_norms)

    words = [w for group in token_groups.values() for w in group]
    resolution = resolve_tokens(tokenizer, words)
    V_a, V_b = token_matrix(lens_a), token_matrix(lens_b)
    curated: dict[str, np.ndarray] = {}
    per_token: dict[str, dict] = {}
    for word, entry in resolution.items():
        for label in ("space", "bare"):  # space-prefixed is the common form
            if entry[label]["single"]:
                tid = entry[label]["ids"][0]
                curated[f"{word}__{label}"] = V[tid].cpu().numpy().astype(np.float32)
                per_token[f"{word}__{label}"] = {
                    "token_id": tid,
                    "norm": float(V[tid].norm()),
                    "split_half_cos": cos(V_a[tid], V_b[tid]),
                }
    np.savez_compressed(data_dir / "jlens_tokens.npz", **curated)

    token_cosines = sorted(d["split_half_cos"] for d in per_token.values())
    report = {
        "config": {"model": MODEL_ID, "layer": LAYER, "dim_batch": DIM_BATCH,
                   "n_prompts": len(prompts), "half": half,
                   "token_atom_formula": "j-lens-atom-effective-final-norm/v2",
                   "effective_norm_class": type(lm._final_norm).__name__,
                   "estimator": "jlens.fitting.fit defaults "
                                "(max_seq_len=128, skip_first=16)"},
        "fit_minutes": round(fit_minutes, 1),
        "gate1": {
            "split_half_matrix_cos": split_half_matrix_cos,
            "curated_token_cos_min_median_max": [
                token_cosines[0], token_cosines[len(token_cosines) // 2],
                token_cosines[-1]],
            "threshold": 0.90,
            "pass": split_half_matrix_cos >= 0.90
                    and token_cosines[len(token_cosines) // 2] >= 0.90,
        },
        "token_resolution": {w: {k: v["ids"] for k, v in e.items()}
                             for w, e in resolution.items()},
        "per_token": per_token,
    }
    (data_dir / "jlens_fit_report.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nfit {len(prompts)} prompts in {fit_minutes:.0f} min")
    print(f"GATE 1 split-half: matrix cos = {split_half_matrix_cos:.4f}, "
          f"curated token cos median = "
          f"{token_cosines[len(token_cosines) // 2]:.4f} "
          f"-> {'PASS' if report['gate1']['pass'] else 'FAIL'}")


if __name__ == "__main__":
    main()
