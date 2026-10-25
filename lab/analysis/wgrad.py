"""Wordle distance-gradient analysis: letters vs vectors vs success.

    python analysis/wgrad.py

Reads every wgrad-*.json result and produces:

  figures/wgrad_letter.png   max P(target) vs LETTER edit distance
  figures/wgrad_vector.png   max P(target) vs RECONSTRUCTION distance
                             (the rung's delta-norm ratio: how far apart
                             the two one-word-different sentences land in
                             the reconstructor's space)
  figures/wgrad_walls.png    P(anchor) vs alpha for every rung, controls
                             overlaid: watch the walls erode (or not)
  figures/wgrad_summary.csv  one row per rung, every number used above

If the letter and vector orderings disagree about which hops are far,
the vector axis is the one the steering mechanism actually lives in.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from choices import answer  # noqa: E402  (same directory)

SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, BLUE, ORANGE = "#e1e0d9", "#c3c2b7", "#2a78d6", "#eb6834"
ANCHOR_COLOR = {"about": BLUE, "crane": ORANGE}
VALIDITY_FLOOR = 0.6  # below this, answers are too broken to interpret

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "DejaVu Sans"], "figure.dpi": 150,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK_2,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True,
})

_NAME = re.compile(r"wgrad-(\w+)-to-(\w+)")


def letter_dist(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


def load(results_dir: Path):
    rungs, controls = [], {}
    for path in sorted(results_dir.glob("wgrad-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data["spec"]["name"]
        if data["spec"].get("direction") == "random":
            anchor = name.rsplit("-", 1)[-1]
            controls[anchor] = data
            continue
        m = _NAME.match(name)
        if m:
            data["anchor"], data["target"] = m.group(1), m.group(2)
            rungs.append(data)
    return rungs, controls


def degenerate(text: str) -> bool:
    """Collapsed output: mostly non-ASCII, or one token spammed 4+ times.
    Discovered the hard way: at high alpha, '/about～～' junk contains the
    literal string 'about', and a naive extractor scores it a bullseye."""
    if not text.strip():
        return True
    if sum(ord(c) < 128 for c in text) / len(text) < 0.8:
        return True
    toks = text.lower().split()
    return bool(toks) and max(Counter(toks).values()) >= 4


_ANSWER_REGISTER = re.compile(
    r"[\"'“‘]([A-Za-z]{5})[.,!?]?[\"'”’]|\b([A-Z]{5})\b")
_SPELLED = re.compile(r"\b([A-Za-z])\s*-\s*([A-Za-z])\s*-\s*([A-Za-z])"
                      r"\s*-\s*([A-Za-z])\s*-\s*([A-Za-z])\b")


def answered_word(text: str) -> str:
    """The word given AS THE ANSWER: quoted or ALL-CAPS, the register every
    genuine baseline answer uses. Discovered the hard way: steering toward
    ABOUT produced coherent text USING 'about' as a preposition ('About
    the game itself...'), which a last-word extractor scores as a landed
    answer. Usage is not answering."""
    sp = _SPELLED.search(text)
    if sp:
        return "".join(sp.groups()).lower()
    m = _ANSWER_REGISTER.search(text)
    return (m.group(1) or m.group(2)).lower() if m else ""


def per_alpha(data: dict, word: str, want_len: int = 5):
    """{alpha: (P(word answered), valid_rate)}; degenerate outputs never
    count, and the target must appear in the answer register."""
    out = {}
    by_alpha: dict[float, list[str]] = {}
    for r in data["records"]:
        by_alpha.setdefault(r["alpha"], []).append(r["text"])
    for a, texts in sorted(by_alpha.items()):
        clean = [not degenerate(t) for t in texts]
        got = [answered_word(t) for t in texts]
        valid = sum(ok and x != ""
                    for ok, x in zip(clean, got)) / len(texts)
        p = sum(ok and x == word for ok, x in zip(clean, got)) / len(texts)
        out[a] = (p, valid)
    return out


def _despine(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def scatter_fig(rows, xkey, xlabel, title, out):
    fig, ax = plt.subplots(figsize=(7, 4.8))
    for r in rows:
        c = ANCHOR_COLOR[r["anchor"]]
        ax.scatter(r[xkey], r["p_target_best"], s=70, color=c, zorder=3)
        ax.annotate(f"{r['target'].upper()}", (r[xkey], r["p_target_best"]),
                    xytext=(7, 0), textcoords="offset points", fontsize=7.5,
                    color=INK_2, va="center")
    for anchor, c in ANCHOR_COLOR.items():
        ax.scatter([], [], s=70, color=c, label=f"from {anchor.upper()}")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("max P(target) at a valid alpha")
    ax.set_ylim(-0.05, 1.05)
    ax.set_title(title, color=INK, fontsize=10, loc="left")
    _despine(ax)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def main() -> None:
    lab = Path(__file__).resolve().parents[1]
    out_dir = Path(__file__).resolve().parent / "figures" / "wordle-gradient"
    out_dir.mkdir(parents=True, exist_ok=True)
    rungs, controls = load(lab / "results")
    if not rungs:
        print("no wgrad results yet")
        return

    rows = []
    for d in rungs:
        anchor, target = d["anchor"], d["target"]
        tgt = per_alpha(d, target)
        anc = per_alpha(d, anchor)
        valid_alphas = [a for a, (_, v) in tgt.items() if v >= VALIDITY_FLOOR]
        best_a = max(valid_alphas, key=lambda a: tgt[a][0]) if valid_alphas else 0.0
        diag = d.get("direction_diagnostics") or {}
        rows.append({
            "anchor": anchor, "target": target,
            "letter_dist": letter_dist(anchor, target),
            "recon_ratio": diag.get("delta_norm_ratio"),
            "cos_pos_neg": diag.get("cos_pos_neg"),
            "p_target_0": tgt[0.0][0], "p_target_best": tgt[best_a][0],
            "best_alpha": best_a,
            "p_anchor_0": anc[0.0][0], "p_anchor_at_best": anc[best_a][0],
            "valid_at_best": tgt[best_a][1],
        })

    rows.sort(key=lambda r: (r["anchor"], r["letter_dist"]))
    hdr = (f"{'rung':<22}{'ldist':>6}{'recon':>7}{'P(tgt)':>8}{'a*':>6}"
           f"{'wall 0->a*':>12}{'valid':>7}")
    print(hdr)
    for r in rows:
        print(f"{r['anchor']}->{r['target']:<15}{r['letter_dist']:>6}"
              f"{(r['recon_ratio'] or 0):>7.2f}{r['p_target_best']:>8.0%}"
              f"{r['best_alpha']:>6g}"
              f"{r['p_anchor_0']:>6.0%}->{r['p_anchor_at_best']:<4.0%}"
              f"{r['valid_at_best']:>7.0%}")

    scatter_fig(rows, "letter_dist", "letter edit distance from anchor",
                "Does LETTER distance predict landing the target word?",
                out_dir / "wgrad_letter.png")
    if all(r["recon_ratio"] is not None for r in rows):
        scatter_fig(rows, "recon_ratio",
                    "reconstruction-space distance (delta-norm ratio)",
                    "Does VECTOR distance in the reconstructor predict it?",
                    out_dir / "wgrad_vector.png")

    # wall erosion: P(anchor) vs alpha, controls overlaid
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, anchor in zip(axes, ANCHOR_COLOR):
        for d in rungs:
            if d["anchor"] != anchor:
                continue
            anc = per_alpha(d, anchor)
            xs = sorted(anc)
            ax.plot(xs, [anc[a][0] for a in xs], color=ANCHOR_COLOR[anchor],
                    linewidth=1.4, alpha=0.55)
            ax.annotate(d["target"].upper(), (xs[-1], anc[xs[-1]][0]),
                        xytext=(5, 0), textcoords="offset points",
                        fontsize=6.5, color=INK_2, va="center")
        if anchor in controls:
            anc = per_alpha(controls[anchor], anchor)
            xs = sorted(anc)
            ax.plot(xs, [anc[a][0] for a in xs], color=INK, linewidth=2,
                    linestyle="--", label="random control")
            ax.legend(frameon=False, fontsize=8, labelcolor=INK_2)
        ax.set_title(f"anchor {anchor.upper()}: P(anchor) vs alpha, one line "
                     "per target", color=INK_2, fontsize=9, loc="left")
        ax.set_xlabel("alpha")
        _despine(ax)
    axes[0].set_ylabel("P(anchor word)")
    fig.suptitle("Wall erosion: how steering toward each target pushes the "
                 "model off its default word", color=INK, fontsize=11,
                 x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out_dir / "wgrad_walls.png")
    plt.close(fig)

    with (out_dir / "wgrad_summary.csv").open("w", newline="",
                                              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nfigures + wgrad_summary.csv written to {out_dir}")


if __name__ == "__main__":
    main()
