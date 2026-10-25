"""Rebuild provenance-sensitive Gemma core figures from immutable results.

Run from ``lab/``::

    python analysis/gemma_core_figures.py

This source file replaces the earlier one-off chart assembly.  It deliberately
labels root ancestry, the greedy-NNLS k25 derivation, and independent unit-L2
views instead of the historical "sayable/leftover" shorthand.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import fmean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


LAB = Path(__file__).resolve().parents[1]
RESULTS = LAB / "results"
OUT = LAB / "analysis" / "figures" / "gemma_core"

SERIES = {
    "g-freeform": "gcore-g-freeform_*.json",
    "comp-g-freeform": "gcore-comp25-g-freeform_*.json",
    "rem-g-freeform": "gcore-resid25-g-freeform_*.json",
    "g-phrase-glyph": "gcore-g-phrase-glyph_*.json",
    "comp-g-phrase-glyph": "gcore-comp25-g-phrase-glyph_*.json",
    "rem-g-phrase-glyph": "gcore-resid25-g-phrase-glyph_*.json",
    "random": "gcore-random-chat_*.json",
}


def newest(pattern: str) -> Path:
    matches = sorted(RESULTS.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"no result matches {pattern}")
    return matches[-1]


def emoji_curve(pattern: str) -> tuple[list[float], list[float]]:
    data = json.loads(newest(pattern).read_text(encoding="utf-8"))
    by_alpha: dict[float, list[float]] = {}
    for record in data["records"]:
        by_alpha.setdefault(float(record["alpha"]), []).append(
            float(record["probes"]["emojis"])
        )
    xs = sorted(by_alpha)
    return xs, [fmean(by_alpha[alpha]) for alpha in xs]


def build_split() -> Path:
    curves = {key: emoji_curve(pattern) for key, pattern in SERIES.items()}
    colors = {
        "parent": "#2a78d6",
        "component": "#1baf7a",
        "remainder": "#898781",
        "random": "#eb6834",
    }
    panels = [
        (
            "g-freeform",
            "comp-g-freeform",
            "rem-g-freeform",
            "Authored AR text-pair parent",
        ),
        (
            "g-phrase-glyph",
            "comp-g-phrase-glyph",
            "rem-g-phrase-glyph",
            "AV-edit-AR glyph-phrase parent",
        ),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.8), sharey=True)
    for ax, (parent, component, remainder, title) in zip(axes, panels):
        for key, color, label in [
            (parent, colors["parent"], "whole parent (unit L2)"),
            (
                component,
                colors["component"],
                "greedy-NNLS k25 component (unit L2)",
            ),
            (
                remainder,
                colors["remainder"],
                "k25 approximation remainder (unit L2)",
            ),
            ("random", colors["random"], "random control"),
        ]:
            xs, ys = curves[key]
            ax.plot(xs, ys, marker="o", linewidth=2.2, color=color, label=label)
        ax.set_title(title, fontsize=11)
        ax.set_xlabel("alpha")
        ax.grid(True, color="#e1e0d9", linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("mean emojis per reply")
    axes[0].legend(frameon=False, fontsize=7.8, loc="upper left")
    fig.suptitle(
        "Gemma: parent vs k25 approximation and remainder (unit-L2 views)",
        fontsize=13,
    )
    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "gemma_split.png"
    fig.savefig(target, dpi=160)
    plt.close(fig)
    return target


if __name__ == "__main__":
    print(f"wrote {build_split()}")
