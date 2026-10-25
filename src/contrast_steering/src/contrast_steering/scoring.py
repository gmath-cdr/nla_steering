from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Score:
    hit: float
    quality: float
    combined: float


def keyword_hit(text: str, terms: list[str]) -> float:
    lowered = text.casefold()
    return float(any(term.casefold() in lowered for term in terms))


def heuristic_quality(text: str, min_words: int = 8, max_words: int = 256) -> float:
    """Cheap, deterministic fluency guard; replaceable by precomputed quality scores."""
    words = re.findall(r"\w+", text.casefold())
    if not words:
        return 0.0
    length_score = min(1.0, len(words) / max(1, min_words))
    overflow = max(0, len(words) - max_words) / max(1, max_words)
    diversity = len(set(words)) / len(words)
    return max(0.0, min(1.0, length_score * min(1.0, diversity * 2.0) - overflow))


def score_text(text: str, terms: list[str], hit_weight: float, quality_weight: float) -> Score:
    hit = keyword_hit(text, terms)
    quality = heuristic_quality(text)
    return Score(hit, quality, hit_weight * hit + quality_weight * quality)


def choose_alpha(summaries: list[dict]) -> float:
    if not summaries:
        raise ValueError("no alpha summaries")
    # Prefer overall score, then quality, then the gentlest intervention.
    best = max(summaries, key=lambda x: (x["combined"], x["quality"], -abs(x["alpha"])))
    return float(best["alpha"])

