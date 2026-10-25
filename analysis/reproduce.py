#!/usr/bin/env python3
"""Rebuild paper-facing tables, figures, and regression metrics from saved runs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
from collections import Counter
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "artifacts/paper_runs/data"
DEFAULT_OUT = ROOT / "artifacts/rebuilt/paper"
EXPECTED = ROOT / "analysis/expected_results.json"
TABLE_NAMES = (
    "tab_panel_full",
    "tab_gemma_independent",
    "tab_discovery500_summary",
    "tab_confirmation30_all",
    "tab_unembedding_per_concept",
    "tab_sae_sweeps",
    "tab_reft_panel14",
    "tab_axbench_rescore",
    "tab_position",
    "tab_reframing",
    "tab_factual_three_arm",
    "tab_gemma10_quality",
    "tab_gemma_six",
)
TABLE_TITLES = {
    "tab_panel_full": "Panel-14 method comparison",
    "tab_gemma_independent": "Independent cross-model replication",
    "tab_discovery500_summary": "Qwen 500-concept discovery summary",
    "tab_confirmation30_all": "Qwen 30-concept confirmation",
    "tab_unembedding_per_concept": "Unembedding projection by concept",
    "tab_sae_sweeps": "SAE coefficient sweeps",
    "tab_reft_panel14": "LoReFT panel-14 results",
    "tab_axbench_rescore": "AxBench LM-judge re-score",
    "tab_position": "Injection-position controls",
    "tab_reframing": "Edit-reframing diagnostic",
    "tab_factual_three_arm": "Three-arm factuality audit",
    "tab_gemma10_quality": "Gemma panel-10 quality checks",
    "tab_gemma_six": "Gemma six-concept sweep",
}


def load(relative: str):
    return json.loads((DATA / relative).read_text())


def pct(value: float) -> float:
    return 100.0 * value


def slug(value: object) -> str:
    return str(value).lower().replace(" ", "_").replace("-", "_")


def table(out: Path, name: str, rows: list[dict]) -> None:
    if not rows:
        raise ValueError(f"empty table: {name}")
    destination = out / "tables"
    destination.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0])
    with (destination / f"{name}.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    lines = [f"# {name}", "", "| " + " | ".join(columns) + " |",
             "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row[column]) for column in columns) + " |")
    (destination / f"{name}.md").write_text("\n".join(lines) + "\n")


def panel_rows() -> list[dict]:
    combined = load("baseline_panel14/combined_summary.json")
    indexed = {(row["model"], row["method"]): row for row in combined}
    prompting = {
        "qwen": load("qwen2.5-7b/prompting_baseline_2026-08-15/quality_matched_unsteered_summary.json"),
        "gemma": load("gemma3-12b/prompting_baseline_2026-08-15/quality_matched_unsteered_summary.json"),
    }
    prompt_results = {
        "qwen": load("qwen2.5-7b/prompting_baseline_2026-08-15/results_matched_unsteered.json"),
        "gemma": load("gemma3-12b/prompting_baseline_2026-08-15/results_matched_unsteered.json"),
    }
    methods = [
        ("Unsteered", "unsteered"), ("Strongest random", "strongest_random"),
        ("Fixed CAA", "caa_fixed"), ("Calibrated CAA", "caa_calibrated"),
        ("DiffMean", "diffmean"), ("NLA steering", "nla_av_edit"),
    ]
    rows: list[dict] = []
    for model in ("qwen", "gemma"):
        for label, key in methods:
            row = indexed[(model, key)]
            rows.append({"model": model, "method": label, "n": row["n"],
                         "target_word_pct": round(pct(row["hit_rate"]), 3),
                         "coverage_pct": round(pct(row.get("prompt_coverage") or 0), 3),
                         "quality_pct": round(pct(row["quality_pass_rate"]), 3)})
        records = prompt_results[model]
        rows.append({"model": model, "method": "Direct prompting", "n": len(records),
                     "target_word_pct": round(pct(mean(bool(r["hit"]) for r in records)), 3),
                     "coverage_pct": round(pct(len({(r["concept_id"], r["prompt_index"]) for r in records if r["hit"]}) /
                                                   len({(r["concept_id"], r["prompt_index"]) for r in records})), 3),
                     "quality_pct": round(pct(prompting[model]["rescored"]["quality_pass_rate"]), 3)})
    return rows


def independent_rows() -> list[dict]:
    payload = load("final_gemma_replication_2026/summary.json")
    return [{"model": row["model"], "panel": row["panel"], "method": row["method"],
             "hit_pct": round(pct(row["strict_hit_rate"]), 3),
             "ci_low_pct": round(pct(row["ci_low"]), 3),
             "ci_high_pct": round(pct(row["ci_high"]), 3)} for row in payload["rows"]]


def discovery_rows() -> tuple[list[dict], dict[str, float]]:
    nla = load("qwen2.5-7b/av_edit_discovery500_2026-08-11/summary.json")["concepts"]
    diff = load("qwen2.5-7b/diffmean_discovery500_2026-08-15_merged/summary.json")["concepts"]
    counts = Counter(row["failure_cause"] for row in nla)
    rows = [{"classification": key, "concepts": value} for key, value in sorted(counts.items())]
    metrics = {
        "discovery.qwen.total": float(len(nla)),
        "discovery.qwen.candidates": float(sum(bool(r["discovery_candidate"]) for r in nla)),
        "discovery.diffmean.candidates": float(sum(bool(r["discovery_candidate"]) for r in diff)),
    }
    return rows, metrics


def confirmation_rows() -> tuple[list[dict], dict[str, float]]:
    payload = load("qwen2.5-7b/av_edit_confirmation30_2026-08-11/confirmation_summary.json")
    rows = []
    for item in payload["concepts"]:
        av = item["arms"]["av_edit"]
        rows.append({"concept_id": item["concept_id"], "av_hit_pct": round(pct(av["hit_rate"]), 3),
                     "coverage_pct": round(pct(item["prompt_coverage"]), 3),
                     "quality_pct": round(pct(av["quality_pass_rate"]), 3),
                     "confirmed": bool(item["confirmed"])})
    return rows, {"confirmation.qwen.confirmed": float(payload["confirmed_count"]),
                  "confirmation.qwen.tested": float(len(rows))}


def unembedding_rows() -> tuple[list[dict], dict[str, float]]:
    rows, metrics = [], {}
    for model, path in (
        ("qwen", "qwen2.5-7b/unembedding_projection_2026-08-15/unembedding_projection.json"),
        ("gemma", "gemma3-12b/unembedding_projection_2026-08-15/unembedding_projection.json"),
    ):
        payload = load(path)
        metrics[f"unembedding.{model}.mean_lift"] = payload["mean_percentile_lift"]
        for item in payload["concepts"]:
            rows.append({"model": model, "concept_id": item["concept_id"],
                         "percentile_lift": round(item["percentile_lift"], 6)})
    return rows, metrics


def sae_rows() -> tuple[list[dict], dict[str, float]]:
    rows, metrics = [], {}
    for model, path in (
        ("qwen", "qwen2.5-7b/sae_alpha_sweep_2026-08-16/sweep_results.json"),
        ("gemma", "gemma3-12b/sae_alpha_sweep_2026-08-16/sweep_results.json"),
        ("gemma10", "gemma3-12b/sae_alpha_sweep_gemma10_2026/sweep_results.json"),
    ):
        values = load(path)
        maximum = 0.0
        for concept in values:
            for alpha, item in concept["by_alpha"].items():
                maximum = max(maximum, item["hit_rate"])
                rows.append({"model": model, "concept_id": concept["concept_id"],
                             "alpha": float(alpha), "n": item["n"],
                             "hit_pct": round(pct(item["hit_rate"]), 3)})
        metrics[f"sae.{model}.maximum_hit"] = maximum
    return rows, metrics


def reft_rows() -> tuple[list[dict], dict[str, float]]:
    rows, metrics = [], {}
    for model, path in (
        ("qwen", "qwen2.5-7b/reft_baseline_2026-08-15/summary.json"),
        ("gemma", "gemma3-12b/reft_baseline_2026-08-15/summary.json"),
    ):
        concepts = load(path)["concepts"]
        candidate_count = 0
        for item in concepts:
            candidate = bool(item.get("candidate", item.get("discovery_candidate", False)))
            candidate_count += candidate
            rows.append({"model": model, "concept_id": item["concept_id"],
                         "unsteered_pct": round(pct(item["arms"]["unsteered"]["hit_rate"]), 3),
                         "reft_pct": round(pct(item["arms"]["reft"]["hit_rate"]), 3),
                         "candidate": candidate})
        metrics[f"reft.{model}.candidate_count"] = float(candidate_count)
    return rows, metrics


def judge_rows() -> tuple[list[dict], dict[str, float]]:
    rows, metrics = [], {}
    for model, path in (
        ("qwen", "qwen2.5-7b/av_edit_confirmation30_2026-08-11/axbench_lm_judge_rescore.summary.json"),
        ("gemma", "gemma3-12b/av_edit_confirmation30_2026_merged/axbench_lm_judge_rescore.summary.json"),
    ):
        for item in load(path)["arms"]:
            rows.append({"model": model, "arm": item["arm"], "n": item["n"],
                         "concept": round(item["mean_concept_relevance"], 6),
                         "instruction": round(item["mean_instruction_relevance"], 6),
                         "fluency": round(item["mean_fluency"], 6)})
            for metric in ("mean_concept_relevance", "mean_instruction_relevance", "mean_fluency"):
                metrics[f"judge.{model}.{item['arm']}.{metric}"] = item[metric]
    return rows, metrics


def diagnostic_rows() -> tuple[list[dict], list[dict], dict[str, float]]:
    payload = load("cross_model_extended_2026-08-08/summary_metrics.json")
    positions, reframing, metrics = [], [], {}
    for model in ("qwen", "gemma"):
        sets = {
            "constant": payload[model]["turn_constant_per_token"],
            "last": payload[model]["turn_last_calibration"],
            "energy": payload[model]["turn_energy_matched"],
        }
        for scheme, values in sets.items():
            for item in values:
                if item["label"] == "unsteered" or item["label"].startswith("nla__"):
                    positions.append({"model": model, "scheme": scheme, "condition": item["label"],
                                      "n": item["n_samples"], "hit_pct": round(pct(item["strict_hit_rate"]), 3)})
                    metrics[f"position.{model}.{scheme}.{item['label']}"] = item["strict_hit_rate"]
        for item in payload[model]["reframing"]:
            if item["label"].startswith(("lexical__", "abstract__", "behavioral__")):
                reframing.append({"model": model, "condition": item["label"],
                                  "n": item["n_samples"], "hit_pct": round(pct(item["strict_hit_rate"]), 3)})
                metrics[f"reframing.{model}.{item['label'].split('__')[0]}"] = item["strict_hit_rate"]
    return positions, reframing, metrics


def factual_rows() -> tuple[list[dict], dict[str, float]]:
    summary = load("baseline_panel14/reliability_v1/factuality_three_arm.json")["summary"]
    rows, metrics = [], {"factual.av_minus_unsteered": summary["av_minus_unsteered_factual"]}
    for arm, item in summary["arms"].items():
        row = {"arm": arm, **item}
        rows.append(row)
        for key, value in item.items():
            if key != "n": metrics[f"factual.{arm}.{key}"] = value
    return rows, metrics


def gemma_quality_rows() -> tuple[list[dict], dict[str, float]]:
    # Use the retroactively corrected prompting score, matched to the same
    # unsteered completions by (concept_id, prompt_index).  The legacy summary
    # beside it contains the original unmatched prompting score.
    methods = load("gemma3-12b/baseline_gemma10_2026/unified_quality_matched_unsteered.json")["methods"]
    rows, metrics = [], {}
    for method, item in methods.items():
        rows.append({"method": method, "n": item["n"],
                     "quality_pct": round(pct(item["quality_pass_rate"]), 3)})
        metrics[f"gemma10.quality.{method}"] = item["quality_pass_rate"]
    return rows, metrics


def gemma_six_rows() -> tuple[list[dict], dict[str, float]]:
    payload = load("cross_model_extended_2026-08-08/gemma/results/axbench_diverse6_strict_metrics.json")
    rows, metrics = [], {}
    for concept_id, items in payload.items():
        for alpha in (0.1, 0.2, 0.3):
            tag = f"c{concept_id}__ar_delta__ratio_{alpha}"
            item = next(row for row in items if row["label"] == tag)
            rows.append({"concept_id": int(concept_id), "alpha": alpha,
                         "n": item["n_samples"], "hit_pct": round(pct(item["strict_hit_rate"]), 3)})
            metrics[f"gemma6.c{concept_id}.alpha_{alpha}"] = item["strict_hit_rate"]
    return rows, metrics


def reverse_metrics() -> dict[str, float]:
    qwen = load("qwen2.5-7b/av_edit_confirm_diffmean_top6_2026/confirmation_summary_retro.json")
    gemma = load("gemma3-12b/av_edit_confirm_diffmean_top6_gemma_2026/confirmation_summary.json")
    # The retroactive Qwen summary predates the explicit ``confirmed_count``
    # field, but contains the equivalent frozen ID list.
    qwen_count = qwen.get("confirmed_count", len(qwen["confirmed_concept_ids"]))
    return {"reverse.qwen.confirmed": float(qwen_count),
            "reverse.gemma.confirmed": float(gemma["confirmed_count"])}


def assemble(out: Path, selected_tables: set[str] | None = None) -> tuple[dict[str, list[dict]], dict[str, float]]:
    tables: dict[str, list[dict]] = {}
    metrics: dict[str, float] = {}
    tables["tab_panel_full"] = panel_rows()
    tables["tab_gemma_independent"] = independent_rows()
    tables["tab_discovery500_summary"], found = discovery_rows(); metrics.update(found)
    tables["tab_confirmation30_all"], found = confirmation_rows(); metrics.update(found)
    tables["tab_unembedding_per_concept"], found = unembedding_rows(); metrics.update(found)
    tables["tab_sae_sweeps"], found = sae_rows(); metrics.update(found)
    tables["tab_reft_panel14"], found = reft_rows(); metrics.update(found)
    tables["tab_axbench_rescore"], found = judge_rows(); metrics.update(found)
    tables["tab_position"], tables["tab_reframing"], found = diagnostic_rows(); metrics.update(found)
    tables["tab_factual_three_arm"], found = factual_rows(); metrics.update(found)
    tables["tab_gemma10_quality"], found = gemma_quality_rows(); metrics.update(found)
    tables["tab_gemma_six"], found = gemma_six_rows(); metrics.update(found)
    metrics.update(reverse_metrics())
    for row in tables["tab_panel_full"]:
        prefix = f"panel14.{row['model']}.{slug(row['method'])}"
        metrics[f"{prefix}.hit"] = row["target_word_pct"] / 100
        metrics[f"{prefix}.coverage"] = row["coverage_pct"] / 100
        metrics[f"{prefix}.quality"] = row["quality_pct"] / 100
    for row in tables["tab_gemma_independent"]:
        metrics[f"independent.{slug(row['model'])}.{slug(row['method'])}.hit"] = row["hit_pct"] / 100
    for name, rows in tables.items():
        metrics[f"table.{name}.rows"] = float(len(rows))
        if selected_tables is None or name in selected_tables:
            table(out, name, rows)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(dict(sorted(metrics.items())), indent=2) + "\n")
    return tables, metrics


def assert_expected(metrics: dict[str, float]) -> None:
    expected = json.loads(EXPECTED.read_text())
    failures = []
    unexpected = sorted(set(metrics) - set(expected["metrics"]))
    if unexpected:
        failures.append(
            "unregistered computed metrics: " + ", ".join(unexpected[:10])
        )
    for key, specification in expected["metrics"].items():
        if key not in metrics:
            failures.append(f"missing metric: {key}")
            continue
        if isinstance(specification, dict):
            value = specification["value"]
            tolerance = specification.get("tolerance", expected["default_tolerance"])
        else:
            value = specification
            tolerance = expected["default_tolerance"]
        if not math.isclose(metrics[key], value, rel_tol=0, abs_tol=tolerance):
            failures.append(f"{key}: got {metrics[key]}, expected {value} ± {tolerance}")
    if failures:
        raise SystemExit("paper result assertion failures:\n" + "\n".join(failures))
    print(f"asserted {len(expected['metrics'])} paper metrics")


def prepare_output(path: Path) -> None:
    """Prepare output without recursively deleting an arbitrary user path."""

    resolved = path.resolve()
    rebuilt_root = (ROOT / "artifacts" / "rebuilt").resolve()
    if resolved == ROOT.resolve() or resolved == Path(resolved.anchor):
        raise SystemExit(f"refusing unsafe output path: {resolved}")
    if resolved == rebuilt_root or rebuilt_root in resolved.parents:
        if resolved.exists():
            shutil.rmtree(resolved)
        return
    # Self-tests use a fresh external temporary directory. Accept it only while
    # empty, and never erase an arbitrary non-repository directory.
    if resolved.exists() and any(resolved.iterdir()):
        raise SystemExit(
            f"external output directory must be empty; refusing to delete: {resolved}"
        )


def pyplot():
    try:
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise SystemExit(
            "PNG rendering requires the locked analysis environment: "
            "uv run --locked --extra analysis ./run.py reproduce"
        ) from error
    return plt


def table_images(out: Path, tables: dict[str, list[dict]], selected: set[str] | None = None) -> None:
    """Render review-friendly PNG copies while retaining CSV as canonical data."""
    plt = pyplot()
    destination = out / "tables"
    destination.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        if selected is not None and name not in selected:
            continue
        columns = list(rows[0])
        def label(column: str) -> str:
            if column.endswith("_pct"):
                return column[:-4].replace("_", " ").title() + " (%)"
            return column.replace("_", " ").title()

        def display(column: str, value: object) -> str:
            aliases = {
                "qwen": "Qwen2.5-7B", "gemma": "Gemma-3-12B",
                "gemma10": "Gemma panel-10", "av_edit": "NLA AV edit",
                "nla_av_edit": "NLA AV edit", "random_17": "Random (17)",
                "random_42": "Random (42)", "random_101": "Random (101)",
            }
            text = str(value)
            if text in aliases:
                return aliases[text]
            if column in {"arm", "condition", "classification"}:
                return text.replace("__", ": ").replace("_", " ")
            if isinstance(value, bool):
                return "Yes" if value else "No"
            return text

        labels = [label(column) for column in columns]
        cells = [[display(column, row[column]) for column in columns] for row in rows]
        lengths = [max(len(labels[index]), *(len(row[index]) for row in cells)) for index in range(len(columns))]
        raw_widths = [min(30, max(7, length)) for length in lengths]
        width_total = sum(raw_widths)
        column_widths = [width / width_total for width in raw_widths]
        figure_width = min(18.0, max(8.0, width_total * 0.115))
        figure_height = max(2.2, 0.255 * (len(rows) + 2))
        fig, ax = plt.subplots(figsize=(figure_width, figure_height))
        fig.subplots_adjust(left=0.01, right=0.99, bottom=0.01, top=0.985)
        ax.axis("off")
        rendered = ax.table(
            cellText=cells,
            colLabels=labels,
            cellLoc="center",
            colLoc="center",
            colWidths=column_widths,
            bbox=[0, 0, 1, 0.975],
        )
        rendered.auto_set_font_size(False)
        rendered.set_fontsize(7)
        for (row_index, _), cell in rendered.get_celld().items():
            cell.set_edgecolor("#cbd5e1")
            cell.set_linewidth(0.45)
            if row_index == 0:
                cell.set_facecolor("#0f766e")
                cell.set_text_props(color="white", weight="bold")
            elif row_index % 2 == 0:
                cell.set_facecolor("#f1f5f9")
        ax.text(0.5, 0.995, TABLE_TITLES[name], transform=ax.transAxes,
                ha="center", va="bottom", fontsize=11, weight="bold")
        fig.savefig(destination / f"{name}.png", dpi=180, bbox_inches="tight",
                    facecolor="white", pad_inches=0.12)
        plt.close(fig)
    print(f"generated {len(list(destination.glob('*.png')))} table images")


def figures(out: Path, tables: dict[str, list[dict]]) -> None:
    plt = pyplot()
    destination = out / "figures"
    destination.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"figure.dpi": 140, "font.size": 8, "axes.spines.top": False,
                         "axes.spines.right": False})

    def save(fig, name):
        fig.tight_layout()
        fig.savefig(destination / f"{name}.png", dpi=220, bbox_inches="tight",
                    facecolor="white", pad_inches=0.08)
        plt.close(fig)

    panel = tables["tab_panel_full"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8), sharey=True)
    for ax, model in zip(axes, ("qwen", "gemma")):
        rows = [r for r in panel if r["model"] == model]
        ax.bar(range(len(rows)), [r["target_word_pct"] for r in rows], color="#2563eb")
        ax.set_xticks(range(len(rows)), [r["method"] for r in rows], rotation=45, ha="right")
        ax.set_title(model.capitalize()); ax.set_ylabel("Responses with concept words (%)")
    save(fig, "fig_panel_main")

    independent = tables["tab_gemma_independent"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8), sharey=True)
    for ax, model in zip(axes, ("Qwen2.5-7B", "Gemma-3-12B")):
        rows = [r for r in independent if r["model"] == model]
        values = [r["hit_pct"] for r in rows]
        errors = [
            [value - row["ci_low_pct"] for value, row in zip(values, rows)],
            [row["ci_high_pct"] - value for value, row in zip(values, rows)],
        ]
        ax.bar(range(len(rows)), values, yerr=errors, capsize=2.5,
               color="#059669", error_kw={"elinewidth": 0.8})
        ax.set_xticks(range(len(rows)), [r["method"] for r in rows], rotation=45, ha="right")
        ax.set_title(model); ax.set_ylabel("Strict hit rate (%)")
    save(fig, "fig_gemma_independent_replication")

    disc = dict((r["classification"], r["concepts"]) for r in tables["tab_discovery500_summary"])
    fig, ax = plt.subplots(figsize=(7, 3.5)); ax.bar(range(len(disc)), list(disc.values()), color="#64748b")
    ax.set_xticks(range(len(disc)), list(disc), rotation=25, ha="right"); ax.set_ylabel("Concepts")
    save(fig, "fig_discovery500_funnel")

    conf = sorted(tables["tab_confirmation30_all"], key=lambda r: r["av_hit_pct"], reverse=True)
    fig, ax = plt.subplots(figsize=(8, 6)); colors = ["#2563eb" if r["confirmed"] else "#9ca3af" for r in conf]
    ax.barh(range(len(conf)), [r["av_hit_pct"] for r in conf], color=colors)
    ax.set_yticks(range(len(conf)), [f"c{r['concept_id']}" for r in conf]); ax.invert_yaxis(); ax.set_xlabel("AV-edit hit rate (%)")
    save(fig, "fig_confirmation_funnel")

    unembed = tables["tab_unembedding_per_concept"]
    fig, ax = plt.subplots(figsize=(9, 4));
    for model, marker in (("qwen", "o"), ("gemma", "s")):
        rows = [r for r in unembed if r["model"] == model]
        ax.plot([r["concept_id"] for r in rows], [100*r["percentile_lift"] for r in rows], marker=marker, label=model)
    ax.set_xlabel("Concept ID"); ax.set_ylabel("Percentile lift (pp)"); ax.legend(frameon=False)
    save(fig, "fig_v2_unembedding_projection")

    sae = tables["tab_sae_sweeps"]
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.5), sharey=True)
    for ax, model in zip(axes, ("qwen", "gemma", "gemma10")):
        rows = [r for r in sae if r["model"] == model]
        for cid in sorted({r["concept_id"] for r in rows}):
            values = [r for r in rows if r["concept_id"] == cid]
            ax.plot([r["alpha"] for r in values], [r["hit_pct"] for r in values], marker="o", label=f"c{cid}")
        ax.set_title(model); ax.set_xlabel("alpha"); ax.legend(frameon=False, fontsize=6)
    axes[0].set_ylabel("Strict hit rate (%)"); save(fig, "fig_v2_sae_alpha_sweep")

    reft = tables["tab_reft_panel14"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8), sharey=True)
    for ax, model in zip(axes, ("qwen", "gemma")):
        rows = [r for r in reft if r["model"] == model]; x = list(range(len(rows)))
        ax.bar([i-.2 for i in x], [r["unsteered_pct"] for r in rows], .4, label="Unsteered", color="#9ca3af")
        ax.bar([i+.2 for i in x], [r["reft_pct"] for r in rows], .4, label="LoReFT", color="#f97316")
        ax.set_xticks(x, [f"c{r['concept_id']}" for r in rows], rotation=45); ax.set_title(model); ax.legend(frameon=False)
    axes[0].set_ylabel("Strict hit rate (%)"); save(fig, "fig_v2_reft_panel14")

    judge = tables["tab_axbench_rescore"]
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.5), sharey=True)
    for ax, model in zip(axes, ("qwen", "gemma")):
        rows = [r for r in judge if r["model"] == model]; x = list(range(len(rows)))
        for offset, metric, label, color in (
            (-0.24, "concept", "Concept", "#2563eb"),
            (0.00, "instruction", "Instruction", "#10b981"),
            (0.24, "fluency", "Fluency", "#f59e0b"),
        ):
            ax.bar([i + offset for i in x], [r[metric] for r in rows], .23,
                   label=label, color=color)
        ax.set_xticks(x, [r["arm"] for r in rows], rotation=25); ax.set_title(model.capitalize())
        ax.legend(frameon=False, fontsize=6)
    axes[0].set_ylabel("Mean judge score (0–2)"); save(fig, "fig_v2_axbench_judge_rescore")

    ref = tables["tab_reframing"]
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.5), sharey=True)
    for ax, model in zip(axes, ("qwen", "gemma")):
        rows = [r for r in ref if r["model"] == model]
        ax.bar(range(len(rows)), [r["hit_pct"] for r in rows], color="#8b5cf6")
        ax.set_xticks(range(len(rows)), [r["condition"].split("__")[0] for r in rows]); ax.set_title(model)
    axes[0].set_ylabel("Strict hit rate (%)"); save(fig, "fig_v2_drift_judge")

    quality = tables["tab_gemma10_quality"]
    fig, ax = plt.subplots(figsize=(8, 3.5)); ax.bar(range(len(quality)), [r["quality_pct"] for r in quality], color="#0ea5e9")
    ax.set_xticks(range(len(quality)), [r["method"] for r in quality], rotation=35, ha="right"); ax.set_ylabel("Quality pass (%)")
    save(fig, "fig_gemma10_gap_closure")

    factual = tables["tab_factual_three_arm"]
    fig, ax = plt.subplots(figsize=(7, 3.5)); ax.bar(range(len(factual)), [r["factual_correctness"] for r in factual], color="#dc2626")
    ax.set_xticks(range(len(factual)), [r["arm"] for r in factual]); ax.set_ylabel("Mean factual score (0–2)")
    save(fig, "fig_factuality_three_arm")
    print(f"generated {len(list(destination.glob('*.png')))} figure images")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("all", "tables", "assert"), nargs="?", default="all")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--table",
        action="append",
        choices=TABLE_NAMES,
        help="with 'tables', emit only this table (repeatable)",
    )
    parser.add_argument(
        "--png",
        action="store_true",
        help="with 'tables', also render PNG table images (requires analysis extra)",
    )
    args = parser.parse_args()
    if args.table and args.command != "tables":
        parser.error("--table is only valid with the 'tables' command")
    if args.png and args.command != "tables":
        parser.error("--png is only valid with the 'tables' command")
    prepare_output(args.out)
    tables, metrics = assemble(args.out, set(args.table) if args.table else None)
    assert_expected(metrics)
    if args.command == "all":
        table_images(args.out, tables)
        figures(args.out, tables)
    elif args.command == "tables" and args.png:
        table_images(args.out, tables, set(args.table) if args.table else None)
    print(f"paper reproduction outputs: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
