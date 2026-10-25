"""Merge the shards of the random 50-concept panel and build its tables.

Read-only on the shard folders. The definitions are copied from the scripts
that produced the selected-panel tables, so both panels are scored one way:

  table rows   src/nla/scripts/summarize_panel_queue.py
               analysis/reproduce.py (panel_rows, the direct-prompting row)
  intervals    experiments/final_gemma_replication_analysis.py on the team's
               steering branch (20,000 concept resamples, seed 20260825)
  helpers      src/nla/experiments/confirmation_metrics.py and discovery500.py

The helpers are copied instead of imported because their modules import torch.
``--self-check`` recomputes the archived 14-concept panel with this file and
requires the stored values, so a drifted copy fails.

Added here and not in the selected-panel scripts: the paired per-concept
differences (``paired`` in the output) and the per-concept table.
"""

from __future__ import annotations

import argparse
import json
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
ARCHIVE = REPO / "artifacts/paper_runs/data"
INTERVAL_SEED = 20260825
RESAMPLES = 20000
BASELINE_METHODS = ("unsteered", "caa_fixed", "caa_calibrated", "diffmean")
TABLE_ORDER = (
    ("Unsteered", "unsteered"), ("Strongest random", "strongest_random"),
    ("Fixed CAA", "caa_fixed"), ("Calibrated CAA", "caa_calibrated"),
    ("DiffMean", "diffmean"), ("NLA steering", "nla_av_edit"),
    ("Direct prompting", "prompting"),
)

# ---- copied from src/nla/experiments/confirmation_metrics.py ----------------
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "could", "did", "do", "does", "for", "from", "had", "has",
    "have", "how", "i", "if", "in", "into", "is", "it", "its", "may",
    "my", "of", "on", "or", "our", "should", "so", "than", "that", "the",
    "their", "then", "there", "these", "they", "this", "to", "was", "we",
    "were", "what", "when", "where", "which", "who", "why", "will", "with",
    "would", "you", "your",
}


def tokens(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9']+", text.lower())
            if len(word) > 2 and word not in STOPWORDS}


def jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(1, len(left | right))


def surface_quality(response: str) -> dict:
    words = re.findall(r"[a-z0-9']+", response.lower())
    trigrams = list(zip(words, words[1:], words[2:]))
    repeated = len(trigrams) - len(set(trigrams))
    return {
        "word_count": len(words),
        "repeated_trigram_rate": repeated / max(1, len(trigrams)),
    }


# ---- copied from src/nla/experiments/discovery500.py ------------------------
def matches(text: str, terms: list[str]) -> list[str]:
    if not terms:
        return []
    pattern = re.compile(r"(?i)(?<!\w)(?:" + "|".join(
        re.escape(term) for term in sorted(terms, key=len, reverse=True)) + r")(?!\w)")
    return pattern.findall(text)


def bootstrap_lower(left: list[bool], right: list[bool], resamples: int = 2000) -> float:
    import random
    rng = random.Random(0)
    values = []
    for _ in range(resamples):
        indices = [rng.randrange(len(left)) for _ in left]
        values.append(sum(left[index] for index in indices) / len(indices) -
                      sum(right[index] for index in indices) / len(indices))
    values.sort()
    return values[max(0, int(0.05 * len(values)))]


# ---- copied from src/nla/scripts/summarize_panel_queue.py -------------------
# Differences: the rows and word lists are passed in, and the per-response
# quality flag is also returned so the per-concept table can use it.
def baseline_summary(data: list[dict], terms: dict, model: str) -> tuple[list[dict], dict]:
    refs: dict = {}
    for x in data:
        if x["method"] == "unsteered":
            refs.setdefault((x["concept_id"], x["prompt_index"]), []).append(x["response"])
    methods = sorted({x["method"] for x in data})
    summaries, flags, per_response = [], {}, {}
    for method in methods:
        subset = [x for x in data if x["method"] == method]
        evaluated = []
        coverage: dict = {}
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
            per_response.setdefault(method, []).append((x["concept_id"], hit, quality))
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
    return summaries, per_response


def nla_summary(concepts: list[dict], model: str) -> list[dict]:
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


# ---- copied from analysis/reproduce.py (panel_rows) -------------------------
# Difference: quality is the mean of the per-response flag, which equals the
# stored ``rescored.quality_pass_rate`` (checked by --self-check).
def prompting_row(records: list[dict], model: str) -> dict:
    keys = {(r["concept_id"], r["prompt_index"]) for r in records}
    return {"model": model, "method": "prompting", "n": len(records),
            "hit_rate": sum(bool(r["hit"]) for r in records) / len(records),
            "prompt_coverage": len({(r["concept_id"], r["prompt_index"]) for r in records if r["hit"]}) / len(keys),
            "quality_pass_rate": sum(bool(r["quality_pass"]) for r in records) / len(records)}


