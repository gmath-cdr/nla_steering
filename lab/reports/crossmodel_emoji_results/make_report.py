"""Build the unified Qwen + Gemma emoji steering ledger.

Inputs are locally retrieved run directories that already passed their COMPLETE
manifest validators. This script performs a second read-only source audit,
reconstructs deterministic safety, and writes only derived report artifacts.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
import hashlib
import html
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import numpy as np
import yaml


COMMON_ARMS = ("caa", "nla_phrase", "nla_glyph")
RANDOM_ARMS = ("random_101", "random_202", "random_303")
TRACKS = ("ordinary_optional", "formal_technical_transfer")
ARM_LABELS = {
    "baseline": "Unsteered",
    "caa": "CAA",
    "nla_phrase": "NLA phrase",
    "nla_glyph": "NLA glyph",
    "jlens_word_atom": "Direct J atom",
    "random": "Mean random",
    "explicit_prompt": "Explicit prompt",
}
MODEL_CONFIG = {
    "qwen": {
        "label": "Qwen2.5-7B-Instruct",
        "short": "Qwen",
        "layer": 20,
        "width": 3584,
        "files": 72,
        "records": 3520,
        "arms": (*COMMON_ARMS, "jlens_word_atom", *RANDOM_ARMS, "baseline", "explicit_prompt"),
        "color": "#177C78",
    },
    "gemma3-12b": {
        "label": "Gemma-3-12B-it",
        "short": "Gemma",
        "layer": 32,
        "width": 3840,
        "files": 64,
        "records": 2720,
        "arms": (*COMMON_ARMS, *RANDOM_ARMS, "baseline", "explicit_prompt"),
        "color": "#7664B4",
    },
}
COLORS = {
    "ink": "#172F3B",
    "muted": "#68767D",
    "paper": "#F5F1E9",
    "sheet": "#FFFEFA",
    "grid": "#DDD7CD",
    "teal": "#0F7F7A",
    "amber": "#C88927",
    "coral": "#D7654E",
    "violet": "#7664B4",
    "gray": "#9AA3A8",
}
ARM_COLORS = {
    "caa": COLORS["gray"],
    "nla_phrase": COLORS["amber"],
    "nla_glyph": COLORS["teal"],
}


class ReportError(RuntimeError):
    """Raised when a frozen source family is incomplete or inconsistent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReportError(f"expected JSON object: {path}")
    return value


def _mean(values: Iterable[float]) -> float:
    rows = list(values)
    return float(np.mean(rows)) if rows else 0.0


def _style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 9.5,
        "axes.titlesize": 12.5,
        "axes.labelsize": 9.5,
        "axes.edgecolor": COLORS["grid"],
        "axes.labelcolor": COLORS["ink"],
        "axes.titlecolor": COLORS["ink"],
        "xtick.color": COLORS["muted"],
        "ytick.color": COLORS["ink"],
        "text.color": COLORS["ink"],
        "figure.facecolor": COLORS["paper"],
        "axes.facecolor": COLORS["paper"],
        "savefig.facecolor": COLORS["paper"],
    })


def _save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=190, bbox_inches="tight", facecolor=COLORS["paper"])
    plt.close(fig)


def _lab_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _prompt_metadata() -> dict[str, dict[str, Any]]:
    payload = yaml.safe_load(
        (_lab_root() / "experiments" / "emoji_method_comparison" / "prompts.heldout.yaml")
        .read_text(encoding="utf-8")
    )
    return {str(row["id"]): dict(row) for row in payload["prompts"]}


def _methods_lock() -> dict[str, Any]:
    return yaml.safe_load(
        (_lab_root() / "experiments" / "emoji_method_comparison" / "methods.lock.yaml")
        .read_text(encoding="utf-8")
    )


def _quality_failures(
    row: Mapping[str, Any], baseline: Mapping[str, Any], gate: Mapping[str, Any]
) -> list[str]:
    metrics = row["target_metrics"]
    base = baseline["target_metrics"]
    checks = {
        "too short": float(metrics["unicode_word_count"]) >= float(gate["minimum_unicode_words"]),
        "lost >50% length": float(metrics["unicode_word_count"]) >= (
            float(gate["minimum_length_fraction_of_paired_baseline"])
            * float(base["unicode_word_count"])
        ),
        "low lexical diversity": float(metrics["unique_unicode_word_ratio"])
        >= float(gate["minimum_unique_unicode_word_ratio"]),
        "token repetition": float(metrics["max_token_run"]) <= float(gate["maximum_token_run"]),
        "dominant token": float(metrics["dominant_token_fraction"])
        <= float(gate["maximum_dominant_token_fraction"]),
        "character run": float(metrics["max_character_run"]) <= float(gate["maximum_character_run"]),
        "replacement character": float(metrics["replacement_character_count"])
        <= float(gate["maximum_replacement_characters"]),
        "emoji count > 6": float(metrics["emoji_cluster_count"])
        <= float(gate["maximum_emoji_clusters"]),
        "emoji/word fraction": float(metrics["emoji_cluster_to_word_fraction"])
        <= float(gate["maximum_emoji_to_word_fraction"]),
    }
    return [name for name, passed in checks.items() if not passed]


def load_run(run_root: Path, expected_model: str, prompts: Mapping[str, Any], lock: Mapping[str, Any]) -> dict[str, Any]:
    config = MODEL_CONFIG[expected_model]
    complete = _read_json(run_root / "COMPLETE.json")
    summary = _read_json(run_root / "heldout_summary.json")
    receipt = _read_json(run_root / "alpha_receipt.json")
    build = _read_json(run_root / "build" / "direction_build_report.json")

    if complete.get("status") != "complete" or complete.get("model_key") != expected_model:
        raise ReportError(f"{run_root} is not a completed {expected_model} result family")
    counts = complete.get("counts", {})
    if counts.get("heldout_result_files") != config["files"] or counts.get("heldout_records") != config["records"]:
        raise ReportError(f"COMPLETE count mismatch for {expected_model}")
    if summary.get("schema") != "emoji-method-comparison-heldout/v1":
        raise ReportError(f"held-out summary schema mismatch for {expected_model}")
    if summary.get("model_key") != expected_model or summary.get("raw_records") != config["records"]:
        raise ReportError(f"held-out summary identity mismatch for {expected_model}")
    if summary.get("instruction_quality", {}).get("status") != "pending_blinded_rating":
        raise ReportError("the report expects both frozen quality sheets to remain unrated")

    registered = {str(row["path"]): row for row in complete.get("files", [])}
    for relative in (
        "alpha_receipt.json",
        "heldout_summary.json",
        "build/direction_build_report.json",
        "build/directions.npz",
    ):
        if relative not in registered or _sha256(run_root / relative) != registered[relative]["sha256"]:
            raise ReportError(f"registered source hash failed for {expected_model}: {relative}")

    results: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chunks: Counter[str] = Counter()
    result_paths = sorted((run_root / "results" / "heldout").glob("*.json"))
    if len(result_paths) != config["files"]:
        raise ReportError(f"held-out file count drifted for {expected_model}")
    for path in result_paths:
        payload = _read_json(path)
        if payload.get("schema") != "steering-run/v2":
            raise ReportError(f"not steering-run/v2: {path}")
        study = payload.get("spec", {}).get("study", {})
        arm = str(study.get("arm_id"))
        if study.get("phase") != "heldout" or study.get("model_key") != expected_model:
            raise ReportError(f"wrong phase/model in {path}")
        chunks[arm] += 1
        results[arm].extend(dict(row) for row in payload["records"])
    expected_arms = set(config["arms"])
    if set(results) != expected_arms or any(chunks[arm] != 8 for arm in expected_arms):
        raise ReportError(f"arm/chunk coverage drifted for {expected_model}: {dict(chunks)}")
    if sum(len(rows) for rows in results.values()) != config["records"]:
        raise ReportError(f"raw held-out record count drifted for {expected_model}")

    explicit_suffix = str(lock["target"]["explicit_prompt_positive_control"])
    seen: dict[str, str] = {}
    for arm, rows in results.items():
        for row in rows:
            prompt_id = str(row["prompt_id"])
            text = str(row["prompt"])
            if prompt_id not in prompts:
                raise ReportError(f"unknown held-out prompt: {prompt_id}")
            expected = str(prompts[prompt_id]["text"])
            if arm == "explicit_prompt":
                if text != f"{expected}\n\n{explicit_suffix}":
                    raise ReportError(f"explicit control text drifted: {expected_model}/{prompt_id}")
            elif text != expected:
                raise ReportError(f"hidden-arm prompt text drifted: {expected_model}/{prompt_id}")
            seen[prompt_id] = expected
    if set(seen) != set(prompts):
        raise ReportError(f"frozen prompt bank mismatch for {expected_model}")

    return {
        "model_key": expected_model,
        "config": config,
        "complete": complete,
        "summary": summary,
        "receipt": receipt,
        "build": build,
        "results": dict(results),
        "run_root": run_root,
        "quality_gate": dict(lock["evaluation"]["quality_gate"]),
    }


