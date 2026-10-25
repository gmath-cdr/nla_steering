"""Build a self-contained HTML ledger for the AXB44 cross-model campaign."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


COLORS = {
    "paper": "#F4F0E8", "sheet": "#FFFEFA", "ink": "#17303B",
    "muted": "#6D797E", "grid": "#DDD7CC", "qwen": "#087F78",
    "gemma": "#7764B4", "prompt": "#D88A25", "diffmean": "#2C7FB8",
    "caa": "#9AA3A8", "nla": "#D45F4B", "base": "#314854",
}
ARMS = ("unsteered", "axbench_prompt_steering", "diffmean", "caa", "nla")
ARM_LABELS = {
    "unsteered": "Unsteered", "axbench_prompt_steering": "AxBench Prompt",
    "diffmean": "DiffMean", "caa": "CAA", "nla": "NLA",
}
ARM_COLORS = {
    "unsteered": COLORS["base"], "axbench_prompt_steering": COLORS["prompt"],
    "diffmean": COLORS["diffmean"], "caa": COLORS["caa"], "nla": COLORS["nla"],
}
MODELS = {"qwen": "Qwen2.5-7B · L20", "gemma": "Gemma-3-12B · L32"}
CONCEPT_SHORT = {
    "20": "medical treatment outcomes", "95": "real estate / property",
    "237": "automotive performance", "256": "concern / inaction",
    "288": "outreach / engagement", "313": "ticket sales / availability",
    "362": "surprise / disbelief", "403": "elegance / elaboration",
    "491": "promotions / discounts", "499": "positive / enjoyable",
}


class ReportError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "axes.facecolor": COLORS["paper"], "figure.facecolor": COLORS["paper"],
        "savefig.facecolor": COLORS["paper"], "axes.edgecolor": COLORS["grid"],
        "axes.labelcolor": COLORS["ink"], "axes.titlecolor": COLORS["ink"],
        "xtick.color": COLORS["muted"], "ytick.color": COLORS["ink"],
        "text.color": COLORS["ink"],
    })


def save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=190, bbox_inches="tight", facecolor=COLORS["paper"])
    plt.close(fig)


def overall_chart(data: dict[str, Any], path: Path) -> None:
    x = np.arange(len(ARMS)); width = 0.36
    fig, ax = plt.subplots(figsize=(10.2, 4.5))
    for offset, model in ((-width / 2, "qwen"), (width / 2, "gemma")):
        values = [data["models"][model]["summaries"]["balanced_union"][a]["mean_axbench_harmonic"] for a in ARMS]
        bars = ax.bar(x + offset, values, width, label=MODELS[model], color=COLORS[model])
        ax.bar_label(bars, labels=[f"{v:.2f}" for v in values], padding=3, fontsize=8.5)
    ax.set_xticks(x, [ARM_LABELS[a] for a in ARMS])
    ax.set_ylabel("Mean AxBench harmonic score (0–2)")
    ax.set_ylim(0, 1.3); ax.grid(axis="y", color=COLORS["grid"], linewidth=.7)
    ax.spines[["top", "right"]].set_visible(False); ax.legend(frameon=False, ncol=2)
    ax.set_title("Visible prompting was strongest; hidden steering was mixed")
    save(fig, path)


def component_chart(data: dict[str, Any], path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharey=True)
    metrics = (("mean_concept", "Concept"), ("mean_instruction", "Instruction"), ("mean_fluency", "Fluency"))
    x = np.arange(len(ARMS)); width = .22
    for ax, model in zip(axes, ("qwen", "gemma")):
        summaries = data["models"][model]["summaries"]["balanced_union"]
        for index, (metric, label) in enumerate(metrics):
            vals = [summaries[a][metric] for a in ARMS]
            ax.bar(x + (index - 1) * width, vals, width, label=label)
        ax.set_xticks(x, [ARM_LABELS[a] for a in ARMS], rotation=24, ha="right")
        ax.set_title(MODELS[model]); ax.set_ylim(0, 2.1)
        ax.grid(axis="y", color=COLORS["grid"], linewidth=.7)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("AxBench judge score (0–2)")
    axes[1].legend(frameon=False, ncol=3, loc="upper center")
    fig.suptitle("The harmonic score charges concept gain for instruction and fluency loss", y=1.02)
    save(fig, path)


def forest_chart(data: dict[str, Any], path: Path) -> None:
    rows = []
    for model in ("qwen", "gemma"):
        for key, label in (("nla_vs_diffmean", "NLA − DiffMean"), ("nla_vs_caa", "NLA − CAA")):
            value = data["models"][model]["primary_contrasts"][key]
            rows.append((f"{MODELS[model]} · {label}", value))
    fig, ax = plt.subplots(figsize=(9.2, 3.7))
    y = np.arange(len(rows))[::-1]
    for yi, (label, value) in zip(y, rows):
        color = COLORS["qwen"] if label.startswith("Qwen") else COLORS["gemma"]
        mean = value["mean_difference"]
        ax.errorbar(mean, yi, xerr=[[mean - value["ci95_low"]], [value["ci95_high"] - mean]],
                    fmt="o", color=color, capsize=4, markersize=7)
        ax.text(value["ci95_high"] + .025, yi, f"Holm p={value['holm_adjusted_p']:.3f}", va="center", fontsize=8.5)
    ax.axvline(0, color=COLORS["base"], linewidth=1)
    ax.set_yticks(y, [row[0] for row in rows]); ax.set_xlabel("Paired mean difference in harmonic score")
    ax.set_title("Preregistered primary contrasts · 95% multilevel-bootstrap intervals")
    ax.grid(axis="x", color=COLORS["grid"], linewidth=.7); ax.spines[["top", "right"]].set_visible(False)
    save(fig, path)


def track_chart(data: dict[str, Any], path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=True)
    tracks = (("concept500_negative", "Concept500 negative prompts"), ("shared_alpaca_transfer", "Shared Alpaca transfer prompts"))
    x = np.arange(len(ARMS)); width = .36
    for ax, (track, title) in zip(axes, tracks):
        for offset, model in ((-width / 2, "qwen"), (width / 2, "gemma")):
            vals = [data["models"][model]["summaries"][track][a]["mean_axbench_harmonic"] for a in ARMS]
            ax.bar(x + offset, vals, width, color=COLORS[model], label=MODELS[model])
        ax.set_xticks(x, [ARM_LABELS[a] for a in ARMS], rotation=24, ha="right")
        ax.set_title(title); ax.grid(axis="y", color=COLORS["grid"], linewidth=.7)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("Mean harmonic score (0–2)"); axes[0].set_ylim(0, 1.25)
    axes[1].legend(frameon=False); fig.suptitle("Prompt tracks are shown separately; these comparisons are descriptive", y=1.02)
    save(fig, path)


def concept_heatmap(data: dict[str, Any], model: str, path: Path) -> None:
    concepts = data["models"][model]["per_concept_descriptive"]
    ids = list(concepts)
    values = np.array([[concepts[c]["arms"][a]["mean_axbench_harmonic"] for a in ("diffmean", "caa", "nla")] for c in ids])
    fig, ax = plt.subplots(figsize=(7.8, 5.2))
    image = ax.imshow(values, cmap="YlGnBu", vmin=0, vmax=max(1.2, float(values.max())))
    ax.set_xticks(range(3), ["DiffMean", "CAA", "NLA"])
    labels = [f"{c} · {CONCEPT_SHORT.get(c, concepts[c]['label'][:32])}" for c in ids]
    ax.set_yticks(range(len(ids)), labels, fontsize=8.2)
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            ax.text(j, i, f"{values[i,j]:.2f}", ha="center", va="center",
                    color="white" if values[i,j] > .65 else COLORS["ink"], fontsize=8)
    ax.set_title(f"{MODELS[model]} · per-concept held-out scores (descriptive)")
    fig.colorbar(image, ax=ax, fraction=.035, pad=.03, label="Harmonic score")
    save(fig, path)


def alpha_chart(data: dict[str, Any], path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 5), sharey=True)
    for ax, model in zip(axes, ("qwen", "gemma")):
        rows = data["models"][model]["selected_calibration_alpha"]
        ids = list(rows); vals = np.array([[rows[c][a] for a in ("diffmean", "caa", "nla")] for c in ids])
        im = ax.imshow(vals, cmap="OrRd", vmin=0, vmax=.75)
        ax.set_xticks(range(3), ["DiffMean", "CAA", "NLA"]); ax.set_yticks(range(len(ids)), ids)
        ax.set_title(MODELS[model]); ax.set_xlabel("Method")
        for i in range(vals.shape[0]):
            for j in range(vals.shape[1]): ax.text(j, i, f"{vals[i,j]:g}", ha="center", va="center", fontsize=8)
    axes[0].set_ylabel("Concept ID"); fig.colorbar(im, ax=axes, fraction=.025, pad=.03, label="Frozen α")
    fig.suptitle("Calibration selected one concept-specific α before held-out evaluation", y=.98)
    save(fig, path)


def image_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def metric(model: dict[str, Any], arm: str) -> float:
    return model["summaries"]["balanced_union"][arm]["mean_axbench_harmonic"]


def build_html(data: dict[str, Any], figures: dict[str, Path], analysis_sha: str) -> str:
    q, g = data["models"]["qwen"], data["models"]["gemma"]
    q_primary, g_primary = q["primary_contrasts"], g["primary_contrasts"]
    concept_rows = []
    for cid, row in q["per_concept_descriptive"].items():
        concept_rows.append(f"<tr><td>{cid}</td><td>{html.escape(row['label'])}</td>"
                            f"<td>{row['arms']['nla']['mean_axbench_harmonic']:.2f}</td>"
                            f"<td>{g['per_concept_descriptive'][cid]['arms']['nla']['mean_axbench_harmonic']:.2f}</td></tr>")
    imgs = {key: image_uri(value) for key, value in figures.items()}
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>AXB44 Cross-model Steering Ledger</title>
<style>
:root{{--paper:{COLORS['paper']};--sheet:{COLORS['sheet']};--ink:{COLORS['ink']};--muted:{COLORS['muted']};--teal:{COLORS['qwen']};--violet:{COLORS['gemma']};--coral:{COLORS['nla']};--line:{COLORS['grid']}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.48 Inter,Segoe UI,Arial,sans-serif}} main{{max-width:1120px;margin:auto;padding:34px 24px 80px}} section{{background:var(--sheet);border:1px solid var(--line);border-radius:18px;padding:34px;margin:0 0 22px;box-shadow:0 6px 20px #20323b0b}} h1{{font:700 48px/1.02 Georgia,serif;margin:8px 0 15px;max-width:800px}} h2{{font:700 29px/1.15 Georgia,serif;margin:0 0 15px}} h3{{margin:0 0 7px}} p{{max-width:880px}} .eyebrow{{letter-spacing:.13em;text-transform:uppercase;font-size:12px;font-weight:700;color:var(--teal)}} .lead{{font-size:19px;color:#40545d;max-width:900px}} .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}} .card{{background:#f8f5ef;border-radius:13px;padding:18px;border:1px solid #e7e0d5}} .big{{font-size:32px;font-weight:750;line-height:1}} .small{{font-size:12px;color:var(--muted)}} .callout{{border-left:5px solid var(--coral);padding:14px 18px;background:#fff4f0;border-radius:8px;margin:16px 0}} .flow{{display:grid;grid-template-columns:repeat(5,1fr);gap:9px;margin:20px 0}} .flow div{{padding:13px 10px;background:#f4f7f6;border-radius:10px;text-align:center;font-size:13px}} img{{width:100%;height:auto;display:block}} figure{{margin:18px 0 0}} figcaption{{color:var(--muted);font-size:12px;margin-top:6px}} table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}} th{{font-size:11px;text-transform:uppercase;letter-spacing:.08em}} code{{font-size:12px}} .pill{{display:inline-block;padding:4px 9px;background:#e8f2f0;border-radius:99px;font-size:12px;margin:2px}} .warn{{color:#874b15}} footer{{color:var(--muted);font-size:12px;padding:10px}}
@media(max-width:760px){{.grid,.flow{{grid-template-columns:1fr}}h1{{font-size:36px}}section{{padding:24px}}}}
</style></head><body><main>
<section><div class='eyebrow'>AXB44 · frozen cross-model campaign · no JLens</div><h1>What transferred when concept steering met fresh prompts?</h1><p class='lead'>A prospectively frozen, curated ten-concept panel tested visible prompting and three hidden-state methods on Qwen and Gemma. The answer is not a simple NLA win: direct prompting was strongest, DiffMean had the highest hidden-method point estimate, and NLA showed genuine but uneven transfer.</p>
<div class='grid'><div class='card'><div class='big'>2,144</div><b>held-out generations</b><div class='small'>1,072 per model; all retrieved and hash-verified</div></div><div class='card'><div class='big'>11,952</div><b>judge calls</b><div class='small'>exactly three AxBench calls per calibration/evaluation output</div></div><div class='card'><div class='big'>10 × 2</div><b>concepts × models</b><div class='small'>five lexical/domain and five abstract/style concepts</div></div></div>
<div class='callout'><b>Headline:</b> PromptSteering scored {metric(q,'axbench_prompt_steering'):.2f} on Qwen and {metric(g,'axbench_prompt_steering'):.2f} on Gemma. Among hidden methods, DiffMean scored {metric(q,'diffmean'):.2f}/{metric(g,'diffmean'):.2f}; NLA scored {metric(q,'nla'):.2f}/{metric(g,'nla'):.2f}; CAA scored {metric(q,'caa'):.2f}/{metric(g,'caa'):.2f}.</div></section>

<section><div class='eyebrow'>Dictionary first</div><h2>What each term means</h2><div class='grid'>
<div class='card'><h3>Carrier prompt</h3>The ordinary user request answered by the model. It does not ask for the target concept.</div><div class='card'><h3>Concept</h3>The content or style we try to introduce into an otherwise unrelated answer.</div><div class='card'><h3>Calibration</h3>Separate prompts used only to choose α. Their outputs are not counted as held-out evidence.</div>
<div class='card'><h3>α (alpha)</h3>The preselected strength of a hidden-state direction. One value was frozen per concept, method, and model.</div><div class='card'><h3>AxBench Prompt</h3>A visible instruction asking for the concept. It is a practical ceiling/reference, not hidden steering.</div><div class='card'><h3>Controls</h3>Unsteered output and three matched random directions show what happens without a meaningful target direction.</div>
<div class='card'><h3>AxBench harmonic</h3>A 0–2 combined score balancing concept presence, instruction adherence, and fluency.</div><div class='card'><h3>Prompt-specific NLA</h3>The AV verbalizes the carrier state, the concept sentence is appended, and AR reconstructs a carrier-specific direction.</div><div class='card'><h3>Fail-closed</h3>Any hash, count, cell, checkpoint, receipt, or judge mismatch stops release instead of silently continuing.</div></div></section>

<section><div class='eyebrow'>Experimental path</div><h2>What happened, in order</h2><div class='flow'><div><b>1 · Freeze</b><br>10 concepts, 44 unique prompt strings, splits, seeds, metrics</div><div><b>2 · Build</b><br>model-specific DiffMean, CAA, and prompt-specific NLA directions</div><div><b>3 · Calibrate</b><br>sweep α on calibration prompts only</div><div><b>4 · Evaluate</b><br>release fixed α on untouched prompts</div><div><b>5 · Judge</b><br>three official-style calls per output; paired analysis</div></div>
<p><span class='pill'>Unsteered</span><span class='pill'>AxBench Prompt</span><span class='pill'>DiffMean</span><span class='pill'>CAA</span><span class='pill'>NLA</span><span class='pill'>Random ×3</span></p><p><b>Important:</b> NLA was injected at last-token-plus-decode positions, while historical DiffMean/CAA used all-position injection. That reproduces the frozen paper protocols, but it is not an intervention-schedule-matched ablation.</p></section>

<section><div class='eyebrow'>Overall scores</div><h2>Visible prompting dominates; hidden steering varies</h2><figure><img src='{imgs['overall']}' alt='Grouped bar chart of harmonic scores'><figcaption>Balanced union: 80 paired held-out cells per named arm and model.</figcaption></figure></section>

<section><div class='eyebrow'>Quality-aware metric</div><h2>Concept gain is not enough by itself</h2><p>The harmonic score falls when a method mentions the concept but damages instruction adherence or fluency. DiffMean produced the strongest hidden-method concept score, but also the highest target-model perplexity: {q['summaries']['balanced_union']['diffmean']['mean_target_model_perplexity']:.1f} on Qwen and {g['summaries']['balanced_union']['diffmean']['mean_target_model_perplexity']:.1f} on Gemma.</p><figure><img src='{imgs['components']}' alt='Concept instruction and fluency components'><figcaption>Means from the exact AxBench three-call judge. Perplexity is computed separately by the target model.</figcaption></figure></section>

<section><div class='eyebrow'>Preregistered comparisons</div><h2>The central claim is mixed, not a ranking victory</h2><figure><img src='{imgs['forest']}' alt='Forest plot of NLA primary contrasts'><figcaption>Positive favors NLA. Intervals resample concepts and prompts while preserving paired seeds.</figcaption></figure>
<div class='callout'><b>Qwen:</b> NLA trailed DiffMean by {abs(q_primary['nla_vs_diffmean']['mean_difference']):.2f}; the interval crossed zero. NLA exceeded CAA by {q_primary['nla_vs_caa']['mean_difference']:.2f}, but Holm-adjusted p={q_primary['nla_vs_caa']['holm_adjusted_p']:.3f} is just above .05. <b>Gemma:</b> neither NLA comparison separated clearly from zero.</div>
<p>NLA did beat each of the three matched random controls on both models in descriptive paired analyses. That supports direction-specific signal; it does not establish NLA as the best method.</p></section>

<section><div class='eyebrow'>Generalization view</div><h2>Where the prompt came from mattered</h2><p>The frozen design used two held-out tracks: source-negative Concept500 prompts specific to each concept, and the same shared open-ended Alpaca prompts across concepts. We report them separately and do not treat either small track as a standalone confirmatory result.</p><figure><img src='{imgs['tracks']}' alt='Scores split by prompt track'><figcaption>Each track has 40 paired cells per arm/model. Track contrasts are descriptive.</figcaption></figure></section>

<section><div class='eyebrow'>Concept-level anatomy</div><h2>No method worked uniformly across concepts</h2><div class='grid' style='grid-template-columns:1fr 1fr'><figure><img src='{imgs['qwen_heat']}' alt='Qwen concept heatmap'></figure><figure><img src='{imgs['gemma_heat']}' alt='Gemma concept heatmap'></figure></div><p class='warn'><b>Do not rank concepts from this alone.</b> Each concept has only eight held-out cells (two prompts per track × two seeds). These heatmaps are hypothesis-generating.</p></section>

<section><div class='eyebrow'>Calibration receipt</div><h2>Strength was chosen before evaluation</h2><p>Calibration searched the frozen grid, applied the frozen rule, and sealed one α for every concept/method/model. Evaluation then used only that chosen α; it did not search the held-out responses.</p><figure><img src='{imgs['alpha']}' alt='Selected alpha heatmap'><figcaption>Different checkpoints and directions require model-specific calibration. Raw α values are not directly comparable as effect sizes.</figcaption></figure></section>

<section><div class='eyebrow'>What this closes—and what it does not</div><h2>Claim boundary</h2><div class='grid'>
<div class='card'><h3>Supported</h3><ul><li>Visible AxBench prompting was strongest.</li><li>DiffMean and NLA both carried concept signal beyond random controls.</li><li>Performance changed substantially by concept and prompt track.</li></ul></div>
<div class='card'><h3>Not supported</h3><ul><li>No general NLA-over-DiffMean victory.</li><li>No arbitrary-concept generality claim.</li><li>No per-concept significance claims.</li><li>No J-space or JLens conclusion.</li></ul></div>
<div class='card'><h3>Design limits</h3><ul><li>Curated, outcome-blind panel—not random Concept500.</li><li>Two held-out prompts per track.</li><li>Injection schedules differ by historical method.</li><li>One exact judge model and fixed decoding protocol.</li></ul></div></div>
<h3 style='margin-top:24px'>Ten frozen concepts</h3><table><thead><tr><th>ID</th><th>Concept label</th><th>Qwen NLA</th><th>Gemma NLA</th></tr></thead><tbody>{''.join(concept_rows)}</tbody></table></section>

<section><div class='eyebrow'>Provenance</div><h2>Every displayed number traces to verified artifacts</h2><p><b>Campaign:</b> <code>axb44-crossmodel-v1</code><br><b>Frozen review hash:</b> <code>0fd7075c011d08192185a8fa60603942eef7458ebadf03bbb0f5974edf85cd56</code><br><b>Analysis SHA-256:</b> <code>{analysis_sha}</code><br><b>Qwen rows:</b> {q['evaluation_rows']} · <b>Gemma rows:</b> {g['evaluation_rows']} · <b>JLens arms:</b> 0</p><p>The two complete manifests each registered 1,403 files; a second local audit found zero missing or mismatched files. All RunPod pods were confirmed stopped before this report was generated.</p></section>
<footer>AXB44 Cross-model Steering Ledger · generated from held-out, hash-verified artifacts · 2026-08-26</footer>
</main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.analysis.read_text(encoding="utf-8"))
    if data.get("schema") != "axb44-heldout-analysis/v1" or data.get("campaign_id") != "axb44-crossmodel-v1":
        raise ReportError("analysis identity mismatch")
    if set(data.get("models", {})) != {"qwen", "gemma"}:
        raise ReportError("expected exactly qwen and gemma")
    for model in ("qwen", "gemma"):
        if data["models"][model].get("evaluation_rows") != 1072:
            raise ReportError(f"unexpected held-out row count for {model}")
        if len(data["models"][model].get("per_concept_descriptive", {})) != 10:
            raise ReportError(f"unexpected concept count for {model}")
    style(); fig_dir = args.output.parent / "figures"
    figures = {
        "overall": fig_dir / "overall_harmonic.png", "components": fig_dir / "judge_components.png",
        "forest": fig_dir / "primary_contrasts.png", "tracks": fig_dir / "prompt_tracks.png",
        "qwen_heat": fig_dir / "qwen_concepts.png", "gemma_heat": fig_dir / "gemma_concepts.png",
        "alpha": fig_dir / "selected_alphas.png",
    }
    overall_chart(data, figures["overall"]); component_chart(data, figures["components"])
    forest_chart(data, figures["forest"]); track_chart(data, figures["tracks"])
    concept_heatmap(data, "qwen", figures["qwen_heat"]); concept_heatmap(data, "gemma", figures["gemma_heat"])
    alpha_chart(data, figures["alpha"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_html(data, figures, sha256(args.analysis)), encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
