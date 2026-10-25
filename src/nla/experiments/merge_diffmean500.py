"""Merge the DiffMean-500 discovery shards into one combined bundle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.diffmean_discovery500 import summarize
from experiments.discovery500 import lexical_terms, save_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-dirs", nargs="+", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--n-samples", type=int, default=2)
    args = parser.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    transcripts = []
    concept_names: dict[int, str] = {}
    for shard in args.shard_dirs:
        transcripts.extend(json.loads((Path(shard) / "transcripts.json").read_text()))
    for row in transcripts:
        concept_names[int(row["concept_id"])] = row["concept"]
    save_json(out / "transcripts.json", transcripts)

    by_concept: dict[int, list[dict]] = {}
    for row in transcripts:
        by_concept.setdefault(int(row["concept_id"]), []).append(row)

    summaries = []
    for cid, rows in by_concept.items():
        rows = sorted(rows, key=lambda r: r["prompt_index"])
        summaries.append(summarize(cid, concept_names[cid], lexical_terms(concept_names[cid]), rows,
                                   args.n_samples))
    summaries.sort(key=lambda row: row["discovery_score"], reverse=True)
    save_json(out / "summary.json", {"protocol_version": "qwen-diffmean-discovery-500-v1-merged",
                                     "concepts": summaries})

    lines = ["# AxBench-500 DiffMean discovery (merged)", "",
            "| Rank | ID | Concept | DiffMean hit | Baseline | Random | Coverage | Score | Classification |",
            "|---:|---:|---|---:|---:|---:|---:|---:|---|"]
    for rank, row in enumerate(summaries, 1):
        arms = row["arms"]
        lines.append(f"| {rank} | {row['concept_id']} | {row['concept']} | {arms['diffmean']['hit_rate']:.1%} | "
                     f"{arms['unsteered']['hit_rate']:.1%} | {arms['random']['hit_rate']:.1%} | "
                     f"{arms['diffmean']['prompt_coverage']:.1%} | {row['discovery_score']:.3f} | {row['failure_cause']} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")
    n_candidates = sum(1 for row in summaries if row["discovery_candidate"])
    print(f"merged {len(by_concept)} concepts, {n_candidates} discovery candidates -> {out}")


if __name__ == "__main__":
    main()