def aggregate(run: Mapping[str, Any], prompts: Mapping[str, Any]) -> dict[str, Any]:
    model = str(run["model_key"])
    summary = run["summary"]
    receipt = run["receipt"]
    results = run["results"]
    gate = run["quality_gate"]
    locked = {arm: float(value) for arm, value in receipt["chosen_alpha_by_arm"].items()}

    absolute: dict[str, Any] = {}
    for arm in ("baseline", *COMMON_ARMS, "explicit_prompt"):
        alpha = 0.0 if arm in ("baseline", "explicit_prompt") else locked[arm]
        absolute[arm] = {}
        for track in TRACKS:
            rows = [row for row in results[arm] if float(row["alpha"]) == alpha and row["prompt_split"] == track]
            absolute[arm][track] = {
                "records": len(rows),
                "presence": _mean(row["target_metrics"]["target_present"] for row in rows),
                "mean_clusters": _mean(row["target_metrics"]["emoji_cluster_count"] for row in rows),
                "total_clusters": int(sum(row["target_metrics"]["emoji_cluster_count"] for row in rows)),
                "mean_words": _mean(row["target_metrics"]["unicode_word_count"] for row in rows),
                "truncation_fraction": _mean(bool(row["generation"]["hit_max_new_tokens"]) for row in rows),
            }

    random_at_method: dict[str, Any] = {}
    for method in COMMON_ARMS:
        alpha = locked[method]
        random_at_method[method] = {}
        for track in TRACKS:
            rows = [
                row for random_arm in RANDOM_ARMS for row in results[random_arm]
                if float(row["alpha"]) == alpha and row["prompt_split"] == track
            ]
            random_at_method[method][track] = {
                "records": len(rows),
                "presence": _mean(row["target_metrics"]["target_present"] for row in rows),
                "mean_clusters": _mean(row["target_metrics"]["emoji_cluster_count"] for row in rows),
            }

    quality: dict[str, Any] = {}
    row_quality: dict[tuple[str, str, int], dict[str, Any]] = {}
    prompt_cells: dict[str, dict[str, Any]] = {}
    examples: dict[str, Any] = {}
    for arm in COMMON_ARMS:
        alpha = locked[arm]
        baseline = {
            (str(row["prompt_id"]), int(row["sample"])): row
            for row in results[arm] if float(row["alpha"]) == 0.0
        }
        selected = [row for row in results[arm] if float(row["alpha"]) == alpha]
        failures: Counter[str] = Counter()
        unsafe = 0
        positive = 0
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        candidates: list[tuple[str, int, Mapping[str, Any]]] = []
        for row in selected:
            prompt_id = str(row["prompt_id"])
            sample = int(row["sample"])
            reasons = _quality_failures(row, baseline[(prompt_id, sample)], gate)
            present = bool(row["target_metrics"]["target_present"])
            row_quality[(arm, prompt_id, sample)] = {"safe": not reasons, "reasons": reasons}
            unsafe += int(bool(reasons))
            positive += int(present)
            failures.update(reasons)
            grouped[prompt_id].append({
                "sample": sample,
                "present": int(present),
                "clusters": int(row["target_metrics"]["emoji_cluster_count"]),
                "safe": not reasons,
            })
            if present and not reasons and row["prompt_split"] == "ordinary_optional":
                candidates.append((prompt_id, sample, row))
        safe_fraction = 1.0 - unsafe / len(selected)
        expected = float(summary["claims"][arm]["deterministic_safe_fraction"])
        if not np.isclose(safe_fraction, expected):
            raise ReportError(f"quality reconstruction mismatch: {model}/{arm}")
        quality[arm] = {
            "active_records": len(selected),
            "safe_fraction": safe_fraction,
            "unsafe_records": unsafe,
            "positive_records": positive,
            "failure_reasons": dict(failures),
        }
        prompt_cells[arm] = {
            prompt_id: {
                "presence": _mean(cell["present"] for cell in cells),
                "safe_fraction": _mean(cell["safe"] for cell in cells),
                "positive_generations": int(sum(cell["present"] for cell in cells)),
            }
            for prompt_id, cells in grouped.items()
        }
        candidates.sort(key=lambda item: (item[0], item[1]))
        if candidates:
            prompt_id, sample, row = candidates[0]
            base = baseline[(prompt_id, sample)]
            examples[arm] = {
                "prompt_id": prompt_id,
                "sample": sample,
                "prompt": str(row["prompt"]),
                "baseline": str(base["text"]),
                "steered": str(row["text"]),
                "clusters": int(row["target_metrics"]["emoji_cluster_count"]),
                "safe": True,
            }

    conservative: dict[str, Any] = {}
    for arm in COMMON_ARMS:
        alpha = locked[arm]
        baseline = {
            (str(row["prompt_id"]), int(row["sample"])): row
            for row in results[arm] if float(row["alpha"]) == 0.0
        }
        deltas: dict[str, list[float]] = defaultdict(list)
        for row in results[arm]:
            if float(row["alpha"]) != alpha or row["prompt_split"] != "ordinary_optional":
                continue
            prompt_id, sample = str(row["prompt_id"]), int(row["sample"])
            safe = row_quality[(arm, prompt_id, sample)]["safe"]
            target = float(row["target_metrics"]["target_present"]) if safe else 0.0
            base = float(baseline[(prompt_id, sample)]["target_metrics"]["target_present"])
            deltas[prompt_id].append(target - base)
        prompt_means = {key: _mean(values) for key, values in deltas.items()}
        conservative[arm] = {
            "ordinary_effect": _mean(prompt_means.values()),
            "positive_prompt_blocks": int(sum(value > 0 for value in prompt_means.values())),
        }

    directions = run["build"]["directions"]
    construction_diagnostics: dict[str, Any] = {}
    if "caa" in directions:
        construction_diagnostics["caa_positive_A_vs_B_half_cosine"] = float(
            directions["caa"]["details"]["positive_A_vs_B_half_cosine"]
        )
    if "jlens_word_atom" in directions:
        construction_diagnostics["jlens_split_half_atom_cosine"] = float(
            directions["jlens_word_atom"]["details"]["split_half_atom_cosine"]
        )

    return {
        "model_key": model,
        "model": run["config"]["label"],
        "layer": run["config"]["layer"],
        "hidden_width": run["config"]["width"],
        "study_id": summary["study_id"],
        "run_completed_utc": run["complete"]["completed_utc"],
        "complete_manifest_sha256": _sha256(run["run_root"] / "COMPLETE.json"),
        "raw_result_files": int(summary["raw_result_files"]),
        "raw_records": int(summary["raw_records"]),
        "locked_alphas": locked,
        "claims": {arm: summary["claims"][arm] for arm in COMMON_ARMS},
        "absolute": absolute,
        "random_at_method": random_at_method,
        "calibration": {arm: receipt["selection_details"][arm] for arm in COMMON_ARMS},
        "quality": quality,
        "conservative": conservative,
        "prompt_cells": prompt_cells,
        "examples": examples,
        "construction_diagnostics": construction_diagnostics,
        "instruction_quality_status": summary["instruction_quality"]["status"],
        "prompt_blocks_with_positive": {
            arm: {
                track: sum(
                    prompt_cells[arm][prompt_id]["presence"] > 0
                    for prompt_id, row in prompts.items() if row["track"] == track
                )
                for track in TRACKS
            }
            for arm in COMMON_ARMS
        },
    }


def chart_effects(models: Mapping[str, Any], path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11.7, 5.2), sharey=True)
    tracks = (("ordinary_optional", "Primary: ordinary prompts"), ("formal_technical_transfer", "Secondary: formal / technical"))
    rows = [(arm, model) for arm in ("nla_glyph", "nla_phrase", "caa") for model in ("qwen", "gemma3-12b")]
    y = np.arange(len(rows))
    for axis, (track, title) in zip(axes, tracks):
        axis.axvline(0, color=COLORS["ink"], linewidth=1)
        for index, (arm, model) in enumerate(rows):
            claim = models[model]["claims"][arm]["tracks"][track]
            effect = 100 * float(claim["effect_vs_alpha_zero"])
            low, high = (100 * float(value) for value in claim["ci95_vs_alpha_zero"])
            color = MODEL_CONFIG[model]["color"]
            marker = "o" if model == "qwen" else "s"
            axis.errorbar(effect, index, xerr=[[effect - low], [high - effect]], fmt=marker,
                          color=color, ecolor=color, elinewidth=2, capsize=3.5, markersize=6.5)
            axis.text(high + 0.8, index, f"{effect:.1f}", va="center", fontsize=8.5)
        axis.set_title(title, loc="left", fontweight="bold")
        axis.set_xlabel("Paired change in emoji presence (percentage points)")
        axis.set_xlim(-3, 43)
        axis.grid(axis="x", color=COLORS["grid"], linewidth=0.7)
        axis.spines[["top", "right", "left"]].set_visible(False)
    labels = [f"{ARM_LABELS[arm]} · {MODEL_CONFIG[model]['short']}" for arm, model in rows]
    axes[0].set_yticks(y, labels)
    axes[0].invert_yaxis()
    fig.suptitle("Matched held-out effects at independently frozen safe doses", x=0.06, ha="left", fontweight="bold")
    fig.text(0.06, 0.01, "95% prompt-block bootstrap intervals. Models are stratified; intervals and effects are not pooled.", color=COLORS["muted"], fontsize=8.5)
    fig.subplots_adjust(bottom=0.18, wspace=0.12)
    _save(fig, path)


