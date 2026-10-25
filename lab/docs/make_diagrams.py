"""Regenerate the README diagrams: workflow.png and experiment_design.png.

    python docs/make_diagrams.py

Pure matplotlib, laptop-side, no GPU or extra packages. Edit and rerun
whenever the lab's shape changes so the pictures never drift from the code.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

OUT = Path(__file__).resolve().parent

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"      # code we run
AQUA = "#1baf7a"      # data artifacts
ORANGE = "#eb6834"    # external / billed services
ZONE_PLAIN = "#f5f5f2"
ZONE_EXT = "#fdf3ee"

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "DejaVu Sans"],
})


def canvas(w=140, h=82):
    fig, ax = plt.subplots(figsize=(14, 8.2))
    fig.patch.set_facecolor(SURFACE)
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.set_facecolor(SURFACE)
    ax.axis("off")
    return fig, ax


def zone(ax, x, y, w, h, title="", external=False):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.6,rounding_size=1.5",
        facecolor=ZONE_EXT if external else ZONE_PLAIN,
        edgecolor=ORANGE if external else AXIS, linewidth=1.3, zorder=0))
    ax.text(x + 1.6, y + h - 2.6, title, fontsize=9, fontweight="bold",
            color=ORANGE if external else INK_2, zorder=3)


def box(ax, x, y, w, h, title, body="", border=BLUE):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.4,rounding_size=1.0",
        facecolor="white", edgecolor=border, linewidth=1.6, zorder=2))
    if body:
        ax.text(x + w / 2, y + h - 1.4, title, fontsize=7.3,
                fontweight="bold", color=INK, ha="center", va="top", zorder=3)
        ax.text(x + w / 2, y + h - 4.6, body, fontsize=6.6, color=INK_2,
                ha="center", va="top", linespacing=1.35, zorder=3)
    else:
        ax.text(x + w / 2, y + h / 2, title, fontsize=7.3, fontweight="bold",
                color=INK, ha="center", va="center", zorder=3)


def arrow(ax, start, end, label="", color=MUTED, lx=0, ly=0, fontsize=6.6):
    ax.annotate("", xy=end, xytext=start, zorder=1,
                arrowprops=dict(arrowstyle="-|>", color=color, linewidth=1.5,
                                shrinkA=3, shrinkB=3,
                                mutation_scale=13))
    if label:
        mx, my = (start[0] + end[0]) / 2 + lx, (start[1] + end[1]) / 2 + ly
        ax.text(mx, my, label, fontsize=fontsize, color=INK_2, ha="center",
                va="center", zorder=3,
                bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1))


def workflow():
    fig, ax = canvas()
    ax.text(1, 79.5, "NLA steering lab: how a run travels, end to end",
            fontsize=13, fontweight="bold", color=INK)

    # ── zones ────────────────────────────────────────────────────────────
    zone(ax, 1, 2, 45, 74, "LAPTOP  (no GPU, free)")
    zone(ax, 49, 26, 22, 34, "GITHUB", external=True)
    zone(ax, 74, 2, 65, 74, "RUNPOD  (billed)", external=True)

    # ── laptop ───────────────────────────────────────────────────────────
    box(ax, 4, 62, 39, 10, "experiment_matrix.csv  +  03 RUN PLAYBOOK",
        "the 34-row plan, sample sizes, locked decisions.\nDecides what runs next (CSV synced into lab/).",
        border=AQUA)
    box(ax, 4, 46, 39, 10, "experiments/specs/*.yaml",
        "one YAML per experiment: direction sentences,\nprompts, alphas, probes  (census = no direction;\ncontrol = direction: random)",
        border=AQUA)
    box(ax, 4, 22, 39, 10, "analysis/plots.py",
        "reads every results JSON, aggregates across\nprompts, applies the coherence floor")
    box(ax, 4, 5, 39, 10, "analysis/figures/  +  summary.csv",
        "dose-response, census shapes, gradient,\ndiagnostics scatter; one summary row per run",
        border=AQUA)
    arrow(ax, (23.5, 62), (23.5, 56.6), "pick the next experiment")
    arrow(ax, (23.5, 22), (23.5, 15.6))
    arrow(ax, (4, 10), (2.8, 10))  # loop back up the left edge
    arrow(ax, (2.8, 10), (2.8, 67), color=AXIS)
    arrow(ax, (2.8, 67), (4, 67), color=AXIS)
    ax.text(2.0, 38.5, "findings update the matrix", rotation=90,
            fontsize=6.6, color=INK_2, ha="center", va="center",
            bbox=dict(facecolor=ZONE_PLAIN, edgecolor="none", pad=1))

    # ── github ───────────────────────────────────────────────────────────
    box(ax, 51, 38, 18, 14, "nla-steering-effects",
        "extended experiment\nbranch only.\nCode, specs and\nresults all travel\nthrough here.",
        border=ORANGE)
    arrow(ax, (43, 51), (51, 47), "git push specs + code", ly=2.4)
    arrow(ax, (51, 41), (43, 29), "git pull results/*.json", lx=-1, ly=3.2)

    # ── runpod: volume + huggingface ─────────────────────────────────────
    box(ax, 77, 62, 36, 11, "Network volume  100 GB  (~$7/mo)",
        "Qwen base 15 GB + verbalizer 16 GB +\nreconstructor 10 GB + repo clone + results",
        border=ORANGE)
    box(ax, 117, 62, 19, 11, "Hugging Face",
        "the 3 released\ncheckpoints", border=ORANGE)
    arrow(ax, (117, 67.5), (113, 67.5), "setup_pod.sh,\nonce ever",
          ly=-4.2, lx=2)

    # ── runpod: the pod pipeline ─────────────────────────────────────────
    zone(ax, 77, 4, 59, 54.5, "")
    ax.text(78.6, 55.5, "A100 80GB pod  (~$1.40/hr)   tools/auto_stop.py "
            "stops it after 45 idle min", fontsize=7.6, fontweight="bold",
            color=INK_2)
    box(ax, 80, 46, 53, 7.5, "tools/session_start.sh  ->  smoke_test.py",
        "every session starts here; hard gate on the plumbing")
    box(ax, 80, 36, 53, 8, "experiments/run.py",
        "reads one spec; logs direction diagnostics (echo + delta-norm\nratio) BEFORE the sweep; warns if the edit died in the bottleneck")
    box(ax, 80, 25.5, 53, 8.5, "nla_lab/session.py   (+ vendor_nla.py, verbatim)",
        "loads the models, builds direction = AR(pos) - AR(neg),\ngenerates with h -> h + alpha * ||h|| * direction at layer 20")
    box(ax, 80, 16, 53, 7.5, "nla_lab/probes.py",
        "21 deterministic counters score every output; no judge, ever")
    box(ax, 80, 6, 53, 7.8, "results/<name>_<timestamp>.json",
        "spec + diagnostics + every raw output + every probe score:\nthe experiment IS this file", border=AQUA)
    arrow(ax, (106.5, 46), (106.5, 44.6))
    arrow(ax, (106.5, 36), (106.5, 34.6))
    arrow(ax, (106.5, 25.5), (106.5, 24.1))
    arrow(ax, (106.5, 16), (106.5, 14.4))
    arrow(ax, (95, 62), (95, 54.1), "weights", lx=4.5)
    arrow(ax, (69, 45), (80, 49.8), "git pull", ly=2)
    arrow(ax, (80, 9.5), (69, 39), "git commit + push results", lx=1.5, ly=-4)

    # ── legend ───────────────────────────────────────────────────────────
    for x, color, label in ((4, BLUE, "code you run"),
                            (24, AQUA, "files / data"),
                            (44, ORANGE, "external service (costs money)")):
        ax.add_patch(FancyBboxPatch((x, 0.6), 2.6, 1.6,
                     boxstyle="round,pad=0.2", facecolor="white",
                     edgecolor=color, linewidth=1.6))
        ax.text(x + 3.6, 1.4, label, fontsize=7, color=INK_2, va="center")

    fig.savefig(OUT / "workflow.png", dpi=170, bbox_inches="tight")
    plt.close(fig)


def experiment_design():
    fig, ax = canvas()
    ax.text(1, 79.5, "NLA steering: the experiment design",
            fontsize=13, fontweight="bold", color=INK)

    # ── panel A: the tier ladder ─────────────────────────────────────────
    zone(ax, 1, 2, 44, 74)
    ax.text(2.6, 74.0, "THE COMPLEXITY GRADIENT (the hypothesis)",
            fontsize=8.6, fontweight="bold", color=INK_2)
    ax.text(2.6, 71.7, "steerability should DECLINE going up.  * = wave 1",
            fontsize=6.8, color=MUTED)
    tiers = [
        ("T1  surface form", "emoji*, ellipsis, exclaim, parens, caps,\ndigits, bold, lists, em-dash swap"),
        ("T2  lexical register", "archaic (Shakespeare)*, contractions,\nverbosity, hedging, sentence length,\n1st/2nd person, alliteration"),
        ("T3  lexical plans", "rhyme repro*, wordle-away*, wordle-letter,\nwordle-word, number, color, first word,\ntopic, end-with-question"),
        ("T4  structure / strategy", "compose, graded, passive,\nnoun capitalization, direction interpolation"),
        ("T5  stance / belief", "self-reference, eval-awareness,\nrefusal (SAFETY-GATED: team + mentor)"),
    ]
    y = 4.5
    heights = [12, 13.5, 13.5, 11, 10.5]
    for (title, body), h in zip(tiers, heights):
        box(ax, 6.5, y, 35, h, title, body)
        y += h + 1.3
    arrow(ax, (4.2, 8), (4.2, 66), color=AXIS)
    ax.text(3.0, 37, "harder to steer (predicted)", rotation=90, fontsize=7,
            color=MUTED, va="center")

    # ── panel B: the waves ───────────────────────────────────────────────
    zone(ax, 48, 50, 91, 26)
    ax.text(49.6, 72.6, "THE WAVES (order is a rule, not a preference)",
            fontsize=8.6, fontweight="bold", color=INK_2)
    box(ax, 51, 53, 26, 15.5, "Wave 0: baseline census",
        "no steering. 90 outputs, all 21\nprobes + choice histograms.\nEvery baseline_shape cell\nbecomes measured fact. ~$0.50")
    box(ax, 81, 53, 26, 15.5, "Wave 1: first gradient sketch",
        "one per tier bracket:\nT1-emoji, T2-archaic,\nT3-rhyme, T3-wordle-away.\n~$1 per experiment")
    box(ax, 111, 53, 25, 15.5, "Wave 2+",
        "fill the matrix, CAA rival\nvectors on shared concepts,\nrobustness + transfer axes")
    arrow(ax, (77, 60.7), (81, 60.7))
    arrow(ax, (107, 60.7), (111, 60.7))
    ax.text(51, 50.9, "rule: no result is ever read against an assumed "
            "baseline; the census runs first", fontsize=6.8, color=MUTED)

    # ── panel C: life of one experiment ──────────────────────────────────
    zone(ax, 48, 2, 91, 44)
    ax.text(49.6, 44.2, "LIFE OF ONE EXPERIMENT", fontsize=8.6,
            fontweight="bold", color=INK_2)
    r1y, r2y, bw, bh = 28, 6.5, 28, 11.5
    box(ax, 51, r1y, bw, bh, "1. Write the two sentences",
        "reconstructor dialect:\ndescriptions of text, never\ninstructions to the model")
    box(ax, 81.5, r1y, bw, bh, "2. Bottleneck gate (cheap)",
        "echo() both sentences +\ndelta-norm ratio. Did the\nconcept survive? (< 0.15 = dead)")
    box(ax, 111, r1y, bw, bh, "3. Calibration sweep",
        "60 generations across the\nalpha ladder; find where it\nworks and where text degrades")
    box(ax, 111, r2y, bw, bh, "4. Freeze alpha, confirm",
        "10 prompts x 5 seeds x\n{baseline, steered, random\ncontrol at matched alpha}")
    box(ax, 81.5, r2y, bw, bh, "5. Classify the outcome",
        "works  /  arrived-but-ignored\n/  died-in-bottleneck\n(only 'ignored' blames layer 20)")
    box(ax, 51, r2y, bw, bh, "6. One point on the gradient",
        "Cliff's delta at frozen alpha,\ncoherence checked, plotted\nby tier in gradient.png")
    arrow(ax, (79, r1y + bh / 2), (81.5, r1y + bh / 2))
    arrow(ax, (109.5, r1y + bh / 2), (111, r1y + bh / 2))
    arrow(ax, (125, r1y), (125, r2y + bh))
    arrow(ax, (111, r2y + bh / 2), (109.5, r2y + bh / 2))
    arrow(ax, (81.5, r2y + bh / 2), (79, r2y + bh / 2))
    arrow(ax, (95, r1y + bh + 1.3), (65, r1y + bh + 1.3), color=ORANGE)
    ax.text(80, r1y + bh + 2.7, "died: fix sentences, retry (no sweep wasted)",
            fontsize=6.4, color=ORANGE, ha="center",
            bbox=dict(facecolor=ZONE_PLAIN, edgecolor="none", pad=1))

    fig.savefig(OUT / "experiment_design.png", dpi=170, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    workflow()
    experiment_design()
    print(f"wrote workflow.png and experiment_design.png to {OUT}")