# ---- copied from experiments/final_gemma_replication_analysis.py ------------
def interval(values, rng) -> list[float]:
    v = np.asarray(values, dtype=float)
    d = rng.choice(v, (RESAMPLES, len(v)), replace=True).mean(1)
    return [float(x) for x in np.quantile(d, [.025, .975])]


def matched(data: list[dict], method: str) -> dict[int, float]:
    r = [x for x in data if x["method"] == method]
    ids = sorted({x["concept_id"] for x in r})
    return {i: float(np.mean([x["hit"] for x in r if x["concept_id"] == i])) for i in ids}


# -----------------------------------------------------------------------------
def load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def collect(shard_roots: list[Path], model: str, allow_partial: bool) -> dict:
    """Concatenate the per-shard outputs. Each concept belongs to one shard."""
    merged = {"results": [], "selections": [], "nla_summary": [], "nla_confirmation": [],
              "nla_strengths": {}, "prompting": [], "prompting_summary": [], "shards": []}
    for root in shard_roots:
        for shard in sorted(Path(root).glob("shard_*")):
            status = load(shard / "STATUS.json") if (shard / "STATUS.json").exists() else {"status": "missing"}
            if status.get("status") != "complete":
                if allow_partial:
                    continue
                raise SystemExit(f"{shard} is not complete: {status}")
            merged["shards"].append({"path": f"{Path(root).name}/{shard.name}", "ids": (shard / "shard.ids").read_text().strip(),
                                     "finished_utc": status["utc"],
                                     "commit": (shard / "queue_logs/repo.commit").read_text().strip()})
            merged["results"] += load(shard / model / "results.json")
            merged["selections"] += load(shard / model / "alpha_selections.json")
            merged["nla_summary"] += load(shard / f"{model}_nla" / "summary.json")["concepts"]
            merged["nla_confirmation"] += load(shard / f"{model}_nla" / "confirmation_summary.json")["concepts"]
            merged["nla_strengths"].update(load(shard / f"{model}_nla_calibration" / "selected_alphas.json"))
            merged["prompting"] += load(shard / f"{model}_prompting" / "results_matched_unsteered.json")
            merged["prompting_summary"] += load(shard / f"{model}_prompting" / "summary.json")["concepts"]
    seen = [row["concept_id"] for row in merged["nla_summary"]]
    if len(seen) != len(set(seen)):
        raise SystemExit("a concept appears in more than one shard")
    return merged