def chart_calibration(models: Mapping[str, Any], path: Path) -> None:
    fig, axes = plt.subplots(2, 3, figsize=(11.8, 6.8), sharey=True)
    for row_index, model in enumerate(("qwen", "gemma3-12b")):
        for column, arm in enumerate(COMMON_ARMS):
            axis = axes[row_index, column]
            details = models[model]["calibration"][arm]
            candidates = [{"alpha": 0.0, "mean_paired_presence_effect": 0.0, "passes_safety": True}, *details["candidates"]]
            x = np.asarray([float(item["alpha"]) for item in candidates])
            y = 100 * np.asarray([float(item["mean_paired_presence_effect"]) for item in candidates])
            color = ARM_COLORS[arm]
            axis.plot(x, y, color=color, linewidth=1.8)
            for item, xx, yy in zip(candidates, x, y):
                safe = bool(item["passes_safety"])
                axis.scatter([xx], [yy], s=42, facecolor=color if safe else COLORS["paper"], edgecolor=color, linewidth=1.5, zorder=3)
                if not safe:
                    axis.scatter([xx], [yy], marker="x", color=COLORS["coral"], s=46, linewidth=1.7, zorder=4)
            chosen = float(models[model]["locked_alphas"][arm])
            chosen_row = next(item for item in candidates if float(item["alpha"]) == chosen)
            chosen_y = 100 * float(chosen_row["mean_paired_presence_effect"])
            axis.scatter([chosen], [chosen_y], marker="*", s=145, color=color, edgecolor=COLORS["ink"], linewidth=0.5, zorder=5)
            if row_index == 0:
                axis.set_title(ARM_LABELS[arm], fontweight="bold")
            if column == 0:
                axis.set_ylabel(f"{MODEL_CONFIG[model]['short']}\npaired effect (pp)")
            if row_index == 1:
                axis.set_xlabel("Alpha")
            axis.set_ylim(-4, 103)
            axis.grid(color=COLORS["grid"], linewidth=0.6)
            axis.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Calibration was separate for every model × construction", x=0.06, ha="left", fontweight="bold")
    fig.text(0.06, 0.01, "Filled = deterministic gate passed; crossed open = failed; star = frozen held-out dose. Calibration used six familiar prompts, not the 40 held-out prompts.", color=COLORS["muted"], fontsize=8.4)
    fig.subplots_adjust(bottom=0.12, top=0.9, hspace=0.28, wspace=0.18)
    _save(fig, path)


def chart_absolute(models: Mapping[str, Any], path: Path) -> None:
    arms = ("baseline", "random", "caa", "nla_phrase", "nla_glyph", "explicit_prompt")
    labels = ["Unsteered", "Mean random\n@glyph dose", "CAA", "NLA phrase", "NLA glyph", "Explicit prompt"]
    fig, axes = plt.subplots(1, 2, figsize=(11.8, 5.0), sharey=True)
    for axis, model in zip(axes, ("qwen", "gemma3-12b")):
        values = []
        for arm in arms:
            if arm == "random":
                value = models[model]["random_at_method"]["nla_glyph"]["ordinary_optional"]["presence"]
            else:
                value = models[model]["absolute"][arm]["ordinary_optional"]["presence"]
            values.append(100 * value)
        colors = [COLORS["gray"], COLORS["gray"], ARM_COLORS["caa"], ARM_COLORS["nla_phrase"], ARM_COLORS["nla_glyph"], COLORS["coral"]]
        bars = axis.bar(np.arange(len(arms)), values, color=colors, width=0.7)
        axis.set_title(MODEL_CONFIG[model]["label"], loc="left", fontweight="bold")
        axis.set_xticks(np.arange(len(arms)), labels, rotation=28, ha="right")
        axis.set_ylim(0, 112)
        axis.grid(axis="y", color=COLORS["grid"], linewidth=0.7)
        axis.spines[["top", "right", "left"]].set_visible(False)
        for bar, value in zip(bars, values):
            axis.text(bar.get_x() + bar.get_width() / 2, value + 2.0, f"{value:.1f}%", ha="center", va="bottom", fontsize=8.3)
    axes[0].set_ylabel("Ordinary responses with ≥1 Unicode emoji")
    fig.suptitle("Absolute outcomes: hidden methods versus controls", x=0.06, ha="left", fontweight="bold")
    fig.text(0.06, 0.005, "Each hidden method is shown at its model-specific frozen alpha. Explicit prompting changes the visible instruction and is an upper-bound control, not a hidden-state method.", color=COLORS["muted"], fontsize=8.3)
    fig.subplots_adjust(bottom=0.28, wspace=0.12)
    _save(fig, path)


def chart_prompt_map(models: Mapping[str, Any], prompts: Mapping[str, Any], path: Path) -> None:
    ids = [f"ordinary-{index:02d}" for index in range(1, 31)] + [f"formal-{index:02d}" for index in range(1, 11)]
    columns = [(model, arm) for model in ("qwen", "gemma3-12b") for arm in COMMON_ARMS]
    matrix = np.zeros((len(ids), len(columns)), dtype=float)
    unsafe = np.zeros_like(matrix, dtype=bool)
    for row_index, prompt_id in enumerate(ids):
        for column, (model, arm) in enumerate(columns):
            cell = models[model]["prompt_cells"][arm][prompt_id]
            matrix[row_index, column] = float(cell["presence"])
            unsafe[row_index, column] = float(cell["safe_fraction"]) < 1.0
    cmap = LinearSegmentedColormap.from_list("emoji_presence", ["#E8E3DA", "#B9DED8", COLORS["teal"]])
    fig, axis = plt.subplots(figsize=(10.7, 12.8))
    image = axis.imshow(matrix, aspect="auto", cmap=cmap, vmin=0, vmax=1)
    for row, column in zip(*np.where(unsafe)):
        axis.text(column, row, "×", ha="center", va="center", color=COLORS["coral"], fontsize=10, fontweight="bold")
    axis.axhline(29.5, color=COLORS["ink"], linewidth=1.2)
    axis.axvline(2.5, color=COLORS["ink"], linewidth=1.2)
    xlabels = [f"{MODEL_CONFIG[model]['short']}\n{ARM_LABELS[arm].replace('NLA ', '')}" for model, arm in columns]
    labels = [f"{prompt_id} · {str(prompts[prompt_id]['task_type']).replace('_', ' ')}" for prompt_id in ids]
    axis.set_xticks(range(len(columns)), xlabels)
    axis.xaxis.tick_top()
    axis.set_yticks(range(len(ids)), labels, fontsize=7.2)
    axis.tick_params(axis="x", length=0, pad=7)
    axis.tick_params(axis="y", length=0)
    axis.set_title("Prompt-level response rate at each frozen alpha", loc="left", pad=36, fontweight="bold")
    colorbar = fig.colorbar(image, ax=axis, fraction=0.025, pad=0.025)
    colorbar.set_label("Fraction of four generations with emoji")
    fig.text(0.09, 0.012, "Each cell summarizes four paired generations. Coral × means at least one generation failed a deterministic coherence rule. Tracks are separated by the horizontal line.", color=COLORS["muted"], fontsize=8.2)
    fig.subplots_adjust(left=0.28, right=0.9, bottom=0.045, top=0.91)
    _save(fig, path)


