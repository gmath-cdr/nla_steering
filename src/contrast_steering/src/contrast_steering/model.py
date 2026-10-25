from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


def resolve_layers(model):
    """Resolve the decoder block list for Qwen/Gemma and standard HF causal LMs."""
    candidates = (
        "language_model.model.layers", # multimodal Gemma 3
        "model.layers",          # Gemma, Qwen2/2.5/3, Llama
        "model.language_model.layers", # alternate multimodal wrapper
        "transformer.h",         # legacy Qwen and GPT-like models
        "transformer.layers",
        "gpt_neox.layers",
    )
    for path in candidates:
        obj = model
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            if len(obj):
                return obj, path
        except (AttributeError, TypeError):
            continue
    raise ValueError(f"unsupported model architecture: {type(model).__name__}")


def resolve_layer(model, layer: int, one_based: bool = True):
    layers, path = resolve_layers(model)
    index = layer - 1 if one_based else layer
    if index < 0 or index >= len(layers):
        basis = "one-based" if one_based else "zero-based"
        raise ValueError(f"layer {layer} ({basis}) is outside model with {len(layers)} layers")
    return layers[index], index, f"{path}.{index}"


def hidden_from_output(output):
    return output[0] if isinstance(output, tuple) else output


def replace_hidden(output, hidden):
    return (hidden, *output[1:]) if isinstance(output, tuple) else hidden


@contextmanager
def capture_last_token(module, sink: list):
    def hook(_module, _inputs, output):
        hidden = hidden_from_output(output)
        sink.append(hidden[:, -1, :].detach().float().cpu())
    handle = module.register_forward_hook(hook)
    try:
        yield
    finally:
        handle.remove()


@contextmanager
def add_vector(module, vector, alpha: float) -> Iterator[None]:
    def hook(_module, _inputs, output):
        hidden = hidden_from_output(output)
        delta = vector.to(device=hidden.device, dtype=hidden.dtype)
        return replace_hidden(output, hidden + alpha * delta.view(1, 1, -1))
    handle = module.register_forward_hook(hook)
    try:
        yield
    finally:
        handle.remove()

