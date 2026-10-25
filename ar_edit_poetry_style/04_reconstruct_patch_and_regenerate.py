"""
Phase 3 of the AR-edit steering test: the actual causal intervention,
mirroring the paper's poetry case study exactly:

  h_tilde_orig = AR(original_explanation)
  h_tilde_edit = AR(edited_explanation)
  delta = h_tilde_edit - h_tilde_orig
  h_new = h_orig + alpha * ||h_orig|| * (delta / ||delta||)

Patches h_new into the residual stream at the target token position and
layer only (a single forward pass during the prefill), then continues
generation from there, sampling multiple times per alpha, to see whether
completions shift toward acknowledging a test/evaluation context.

Run this WHILE SGLANG IS STOPPED -- it loads both the AR checkpoint's
truncated backbone and the full base Qwen2.5-7B-Instruct model.

Requires ar_edit_edited_explanation.txt to already exist (hand-write
this yourself, based on ar_edit_original_explanation.txt from phase 2 --
inject eval-awareness framing the same way the paper swapped
rabbit->mouse).

Usage:
    python3 04_reconstruct_patch_and_regenerate.py \
        --ar-checkpoint ~/NLASteering/models/nla-qwen2.5-7b-L20-ar
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from nla_inference import NLACritic


MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"
ALPHAS = [0.0, 0.3, 0.6, 1.0, 1.5, 2.0]
NUM_SAMPLES_PER_ALPHA = 4
MAX_NEW_TOKENS = 100


def get_model_device(model: torch.nn.Module) -> torch.device:
    return model.get_input_embeddings().weight.device


def make_single_token_patch_hook(delta_direction: torch.Tensor, alpha: float):
    """
    Patches ONLY the last position of a multi-token forward call (the
    prefill step, i.e. the target token itself), leaving every later
    single-token decode step untouched. This matches "steer at the
    newline token only" from the paper -- a one-time patch, not
    persistent steering across the whole generation.
    """

    def hook(module, inputs, output):
        if isinstance(output, tuple):
            hidden_states = output[0]
            rest = output[1:]
        else:
            hidden_states = output
            rest = None

        if hidden_states.shape[1] > 1:
            # Prefill step: hidden_states[:, -1, :] is our target token.
            h_orig = hidden_states[:, -1, :]
            h_orig_norm = h_orig.float().norm(dim=-1, keepdim=True)
            direction = delta_direction.to(
                device=hidden_states.device, dtype=hidden_states.dtype
            )
            addition = (
                alpha * h_orig_norm.to(hidden_states.dtype) * direction
            )
            hidden_states = hidden_states.clone()
            hidden_states[:, -1, :] = hidden_states[:, -1, :] + addition

        if rest is not None:
            return (hidden_states, *rest)
        return hidden_states

    return hook


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ar-checkpoint", required=True)
    ap.add_argument("--original", default="ar_edit_original_explanation.txt")
    ap.add_argument("--edited", default="ar_edit_edited_explanation.txt")
    args = ap.parse_args()

    with open(args.original, encoding="utf-8") as f:
        original_explanation = f.read().strip()
    with open(args.edited, encoding="utf-8") as f:
        edited_explanation = f.read().strip()

    print("Original explanation:")
    print(f"  {original_explanation[:200]}...")
    print("Edited explanation:")
    print(f"  {edited_explanation[:200]}...")

    print(f"\nLoading AR (critic) from {args.ar_checkpoint} ...")
    critic = NLACritic(args.ar_checkpoint, device="cuda")

    h_tilde_orig = critic.reconstruct(original_explanation)
    h_tilde_edit = critic.reconstruct(edited_explanation)
    delta = h_tilde_edit - h_tilde_orig
    delta_norm = delta.norm().item()
    print(f"Delta norm: {delta_norm:.3f}")
    if delta_norm < 1e-6:
        raise ValueError(
            "Delta is ~zero -- the edited explanation may not differ "
            "meaningfully from the original in a way the AR can capture. "
            "Try a more substantial edit."
        )
    delta_direction = (delta / delta_norm).float()

    with open("ar_edit_target.json", encoding="utf-8") as f:
        metadata = json.load(f)
    data = np.load("ar_edit_target.npz")
    full_input_ids = torch.tensor(data["full_input_ids"], dtype=torch.long)
    target_seq_position = metadata["target_seq_position"]
    nla_layer = metadata["nla_layer"]

    print(f"\nLoading base model {MODEL_NAME} ...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME, dtype=torch.bfloat16, device_map="auto"
    )
    model.eval()

    prefix_ids = full_input_ids[: target_seq_position + 1].unsqueeze(0).to(
        get_model_device(model)
    )

    # hidden_states[nla_layer] is the output of model.model.layers[nla_layer - 1]
    # (hidden_states[0] is the embedding output, so hidden_states[k] is the
    # output after k transformer blocks).
    target_layer_module = model.model.layers[nla_layer - 1]

    print(f"\nPrompt: {metadata['prompt']}")
    print(f"Original response: {metadata['response_text']}")
    print(
        f"Target token: {metadata['target_token_text']!r} at response "
        f"position {metadata['target_response_position']}"
    )

    for alpha in ALPHAS:
        print(f"\n{'=' * 80}\nALPHA {alpha:+.2f}\n{'=' * 80}")

        handle = target_layer_module.register_forward_hook(
            make_single_token_patch_hook(delta_direction, alpha)
        )
        try:
            for sample_idx in range(NUM_SAMPLES_PER_ALPHA):
                with torch.inference_mode():
                    output_ids = model.generate(
                        input_ids=prefix_ids,
                        max_new_tokens=MAX_NEW_TOKENS,
                        do_sample=True,
                        temperature=0.8,
                        top_p=0.9,
                        pad_token_id=tokenizer.eos_token_id,
                    )
                continuation = tokenizer.decode(
                    output_ids[0, prefix_ids.shape[1]:], skip_special_tokens=True
                )
                print(f"  sample {sample_idx}: {continuation[:300]!r}")
        finally:
            handle.remove()

    print(
        "\nCompare completions across alpha values to the alpha=0.00 "
        "baseline (which should match the original unpatched "
        "continuation). Look for any shift toward acknowledging a "
        "test/evaluation/benchmark context, the same way the paper "
        "looked for a shift from 'rabbit' to 'mouse' completions."
    )


if __name__ == "__main__":
    main()