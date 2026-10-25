"""Histogram analysis for choice-task runs (Wordle, number, color).

    python analysis/choices.py                 # all runs with answer_format
    python analysis/choices.py results/census-choices_*.json

For every matching results JSON: extract the first word of each reply,
tally answers per (prompt, alpha), print the histogram table, and write
figures/choices_<name>.png: one bar panel per alpha showing the top
answers, so you can watch the distribution move (or not) as alpha rises.
The alpha=0 column of every run doubles as that prompt's baseline
histogram.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

LAB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_DIR))

from nla_lab.methods import (  # noqa: E402
    MethodMetadataError,
    resolve_method,
    short_label,
)

SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, BLUE = "#e1e0d9", "#c3c2b7", "#2a78d6"
_WORD = re.compile(r"[A-Za-z]+")

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "DejaVu Sans"], "figure.dpi": 150,
    "axes.edgecolor": AXIS, "xtick.color": MUTED, "ytick.color": MUTED,
})


def answer(text: str, want_len: int | None = None) -> str:
    """Mirror of probes.wordle_answer, generalized. When the spec gives
    answer_len (wordle: 5), only tokens of exactly that length count;
    otherwise first ALL-CAPS 3-6 letter token, else first 5-letter word,
    else first word."""
    toks = _WORD.findall(text)
    if want_len:
        m = re.search(r"[\"'“‘]([A-Za-z]{%d})[\"'”’]" % want_len, text)
        if m:
            return m.group(1).upper()
        for t in toks:
            if len(t) == want_len and t.isupper():
                return t
        for t in reversed(toks):
            if len(t) == want_len:
                return t.upper()
        return "(none)"
    for t in toks:
        if 3 <= len(t) <= 6 and t.isupper():
            return t
    for t in toks:
        if len(t) == 5:
            return t.upper()
    return toks[0].upper() if toks else "(none)"


def load_run(path: Path) -> dict[str, Any] | None:
    """Return a generation run, or ``None`` for other results JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("spec"), dict):
        return None
    if not isinstance(data.get("records"), list):
        return None
    return data


def analyze(path: Path, out_dir: Path) -> bool:
    data = load_run(path)
    if data is None:
        print(f"skipped {path}: not a generation-run result")
        return False
    spec = data["spec"]
    name = spec["name"]
    want_len = spec.get("answer_len")
    try:
        method_label = short_label(resolve_method(spec))
    except MethodMetadataError as exc:
        method_label = f"unresolved method metadata ({exc})"
    alphas = sorted({r["alpha"] for r in data["records"]})
    if not alphas:
        print(f"skipped {path}: generation run has no records")
        return False
    by_alpha: dict[float, Counter] = {
        a: Counter(answer(r["text"], want_len)
                   for r in data["records"] if r["alpha"] == a)
        for a in alphas
    }
    modal, modal_n = by_alpha[alphas[0]].most_common(1)[0]
    n0 = sum(by_alpha[alphas[0]].values())
    print(f"\n{name} [{method_label}]: baseline modal answer {modal} "
          f"({modal_n}/{n0} = {modal_n / n0:.0%})")
    print(f"{'alpha':>6} {'P(' + modal + ')':>10} {'top answers':<60}")
    for a in alphas:
        c = by_alpha[a]
        n = sum(c.values())
        top = "  ".join(f"{w}:{k}" for w, k in c.most_common(5))
        print(f"{a:>6g} {c[modal] / n:>10.0%} {top:<60}")

    fig, axes = plt.subplots(1, len(alphas), figsize=(2.3 * len(alphas), 3.2),
                             sharey=True)
    for ax, a in zip(axes if len(alphas) > 1 else [axes], alphas):
        c = by_alpha[a]
        n = sum(c.values())
        words, counts = zip(*c.most_common(6))
        colors = [BLUE if w == modal else AXIS for w in words]
        ax.bar(range(len(words)), [k / n for k in counts], color=colors,
               width=0.7)
        ax.set_xticks(range(len(words)), words, rotation=90, fontsize=6.5)
        ax.set_title(f"alpha {a:g}", fontsize=8, color=INK_2)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    fig.suptitle(f"{name}: answer distribution by alpha "
                 f"(blue = baseline modal '{modal}')",
                 fontsize=10, color=INK, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    run_dir = out_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / "answers_by_alpha.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"  figure: {out}")
    return True


def main() -> None:
    out_dir = Path(__file__).resolve().parent / "figures"
    out_dir.mkdir(exist_ok=True)
    if len(sys.argv) > 1:
        paths = [Path(p) for pat in sys.argv[1:] for p in
                 (Path(".").glob(pat) if "*" in pat else [Path(pat)])]
    else:
        paths = []
        for path in sorted((LAB_DIR / "results").glob("*.json")):
            data = load_run(path)
            if data is not None and data["spec"].get("answer_format"):
                paths.append(path)
    if not paths:
        print("no choice-task results found (specs need answer_format)")
        return
    for p in paths:
        analyze(p, out_dir)


if __name__ == "__main__":
    main()
