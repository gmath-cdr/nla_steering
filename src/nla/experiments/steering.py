"""Target-model residual capture and intervention helpers for NLA experiments.

This module intentionally uses the released ``NLAClient`` and ``NLACritic``
instead of reimplementing AV/AR loading. It is inference-only: the training
package under ``nla/`` remains unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


def load_target(model_id: str, device: str = "cuda:0") -> tuple[Any, Any]:
    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_id, torch_dtype=torch.bfloat16, trust_remote_code=True,
        low_cpu_mem_usage=True, device_map={"": device},
    ).eval()
    return model, tokenizer


def decoder_layers(model: Any):
    """Return the text decoder blocks for Qwen, Llama, and Gemma wrappers."""
    queue = [model]
    seen = set()
    while queue:
        candidate = queue.pop(0)
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        layers = getattr(candidate, "layers", None)
        if layers is not None:
            return layers
        for attr in ("model", "language_model", "text_model", "transformer"):
            child = getattr(candidate, attr, None)
            if child is not None:
                queue.append(child)
    raise ValueError(f"could not find decoder layers on {type(model).__name__}")


def chat_ids(tokenizer: Any, messages: list[dict[str, str]]) -> list[int]:
    encoded = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    ids = getattr(encoded, "ids", None)
    if ids is None:
        ids = getattr(encoded, "input_ids", encoded)
    return list(ids)


def fixed_position(index: int) -> Callable[[torch.Tensor, int], torch.Tensor]:
    def resolve(hidden: torch.Tensor, _prompt_len: int) -> torch.Tensor:
        mask = torch.zeros(hidden.shape[1], dtype=torch.bool, device=hidden.device)
        position = index if index >= 0 else hidden.shape[1] + index
        if 0 <= position < hidden.shape[1]:
            mask[position] = True
        return mask
    return resolve


def char_position(tokenizer: Any, rendered: str, char_index: int) -> int:
    if not 0 <= char_index < len(rendered):
        raise ValueError("character index is outside prompt")
    return len(tokenizer(rendered[:char_index + 1], add_special_tokens=False).input_ids) - 1


@torch.inference_mode()
def capture_residual(model: Any, tokenizer: Any, messages: list[dict[str, str]],
                     layer: int, resolver: Callable[[torch.Tensor, int], torch.Tensor]) -> torch.Tensor:
    ids = chat_ids(tokenizer, messages)
    blocks = decoder_layers(model)
    captured: list[torch.Tensor] = []

    def hook(_module: Any, _inputs: Any, output: Any) -> Any:
        hidden = output[0] if isinstance(output, tuple) else output
        mask = resolver(hidden, len(ids))
        if int(mask.sum()) != 1:
            raise ValueError("capture requires exactly one target token")
        captured.append(hidden[0, mask][0].float().cpu().clone())
        return output

    handle = blocks[layer].register_forward_hook(hook)
    try:
        model(torch.tensor([ids], device=next(model.parameters()).device), use_cache=False)
    finally:
        handle.remove()
    if len(captured) != 1:
        raise RuntimeError("target residual was not captured")
    return captured[0]


def normalize(vector: torch.Tensor) -> torch.Tensor:
    return vector.float() / vector.float().norm().clamp_min(1e-12)


def av_edit_vector(actor: Any, critic: Any, activation: torch.Tensor, edit: Callable[[str], str],
                   rollouts: int = 5, return_details: bool = False):
    """Native AV → edited explanation → AR delta, averaged over rollouts."""
    deltas, details = [], []
    for _ in range(rollouts):
        raw = actor.generate(activation, extract_explanation=False, temperature=1.0,
                             max_new_tokens=200)
        original = raw if "<explanation>" in raw else f"<explanation>{raw}</explanation>"
        changed = edit(original)
        if changed == original:
            raise ValueError("edit text was absent from AV explanation; refusing a zero vector")
        delta = critic.reconstruct(changed) - critic.reconstruct(original)
        deltas.append(normalize(delta))
        details.append({"original_explanation": original, "edited_explanation": changed,
                        "delta_norm": float(delta.norm())})
    vector = normalize(sum(deltas, torch.zeros_like(deltas[0])))
    if return_details:
        return vector, {"activation_norm": float(activation.norm()), "rollouts": details}
    return vector


@torch.inference_mode()
def generate(model: Any, tokenizer: Any, messages: list[dict[str, str]], layer: int,
             vector: torch.Tensor | None, position: int | Callable[[torch.Tensor, int], torch.Tensor], alpha: float,
             n_samples: int = 1, max_new_tokens: int = 96, temperature: float = 0.7,
             energy_matched: bool = False) -> list[str]:
    """Generate batched samples while adding a norm-scaled vector at one token."""
    ids = chat_ids(tokenizer, messages)
    if vector is None:
        specs = None
    else:
        specs = (normalize(vector), position if callable(position) else fixed_position(position))
    blocks = decoder_layers(model)

    def hook(_module: Any, _inputs: Any, output: Any) -> Any:
        if specs is None:
            return output
        hidden = output[0] if isinstance(output, tuple) else output
        direction, resolver = specs
        mask = resolver(hidden, len(ids))
        if not int(mask.sum()):
            return output
        norm = hidden[:, -1].float().norm(dim=-1)
        coefficient = alpha
        if energy_matched:
            # Prompt sections can span several tokens, while decode-time
            # assistant steering is applied once per generated step. Match a
            # planned total squared intervention budget: divide prompt edits by
            # sqrt(selected positions), and persistent decode edits by the
            # fixed generation horizon. Shorter generations are conservative.
            budget = max_new_tokens if hidden.shape[1] < len(ids) else int(mask.sum())
            coefficient /= budget ** 0.5
        hidden[:, mask] += (coefficient * norm[:, None, None] *
                            direction.to(hidden.device, hidden.dtype))
        return output

    handle = blocks[layer].register_forward_hook(hook) if specs else None
    try:
        input_ids = torch.tensor([ids] * n_samples, device=next(model.parameters()).device)
        output = model.generate(input_ids, max_new_tokens=max_new_tokens,
                                do_sample=temperature > 0, temperature=temperature,
                                pad_token_id=tokenizer.eos_token_id)
    finally:
        if handle is not None:
            handle.remove()
    return [tokenizer.decode(row[len(ids):], skip_special_tokens=True).strip() for row in output]