def chart_safety(models: Mapping[str, Any], path: Path) -> None:
    labels = [f"{ARM_LABELS[arm]} · {MODEL_CONFIG[model]['short']}" for arm in COMMON_ARMS for model in ("qwen", "gemma3-12b")]
    rows = [(arm, model) for arm in COMMON_ARMS for model in ("qwen", "gemma3-12b")]
    safe = [100 * models[model]["quality"][arm]["safe_fraction"] for arm, model in rows]
    raw_effect = [100 * models[model]["claims"][arm]["tracks"]["ordinary_optional"]["effect_vs_alpha_zero"] for arm, model in rows]
    conservative = [100 * models[model]["conservative"][arm]["ordinary_effect"] for arm, model in rows]
    colors = [MODEL_CONFIG[model]["color"] for _, model in rows]
    y = np.arange(len(rows))
    fig, axes = plt.subplots(1, 2, figsize=(11.6, 5.2))
    axes[0].barh(y, safe, color=colors, height=0.66)
    axes[0].axvline(90, color=COLORS["coral"], linestyle="--", linewidth=1.2)
    axes[0].set_xlim(88, 101)
    axes[0].set_xlabel("Outputs passing deterministic gate (%)")
    axes[0].set_yticks(y, labels)
    axes[0].invert_yaxis()
    for index, value in enumerate(safe):
        axes[0].text(value + 0.15, index, f"{value:.1f}%", va="center", fontsize=8.3)
    width = 0.32
    axes[1].barh(y - width / 2, raw_effect, height=width, color=colors, label="Preregistered")
    axes[1].barh(y + width / 2, conservative, height=width, color="none", edgecolor=colors, hatch="///", label="Unsafe recoded as zero")
    axes[1].axvline(0, color=COLORS["ink"], linewidth=1)
    axes[1].set_xlabel("Ordinary paired effect (percentage points)")
    axes[1].set_yticks([])
    axes[1].invert_yaxis()
    axes[1].legend(frameon=False, fontsize=8.2, loc="lower right")
    for axis in axes:
        axis.grid(axis="x", color=COLORS["grid"], linewidth=0.7)
        axis.spines[["top", "right", "left"]].set_visible(False)
    fig.suptitle("Deterministic safety and a conservative target-failure stress test", x=0.06, ha="left", fontweight="bold")
    fig.text(0.06, 0.01, "The hatched estimate is post-hoc and deliberately harsh: any deterministic-gate failure is counted as no emoji success. Blinded relevance/fluency ratings remain separate and pending.", color=COLORS["muted"], fontsize=8.2)
    fig.subplots_adjust(bottom=0.2, wspace=0.12)
    _save(fig, path)


def chart_replication(models: Mapping[str, Any], path: Path) -> None:
    fig, axis = plt.subplots(figsize=(9.7, 4.9))
    y = np.arange(len(COMMON_ARMS))
    left_offsets = {"caa": -0.55, "nla_phrase": 0.0, "nla_glyph": 0.0}
    for index, arm in enumerate(COMMON_ARMS):
        qwen = 100 * models["qwen"]["claims"][arm]["tracks"]["ordinary_optional"]["effect_vs_alpha_zero"]
        gemma = 100 * models["gemma3-12b"]["claims"][arm]["tracks"]["ordinary_optional"]["effect_vs_alpha_zero"]
        axis.plot([0, 1], [qwen, gemma], color=ARM_COLORS[arm], linewidth=2.4, marker="o", markersize=7)
        axis.text(-0.035, qwen + left_offsets[arm], f"{ARM_LABELS[arm]}  {qwen:.1f}", ha="right", va="center", fontsize=9)
        axis.text(1.035, gemma, f"{gemma:.1f}  {ARM_LABELS[arm]}", ha="left", va="center", fontsize=9)
    axis.set_xlim(-0.42, 1.42)
    axis.set_ylim(-2, 33)
    axis.set_xticks([0, 1], ["Qwen · L20", "Gemma · L32"])
    axis.set_ylabel("Ordinary paired effect (percentage points)")
    axis.grid(axis="y", color=COLORS["grid"], linewidth=0.7)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.set_title("What replicated, and what remained model-specific", loc="left", fontweight="bold")
    fig.text(0.07, 0.01, "Lines connect matched constructions, not shared vectors. Each direction was rebuilt and calibrated inside its own model residual space.", color=COLORS["muted"], fontsize=8.3)
    fig.subplots_adjust(left=0.22, right=0.78, bottom=0.17)
    _save(fig, path)


def _data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _is_emoji_char(char: str) -> bool:
    code = ord(char)
    return 0x1F1E6 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF


