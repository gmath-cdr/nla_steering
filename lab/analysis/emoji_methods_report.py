"""Figures + summary for the emoji method-comparison batch. Laptop only.

    python analysis/emoji_methods_report.py

Reads results/emojimx-*.json (newest file per spec name wins) and
experiments/emoji_methods/data/build_report.json. Writes to
analysis/figures/emoji_methods/:

  split_verdict.png   AR-pair parent vs legacy OLS component/remainder/random,
                      mean emojis per reply vs alpha (the causal chart)
  methods.png         small multiples by construction method: freeform,
                      phrase edits, single-token edits (member lines
                      faint, group mean bold)
  ranking.png         lens transport gain vs steering effect, one dot
                      per arm (retrospective diagnostic)
  summary.json        per-arm numbers the artifact page reads

Conventions follow analysis/plots.py: coherence probe unique_word_ratio,
an alpha is out of bounds when its mean coherence drops below 0.8x the
alpha-0 mean, effect size is Cliff's delta vs the same-arm alpha-0
sample, reported at the coherence-safe alpha with the largest |delta|.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

LAB = Path(__file__).resolve().parents[1]
OUT = LAB / "analysis" / "figures" / "emoji_methods"

SURFACE, INK, INK2 = "#fcfcfb", "#0b0b0b", "#52514e"
GRID, AXIS, MUTED = "#e1e0d9", "#c3c2b7", "#898781"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "text.color": INK,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False,
    "font.size": 10,
})

COHERENCE_FLOOR = 0.8


def cliffs_delta(a: list[float], b: list[float]) -> float:
    """Effect of a vs b: P(a>b) - P(a<b), scale-free."""
    if not a or not b:
        return 0.0
    arr_a, arr_b = np.asarray(a)[:, None], np.asarray(b)[None, :]
    return float(((arr_a > arr_b).mean()) - ((arr_a < arr_b).mean()))


def load_runs() -> dict[str, dict]:
    newest: dict[str, Path] = {}
    for p in sorted((LAB / "results").glob("emojimx-*.json")):
        name = p.stem.rsplit("_", 1)[0]
        newest[name] = p
    runs = {}
    for name, p in newest.items():
        data = json.loads(p.read_text(encoding="utf-8"))
        by_alpha: dict[float, dict] = defaultdict(lambda: {"emojis": [],
                                                           "coherence": []})
        for rec in data["records"]:
            a = float(rec["alpha"])
            by_alpha[a]["emojis"].append(float(rec["probes"]["emojis"]))
            by_alpha[a]["coherence"].append(
                float(rec["probes"]["unique_word_ratio"]))
            by_alpha[a].setdefault("n_words", []).append(
                float(rec["probes"]["n_words"]))
            by_alpha[a].setdefault("exclamations", []).append(
                float(rec["probes"]["exclamations"]))
        arm = name.removeprefix("emojimx-").replace("-", "_")
        runs[arm] = {"by_alpha": dict(sorted(by_alpha.items())), "file": p.name}
    return runs


def arm_stats(run: dict) -> dict:
    by_alpha = run["by_alpha"]
    base = by_alpha.get(0.0, {"emojis": [], "coherence": [], "n_words": []})
    coh0 = float(np.mean(base["coherence"])) if base["coherence"] else 1.0
    words0 = float(np.mean(base["n_words"])) if base.get("n_words") else 1.0
    rows = {}
    best = {"alpha": 0.0, "delta": 0.0}
    for a, cell in by_alpha.items():
        coh = float(np.mean(cell["coherence"]))
        words = float(np.mean(cell.get("n_words", [words0])))
        delta = cliffs_delta(cell["emojis"], base["emojis"]) if a else 0.0
        # unique_word_ratio is blind to unspaced streams (emoji floods,
        # Chinese); the word-count floor catches text being REPLACED by
        # symbols rather than decorated with them.
        safe = (coh >= COHERENCE_FLOOR * coh0
                and words >= 0.5 * words0)
        rows[a] = {"mean_emojis": float(np.mean(cell["emojis"])),
                   "coherence": coh, "mean_words": words,
                   "cliffs_delta": delta, "coherence_safe": safe}
        if safe and abs(delta) > abs(best["delta"]):
            best = {"alpha": a, "delta": delta}
    return {"rows": rows, "best": best}


def line(ax, run: dict, color: str, label: str, lw: float = 2.0,
         alpha: float = 1.0) -> None:
    xs = list(run["by_alpha"].keys())
    ys = [float(np.mean(c["emojis"])) for c in run["by_alpha"].values()]
    ax.plot(xs, ys, color=color, lw=lw, alpha=alpha, marker="o",
            markersize=4.5, label=label)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    runs = load_runs()
    build = json.loads((LAB / "experiments" / "emoji_methods" / "data"
                        / "build_report.json").read_text(encoding="utf-8"))
    stats = {arm: arm_stats(run) for arm, run in runs.items()}

    # --- split_verdict ---------------------------------------------------
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    # ``readable`` and ``leftover`` are immutable legacy artifact keys. They
    # are a signed top-50 OLS split, not the later sparse nonnegative method.
    series = [("freeform", BLUE, "AR text-pair parent (unit L2)"),
              ("readable", AQUA, "legacy top-50 OLS component (unit L2)"),
              ("leftover", MUTED, "legacy OLS remainder (unit L2)"),
              ("random", ORANGE, "random control")]
    for arm, color, label in series:
        if arm not in runs:
            continue
        st = stats[arm]["rows"]
        xs = sorted(runs[arm]["by_alpha"])
        ys = [st[a]["mean_emojis"] for a in xs]
        ax.plot(xs, ys, color=color, lw=2.0, label=label, zorder=2)
        for a, y in zip(xs, ys):
            if st[a]["coherence_safe"]:
                ax.plot(a, y, "o", color=color, markersize=5, zorder=3)
            else:  # open circle = text degenerated (emoji flood), per lab convention
                ax.plot(a, y, "o", mfc=SURFACE, mec=color, mew=1.6,
                        markersize=6, zorder=3)
    ax.set_xlabel("steering strength (alpha)")
    ax.set_ylabel("mean emojis per reply")
    ax.set_title("Legacy signed-OLS split (not greedy-NNLS k25) "
                 "(open circles = text degenerated)", color=INK, fontsize=12)
    ax.legend(frameon=False, fontsize=9)
    ax.set_xlim(-0.03, 0.86)
    fig.tight_layout()
    fig.savefig(OUT / "split_verdict.png", dpi=160)
    plt.close(fig)

    # --- methods small multiples ----------------------------------------
    groups = [("freeform", ["freeform"], "AR text pair (2 authored descriptions)"),
              ("phrase", [f"phrase_{i}" for i in range(1, 6)],
               "phrase edits (5 versions)"),
              ("single", [f"single_{i}" for i in range(1, 6)],
               "single-token edits (5 versions)")]
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.6), sharey=True)
    for ax, (key, members, title) in zip(axes, groups):
        present = [m for m in members if m in runs]
        for m in present:
            line(ax, runs[m], BLUE, None, lw=1.0, alpha=0.35)
        if present:
            xs = sorted(runs[present[0]]["by_alpha"])
            mean_ys = [float(np.mean([np.mean(runs[m]["by_alpha"][a]["emojis"])
                                      for m in present])) for a in xs]
            ax.plot(xs, mean_ys, color=BLUE, lw=2.4, marker="o",
                    markersize=4.5)
        if "random" in runs:
            line(ax, runs["random"], ORANGE, None, lw=1.6)
        ax.set_title(title, fontsize=10, color=INK)
        ax.set_xlabel("alpha")
    axes[0].set_ylabel("mean emojis per reply")
    fig.suptitle("Steering by construction method (orange = random control)",
                 color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "methods.png", dpi=160)
    plt.close(fig)

    # --- ranking ---------------------------------------------------------
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    for arm, st in stats.items():
        jgain = build["per_arm"].get(arm, {}).get("jgain")
        if jgain is None:
            continue
        color = {"freeform": BLUE, "readable": AQUA, "random": ORANGE,
                 "leftover": MUTED}.get(arm, BLUE)
        marker = "o" if arm in ("freeform", "readable", "leftover", "random") \
            else ("s" if arm.startswith("phrase") else "^")
        ax.scatter(jgain, st["best"]["delta"], s=64, color=color,
                   marker=marker, zorder=3,
                   edgecolors=SURFACE, linewidths=1.5)
        ax.annotate(arm, (jgain, st["best"]["delta"]),
                    textcoords="offset points", xytext=(7, 4),
                    fontsize=8.5, color=INK2)
    ax.set_xlabel("lens transport gain |J d|")
    ax.set_ylabel("steering effect (Cliff's delta, best safe alpha)")
    ax.set_title("Lens transport gain vs observed steering (retrospective)",
                 color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "ranking.png", dpi=160)
    plt.close(fig)

    # --- retrospective readout/outcome alignment -------------------------
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    forecast = [("single_1", BLUE, 'single_1 "excited" (readout has !)'),
                ("single_2", AQUA, 'single_2 "enthusiastic" (readout has !)'),
                ("single_3", "#4a3aa7", 'single_3 "cheerful" (happy words)'),
                ("single_4", MUTED, 'single_4 "expressive" (no ! in readout)'),
                ("random", ORANGE, "random control")]
    for arm, color, label in forecast:
        if arm not in runs:
            continue
        xs = sorted(runs[arm]["by_alpha"])
        ys = [float(np.mean(runs[arm]["by_alpha"][a]["exclamations"]))
              for a in xs]
        # dashed line adds a non-color cue for the mid-pack series
        ax.plot(xs, ys, color=color, lw=2.0, marker="o", markersize=4.5,
                linestyle="--" if arm == "single_3" else "-", label=label)
    ax.set_xlabel("steering strength (alpha)")
    ax.set_ylabel("mean exclamation marks per reply")
    ax.set_title("Observed readout/outcome alignment (not a blinded forecast)",
                 color=INK, fontsize=12)
    ax.legend(frameon=False, fontsize=8.5)
    fig.tight_layout()
    fig.savefig(OUT / "forecast.png", dpi=160)
    plt.close(fig)

    summary = {arm: {"best": st["best"],
                     "rows": {str(a): r for a, r in st["rows"].items()},
                     "jgain": build["per_arm"].get(arm, {}).get("jgain"),
                     "delta_norm_ratio": build["per_arm"].get(arm, {}).get(
                         "delta_norm_ratio")}
               for arm, st in stats.items()}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1),
                                      encoding="utf-8")
    print(f"{'arm':<12}{'best a':>7}{'delta':>8}{'jgain':>8}")
    for arm, s in sorted(summary.items()):
        jg = s["jgain"]
        print(f"{arm:<12}{s['best']['alpha']:>7.1f}{s['best']['delta']:>8.2f}"
              f"{(f'{jg:.2f}' if jg else '  -'):>8}")
    print(f"\nwrote figures + summary.json to {OUT}")


if __name__ == "__main__":
    main()
