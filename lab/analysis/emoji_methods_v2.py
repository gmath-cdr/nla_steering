"""Wave 1+2 pooled analysis for the emoji method comparison. Laptop.

    python analysis/emoji_methods_v2.py

Pools wave-1 results (seeds 0-14) with wave-2 fresh-seed reruns (seeds
15-29) for the base arms (n=30 per cell), loads wave-2-only arms (new
singles, faithful k=25 components and residuals) at n=15, and writes to
analysis/figures/emoji_methods_v2/:

  corrected_singles.png  the five target-relevant one-word edits
  share_vs_effect.png    k25 component-norm ratio vs steering effect
  purification.png       unit-L2 component potency vs parent potency
  split_verdict_v2.png   whole / component / residual, freeform + phrase_4
  summary_v2.json        every number the report and artifact read

Conventions identical to analysis/emoji_methods_report.py (imported):
Cliff's delta vs same-arm alpha-0, coherence gate = unique_word_ratio
floor AND word-count floor, degenerate cells drawn as open circles.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from emoji_methods_report import (  # noqa: E402
    AQUA, BLUE, INK, INK2, MUTED, ORANGE, SURFACE, arm_stats, cliffs_delta)

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

LAB = Path(__file__).resolve().parents[1]
OUT = LAB / "analysis" / "figures" / "emoji_methods_v2"
VIOLET = "#4a3aa7"

BASE_ARMS = (["freeform"] + [f"single_{i}" for i in range(1, 6)]
             + [f"phrase_{i}" for i in range(1, 6)] + ["random"])
NEW_SINGLES = ["s2_emoji", "s2_emojis", "s2_emoticon", "s2_smiley", "s2_glyph"]


def newest(pattern: str) -> Path | None:
    files = sorted((LAB / "results").glob(pattern))
    return files[-1] if files else None


def add_records(by_alpha: dict, path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for rec in data["records"]:
        a = float(rec["alpha"])
        cell = by_alpha.setdefault(a, {"emojis": [], "coherence": [],
                                       "n_words": [], "exclamations": []})
        cell["emojis"].append(float(rec["probes"]["emojis"]))
        cell["coherence"].append(float(rec["probes"]["unique_word_ratio"]))
        cell["n_words"].append(float(rec["probes"]["n_words"]))
        cell["exclamations"].append(float(rec["probes"]["exclamations"]))


def load_all() -> dict[str, dict]:
    runs: dict[str, dict] = {}
    for arm in BASE_ARMS:
        dash = arm.replace("_", "-")
        by_alpha: dict = {}
        for pat in (f"emojimx-{dash}_*.json", f"emojimx2-{dash}-r2_*.json"):
            p = newest(pat)
            if p:
                add_records(by_alpha, p)
        if by_alpha:
            runs[arm] = {"by_alpha": dict(sorted(by_alpha.items()))}
    extra = NEW_SINGLES + [f"comp25_{a}" for a in BASE_ARMS if a != "random"] \
        + [f"comp25_{a}" for a in NEW_SINGLES] \
        + [f"resid25_{a}" for a in ["freeform"] + [f"phrase_{i}" for i in range(1, 6)] + NEW_SINGLES]
    for arm in extra:
        p = newest(f"emojimx2-{arm.replace('_', '-')}_*.json")
        if p:
            by_alpha: dict = {}
            add_records(by_alpha, p)
            runs[arm] = {"by_alpha": dict(sorted(by_alpha.items()))}
    return runs


def dose_line(ax, runs, stats, arm, color, label=None, lw=2.0):
    if arm not in runs:
        return
    st = stats[arm]["rows"]
    xs = sorted(runs[arm]["by_alpha"])
    ys = [st[a]["mean_emojis"] for a in xs]
    ax.plot(xs, ys, color=color, lw=lw, label=label, zorder=2)
    for a, y in zip(xs, ys):
        if st[a]["coherence_safe"]:
            ax.plot(a, y, "o", color=color, markersize=4.5, zorder=3)
        else:
            ax.plot(a, y, "o", mfc=SURFACE, mec=color, mew=1.5,
                    markersize=5.5, zorder=3)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    runs = load_all()
    stats = {arm: arm_stats(run) for arm, run in runs.items()}
    deco = json.loads((LAB / "experiments" / "emoji_methods" / "data"
                       / "decomposition_v2.json").read_text(encoding="utf-8"))

    # --- corrected singles ---------------------------------------------
    fig, ax = plt.subplots(figsize=(7.4, 4.6))
    colors = [BLUE, AQUA, VIOLET, "#eda100", "#e87ba4"]
    labels = ['"emoji"', '"emojis"', '"emoticon"', '"smiley"', "bare glyph"]
    for arm, c, lbl in zip(NEW_SINGLES, colors, labels):
        dose_line(ax, runs, stats, arm, c, lbl)
    dose_line(ax, runs, stats, "random", ORANGE, "random control")
    ax.set_xlabel("steering strength (alpha)")
    ax.set_ylabel("mean emojis per reply")
    ax.set_title("Target-relevant one-word edits (open circles = degenerated)",
                 color=INK, fontsize=12)
    ax.legend(frameon=False, fontsize=8.5)
    fig.tight_layout()
    fig.savefig(OUT / "corrected_singles.png", dpi=160)
    plt.close(fig)

    # --- share vs effect ------------------------------------------------
    fig, ax = plt.subplots(figsize=(7.2, 4.8))
    for arm in [a for a in BASE_ARMS if a != "random"] + NEW_SINGLES:
        if arm not in stats or arm not in deco["per_vector"]:
            continue
        share = deco["per_vector"][arm]["25"]["share"]
        delta = stats[arm]["best"]["delta"]
        marker = "^" if arm.startswith("single") else (
            "s" if arm.startswith("phrase") else (
                "D" if arm.startswith("s2") else "o"))
        ax.scatter(share, delta, s=60, color=BLUE, marker=marker, zorder=3,
                   edgecolors=SURFACE, linewidths=1.4)
        ax.annotate(arm, (share, delta), textcoords="offset points",
                    xytext=(6, 3), fontsize=8, color=INK2)
    ax.set_xlabel("k25 component-norm ratio ||c25|| / ||d|| (greedy NNLS)")
    ax.set_ylabel("steering effect (Cliff's delta, best safe alpha)")
    ax.set_title("Does k25 component-norm ratio predict steering?", color=INK,
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "share_vs_effect.png", dpi=160)
    plt.close(fig)

    # --- purification ---------------------------------------------------
    fig, ax = plt.subplots(figsize=(6.8, 5.4))
    lims = [-0.15, 1.1]
    ax.plot(lims, lims, color=MUTED, lw=1, ls="--", zorder=1)
    for arm in [a for a in BASE_ARMS if a != "random"] + NEW_SINGLES:
        comp = f"comp25_{arm}"
        if arm not in stats or comp not in stats:
            continue
        x, y = stats[arm]["best"]["delta"], stats[comp]["best"]["delta"]
        ax.scatter(x, y, s=60, color=AQUA, zorder=3,
                   edgecolors=SURFACE, linewidths=1.4)
        ax.annotate(arm, (x, y), textcoords="offset points", xytext=(6, 3),
                    fontsize=8, color=INK2)
    ax.set_xlim(lims); ax.set_ylim(lims)
    ax.set_xlabel("parent vector effect (Cliff's delta)")
    ax.set_ylabel("unit-L2 k25 component effect (Cliff's delta)")
    ax.set_title("Equal-norm potency: points above the line = component beats parent",
                 color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "purification.png", dpi=160)
    plt.close(fig)

    # --- split verdict v2 ----------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.2), sharey=True)
    for ax, parent, title in [
            (axes[0], "freeform", "AR text-pair parent"),
            (axes[1], "phrase_4", "AV-edit-AR glyph-phrase parent")]:
        dose_line(ax, runs, stats, parent, BLUE, "whole parent (unit L2)")
        dose_line(ax, runs, stats, f"comp25_{parent}", AQUA,
                  "greedy-NNLS k25 component (unit L2)")
        dose_line(ax, runs, stats, f"resid25_{parent}", MUTED,
                  "k25 approximation remainder (unit L2)")
        dose_line(ax, runs, stats, "random", ORANGE, "random control", lw=1.4)
        ax.set_title(title, fontsize=11, color=INK)
        ax.set_xlabel("alpha")
    axes[0].set_ylabel("mean emojis per reply")
    axes[0].legend(frameon=False, fontsize=8.5)
    fig.suptitle("Parent vs k25 approximation and remainder (unit-L2 views)",
                 color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "split_verdict_v2.png", dpi=160)
    plt.close(fig)

    summary = {}
    for arm, st in stats.items():
        entry = {"best": st["best"],
                 "rows": {str(a): r for a, r in st["rows"].items()},
                 "n_per_cell": len(runs[arm]["by_alpha"].get(0.0, {})
                                   .get("emojis", []))}
        if arm in deco["per_vector"]:
            entry["share_k25"] = deco["per_vector"][arm]["25"]["share"]
            entry["atoms_k25"] = deco["per_vector"][arm]["25"]["tokens"][:8]
        summary[arm] = entry
    (OUT / "summary_v2.json").write_text(json.dumps(summary, indent=1),
                                         encoding="utf-8")
    print(f"{'arm':<20}{'n':>4}{'best a':>8}{'delta':>8}{'share':>8}")
    for arm in sorted(summary):
        s = summary[arm]
        sh = s.get("share_k25")
        print(f"{arm:<20}{s['n_per_cell']:>4}{s['best']['alpha']:>8.1f}"
              f"{s['best']['delta']:>8.2f}{(f'{sh:.2f}' if sh else '   -'):>8}")
    print(f"\nwrote figures + summary_v2.json to {OUT}")


if __name__ == "__main__":
    main()