def _shorten(text: str, limit: int = 560, *, retain_emoji: bool = False) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    if retain_emoji:
        emoji_index = next((index for index, char in enumerate(collapsed) if _is_emoji_char(char)), -1)
        if emoji_index >= limit:
            head_length = limit // 2
            window_start = max(head_length, emoji_index - limit // 5)
            tail_length = limit - head_length - 3
            return collapsed[:head_length].rstrip() + " … " + collapsed[window_start:window_start + tail_length].strip()
    return collapsed[: limit - 1].rstrip() + "…"


def _highlight_emoji(text: str) -> str:
    pieces: list[str] = []
    for char in _shorten(text, retain_emoji=True):
        escaped = html.escape(char)
        if _is_emoji_char(char):
            pieces.append(f'<mark class="emoji">{escaped}</mark>')
        else:
            pieces.append(escaped)
    return "".join(pieces)


def _fmt_p(value: float) -> str:
    if value < 0.001:
        return "<0.001"
    return f"{value:.3f}"


def _page_header(page: int, total: int) -> str:
    return f'<div class="pagehead"><span>Cross-model emoji steering</span><span>Qwen × Gemma</span><span>{page:02d} / {total:02d}</span></div>'


def _page_footer(text: str) -> str:
    return f'<div class="pagefoot"><span>{html.escape(text)}</span><span>25 August 2026</span></div>'


def render_html(analysis: Mapping[str, Any], figures: Mapping[str, Path]) -> str:
    models = analysis["models"]
    qwen = models["qwen"]
    gemma = models["gemma3-12b"]
    image = {key: _data_uri(path) for key, path in figures.items()}
    total = 10

    method_rows = []
    for arm in ("nla_glyph", "nla_phrase", "caa"):
        for model in ("qwen", "gemma3-12b"):
            claim = models[model]["claims"][arm]
            ordinary = claim["tracks"]["ordinary_optional"]
            success = bool(claim["statistical_and_deterministic_success"])
            verdict = "Supported†" if success else "Not detected"
            css = "positive" if success else "neutral"
            method_rows.append(
                f'<tr><td>{ARM_LABELS[arm]}</td><td>{MODEL_CONFIG[model]["short"]}</td>'
                f'<td>{models[model]["locked_alphas"][arm]:.2f}</td>'
                f'<td>+{100*ordinary["effect_vs_alpha_zero"]:.1f} pp '
                f'[{100*ordinary["ci95_vs_alpha_zero"][0]:.1f}, {100*ordinary["ci95_vs_alpha_zero"][1]:.1f}]</td>'
                f'<td>{100*claim["deterministic_safe_fraction"]:.1f}%</td>'
                f'<td class="result {css}">{verdict}</td></tr>'
            )

    calibration_rows = []
    for arm in COMMON_ARMS:
        calibration_rows.append(
            f'<tr><td>{ARM_LABELS[arm]}</td>'
            f'<td>{qwen["locked_alphas"][arm]:.2f}</td><td>{gemma["locked_alphas"][arm]:.2f}</td>'
            f'<td>{100*qwen["calibration"][arm]["best_safe_effect"]:.1f} / '
            f'{100*gemma["calibration"][arm]["best_safe_effect"]:.1f} pp</td></tr>'
        )

    prompt_rows = []
    for arm in ("nla_glyph", "nla_phrase", "caa"):
        prompt_rows.append(
            f'<tr><td>{ARM_LABELS[arm]}</td>'
            f'<td>{qwen["prompt_blocks_with_positive"][arm]["ordinary_optional"]}/30</td>'
            f'<td>{gemma["prompt_blocks_with_positive"][arm]["ordinary_optional"]}/30</td>'
            f'<td>{qwen["prompt_blocks_with_positive"][arm]["formal_technical_transfer"]}/10</td>'
            f'<td>{gemma["prompt_blocks_with_positive"][arm]["formal_technical_transfer"]}/10</td></tr>'
        )

    q_example = qwen["examples"]["nla_glyph"]
    g_example = gemma["examples"]["nla_glyph"]
    shared_key = analysis["examples"]["shared_glyph"]
    if shared_key:
        q_example = shared_key["qwen"]
        g_example = shared_key["gemma"]
    g_phrase = gemma["examples"].get("nla_phrase", g_example)
    g_caa = gemma["examples"].get("caa", g_example)

    css = """
    :root{--paper:#f5f1e9;--sheet:#fffefa;--ink:#172f3b;--muted:#68767d;--line:#ddd7cd;--teal:#0f7f7a;--teal-soft:#e1f0ed;--amber:#c88927;--amber-soft:#f8ecd5;--coral:#d7654e;--coral-soft:#f8e2dc;--violet:#7664b4;--violet-soft:#ece8f7;--shadow:0 12px 32px rgba(22,43,52,.13)}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.43}.toc{position:sticky;top:0;z-index:10;display:flex;justify-content:center;gap:18px;padding:10px 14px;background:rgba(23,47,59,.96);backdrop-filter:blur(8px)}.toc a{color:#f7f1e8;text-decoration:none;font-size:9px;letter-spacing:.08em;text-transform:uppercase}.book{padding:26px 0 46px}.sheet{width:210mm;min-height:297mm;margin:0 auto 24px;padding:13mm 16mm 11mm;background:var(--sheet);box-shadow:var(--shadow);display:grid;grid-template-rows:auto 1fr auto;gap:8mm;position:relative;overflow:hidden}.pagehead,.pagefoot{display:flex;justify-content:space-between;gap:12px;color:var(--muted);font-size:9px;letter-spacing:.1em;text-transform:uppercase}.pagehead{padding-bottom:7px;border-bottom:1px solid var(--line)}.pagefoot{padding-top:7px;border-top:1px solid var(--line);letter-spacing:.04em;text-transform:none}.pagebody{min-height:0}.eyebrow{text-align:center;color:var(--teal);font-size:10px;font-weight:700;letter-spacing:.18em;text-transform:uppercase;margin:2px 0 9px}h1,h2,h3{font-family:Georgia,"Times New Roman",serif;color:var(--ink)}h1{text-align:center;font-size:35px;line-height:1.04;margin:0 0 14px}h2{text-align:center;font-size:25px;line-height:1.12;margin:0 0 9px}h3{font-size:16px;margin:0 0 6px}.dek{max-width:670px;margin:0 auto 18px;text-align:center;color:#41535c;font-family:Georgia,"Times New Roman",serif;font-size:15px}p{margin:0 0 10px}.small{font-size:10.5px;color:var(--muted)}.caps{font-size:9px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}.hero-answer{border-left:4px solid var(--teal);padding:11px 14px;margin:18px 0;background:linear-gradient(90deg,var(--teal-soft),transparent)}.hero-answer b{font-family:Georgia,"Times New Roman",serif;font-size:18px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:14px 0}.metric{border-top:3px solid var(--teal);background:#fbfaf7;padding:11px 12px;min-height:82px}.metric:nth-child(2){border-color:var(--violet)}.metric:nth-child(3){border-color:var(--amber)}.metric strong{display:block;font-family:Georgia,"Times New Roman",serif;font-size:25px;line-height:1;color:var(--teal);margin-bottom:6px}.metric span{font-size:10px;color:var(--muted)}.figure{margin:13px 0 0}.figure img{display:block;width:100%;height:auto;background:#faf8f3;border:1px solid var(--line);border-radius:8px}.figure figcaption{font-size:10px;color:var(--muted);margin-top:6px}.table-wrap{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}table{width:100%;border-collapse:collapse;font-size:10.5px}th{font-size:8px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);background:#faf8f3}th,td{padding:8px 9px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:0}.result{font-weight:700}.positive{color:var(--teal)}.neutral{color:var(--coral)}.explore{color:var(--violet)}.pipeline{display:grid;grid-template-columns:1fr auto 1.2fr auto .85fr auto 1.15fr;align-items:center;gap:7px;margin:18px 0}.node{border:1px solid var(--line);border-radius:8px;padding:10px 8px;text-align:center;background:#fff;font-size:10px;min-height:60px;display:grid;place-content:center}.node b{display:block;color:var(--teal);margin-bottom:3px}.arrow{color:var(--amber);font-size:18px}.method-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}.method{padding:12px;border:1px solid var(--line);border-radius:8px;background:#fff;min-height:156px}.method .num{float:right;font-family:Georgia,"Times New Roman",serif;font-size:28px;color:#d8d0c4}.method code{font-size:9.3px;color:#41535c;white-space:normal}.method p{font-size:10.5px;margin:5px 0 0}.method.glyph{border-left:4px solid var(--teal)}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}.map-layout{display:grid;grid-template-columns:1.45fr .65fr;gap:14px;align-items:start}.map-layout .figure{margin:0}.map-layout .figure img{max-height:220mm;object-fit:contain}.note{padding:11px 12px;border-left:4px solid var(--amber);background:var(--amber-soft);font-size:11px;margin:10px 0}.note.teal{border-color:var(--teal);background:var(--teal-soft)}.note.violet{border-color:var(--violet);background:var(--violet-soft)}.note.coral{border-color:var(--coral);background:var(--coral-soft)}.verdict-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:12px}.verdict{padding:10px;border-top:3px solid var(--line);background:#faf9f5;font-size:10.5px}.verdict.worked{border-color:var(--teal)}.verdict.mixed{border-color:var(--amber)}.verdict.null{border-color:var(--coral)}.verdict.exploratory{border-color:var(--violet)}.responses{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0}.response-card{border:1px solid var(--line);border-radius:8px;padding:11px;background:#fff}.response-card .response{font-family:Georgia,"Times New Roman",serif;font-size:10.2px;white-space:pre-wrap;background:#faf8f3;padding:9px;margin-top:7px;border-radius:6px;max-height:180px;overflow:hidden}.response-card.steered{border-top:4px solid var(--teal)}mark.emoji{background:#ffe2a8;color:var(--ink);padding:0 2px;border-radius:2px}.claim-table td:first-child{width:34%;font-weight:700}.status-yes{color:var(--teal)}.status-no{color:var(--coral)}.status-pending{color:var(--amber)}code{word-break:break-word}.glossary-flow{grid-template-columns:1fr auto 1fr auto 1fr auto 1fr}.glossary-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:12px}.term{padding:10px 11px;border:1px solid var(--line);border-radius:8px;background:#fff;min-height:108px}.term h3{font-size:14px;margin-bottom:4px}.term p{font-size:10px;margin:0;color:#41535c}.term.teal{border-top:3px solid var(--teal)}.term.amber{border-top:3px solid var(--amber)}.term.violet{border-top:3px solid var(--violet)}#results .figure{margin:8px 0}#results .figure img{max-height:64mm;object-fit:contain}#results table{font-size:9.5px}#results th,#results td{padding:5px 7px}#results .verdict-grid{margin-top:8px;gap:7px}#results .verdict{padding:7px;font-size:9.5px}#results .verdict h3{font-size:14px}#controls .figure{margin:7px 0 0}#controls .figure img{max-height:66mm;object-fit:contain}#controls .figure figcaption{margin-top:3px}#controls .note{padding:7px 9px;margin:7px 0;font-size:9.8px}@media screen and (max-width:760px){.toc{justify-content:flex-start;overflow-x:auto}.book{padding:14px 0 30px}.sheet{width:100%;min-height:auto;margin-bottom:16px;padding:24px 18px;box-shadow:none}.metrics,.method-grid,.two-col,.map-layout,.responses,.verdict-grid,.glossary-grid{grid-template-columns:1fr}.pipeline{grid-template-columns:1fr}.arrow{transform:rotate(90deg);text-align:center}.pagehead span:nth-child(2){display:none}.map-layout .figure img,#results .figure img,#controls .figure img{max-height:none}h1{font-size:31px}}@media print{@page{size:A4;margin:0}body{background:#fff}.toc{display:none}.book{padding:0}.sheet{width:210mm;height:297mm;min-height:297mm;margin:0;padding:13mm 16mm 11mm;box-shadow:none;break-after:page}.sheet:last-child{break-after:auto}}
    """

    pages: list[str] = []
    pages.append(f'''<section class="sheet" id="start-here">{_page_header(1,total)}<div class="pagebody">
      <div class="eyebrow">Reader's dictionary</div><h1>How to Read This Ledger</h1>
      <p class="dek">The report uses technical shorthand. This page defines what each recurring term means in this experiment.</p>
      <div class="hero-answer"><b>“Hidden” means internal—not secret.</b><p>A hidden-state method leaves the user-visible prompt unchanged. While the model generates its answer, we alter an internal residual activation at one registered layer. By contrast, the explicit-prompt control visibly asks the model to use emoji.</p></div>
      <div class="pipeline glossary-flow"><div class="node"><b>Same visible prompt</b>“How should I plan a trip?”</div><div class="arrow">→</div><div class="node"><b>Internal layer state</b>activation vector <code>h</code></div><div class="arrow">→</div><div class="node"><b>Hidden intervention</b><code>h′ = h + α||h||d̂</code></div><div class="arrow">→</div><div class="node"><b>Generated answer</b>measure emoji + quality</div></div>
      <div class="glossary-grid">
        <div class="term teal"><h3>Residual state / layer</h3><p>The model's internal numerical representation at one depth: Qwen layer 20 or Gemma layer 32. It is not text and is different for every model.</p></div>
        <div class="term teal"><h3>Direction / vector</h3><p>A same-size arrow in that internal space representing the change we want to add. CAA and NLA construct this arrow differently.</p></div>
        <div class="term teal"><h3>Alpha (α) / dose</h3><p>How strongly the unit direction is added. Larger alpha means a stronger internal push—and usually more risk of distorted output.</p></div>
        <div class="term amber"><h3>NLA</h3><p><b>Natural Language Autoencoder.</b> AV turns an activation into readable text; AR reconstructs an activation from text. We edited the text, reconstructed both versions, and subtracted them.</p></div>
        <div class="term amber"><h3>CAA</h3><p><b>Contrastive Activation Addition.</b> Average many internal differences between matched emoji-positive and emoji-negative examples, then inject that mean direction.</p></div>
        <div class="term amber"><h3>Control</h3><p>A comparison condition, not another claim. <b>Baseline:</b> no steering. <b>Random:</b> arbitrary internal push. <b>Explicit:</b> visible instruction asking for emoji.</p></div>
        <div class="term violet"><h3>Calibration / frozen alpha</h3><p>Try doses on six separate prompts, reject unsafe doses, then lock one dose. “Frozen” means it could not be changed after seeing held-out results.</p></div>
        <div class="term violet"><h3>Held-out / paired</h3><p><b>Held-out:</b> 40 prompts excluded from dose choice. <b>Paired:</b> compare the same prompt and generation seed with versus without steering.</p></div>
        <div class="term violet"><h3>Primary and secondary tracks</h3><p>The 30 ordinary prompts are primary. The 10 formal/technical prompts test transfer or intrusion and are analyzed separately—never pooled.</p></div>
        <div class="term teal"><h3>Emoji presence</h3><p>The primary outcome is binary: did the response contain at least one Unicode emoji cluster? ASCII emoticons and the total emoji count are secondary.</p></div>
        <div class="term teal"><h3>pp, effect, and 95% CI</h3><p><b>pp</b> means percentage points: 0% to 15.8% is +15.8 pp. The 95% interval shows prompt-level uncertainty around that paired effect.</p></div>
        <div class="term violet"><h3>Deterministic safety / failure</h3><p>Frozen automatic checks for shortening, repetition, corrupted characters, or emoji flooding. Failure means a threshold was crossed—not necessarily human-rated nonsense. “Supported†” still awaits blinded relevance/fluency ratings.</p></div>
      </div>
    </div>{_page_footer('Reading rule: hidden methods change an internal activation; explicit prompting changes the text the user can see.')}</section>''')

    pages.append(f'''<section class="sheet" id="overview">{_page_header(2,total)}<div class="pagebody">
      <div class="eyebrow">Frozen held-out comparison · Qwen L20 + Gemma L32</div><h1>The Cross-Model Emoji Steering Ledger</h1>
      <p class="dek">A unified record of three matched hidden-state constructions, two independently calibrated models, and one result that clearly replicated.</p>
      <div class="hero-answer"><b>Did hidden-state steering make both models use emoji? Yes.</b><p>The NLA direction built by appending one smile glyph to the model's verbalized state increased held-out emoji presence in Qwen and Gemma. CAA and the longer phrase direction were positive only in Gemma at their frozen safe doses.</p></div>
      <div class="metrics"><div class="metric"><strong>+15.8 pp</strong><span>Qwen NLA-glyph effect on ordinary prompts; 95% CI 7.5 to 25.8</span></div><div class="metric"><strong>+28.3 pp</strong><span>Gemma NLA-glyph effect on ordinary prompts; 95% CI 18.3 to 39.2</span></div><div class="metric"><strong>6,240</strong><span>archived held-out rows across both runs; paired and repeated cells, not independent samples</span></div></div>
      <figure class="figure"><img src="{image['effects']}" alt="Cross-model held-out emoji-presence effect forest plot"><figcaption>The primary ordinary-prompt result is stratified by model. The same glyph construction clears zero on both models; formal/technical transfer remains secondary.</figcaption></figure>
    </div>{_page_footer('Headline: the minimal NLA glyph construction replicated; method efficacy was still model-dependent.')}</section>''')

    pages.append(f'''<section class="sheet" id="design">{_page_header(3,total)}<div class="pagebody">
      <div class="eyebrow">Matched design</div><h2>What was held constant—and what was rebuilt</h2><p class="dek">The user-visible hidden-arm prompts were identical. The vectors were not: every direction was constructed fresh inside the target model's registered residual space.</p>
      <div class="pipeline"><div class="node"><b>Frozen inputs</b>30 ordinary + 10 formal</div><div class="arrow">→</div><div class="node"><b>Build per model</b>Qwen L20 or Gemma L32</div><div class="arrow">→</div><div class="node"><b>Inject</b><code>h + α||h||d̂</code></div><div class="arrow">→</div><div class="node"><b>Paired score</b>same prompt + seed</div></div>
      <div class="method-grid"><div class="method"><span class="num">1</span><span class="caps">CAA</span><h3>Average 300 activation contrasts</h3><code>d = mean(h_positive_answer − h_negative_answer)</code><p>Balanced A/B answer letters from 300 frozen AxBench-derived text inputs; oriented toward the emoji-positive requirement.</p></div><div class="method"><span class="num">2</span><span class="caps">NLA phrase</span><h3>Edit one verbalized state</h3><code>AR(z + “The response should include Unicode emojis and emoticons.”) − AR(z)</code><p>No averaging. AV produces the readable state; AR reconstructs original and edited text into residual vectors.</p></div><div class="method glyph"><span class="num">3</span><span class="caps">NLA glyph</span><h3>Append one smile glyph</h3><code>AR(z + “😊”) − AR(z)</code><p>The smallest frozen semantic edit. This was the only construction with a clear positive result in both models.</p></div></div>
      <div class="note teal"><b>Matched administration.</b> Each unit direction used the same decode-time all-position hook, temperature 0.7, 192-token ceiling, 40 prompts, and four prompt-specific paired seeds.</div>
      <div class="two-col"><div class="note"><b>Independently calibrated.</b> Qwen and Gemma used different alpha ladders and each method selected its own safe dose before held-out evaluation.</div><div class="note violet"><b>No vector transport.</b> Qwen d=3,584 vectors never entered Gemma d=3,840 space. This is a shared-text protocol, not a shared-vector test.</div></div>
      <div class="table-wrap"><table><thead><tr><th>Component</th><th>Frozen choice</th></tr></thead><tbody><tr><td>Prompt source</td><td>40 outcome-blind AxBench Concept500 input rows; expected outputs and concept labels were not used to score steering</td></tr><tr><td>Primary track</td><td>30 ordinary, emoji-optional prompts × 4 generations = 120 paired cells/model</td></tr><tr><td>Secondary track</td><td>10 formal/technical prompts × 4 generations = 40 paired cells/model; never pooled with primary</td></tr><tr><td>Controls</td><td>Unsteered baseline, 3 isotropic random directions, and a visible explicit-prompt upper bound</td></tr></tbody></table></div>
    </div>{_page_footer('This is an AxBench-derived prompt study, not an official AxBench benchmark score.')}</section>''')

    pages.append(f'''<section class="sheet" id="results">{_page_header(4,total)}<div class="pagebody">
      <div class="eyebrow">Primary matched outcomes</div><h2>One shared success; two model-specific successes</h2><p class="dek">Effects are paired changes in binary Unicode-emoji presence, relative to each arm's identical alpha-zero output.</p>
      <figure class="figure"><img src="{image['replication']}" alt="Slope chart comparing Qwen and Gemma effects for each matched method"><figcaption>The glyph result increases on both sides. CAA and phrase move only on Gemma. Connected lines indicate matched construction names, not the same vector.</figcaption></figure>
      <div class="table-wrap"><table><thead><tr><th>Construction</th><th>Model</th><th>α</th><th>Ordinary effect [95% CI]</th><th>Safe</th><th>Machine verdict</th></tr></thead><tbody>{''.join(method_rows)}</tbody></table></div>
      <div class="verdict-grid"><div class="verdict worked"><span class="caps">Replicated</span><h3>NLA glyph</h3><p>Qwen +15.8 pp; Gemma +28.3 pp. Both also beat the mean of three random controls under the frozen within-model tests.</p></div><div class="verdict mixed"><span class="caps">Gemma only</span><h3>CAA</h3><p>Gemma +15.8 pp; Qwen 0.0 pp. Qwen's two answer-order half-vectors opposed each other, suggesting construction cancellation.</p></div><div class="verdict mixed"><span class="caps">Gemma only</span><h3>NLA phrase</h3><p>Gemma +8.3 pp; Qwen +0.8 pp with an interval including zero.</p></div><div class="verdict null"><span class="caps">Still pending</span><h3>Human quality check</h3><p>†“Supported” here means the frozen statistical + deterministic gate passed. The strongest final claim awaits blinded relevance/fluency ratings.</p></div></div>
    </div>{_page_footer('No cross-model pooled p-value is reported; each model has its own three-method Holm family.')}</section>''')

    pages.append(f'''<section class="sheet" id="calibration">{_page_header(5,total)}<div class="pagebody">
      <div class="eyebrow">Dose selection</div><h2>Safe alpha was chosen before held-out analysis</h2><p class="dek">Calibration used six familiar chat prompts and three generations per prompt. A dose had to pass the frozen deterministic gate before target effect could select it.</p>
      <figure class="figure"><img src="{image['calibration']}" alt="Six calibration curves for model and construction combinations"><figcaption>Stars are the locked held-out doses. Gemma's glyph effect rose sharply at 0.15; 0.20 was rejected because only half of its calibration outputs were safe.</figcaption></figure>
      <div class="two-col"><div class="table-wrap"><table><thead><tr><th>Method</th><th>Qwen α</th><th>Gemma α</th><th>Best safe calibration effect Q/G</th></tr></thead><tbody>{''.join(calibration_rows)}</tbody></table></div><div><div class="note teal"><b>Glyph:</b> Qwen locked 0.60; Gemma locked 0.15.</div><div class="note"><b>Phrase:</b> Qwen locked 0.40; Gemma locked 0.20.</div><div class="note violet"><b>CAA:</b> Qwen locked 0.10; Gemma locked 0.20.</div></div></div>
      <div class="note coral"><b>Cross-model interpretation limit.</b> This compares each method at its own calibrated safe dose. It does not show that Gemma is intrinsically “more steerable” at equal alpha, because alpha scales and residual geometries are model-specific.</div>
    </div>{_page_footer('Held-out prompts were not consulted when choosing alpha. Ties favored the smaller safe dose.')}</section>''')

    pages.append(f'''<section class="sheet" id="prompts">{_page_header(6,total)}<div class="pagebody">
      <div class="eyebrow">Prompt heterogeneity</div><h2>Average effects conceal where each direction fired</h2><p class="dek">Each cell shows the fraction of four paired generations that contained at least one Unicode emoji.</p>
      <div class="map-layout"><figure class="figure"><img src="{image['prompt_map']}" alt="Forty prompts by six model-method cells showing emoji response rates"><figcaption>Darker teal means more positive generations. A coral × marks any deterministic-gate failure within that four-generation prompt block.</figcaption></figure><div><span class="caps">Prompt blocks with any positive</span><div class="table-wrap"><table><thead><tr><th>Method</th><th>Q ordinary</th><th>G ordinary</th><th>Q formal</th><th>G formal</th></tr></thead><tbody>{''.join(prompt_rows)}</tbody></table></div><div class="note teal"><b>Shared glyph example.</b> Both models produced emoji for <i>{html.escape(str(q_example['prompt']))}</i> at the same generation seed.</div><div class="note"><b>Formal transfer differs.</b> Qwen glyph appeared in 1/10 formal prompt blocks; Gemma glyph appeared in {gemma['prompt_blocks_with_positive']['nla_glyph']['formal_technical_transfer']}/10.</div><div class="note coral"><b>Descriptive map only.</b> Prompt types were not powered as separate hypothesis families, and their cell counts are small.</div></div></div>
    </div>{_page_footer('Prompt-level variation is a design signal for follow-up studies, not a post-hoc method-ranking endpoint.')}</section>''')

    pages.append(f'''<section class="sheet" id="controls">{_page_header(7,total)}<div class="pagebody">
      <div class="eyebrow">Controls and quality</div><h2>The effect sits between a silent baseline and an explicit ceiling</h2><p class="dek">Baseline responses contained no Unicode emoji. Explicitly requesting emoji produced them in every response, showing a much stronger visible-prompt upper bound.</p>
      <figure class="figure"><img src="{image['absolute']}" alt="Absolute emoji presence for hidden methods and controls on Qwen and Gemma"><figcaption>Ordinary prompt track. Random is the mean of three random directions at the model's glyph alpha. Gemma random seed 202 produced a few positives; the hidden glyph effect remained much larger.</figcaption></figure>
      <figure class="figure"><img src="{image['safety']}" alt="Deterministic safety and conservative sensitivity by model and method"><figcaption>All six matched method/model arms cleared the preregistered 90% deterministic-safety threshold.</figcaption></figure>
      <div class="two-col"><div class="note teal"><b>Qwen glyph:</b> {100*qwen['quality']['nla_glyph']['safe_fraction']:.1f}% safe; conservative recoding still gives +{100*qwen['conservative']['nla_glyph']['ordinary_effect']:.1f} pp.</div><div class="note violet"><b>Gemma glyph:</b> {100*gemma['quality']['nla_glyph']['safe_fraction']:.1f}% safe; conservative recoding still gives +{100*gemma['conservative']['nla_glyph']['ordinary_effect']:.1f} pp.</div></div>
      <div class="note coral"><b>Not yet established:</b> deterministic coherence is not the same as instruction relevance or fluency. Qwen's 240-row and Gemma's 200-row blinded quality sheets remain unrated.</div>
    </div>{_page_footer('Unsafe outputs remain in the archive; the sensitivity analysis does not delete or conceal them.')}</section>''')

    pages.append(f'''<section class="sheet" id="examples">{_page_header(8,total)}<div class="pagebody">
      <div class="eyebrow">Paired output examples</div><h2>What changed in the generated text</h2><p class="dek">These are deterministic examples selected by a frozen rule: the first safe ordinary positive for each requested arm. They illustrate behavior; they are not substitute statistics.</p>
      <div class="note teal"><b>Same prompt and seed, glyph arm:</b> {html.escape(str(q_example['prompt']))} · seed {q_example['sample']}</div>
      <div class="responses"><div class="response-card"><span class="caps">Qwen · alpha zero</span><div class="response">{_highlight_emoji(q_example['baseline'])}</div></div><div class="response-card steered"><span class="caps">Qwen · NLA glyph α={qwen['locked_alphas']['nla_glyph']:.2f}</span><div class="response">{_highlight_emoji(q_example['steered'])}</div></div></div>
      <div class="responses"><div class="response-card"><span class="caps">Gemma · alpha zero</span><div class="response">{_highlight_emoji(g_example['baseline'])}</div></div><div class="response-card steered"><span class="caps">Gemma · NLA glyph α={gemma['locked_alphas']['nla_glyph']:.2f}</span><div class="response">{_highlight_emoji(g_example['steered'])}</div></div></div>
      <div class="two-col"><div class="note"><b>Gemma phrase example:</b> {html.escape(str(g_phrase['prompt']))}<br><span class="small">Steered excerpt: {_highlight_emoji(g_phrase['steered'])}</span></div><div class="note violet"><b>Gemma CAA example:</b> {html.escape(str(g_caa['prompt']))}<br><span class="small">Steered excerpt: {_highlight_emoji(g_caa['steered'])}</span></div></div>
    </div>{_page_footer('Highlighted glyphs were generated by the model; the hidden-arm user prompt itself was unchanged.')}</section>''')

    pages.append(f'''<section class="sheet" id="jspace">{_page_header(9,total)}<div class="pagebody">
      <div class="eyebrow">J-space boundary</div><h2>Why the direct J-lens arm is not in the matched result</h2><p class="dek">The Qwen archive includes one direct J-lens token atom. The Gemma run intentionally omitted behavioral J-lens steering, so it cannot be presented as a two-model comparison.</p>
      <div class="hero-answer"><b>The main comparison is CAA versus two NLA edit constructions.</b><p>Qwen's direct <code>“ emoji”</code> atom is preserved exactly as completed, but it produced 0.0 pp at its safe alpha 0.10 and remains exploratory. No completed Qwen data were deleted.</p></div>
      <div class="two-col"><div class="verdict exploratory"><span class="caps">What Qwen actually ran</span><h3>Direct J-atom injection</h3><p><code>d = Jᵀ(norm-scale × unembedding[token])</code></p><p>The atom was stable across split WikiText lens halves (cosine {qwen['construction_diagnostics']['jlens_split_half_atom_cosine']:.3f}) but behaviorally null at the safe dose.</p></div><div class="verdict null"><span class="caps">What this was not</span><h3>Not the earlier k=25 split</h3><p>This new direct atom did not approximate an NLA parent with 25 atoms, did not inject a parent-minus-approximation remainder, and did not move a live activation component between models.</p></div></div>
      <div class="pipeline"><div class="node"><b>Main matched report</b>CAA + NLA phrase + NLA glyph</div><div class="arrow">≠</div><div class="node"><b>Qwen exploratory arm</b>one direct J-lens token atom</div><div class="arrow">≠</div><div class="node"><b>Earlier J-derived work</b>k=25 approximation + remainder</div><div class="arrow">≠</div><div class="node"><b>Future matched J test</b>would require a frozen two-model protocol</div></div>
      <div class="note violet"><b>Interpretation:</b> split-half stability says the fitted token direction was reproducible under that corpus split. It does not imply behavioral sufficiency. The direct atom's null result does not invalidate J-space generally.</div>
    </div>{_page_footer('Gemma J-lens construction artifacts may exist for diagnostics; no Gemma J-lens behavioral arm was generated here.')}</section>''')

    pages.append(f'''<section class="sheet" id="claims">{_page_header(10,total)}<div class="pagebody">
      <div class="eyebrow">Claims, limits, and provenance</div><h2>What the combined experiment proves—and what it does not</h2><p class="dek">The strongest defensible conclusion is narrower than “emoji steering always works,” but stronger than a one-model anecdote.</p>
      <div class="table-wrap"><table class="claim-table"><tbody><tr><td>Supported now</td><td class="status-yes">At calibrated safe doses, the minimal NLA glyph construction increased Unicode-emoji presence on held-out ordinary prompts in both Qwen and Gemma.</td></tr><tr><td>Supported in Gemma only</td><td class="status-yes">This CAA construction and the longer NLA phrase construction cleared the frozen within-Gemma statistical and deterministic gates.</td></tr><tr><td>Not supported</td><td class="status-no">A model-independent ranking of CAA versus NLA; equal-alpha superiority; transport of a residual vector across models; uniform transfer to formal/technical prompts.</td></tr><tr><td>Still pending</td><td class="status-pending">Blinded instruction-relevance and fluency ratings. Until completed, do not state that instruction quality was fully preserved.</td></tr><tr><td>Exploratory only</td><td class="explore">Qwen's direct J-lens atom null and all prompt-type heterogeneity observations.</td></tr></tbody></table></div>
      <div class="metrics"><div class="metric"><strong>40</strong><span>same frozen held-out prompts per model: 30 primary ordinary + 10 secondary formal</span></div><div class="metric"><strong>4</strong><span>paired generations per prompt, with prompt-specific seeds reused across arms</span></div><div class="metric"><strong>3 + 3</strong><span>matched treatment constructions plus three independent random-direction controls</span></div></div>
      <div class="two-col"><div><span class="caps">Qwen source</span><div class="table-wrap"><table><tbody><tr><td>Model/layer</td><td>Qwen2.5-7B-Instruct · L20 · d3584</td></tr><tr><td>Run files/rows</td><td>{qwen['raw_result_files']} / {qwen['raw_records']:,}</td></tr><tr><td>Completed</td><td>{html.escape(qwen['run_completed_utc'])}</td></tr><tr><td>COMPLETE SHA-256</td><td><code>{qwen['complete_manifest_sha256']}</code></td></tr></tbody></table></div></div><div><span class="caps">Gemma source</span><div class="table-wrap"><table><tbody><tr><td>Model/layer</td><td>Gemma-3-12B-it · L32 · d3840</td></tr><tr><td>Run files/rows</td><td>{gemma['raw_result_files']} / {gemma['raw_records']:,}</td></tr><tr><td>Completed</td><td>{html.escape(gemma['run_completed_utc'])}</td></tr><tr><td>COMPLETE SHA-256</td><td><code>{gemma['complete_manifest_sha256']}</code></td></tr></tbody></table></div></div></div>
      <div class="note teal"><b>Bottom line.</b> The minimal AV-edit-AR glyph direction is the best cross-model candidate from this study. CAA deserves a corrected Qwen construction before being judged broadly, and phrase steering appears more model-sensitive.</div>
      <div class="note"><b>Next required analysis step:</b> fill the two frozen blinded rating sheets, decode them only after scoring, and report relevance/fluency alongside the already locked behavioral results.</div>
    </div>{_page_footer('Derived from hash-validated raw archives. Report generation does not alter or rerun model outputs.')}</section>''')

    nav = "".join(f'<a href="#{anchor}">{label}</a>' for anchor, label in (("start-here", "Dictionary"), ("overview", "Overview"), ("design", "Design"), ("results", "Results"), ("calibration", "Calibration"), ("prompts", "Prompts"), ("controls", "Controls"), ("examples", "Examples"), ("jspace", "J-space"), ("claims", "Claims")))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>The Cross-Model Emoji Steering Ledger</title><style>{css}</style></head><body><nav class="toc">{nav}</nav><main class="book">{''.join(pages)}</main></body></html>'''


def shared_examples(models: Mapping[str, Any], runs: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    arm = "nla_glyph"
    selected: dict[str, dict[tuple[str, int], Mapping[str, Any]]] = {}
    baselines: dict[str, dict[tuple[str, int], Mapping[str, Any]]] = {}
    for model in ("qwen", "gemma3-12b"):
        alpha = float(models[model]["locked_alphas"][arm])
        selected[model] = {
            (str(row["prompt_id"]), int(row["sample"])): row
            for row in runs[model]["results"][arm] if float(row["alpha"]) == alpha
        }
        baselines[model] = {
            (str(row["prompt_id"]), int(row["sample"])): row
            for row in runs[model]["results"][arm] if float(row["alpha"]) == 0.0
        }
    candidates = []
    for key in sorted(set(selected["qwen"]) & set(selected["gemma3-12b"])):
        if not all(selected[model][key]["target_metrics"]["target_present"] for model in selected):
            continue
        if not all(
            not _quality_failures(selected[model][key], baselines[model][key], runs[model]["quality_gate"])
            for model in selected
        ):
            continue
        candidates.append(key)
    if candidates:
        key = candidates[0]
        output["shared_glyph"] = {}
        for model, label in (("qwen", "qwen"), ("gemma3-12b", "gemma")):
            row = selected[model][key]
            base = baselines[model][key]
            output["shared_glyph"][label] = {
                "prompt_id": key[0],
                "sample": key[1],
                "prompt": str(row["prompt"]),
                "baseline": str(base["text"]),
                "steered": str(row["text"]),
                "clusters": int(row["target_metrics"]["emoji_cluster_count"]),
            }
    else:
        output["shared_glyph"] = None
    return output


def build_report(qwen_root: Path, gemma_root: Path, output_dir: Path) -> dict[str, Path]:
    _style()
    prompts = _prompt_metadata()
    lock = _methods_lock()
    runs = {
        "qwen": load_run(qwen_root.resolve(), "qwen", prompts, lock),
        "gemma3-12b": load_run(gemma_root.resolve(), "gemma3-12b", prompts, lock),
    }
    models = {model: aggregate(run, prompts) for model, run in runs.items()}
    if runs["qwen"]["summary"]["active_primary_treatments"][:3] != list(COMMON_ARMS):
        raise ReportError("Qwen matched primary treatment order drifted")
    if runs["gemma3-12b"]["summary"]["active_primary_treatments"] != list(COMMON_ARMS):
        raise ReportError("Gemma matched primary treatment family drifted")
    if models["qwen"]["instruction_quality_status"] != "pending_blinded_rating" or models["gemma3-12b"]["instruction_quality_status"] != "pending_blinded_rating":
        raise ReportError("instruction-quality status drifted")

    analysis = {
        "schema": "crossmodel-emoji-results-visual-analysis/v1",
        "claim_scope": "matched CAA, NLA phrase, and NLA glyph; model-stratified",
        "source_prompt_bank": {
            "path": "lab/experiments/emoji_method_comparison/prompts.heldout.yaml",
            "sha256": _sha256(_lab_root() / "experiments" / "emoji_method_comparison" / "prompts.heldout.yaml"),
            "prompts": 40,
            "ordinary": 30,
            "formal": 10,
            "generations_per_prompt": 4,
        },
        "models": models,
        "examples": shared_examples(models, runs),
        "combined_archived_rows": sum(model["raw_records"] for model in models.values()),
        "combined_archived_files": sum(model["raw_result_files"] for model in models.values()),
        "jspace_scope": "Qwen direct atom preserved as exploratory; Gemma behavioral J-lens arm absent by design",
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    figures_dir = output_dir / "figures"
    figures = {
        "effects": figures_dir / "01-crossmodel-effects.png",
        "replication": figures_dir / "02-replication-slope.png",
        "calibration": figures_dir / "03-calibration-grid.png",
        "absolute": figures_dir / "04-absolute-controls.png",
        "prompt_map": figures_dir / "05-prompt-map.png",
        "safety": figures_dir / "06-safety-sensitivity.png",
    }
    chart_effects(models, figures["effects"])
    chart_replication(models, figures["replication"])
    chart_calibration(models, figures["calibration"])
    chart_absolute(models, figures["absolute"])
    chart_prompt_map(models, prompts, figures["prompt_map"])
    chart_safety(models, figures["safety"])

    analysis_path = output_dir / "analysis.json"
    analysis_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    rendered = render_html(analysis, figures)
    ledger_path = output_dir / "gemma_qwen_emoji_steering_ledger.html"
    alias_path = output_dir / "crossmodel_emoji_results.html"
    ledger_path.write_text(rendered, encoding="utf-8")
    alias_path.write_text(rendered, encoding="utf-8")
    return {"ledger": ledger_path, "alias": alias_path, "analysis": analysis_path, **figures}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwen-run", type=Path, required=True)
    parser.add_argument("--gemma-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "output")
    args = parser.parse_args()
    outputs = build_report(args.qwen_run, args.gemma_run, args.output_dir)
    print(json.dumps({key: str(path.resolve()) for key, path in outputs.items()}, indent=2))


if __name__ == "__main__":
    main()
