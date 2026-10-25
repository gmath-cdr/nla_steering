"""Optional Jacobian-lens fit, geometry, and projection ablation utilities."""

from __future__ import annotations

import torch

from experiments.steering import normalize


def fit(model, tokenizer, prompts: list[str], layer: int, output: str,
        dim_batch: int = 16, max_seq_len: int = 64):
    """Fit a lens. Requires separately installing ``jacobian-lens``."""
    import jlens
    from jlens.hf import from_hf

    lens = jlens.fit(from_hf(model, tokenizer, force_bos=True), prompts=prompts,
                     source_layers=[layer], dim_batch=dim_batch, max_seq_len=max_seq_len,
                     checkpoint_path=output + ".fit-ckpt")
    lens.save(output)
    return lens


def basis(lens, layer: int, k: int = 32) -> torch.Tensor:
    _, _, right = torch.linalg.svd(lens.jacobians[layer].double(), full_matrices=False)
    return right[:k].float()


def project(vector: torch.Tensor, vectors: torch.Tensor) -> torch.Tensor:
    return vectors.t() @ (vectors @ vector)


def geometry(lens, layer: int, vectors: dict[str, torch.Tensor], k: int = 32) -> dict:
    directions = basis(lens, layer, k)
    jacobian = lens.jacobians[layer].float()
    output = {}
    for name, vector in vectors.items():
        vector = normalize(vector)
        projection = project(vector, directions)
        output[name] = {"projection_fraction": float(projection.norm()),
                        "jacobian_alignment": float((jacobian @ vector).norm() / jacobian.norm()),
                        "top_cosines": [float(vector @ direction) for direction in directions[:5]]}
    return output
