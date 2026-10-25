"""The steerability map: where in (direction size, alpha) space does
steering work, do nothing, or destroy the text?

    python analysis/steer_map.py

Every steered run with logged diagnostics contributes one point per
nonzero alpha:
  x = the run's delta-norm ratio (how hard the sentence edit cranked the
      wheel in the reconstructor's space)
  y = alpha (how hard we pushed at layer 20)
  outcome at that point:
      MOVED    target probe shifted vs alpha 0 (|Cliff's delta| >= 0.3)
               with coherent text
      GARBLED  text coherence collapsed (whatever the probe says)
      INERT    coherent but nothing moved

Writes figures/steer_map.png. The empty upper-left (small crank, big
alpha) and the orange band up top are the invisible boundaries: the
steerable region is what remains.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, BLUE, ORANGE, AQUA = ("#e1e0d9", "#c3c2b7", "#2a78d6",
                                  "#eb6834", "#1baf7a")

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "DejaVu Sans"], "figure.dpi": 150,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK_2,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True,
})


def degenerate(text: str) -> bool:
    if not text.strip():
        return True
    if sum(ord(c) < 128 for c in text) / len(text) < 0.8:
        return True
    toks = text.lower().split()
    return bool(toks) and max(Counter(toks).values()) >= 4


def cliffs(x, y) -> float:
    if not len(x) or not len(y):
        return 0.0
    x, y = np.asarray(x, float), np.asarray(y, float)
    d = x[:, None] - y[None, :]
    return float(((d > 0).sum() - (d < 0).sum()) / d.size)


def coherent(texts, probes_list) -> bool:
    """Coherence at one alpha: unique_word_ratio floor when recorded,
    else a degenerate-text fraction check straight from the raw text."""
    uwr = [p.get("unique_word_ratio") for p in probes_list]
    if all(v is not None for v in uwr):
        return float(np.mean(uwr)) >= 0.45
    return np.mean([degenerate(t) for t in texts]) <= 0.4


def main() -> None:
    lab = Path(__file__).resolve().parents[1]
    out_dir = Path(__file__).resolve().parent / "figures"
    out_dir.mkdir(exist_ok=True)
    pts = []
    for path in sorted((lab / "results").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "spec" not in data:  # e.g. vector_metrics.json lives here too
            continue
        spec, diag = data["spec"], data.get("direction_diagnostics") or {}
        ratio = diag.get("delta_norm_ratio")
        if not isinstance(spec.get("direction"), dict) or ratio is None:
            continue
        probe = spec["probes"][0]
        by_alpha: dict[float, list[dict]] = {}
        for r in data["records"]:
            by_alpha.setdefault(r["alpha"], []).append(r)
        base = [r["probes"][probe] for r in by_alpha.get(0.0, [])]
        for a, recs in by_alpha.items():
            if a == 0.0:
                continue
            vals = [r["probes"][probe] for r in recs]
            ok = coherent([r["text"] for r in recs],
                          [r["probes"] for r in recs])
            moved = abs(cliffs(vals, base)) >= 0.3
            outcome = "garbled" if not ok else ("moved" if moved else "inert")
            pts.append((ratio, a, outcome, spec["name"]))

    fig, ax = plt.subplots(figsize=(8, 5.2))
    rng = np.random.default_rng(0)
    style = {"moved": (BLUE, 1.0, 60), "inert": (AXIS, 0.9, 36),
             "garbled": (ORANGE, 0.95, 48)}
    for outcome in ("inert", "garbled", "moved"):
        xs = [p[0] + rng.uniform(-0.008, 0.008) for p in pts if p[2] == outcome]
        ys = [p[1] + rng.uniform(-0.015, 0.015) for p in pts if p[2] == outcome]
        c, al, s = style[outcome]
        ax.scatter(xs, ys, s=s, color=c, alpha=al, linewidths=0,
                   label=f"{outcome} ({len(xs)})", zorder=3)
    ax.set_xlabel("direction size: delta-norm ratio of the sentence edit "
                  "(how far you cranked the wheel)")
    ax.set_ylabel("alpha (how hard you pushed)")
    ax.set_title("The steerability map: every steered (run, alpha) cell "
                 "to date", color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=8.5, labelcolor=INK_2,
              title="outcome", title_fontsize=8.5)
    for s_ in ("top", "right"):
        ax.spines[s_].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "steer_map.png")
    plt.close(fig)
    print(f"{len(pts)} cells -> {out_dir / 'steer_map.png'}")
    for o in ("moved", "inert", "garbled"):
        n = sum(1 for p in pts if p[2] == o)
        print(f"  {o}: {n}")


if __name__ == "__main__":
    main()
