"""Render results/vector_metrics.json into geometry figures.

    python analysis/geometry.py

Root figures:
  figures/vector_map.png            every AR(+) and AR(-) plus every task
                                    prompt's layer-20 state on one shared
                                    2D map (PCA); pair endpoints joined
  figures/direction_similarity.png  pairwise cosine heatmap between all
                                    unit directions

Per experiment (figures/<name>/vector_geometry.png): that pair on the
map with everything else dimmed, its own task prompts highlighted, and
the numbers (pair cosine, delta ratio, norms, direction-to-prompt
cosine) printed beside it.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import yaml  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, BLUE, ORANGE = "#e1e0d9", "#c3c2b7", "#2a78d6", "#eb6834"
RED, MID = "#e34948", "#f0efec"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "DejaVu Sans"], "figure.dpi": 150,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK_2,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True,
})


def spec_prompts(lab_dir: Path) -> dict[str, list[str]]:
    out = {}
    for f in (lab_dir / "experiments" / "specs").glob("*.yaml"):
        s = yaml.safe_load(f.read_text(encoding="utf-8"))
        if isinstance(s.get("direction"), dict):
            out[s["name"]] = s.get("prompts", [])
    return out


def base_map(ax, m, dim=False):
    a = 0.25 if dim else 0.8
    for p, info in m["prompts"].items():
        x, y = info["xy"]
        ax.scatter(x, y, marker="D", s=26, color=MUTED, alpha=a * 0.8,
                   linewidths=0, zorder=2)
    for name, d in m["directions"].items():
        (x1, y1), (x2, y2) = d["xy_pos"], d["xy_neg"]
        ax.plot([x1, x2], [y1, y2], color=AXIS, linewidth=0.8, alpha=a,
                zorder=1)
        ax.scatter(x1, y1, s=22, color=BLUE, alpha=a, linewidths=0, zorder=3)
        ax.scatter(x2, y2, s=22, color=ORANGE, alpha=a, linewidths=0, zorder=3)


def main() -> None:
    lab_dir = Path(__file__).resolve().parents[1]
    out_dir = Path(__file__).resolve().parent / "figures"
    metrics = lab_dir / "results" / "vector_metrics.json"
    if not metrics.exists():
        print("no results/vector_metrics.json yet: run "
              "experiments/vector_metrics.py on the pod first")
        return
    m = json.loads(metrics.read_text(encoding="utf-8"))
    prompts_of = spec_prompts(lab_dir)

    # global map
    fig, ax = plt.subplots(figsize=(9, 6.5))
    base_map(ax, m)
    for name, d in m["directions"].items():
        ax.annotate(name.replace("-freeform", ""), d["xy_pos"], xytext=(4, 2),
                    textcoords="offset points", fontsize=5, color=INK_2)
    ax.scatter([], [], s=30, color=BLUE, label="AR(+) reconstruction")
    ax.scatter([], [], s=30, color=ORANGE, label="AR(-) reconstruction")
    ax.scatter([], [], marker="D", s=30, color=MUTED,
               label="task prompt layer-20 state")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2)
    ax.set_title("The vector map: every direction pair and every prompt "
                 "state, one shared 2D projection", color=INK, fontsize=11,
                 loc="left")
    ax.set_xlabel("PC1")
    ax.set_ylabel("PC2")
    fig.tight_layout()
    fig.savefig(out_dir / "vector_map.png")
    plt.close(fig)

    # similarity heatmap
    names = m["names"]
    cosm = np.array(m["direction_cos"])
    cmap = LinearSegmentedColormap.from_list("div", [RED, MID, BLUE])
    fig, ax = plt.subplots(figsize=(10, 8.5))
    im = ax.imshow(cosm, cmap=cmap, vmin=-1, vmax=1)
    short = [n.replace("-freeform", "") for n in names]
    ax.set_xticks(range(len(names)), short, rotation=90, fontsize=5.5)
    ax.set_yticks(range(len(names)), short, fontsize=5.5)
    ax.grid(False)
    fig.colorbar(im, ax=ax, shrink=0.7, label="cosine between unit directions")
    ax.set_title("Do different experiments push in related directions?",
                 color=INK, fontsize=11, loc="left")
    fig.tight_layout()
    fig.savefig(out_dir / "direction_similarity.png")
    plt.close(fig)

    # per-experiment zoom: chart on top, plain-words reading guide below
    for name, d in m["directions"].items():
        run_dir = out_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        fig = plt.figure(figsize=(7.5, 8.2))
        gs = fig.add_gridspec(2, 1, height_ratios=[3.1, 1.9], hspace=0.3)
        ax = fig.add_subplot(gs[0])
        cax = fig.add_subplot(gs[1])
        cax.axis("off")

        base_map(ax, m, dim=True)
        (x1, y1), (x2, y2) = d["xy_pos"], d["xy_neg"]
        ax.annotate("", xy=(x1, y1), xytext=(x2, y2),
                    arrowprops=dict(arrowstyle="-|>", color=INK,
                                    linewidth=2, mutation_scale=16))
        ax.scatter([x1], [y1], s=100, color=BLUE, zorder=5)
        ax.scatter([x2], [y2], s=100, color=ORANGE, zorder=5)
        own = prompts_of.get(name, [])
        for p in own:
            if p in m["prompts"]:
                px, py = m["prompts"][p]["xy"]
                ax.scatter([px], [py], marker="D", s=100, color=INK,
                           zorder=5)
        ax.set_title(f"{name}: its direction on the shared vector map",
                     color=INK, fontsize=10.5, loc="left")
        ax.set_xlabel("map axis 1", fontsize=8)
        ax.set_ylabel("map axis 2", fontsize=8)

        own_cos = [d["cos_to_prompts"][p] for p in own
                   if p in d["cos_to_prompts"]]
        own_cos_txt = (", ".join(f"{c:+.2f}" for c in own_cos)
                       if own_cos else "n/a")
        guide = (
            "HOW TO READ THIS CHART\n"
            "\n"
            "The axes: every point is really 3,584 numbers; this map is a "
            "2D shadow of that space (PCA).\n"
            "The two axes are the directions along which all our vectors "
            "differ most. Only RELATIVE\n"
            "position carries meaning: points close together are similar "
            "vectors. Units are arbitrary.\n"
            "\n"
            "* blue dot        AR(+): the reconstructor's vector for "
            "the steer-TOWARD sentence\n"
            "* orange dot      AR(−): its vector for the "
            "steer-AWAY sentence\n"
            "* black arrow     the injected steering direction: from "
            "AR(−) to AR(+)\n"
            "* black diamond   the model's internal state at layer 20 "
            "while reading THIS experiment's\n"
            "                  question. This is what steering pushes "
            "on: generation starts from this\n"
            "                  state, plus alpha times the arrow.\n"
            "* gray diamonds   the same thing for every other question "
            "used in any experiment\n"
            "\n"
            "Sizes: LARGE solid marks belong to this experiment. Small "
            "faded dots and lines are every\n"
            "other experiment's sentence pair, drawn only as background "
            "so you can see where this one\n"
            "sits in the full landscape.\n"
            "\n"
            f"This pair's numbers:  cos(AR+, AR−) = "
            f"{d['cos_pair']:.3f}   (1.0 would mean the reconstructor "
            "heard both\n"
            "sentences as identical)      delta-norm ratio = "
            f"{d['delta_norm_ratio']:.3f}   (direction-size diagnostic; "
            "under ~0.15 triggers\n"
            "a historical screening warning, not an impossibility claim)"
            "      cos(direction, own\n"
            f"prompt state) = {own_cos_txt}"
        )
        cax.text(0.5, 0.98, guide, transform=cax.transAxes, fontsize=8,
                 color=INK_2, va="top", ha="center", linespacing=1.55,
                 family="sans-serif",
                 bbox=dict(boxstyle="round,pad=0.6", facecolor="white",
                           edgecolor=AXIS, linewidth=1.2))
        fig.savefig(run_dir / "vector_geometry.png",
                    bbox_inches="tight")
        plt.close(fig)
    print(f"geometry figures written under {out_dir}")


if __name__ == "__main__":
    main()
