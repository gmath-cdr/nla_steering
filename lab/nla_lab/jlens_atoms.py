"""Pure helpers for constructing and hashing J-lens token atoms."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np


ATOM_FORMULA_ID = "j-lens-atom-effective-final-norm/v2"
TENSOR_HASH_SCHEMA = "steering-tensor-f32-le/v1"


class JLensAtomError(ValueError):
    """Raised when a purported pure token atom is ambiguous or mismatched."""


def effective_scale_from_norm_response(response: Any, hidden_width: int) -> np.ndarray:
    """Validate ``final_norm(ones)`` as the architecture-safe scale vector.

    Qwen RMSNorm multiplies by ``weight`` while Gemma RMSNorm multiplies by
    ``1 + weight``. Calling the actual module on an all-ones vector captures
    either convention. Its near-one global RMS scalar is irrelevant after atom
    unit normalization but its per-coordinate ratios are decisive.
    """
    scale = np.asarray(response, dtype=np.float32).reshape(-1)
    if scale.size != hidden_width:
        raise JLensAtomError(
            f"final norm response has {scale.size} values, expected {hidden_width}"
        )
    if not np.isfinite(scale).all() or np.linalg.norm(scale) <= 1e-12:
        raise JLensAtomError("final norm response is non-finite or zero")
    return scale


def atom_from_readout(
    jacobian: Any,
    effective_scale: Any,
    unembedding_row: Any,
) -> np.ndarray:
    """Return ``J.T @ (effective_scale * W_U[token])`` in float32."""
    J = np.asarray(jacobian, dtype=np.float32)
    scale = np.asarray(effective_scale, dtype=np.float32).reshape(-1)
    row = np.asarray(unembedding_row, dtype=np.float32).reshape(-1)
    if J.ndim != 2 or J.shape[0] != J.shape[1]:
        raise JLensAtomError(f"J must be square, got {J.shape}")
    if scale.size != J.shape[0] or row.size != J.shape[0]:
        raise JLensAtomError(
            f"shape mismatch J={J.shape}, scale={scale.shape}, row={row.shape}"
        )
    atom = J.T @ (scale * row)
    if not np.isfinite(atom).all() or np.linalg.norm(atom) <= 1e-12:
        raise JLensAtomError("constructed atom is non-finite or zero")
    return atom.astype(np.float32, copy=False)


def tensor_f32_sha256(value: Any) -> str:
    tensor = np.asarray(value, dtype="<f4")
    digest = hashlib.sha256()
    digest.update((TENSOR_HASH_SCHEMA + "\0").encode())
    digest.update(json.dumps(list(tensor.shape), separators=(",", ":")).encode())
    digest.update(b"\0")
    digest.update(tensor.tobytes(order="C"))
    return digest.hexdigest()


def unit(value: Any) -> np.ndarray:
    array = np.asarray(value, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(array))
    if not np.isfinite(norm) or norm <= 1e-12:
        raise JLensAtomError("cannot normalize a non-finite or zero atom")
    return array / norm


def resolve_single_token(tokenizer: Any, surface: str, variants: list[str]) -> dict[str, Any]:
    """Resolve an explicitly ordered bare/space variant; fail on multi-token."""
    resolutions: dict[str, dict[str, Any]] = {}
    for variant in variants:
        if variant not in {"bare", "space"}:
            raise JLensAtomError(f"unsupported token surface variant {variant!r}")
        rendered = surface if variant == "bare" else " " + surface
        ids = list(tokenizer.encode(rendered, add_special_tokens=False))
        resolutions[variant] = {
            "surface": rendered,
            "ids": [int(value) for value in ids],
            "single": len(ids) == 1,
        }
    for variant in variants:
        entry = resolutions[variant]
        if entry["single"]:
            token_id = entry["ids"][0]
            decoded = tokenizer.decode([token_id])
            return {
                "chosen_variant": variant,
                "token_id": token_id,
                "decoded": decoded,
                "resolutions": resolutions,
            }
    raise JLensAtomError(
        f"{surface!r} is not a single token under requested variants {variants}"
    )
