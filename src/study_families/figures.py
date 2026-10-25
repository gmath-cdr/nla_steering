"""Rebuild paper-facing figures for the added study families.

The plots read only authenticated, archived data.  They are intentionally
separate from model generation and API judging so figure regeneration never
changes the scientific inputs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

from study_families.common import DATA_ROOT, load_json
from study_families.wordle import rebuild_summary


COLORS = {
    "unsteered": "#9CA3AF",
    "caa": "#FBBF24",
    "diffmean": "#8B5CF6",
    "nla": "#2563EB",
    "prompt": "#059669",
    "crane": "#9CA3AF",
    "home": "#2563EB",
    "house_or_homes": "#8B5CF6",
    "other": "#D1D5DB",
    "no_answer": "#374151",
}


def _plotting_modules() -> tuple[Any, Any]:
    """Import plotting dependencies only when figures are requested."""

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as error:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "figure generation needs the locked analysis dependencies; run "
            "`uv run --locked --extra analysis python run.py reproduce-studies --figures`"
        ) from error
    return plt, np


def _style(plt: Any) -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 11,
            "axes.labelsize": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.dpi": 150,
            "pdf.fonttype": 42,
        }
    )


def chosen_in_advance(output: Path) -> Path:
    """Run the frozen 20,000-concept-bootstrap figure implementation."""

    source = (
        DATA_ROOT
        / "outcome_blind_axb44"
        / "figure_source"
        / "generate_chosen_in_advance_headline.py"
    )
    spec = importlib.util.spec_from_file_location("chosen_in_advance_figure", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load figure generator: {source}")
    module = importlib.util.module_from_spec(spec)
    # The generator is an immutable artifact input. Suppress bytecode so a
    # read-only rebuild never creates __pycache__ inside the authenticated tree.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    module.OUTPUT_PDF = output
    module.OUTPUT_CSV = output.with_suffix(".csv")
    data = module.load_data()
    results = module.concept_bootstrap(data)
    module.write_csv(results)
    module.build_figure(results)
    return output


def emoji_effects(output: Path) -> Path:
    """Plot held-out emoji presence effects and their prompt ceiling."""

    plt, np = _plotting_modules()
    _style(plt)
    data = load_json(DATA_ROOT / "freeform_emoji" / "derived" / "analysis.json")
    model_keys = ("qwen", "gemma3-12b")
    model_labels = ("Qwen2.5-7B", "Gemma-3-12B")
    arms = ("caa", "nla_phrase", "nla_glyph", "explicit_prompt")
    labels = ("CAA", "NLA phrase", "NLA glyph", "Prompt")
    colors = (COLORS["caa"], "#60A5FA", COLORS["nla"], COLORS["prompt"])
    x = np.arange(len(arms), dtype=float)

    fig, axes = plt.subplots(1, 2, figsize=(8.4, 2.7), sharey=True)
    for axis, model_key, title in zip(axes, model_keys, model_labels):
        model = data["models"][model_key]
        means: list[float] = []
        low: list[float] = []
        high: list[float] = []
        for arm in arms:
            if arm == "explicit_prompt":
                value = float(model["absolute"][arm]["ordinary_optional"]["presence"])
                interval = (value, value)
            else:
                claim = model["claims"][arm]["tracks"]["ordinary_optional"]
                value = float(claim["effect_vs_alpha_zero"])
                interval = tuple(float(v) for v in claim["ci95_vs_alpha_zero"])
            means.append(value)
            low.append(interval[0])
            high.append(interval[1])
        # The archived effects are fractions. Plot percentage-point units so
        # the axis and the labels use the same scale as the paper tables.
        values = 100.0 * np.asarray(means)
        errors = 100.0 * np.vstack(
            (np.asarray(means) - np.asarray(low), np.asarray(high) - np.asarray(means))
        )
        bars = axis.bar(
            x,
            values,
            width=0.68,
            color=colors,
            yerr=errors,
            error_kw={"ecolor": "#1F2937", "elinewidth": 0.9, "capsize": 2.5},
        )
        axis.bar_label(bars, labels=[f"{value:.1f}" for value in values], fontsize=8)
        axis.set_xticks(x, labels, rotation=18, ha="right")
        axis.set_title(title)
        axis.grid(axis="y", alpha=0.2, linewidth=0.6)
        axis.set_axisbelow(True)
    axes[0].set_ylabel("Emoji presence change (pp)")
    axes[0].set_ylim(0, 113)
    fig.suptitle("Held-out ordinary prompts", y=0.99)
    fig.tight_layout()
    fig.savefig(output, bbox_inches="tight")
    plt.close(fig)
    return output


def emoji_calibration(output: Path) -> Path:
    """Plot calibration sweeps, including safety failures and locked alphas."""

    plt, _ = _plotting_modules()
    _style(plt)
    data = load_json(DATA_ROOT / "freeform_emoji" / "derived" / "analysis.json")
    models = (("qwen", "Qwen2.5-7B"), ("gemma3-12b", "Gemma-3-12B"))
    arms = (("caa", "CAA", COLORS["caa"]), ("nla_phrase", "NLA phrase", "#60A5FA"), ("nla_glyph", "NLA glyph", COLORS["nla"]))
    fig, axes = plt.subplots(1, 2, figsize=(8.4, 2.7), sharey=True)
    for axis, (model_key, title) in zip(axes, models):
        calibration = data["models"][model_key]["calibration"]
        for arm, label, color in arms:
            candidates = calibration[arm]["candidates"]
            xs = [float(row["alpha"]) for row in candidates]
            ys = [float(row["mean_paired_presence_effect"]) for row in candidates]
            axis.plot(xs, ys, marker="o", color=color, label=label, linewidth=1.6)
            for row in candidates:
                if not row["passes_safety"]:
                    axis.scatter(row["alpha"], row["mean_paired_presence_effect"], facecolors="none", edgecolors="#DC2626", s=65, linewidth=1.2, zorder=4)
            chosen = float(calibration[arm]["chosen_alpha"])
            chosen_row = next(row for row in candidates if float(row["alpha"]) == chosen)
            axis.scatter(chosen, chosen_row["mean_paired_presence_effect"], facecolors="none", edgecolors=color, s=110, linewidth=1.8, zorder=5)
        axis.set_title(title)
        axis.set_xlabel("Calibration alpha")
        axis.grid(alpha=0.2, linewidth=0.6)
        axis.set_axisbelow(True)
    axes[0].set_ylabel("Paired emoji-presence effect")
    axes[1].legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(output, bbox_inches="tight")
    plt.close(fig)
    return output


def wordle_dwelling(output: Path) -> Path:
    """Rebuild the cross-model dwelling-response ladder from raw completions."""

    plt, np = _plotting_modules()
    _style(plt)
    summary = rebuild_summary()
    panels = (
        (summary["qwen_natural_dwelling"]["figure_ladder"], "Qwen2.5-7B"),
        (summary["gemma_natural_dwelling"]["figure_ladder"], "Gemma-3-12B"),
    )
    categories = ("crane", "home", "house_or_homes", "other", "no_answer")
    labels = ("CRANE", "HOME", "HOUSE/HOMES", "Other", "No answer")
    fig, axes = plt.subplots(1, 2, figsize=(8.4, 2.75), sharey=True)
    for axis, (ladder, title) in zip(axes, panels):
        alphas = sorted((float(value) for value in ladder), key=float)
        x = np.arange(len(alphas))
        bottom = np.zeros(len(alphas))
        for category, label in zip(categories, labels):
            values = np.asarray([ladder[str(alpha)][category] for alpha in alphas])
            axis.bar(x, values, bottom=bottom, label=label, color=COLORS[category], width=0.72)
            bottom += values
        axis.set_xticks(x, [f"{value:g}" for value in alphas])
        axis.set_title(title)
        axis.set_xlabel("Alpha")
        axis.grid(axis="y", alpha=0.2, linewidth=0.6)
        axis.set_axisbelow(True)
    axes[0].set_ylabel("Responses (60 per alpha)")
    handles, legend_labels = axes[1].get_legend_handles_labels()
    fig.legend(
        handles,
        legend_labels,
        frameon=False,
        fontsize=7,
        ncol=5,
        loc="lower center",
        bbox_to_anchor=(0.5, -0.01),
    )
    fig.suptitle("Dwelling contrast on natural Wordle prompts", y=0.99)
    fig.tight_layout(rect=(0, 0.13, 1, 0.95))
    fig.savefig(output, bbox_inches="tight")
    plt.close(fig)
    return output


def build_all(output_dir: Path) -> list[Path]:
    """Build every added study figure into a disposable output directory."""

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = (
        chosen_in_advance(output_dir / "fig_chosen_in_advance_headline.pdf"),
        emoji_effects(output_dir / "fig_emoji_freeform.pdf"),
        emoji_calibration(output_dir / "fig_emoji_dose_response.pdf"),
        wordle_dwelling(output_dir / "fig_wordle_dwelling.png"),
    )
    return list(outputs)
