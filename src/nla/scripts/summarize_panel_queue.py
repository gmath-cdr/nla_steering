"""Audit raw panel artifacts and write machine-readable and Markdown comparisons."""
from __future__ import annotations

import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.confirmation_metrics import jaccard, surface_quality, tokens
from experiments.discovery500 import bootstrap_lower, matches


def baseline_summary(base, model):
    data = json.loads((base / model / "results.json").read_text())
    terms = json.loads((ROOT / "experiments/configs/gemma_qwen_positive14_terms.json").read_text())
    refs = {}
    for x in data:
        if x["method"] == "unsteered":
            refs.setdefault((x["concept_id"], x["prompt_index"]), []).append(x["response"])
    methods = sorted({x["method"] for x in data})
    summaries, flags = [], {}
    for method in methods:
        subset = [x for x in data if x["method"] == method]
        evaluated = []
        coverage = {}
        for x in subset:
            lex = terms[str(x["concept_id"])]
            hit = bool(matches(x["response"], lex))
            surface = surface_quality(x["response"])
            similarity = max(jaccard(tokens(x["response"]), tokens(ref))
                             for ref in refs[(x["concept_id"], x["prompt_index"])])
            term_count = len(matches(x["response"], lex))
            quality = (8 <= surface["word_count"] <= 350 and
                       surface["repeated_trigram_rate"] <= .10 and similarity >= .12)
            evaluated.append((hit, quality, similarity, surface["repeated_trigram_rate"], term_count))
            coverage.setdefault((x["concept_id"], x["prompt_index"]), []).append(hit)
        n = len(evaluated)
        flags[method] = [x[0] for x in evaluated]
        summaries.append({"model": model, "method": method, "n": n,
                          "hit_rate": sum(x[0] for x in evaluated) / n,
                          "quality_pass_rate": sum(x[1] for x in evaluated) / n,
                          "prompt_coverage": sum(any(v) for v in coverage.values()) / len(coverage),
                          "mean_reference_similarity": sum(x[2] for x in evaluated) / n,
                          "mean_repeated_trigram_rate": sum(x[3] for x in evaluated) / n,
                          "mean_target_term_count": sum(x[4] for x in evaluated) / n})
    by_method = {x["method"]: x for x in summaries}
    strongest = max((x for x in summaries if x["method"].startswith("random_")),
                    key=lambda x: x["hit_rate"])
    strongest_row = dict(strongest, method="strongest_random",
                         source_random_method=strongest["method"])
    summaries.append(strongest_row)
    base_rate = by_method["unsteered"]["hit_rate"]
    for row in summaries:
        row["lift_vs_unsteered"] = row["hit_rate"] - base_rate
        if not row["method"].startswith("random_") and row["method"] not in ("unsteered", "strongest_random"):
            row["lift_vs_strongest_random"] = row["hit_rate"] - strongest["hit_rate"]
            row["bootstrap_lower_vs_unsteered"] = bootstrap_lower(flags[row["method"]], flags["unsteered"])
            row["bootstrap_lower_vs_strongest_random"] = bootstrap_lower(
                flags[row["method"]], flags[strongest["method"]])
    return summaries


def nla_summary(base, model):
    bundle = json.loads((base / f"{model}_nla" / "confirmation_summary.json").read_text())
    concepts = bundle["concepts"]
    rows = []
    for method in ("unsteered", "av_edit", "random"):
        arms = [x["arms"][method] for x in concepts]
        row = {"model": model, "method": f"nla_{method}",
               "n": sum(x["n"] for x in arms)}
        for key in ("hit_rate", "quality_pass_rate", "mean_reference_similarity",
                    "mean_repeated_trigram_rate", "mean_target_term_count"):
            row[key] = sum(x[key] * x["n"] for x in arms) / row["n"]
        if method == "av_edit":
            row["prompt_coverage"] = sum(x["prompt_coverage"] for x in concepts) / len(concepts)
            row["lift_vs_unsteered"] = sum(x["lift_vs_unsteered"] for x in concepts) / len(concepts)
            row["lift_vs_strongest_random"] = sum(x["lift_vs_random"] for x in concepts) / len(concepts)
            row["bootstrap_lower_vs_unsteered"] = min(x["bootstrap_lower_vs_unsteered"] for x in concepts)
            row["bootstrap_lower_vs_strongest_random"] = min(x["bootstrap_lower_vs_random"] for x in concepts)
        rows.append(row)
    return rows


def main():
    base = ROOT / "artifacts/baseline_panel14"
    rows = []
    for model in ("qwen", "gemma"):
        rows += baseline_summary(base, model)
        rows += nla_summary(base, model)
    (base / "combined_summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    shown = [x for x in rows if x["method"] in
             ("unsteered", "strongest_random", "diffmean", "caa_fixed",
              "caa_calibrated", "nla_av_edit")]
    lines = ["# Qwen-selected 14-concept steering comparison", "",
             "Gemma rows are transfer results on the Qwen-confirmed panel.", "",
             "| Model | Method | N | Hit | Coverage | Quality | Lift/base | Lift/random |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for x in shown:
        pct = lambda key: "—" if key not in x else f"{x[key]:.1%}"
        lines.append(f"| {x['model']} | {x['method']} | {x['n']} | {pct('hit_rate')} | "
                     f"{pct('prompt_coverage')} | {pct('quality_pass_rate')} | "
                     f"{pct('lift_vs_unsteered')} | {pct('lift_vs_strongest_random')} |")
    (base / "combined_report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