def summarize(merged: dict, terms: dict, model: str) -> dict:
    data = merged["results"]
    stored_hits = [bool(x["hit"]) for x in data]
    fresh_hits = [bool(matches(x["response"], terms[str(x["concept_id"])])) for x in data]
    if stored_hits != fresh_hits:
        raise SystemExit("stored hit flags disagree with the word lists")
    baseline_rows, per_response = baseline_summary(data, terms, model)
    rows = baseline_rows + nla_summary(merged["nla_confirmation"], model)
    rows.append(prompting_row(merged["prompting"], model))
    base_rate = next(r["hit_rate"] for r in rows if r["method"] == "unsteered")
    strongest_rate = next(r["hit_rate"] for r in rows if r["method"] == "strongest_random")
    rows[-1]["lift_vs_unsteered"] = rows[-1]["hit_rate"] - base_rate
    rows[-1]["lift_vs_strongest_random"] = rows[-1]["hit_rate"] - strongest_rate

    # Per-concept rates, from the same fields the interval script reads.
    per_concept = {method: matched(data, method) for method in BASELINE_METHODS}
    per_concept["nla_av_edit"] = {int(x["concept_id"]): x["arms"]["av_edit"]["hit_rate"] for x in merged["nla_summary"]}
    per_concept["nla_unsteered"] = {int(x["concept_id"]): x["arms"]["unsteered"]["hit_rate"] for x in merged["nla_summary"]}
    per_concept["prompting"] = {int(x["concept_id"]): x["strict_hit_rate"] for x in merged["prompting_summary"]}
    ids = sorted(per_concept["nla_av_edit"])
    for method, values in per_concept.items():
        if sorted(values) != ids:
            raise SystemExit(f"{method} covers a different concept set")

    rng = np.random.default_rng(INTERVAL_SEED)
    intervals = {}
    for method in (*BASELINE_METHODS, "nla_av_edit", "prompting"):
        values = [per_concept[method][i] for i in ids]
        intervals[method] = {"mean": float(np.mean(values)), "interval95": interval(values, rng)}

    def paired(left: str, right: str, left_base: str | None = None, right_base: str | None = None) -> dict:
        diffs = []
        for i in ids:
            a = per_concept[left][i] - (per_concept[left_base][i] if left_base else 0.0)
            b = per_concept[right][i] - (per_concept[right_base][i] if right_base else 0.0)
            diffs.append(a - b)
        return {"mean_difference": float(np.mean(diffs)), "interval95": interval(diffs, rng),
                "concepts_left_higher": sum(d > 0 for d in diffs),
                "concepts_right_higher": sum(d < 0 for d in diffs),
                "concepts_tied": sum(d == 0 for d in diffs)}

    comparisons = {
        "nla_minus_diffmean": paired("nla_av_edit", "diffmean"),
        "nla_gain_minus_diffmean_gain": paired("nla_av_edit", "diffmean", "nla_unsteered", "unsteered"),
        "nla_minus_its_unsteered": paired("nla_av_edit", "nla_unsteered"),
        "diffmean_minus_unsteered": paired("diffmean", "unsteered"),
        "caa_fixed_minus_unsteered": paired("caa_fixed", "unsteered"),
        "caa_calibrated_minus_unsteered": paired("caa_calibrated", "unsteered"),
        "prompting_minus_unsteered": paired("prompting", "unsteered"),
    }

    quality = {}
    for method in ("diffmean", "caa_fixed", "caa_calibrated", "unsteered"):
        for concept_id, _hit, passed in per_response[method]:
            quality.setdefault(method, {}).setdefault(concept_id, []).append(passed)
    strengths = {(row["concept_id"], row["method"]): row for row in merged["selections"]}
    confirmation = {int(x["concept_id"]): x for x in merged["nla_confirmation"]}
    concept_rows = []
    for x in merged["nla_summary"]:
        i = int(x["concept_id"])
        c = confirmation[i]
        concept_rows.append({
            "concept_id": i, "concept": x["concept"], "terms": x["terms"],
            "unsteered": per_concept["unsteered"][i], "caa_fixed": per_concept["caa_fixed"][i],
            "caa_calibrated": per_concept["caa_calibrated"][i], "diffmean": per_concept["diffmean"][i],
            "nla_unsteered": per_concept["nla_unsteered"][i], "nla": per_concept["nla_av_edit"][i],
            "prompting": per_concept["prompting"][i],
            "diffmean_quality": float(np.mean(quality["diffmean"][i])),
            "nla_quality": c["arms"]["av_edit"]["quality_pass_rate"],
            "diffmean_strength": strengths[(i, "diffmean")]["chosen_alpha"],
            "diffmean_strength_status": strengths[(i, "diffmean")]["status"],
            "nla_strength": merged["nla_strengths"][str(i)],
            "nla_confirmed": bool(c["confirmed"]), "nla_causal_pass": bool(c["causal_pass"]),
            "nla_quality_gate_pass": bool(c["semantic_quality_pass"]),
        })
    return {
        "model": model, "n_concepts": len(ids), "responses_per_method": len(ids) * 16,
        "interval_method": f"{RESAMPLES} concept resamples, percentile, numpy default_rng({INTERVAL_SEED})",
        "rows": rows, "intervals": intervals, "paired": comparisons,
        "nla_confirmed_concepts": sorted(i for i, c in confirmation.items() if c["confirmed"]),
        "concepts": concept_rows, "shards": merged["shards"],
    }


