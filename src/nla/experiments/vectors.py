"""NLA and activation-derived steering vectors used by AxBench experiments."""

from __future__ import annotations

import torch
from pathlib import Path

from experiments.steering import normalize


def ar_delta(critic, positive: list[str], negative: list[str]) -> torch.Tensor:
    return normalize(torch.stack([critic.reconstruct(text) for text in positive]).mean(0) -
                     torch.stack([critic.reconstruct(text) for text in negative]).mean(0))


def concept_delta(critic, concept: str) -> torch.Tensor:
    return ar_delta(critic,
                    [f"This passage strongly concerns {concept}.",
                     f"The central topic is {concept}.",
                     f"The writer repeatedly emphasizes {concept}."],
                    ["This passage is generic and unrelated to any special concept.",
                     "The writing contains no particular persona or topic.",
                     "This is mundane content without the target concept."])


def diffmean(positive: list[torch.Tensor], negative: list[torch.Tensor]) -> torch.Tensor:
    return normalize(torch.stack(positive).mean(0) - torch.stack(negative).mean(0))


def pca(activations: list[torch.Tensor]) -> torch.Tensor:
    matrix = torch.stack(activations).float()
    _, _, vectors = torch.pca_lowrank(matrix - matrix.mean(0), q=1)
    return normalize(vectors[:, 0])


def random_like(vector: torch.Tensor, seed: int = 42) -> torch.Tensor:
    generator = torch.Generator().manual_seed(seed)
    return normalize(torch.randn(vector.numel(), generator=generator))


def save(vector: torch.Tensor, path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    torch.save(normalize(vector).cpu(), target)


def load(path: str) -> torch.Tensor:
    return normalize(torch.load(path, map_location="cpu", weights_only=True))
