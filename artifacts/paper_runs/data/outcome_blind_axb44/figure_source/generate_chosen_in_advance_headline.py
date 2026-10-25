"""Build the main-text figure for the concepts chosen before steering."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


HERE = Path(__file__).resolve().parent
ANALYSIS = HERE / "chosen_in_advance_analysis.json"
OUTPUT_PDF = HERE.parent / "fig_chosen_in_advance_headline.pdf"
OUTPUT_CSV = HERE / "fig_chosen_in_advance_headline_data.csv"

MODELS = ("qwen", "gemma")
MODEL_LABELS = {
    "qwen": "Qwen2.5-7B (chosen-in-advance 10)",
    "gemma": "Gemma-3-12B (chosen-in-advance 10)",
}
METHODS = (
    "unsteered",
    "caa",
    "diffmean",
    "nla",
    "axbench_prompt_steering",
)
METHOD_LABELS = {
    "unsteered": "Unsteered",
    "caa": "CAA",
    "diffmean": "DiffMean",
    "nla": "NLA",
    "axbench_prompt_steering": "Prompt",
}
COLORS = {
    "unsteered": "#9CA3AF",
    "caa": "#FBBF24",
    "diffmean": "#8B5CF6",
    "nla": "#2563EB",
    "axbench_prompt_steering": "#059669",
}
BOOTSTRAP_REPETITIONS = 20_000
BOOTSTRAP_SEED = 20260831


def load_data() -> dict:
    data = json.loads(ANALYSIS.read_text(encoding="utf-8"))
    if data.get("schema") != "axb44-heldout-analysis/v1":
        raise ValueError("unexpected analysis schema")
    if data.get("campaign_id") != "axb44-crossmodel-v1":
        raise ValueError("unexpected campaign")
    if set(data.get("models", {})) != set(MODELS):
        raise ValueError("expected Qwen and Gemma results")
    for model in MODELS:
        concepts = data["models"][model].get("per_concept_descriptive", {})
        if len(concepts) != 10:
            raise ValueError(f"expected 10 concepts for {model}")
        for concept in concepts.values():
            if set(METHODS) - set(concept.get("arms", {})):
                raise ValueError(f"missing method for {model}")
    return data


def concept_bootstrap(data: dict) -> dict[str, dict[str, tuple[float, float, float]]]:
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    n_concepts = 10
    draws = rng.integers(0, n_concepts, size=(BOOTSTRAP_REPETITIONS, n_concepts))
    results: dict[str, dict[str, tuple[float, float, float]]] = {}
    for model in MODELS:
        concepts = data["models"][model]["per_concept_descriptive"]
        concept_ids = sorted(concepts, key=int)
        results[model] = {}
        for method in METHODS:
            values = np.asarray(
                [
                    concepts[concept_id]["arms"][method]["mean_axbench_harmonic"]
                    for concept_id in concept_ids
                ],
                dtype=float,
            )
            observed = float(values.mean())
            expected = data["models"][model]["summaries"]["balanced_union"][method][
                "mean_axbench_harmonic"
            ]
            if not np.isclose(observed, expected, atol=1e-12):
                raise ValueError(f"mean mismatch for {model}/{method}")
            bootstrap_means = values[draws].mean(axis=1)
            low, high = np.quantile(bootstrap_means, [0.025, 0.975])
            results[model][method] = (observed, float(low), float(high))
    return results


def write_csv(results: dict[str, dict[str, tuple[float, float, float]]]) -> None:
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "selection",
                "model",
                "method",
                "mean_axbench_h",
                "ci95_low",
                "ci95_high",
                "bootstrap_unit",
                "bootstrap_repetitions",
                "bootstrap_seed",
            ]
        )
        for model in MODELS:
            for method in METHODS:
                mean, low, high = results[model][method]
                writer.writerow(
                    [
                        "chosen-in-advance 10",
                        model,
                        method,
                        f"{mean:.8f}",
                        f"{low:.8f}",
                        f"{high:.8f}",
                        "concept",
                        BOOTSTRAP_REPETITIONS,
                        BOOTSTRAP_SEED,
                    ]
                )


def build_figure(results: dict[str, dict[str, tuple[float, float, float]]]) -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 12,
            "axes.titlesize": 13,
            "axes.labelsize": 12,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.dpi": 150,
            "pdf.fonttype": 42,
        }
    )
    fig, axes = plt.subplots(1, 2, figsize=(12.0, 2.45), sharey=True)
    x = np.arange(len(METHODS))
    colors = [COLORS[method] for method in METHODS]
    labels = [METHOD_LABELS[method] for method in METHODS]

    for ax, model in zip(axes, MODELS):
        means = np.asarray([results[model][method][0] for method in METHODS])
        lows = np.asarray([results[model][method][1] for method in METHODS])
        highs = np.asarray([results[model][method][2] for method in METHODS])
        errors = np.vstack((means - lows, highs - means))
        bars = ax.bar(
            x,
            means,
            width=0.68,
            color=colors,
            edgecolor="white",
            linewidth=0.5,
            yerr=errors,
            error_kw={"ecolor": "#1F2937", "elinewidth": 1.0, "capsize": 2.5},
        )
        ax.bar_label(bars, labels=[f"{value:.2f}" for value in means], padding=2, fontsize=10)
        ax.set_xticks(x, labels)
        ax.set_title(MODEL_LABELS[model])
        ax.grid(axis="y", alpha=0.2, linewidth=0.6)
        ax.set_axisbelow(True)

    axes[0].set_ylabel("Mean judge H (0-2)")
    axes[0].set_ylim(0, 1.42)
    fig.suptitle("Concepts chosen in advance", fontsize=14, y=0.98)
    fig.subplots_adjust(left=0.08, right=0.995, bottom=0.18, top=0.73, wspace=0.08)
    fixed_timestamp = datetime(2026, 8, 31, tzinfo=timezone.utc)
    fig.savefig(
        OUTPUT_PDF,
        format="pdf",
        metadata={
            "Title": "Concepts chosen in advance",
            "Author": "NLA Steering study",
            "Creator": "generate_chosen_in_advance_headline.py",
            "CreationDate": fixed_timestamp,
            "ModDate": fixed_timestamp,
        },
    )
    plt.close(fig)


def main() -> None:
    data = load_data()
    results = concept_bootstrap(data)
    write_csv(results)
    build_figure(results)
    print(f"wrote {OUTPUT_PDF}")
    print(f"wrote {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