def markdown(summary: dict) -> str:
    by_method = {row["method"]: row for row in summary["rows"]}
    # Halves round up, as the paper prints them.
    pct = lambda value: "n/a" if value is None else str(
        Decimal(str(round(100 * value, 6))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    lines = [f"# Random {summary['n_concepts']}-concept panel, {summary['model']}", "",
             f"{summary['responses_per_method']} held-out responses per method. Intervals: {summary['interval_method']}.", "",
             "| Method | Target word | 95% interval | Coverage | Quality | Lift/base | Lift/random |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for label, key in TABLE_ORDER:
        row = by_method[key]
        band = summary["intervals"].get(key)
        band_text = f"[{pct(band['interval95'][0])}, {pct(band['interval95'][1])}]" if band else "n/a"
        lines.append(f"| {label} | {pct(row['hit_rate'])} | {band_text} | {pct(row.get('prompt_coverage'))} | "
                     f"{pct(row['quality_pass_rate'])} | {pct(row.get('lift_vs_unsteered'))} | "
                     f"{pct(row.get('lift_vs_strongest_random'))} |")
    lines += ["", "| Paired difference across concepts | Mean (points) | 95% interval | Higher / lower / tied |", "|---|---:|---:|---:|"]
    for name, item in summary["paired"].items():
        lines.append(f"| {name} | {pct(item['mean_difference'])} | [{pct(item['interval95'][0])}, {pct(item['interval95'][1])}] | "
                     f"{item['concepts_left_higher']} / {item['concepts_right_higher']} / {item['concepts_tied']} |")
    lines += ["", f"Concepts passing the team's NLA confirmation rule: {len(summary['nla_confirmed_concepts'])} of "
              f"{summary['n_concepts']} {summary['nla_confirmed_concepts']}", ""]
    return "\n".join(lines)


def self_check() -> None:
    """Recompute the archived 14-concept panel and require the stored values."""
    terms = load(REPO / "src/nla/experiments/configs/gemma_qwen_positive14_terms.json")
    stored = load(ARCHIVE / "baseline_panel14/combined_summary.json")
    checked = 0
    for model, folder in (("qwen", "qwen2.5-7b"), ("gemma", "gemma3-12b")):
        data = load(ARCHIVE / "baseline_panel14" / model / "results.json")
        rows, _ = baseline_summary(data, terms, model)
        rows += nla_summary(load(ARCHIVE / "baseline_panel14" / f"{model}_nla" / "confirmation_summary.json")["concepts"], model)
        mine = {row["method"]: row for row in rows}
        for want in (row for row in stored if row["model"] == model):
            got = mine[want["method"]]
            for key, value in want.items():
                same = got[key] == value if isinstance(value, str) else abs(got[key] - value) < 1e-12
                if not same:
                    raise SystemExit(f"self-check: {model} {want['method']} {key}: {got[key]} != {value}")
                checked += 1
        prompting = prompting_row(load(ARCHIVE / folder / "prompting_baseline_2026-08-15/results_matched_unsteered.json"), model)
        stored_quality = load(ARCHIVE / folder / "prompting_baseline_2026-08-15/quality_matched_unsteered_summary.json")["rescored"]["quality_pass_rate"]
        if abs(prompting["quality_pass_rate"] - stored_quality) > 1e-12:
            raise SystemExit(f"self-check: {model} prompting quality {prompting['quality_pass_rate']} != {stored_quality}")
        checked += 1
    # The interval script draws Qwen's methods first, in this order.
    stored_intervals = {row["method"]: row for row in load(ARCHIVE / "final_gemma_replication_2026/summary.json")["rows"]
                        if row["model"] == "Qwen2.5-7B"}
    data = load(ARCHIVE / "baseline_panel14/qwen/results.json")
    nla = [x["arms"]["av_edit"]["hit_rate"] for x in load(ARCHIVE / "baseline_panel14/qwen_nla/summary.json")["concepts"]]
    rng = np.random.default_rng(INTERVAL_SEED)
    for label, values in (("Unsteered", list(matched(data, "unsteered").values())),
                          ("Fixed CAA", list(matched(data, "caa_fixed").values())),
                          ("Calibrated CAA", list(matched(data, "caa_calibrated").values())),
                          ("DiffMean", list(matched(data, "diffmean").values())),
                          ("NLA AV edit", nla)):
        low, high = interval(values, rng)
        want = stored_intervals[label]
        if max(abs(low - want["ci_low"]), abs(high - want["ci_high"]), abs(float(np.mean(values)) - want["strict_hit_rate"])) > 1e-12:
            raise SystemExit(f"self-check: interval for {label}: {low, high} != {want['ci_low'], want['ci_high']}")
        checked += 3
    print(f"SELF-CHECK OK: {checked} archived values reproduced")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--model", choices=("qwen", "gemma"))
    parser.add_argument("--shards", type=Path, nargs="+", help="folders that hold shard_* directories")
    parser.add_argument("--terms", type=Path, default=HERE / "terms.random50.json")
    parser.add_argument("--panel", type=Path, default=HERE / "panel.random50.json")
    parser.add_argument("--allow-partial", action="store_true", help="skip unfinished shards and accept fewer concepts")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.self_check:
        self_check()
    if not args.model:
        return 0
    if not args.shards or not args.out:
        parser.error("--model needs --shards and --out")
    merged = collect(args.shards, args.model, args.allow_partial)
    summary = summarize(merged, load(args.terms), args.model)
    drawn = load(args.panel)["concept_ids"]
    found = sorted(row["concept_id"] for row in summary["concepts"])
    summary["complete"] = found == sorted(drawn)
    if not summary["complete"] and not args.allow_partial:
        raise SystemExit(f"{len(found)} concepts found, {len(drawn)} drawn")
    order = {concept: index for index, concept in enumerate(drawn)}
    summary["concepts"].sort(key=lambda row: order[row["concept_id"]])
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"summary.{args.model}.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    report = markdown(summary)
    (args.out / f"report.{args.model}.md").write_text(report + "\n", encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
