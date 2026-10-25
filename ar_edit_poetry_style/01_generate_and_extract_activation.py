"""
Phase 1 of the AR-edit steering test (mirrors the paper's "Planning in
Poetry" case study, applied to eval-awareness instead of rhyme).

Generates one real (unsteered) response, picks one response-token
position, and saves that token's layer-20 activation plus enough
metadata to later patch and regenerate from that exact point.

Run this WHILE SGLANG IS STOPPED -- it loads the full base
Qwen2.5-7B-Instruct model directly.

Usage:
    python3 ar_edit_phase1_extract.py

Output:
    ar_edit_target.npz   (activation vector + full_input_ids as arrays)
    ar_edit_target.json  (text metadata: prompt, response, position, etc.)
"""

from __future__ import annotations

import json

import numpy as np
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"
NLA_LAYER = 20
MAX_NEW_TOKENS = 150

# Same derivative prompt used throughout our eval-awareness experiments.
TARGET_PROMPT = (
    "What is the derivative of f(x) = x^3 - 4x^2 + 2x with respect to x?"
)

# Which response token (0-indexed into the generated tokens, not the
# full sequence) to target. Early-ish so there's some context but we're
# not deep into a settled answer -- analogous to the newline token right
# after "grab it" in the poem, a point where the model might still be
# "deciding" something about how to proceed.
TARGET_RESPONSE_POSITION = 3


def get_model_device(model: torch.nn.Module) -> torch.device:
    return model.get_input_embeddings().weight.device


def format_chat(tokenizer: AutoTokenizer, user_text: str) -> str:
    return tokenizer.apply_chat_template(
        [{"role": "user", "content": user_text}],
        tokenize=False,
        add_generation_prompt=True,
    )


def main() -> None:
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME, dtype=torch.bfloat16, device_map="auto"
    )
    model.eval()
    print(f"Loaded {MODEL_NAME}")

    formatted = format_chat(tokenizer, TARGET_PROMPT)
    inputs = tokenizer(formatted, return_tensors="pt").to(get_model_device(model))
    prompt_len = inputs["input_ids"].shape[1]

    with torch.inference_mode():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=True,
            temperature=0.8,
            top_p=0.9,
            pad_token_id=tokenizer.eos_token_id,
        )

    full_input_ids = output_ids[0]
    generated_ids = full_input_ids[prompt_len:]
    response_text = tokenizer.decode(generated_ids, skip_special_tokens=True)

    if TARGET_RESPONSE_POSITION >= generated_ids.shape[0]:
        raise ValueError(
            f"Response only has {generated_ids.shape[0]} tokens, can't "
            f"target position {TARGET_RESPONSE_POSITION}."
        )

    target_seq_position = prompt_len + TARGET_RESPONSE_POSITION
    target_token_text = tokenizer.decode(
        [full_input_ids[target_seq_position].item()]
    )

    print(f"Prompt: {TARGET_PROMPT}")
    print(f"Full response: {response_text}")
    print(
        f"Target response position: {TARGET_RESPONSE_POSITION} "
        f"(sequence position {target_seq_position}) | "
        f"token: {target_token_text!r}"
    )

    # Extract layer-20 activation AT the target position, using only the
    # prefix up through that position (so this matches exactly what we'll
    # patch during phase 3's prefill).
    prefix_ids = full_input_ids[: target_seq_position + 1].unsqueeze(0).to(
        get_model_device(model)
    )
    with torch.inference_mode():
        outputs = model(
            input_ids=prefix_ids,
            output_hidden_states=True,
            use_cache=False,
            return_dict=True,
        )
    target_activation = (
        outputs.hidden_states[NLA_LAYER][0, -1, :].detach().float().cpu().numpy()
    )
    print(f"Target activation norm: {np.linalg.norm(target_activation):.2f}")

    np.savez(
        "ar_edit_target.npz",
        target_activation=target_activation,
        full_input_ids=full_input_ids.cpu().numpy(),
    )

    metadata = {
        "prompt": TARGET_PROMPT,
        "response_text": response_text,
        "prompt_len": int(prompt_len),
        "target_response_position": int(TARGET_RESPONSE_POSITION),
        "target_seq_position": int(target_seq_position),
        "target_token_text": target_token_text,
        "nla_layer": NLA_LAYER,
    }
    with open("ar_edit_target.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    print("\nWrote ar_edit_target.npz and ar_edit_target.json")
    print("Next: stop this process, start SGLang, run ar_edit_phase2_decode.py")


if __name__ == "__main__":
    main()