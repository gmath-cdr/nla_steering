"""Merge prompt-specific AV-edit discovery shards into one validated bundle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.discovery500 import save_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-dirs", nargs="+", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--confirmation-limit", type=int, default=30)
    args = parser.parse_args()

    shards = [Path(value) for value in args.shard_dirs]
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    summaries, transcripts = [], []
    prompts, lexicons, protocols = {}, {}, []
    for shard in shards:
        summary = json.loads((shard / "summary.json").read_text())
        summaries.extend(summary["concepts"])
        transcripts.extend(json.loads((shard / "transcripts.json").read_text()))
        prompts.update(json.loads((shard / "prompts.json").read_text()))
        lexicons.update(json.loads((shard / "lexicons.json").read_text()))
        protocols.append(json.loads((shard / "protocol.json").read_text()))

    concept_ids = [int(row["concept_id"]) for row in summaries]
    if len(concept_ids) != 500 or len(set(concept_ids)) != 500:
        raise ValueError(f"expected 500 unique concepts, got {len(set(concept_ids))}/{len(concept_ids)}")
    record_keys = [(int(row["concept_id"]), int(row["prompt_index"])) for row in transcripts]
    if len(record_keys) != 1000 or len(set(record_keys)) != 1000:
        raise ValueError(f"expected 1000 unique prompt records, got {len(set(record_keys))}/{len(record_keys)}")
    if any(row.get("status") != "complete" for row in transcripts):
        raise ValueError("all transcript records must be complete")
    if set(map(str, concept_ids)) != set(prompts) or set(prompts) != set(lexicons):
        raise ValueError("summary, prompt, and lexicon concept IDs do not match")

    summaries.sort(key=lambda row: (-float(row["discovery_score"]), int(row["concept_id"])))
    strict = [row for row in summaries
              if row["failure_cause"] == "CANDIDATE_REQUIRES_CONFIRMATION"]
    selected = strict[:args.confirmation_limit]

    save_json(out / "summary.json", {
        "protocol_version": "gemma3-12b-av-edit-discovery-500-v1-merged",
        "concepts": summaries,
    })
    save_json(out / "transcripts.json", sorted(
        transcripts, key=lambda row: (int(row["concept_id"]), int(row["prompt_index"]))))
    save_json(out / "prompts.json", prompts)
    save_json(out / "lexicons.json", lexicons)
    save_json(out / "protocol.json", {
        "protocol_version": "gemma3-12b-av-edit-discovery-500-v1-merged",
        "source_shards": [str(path) for path in shards],
        "source_protocols": protocols,
        "n_concepts": 500,
        "n_prompt_records": 1000,
    })
    save_json(out / "confirmation_candidates.json", {
        "selection_rule": "top discovery_score among CANDIDATE_REQUIRES_CONFIRMATION",
        "available_strict_candidates": len(strict),
        "confirmation_limit": args.confirmation_limit,
        "selected_concept_ids": [int(row["concept_id"]) for row in selected],
        "selected": selected,
    })

    lines = ["# Gemma-3-12B AV-edit discovery-500 (merged)", "",
             f"Strict confirmation candidates: {len(strict)}; selected: {len(selected)}.", "",
             "| Rank | ID | Concept | AV hit | Baseline | Random | Coverage | Score | Classification |",
             "|---:|---:|---|---:|---:|---:|---:|---:|---|"]
    for rank, row in enumerate(summaries, 1):
        arms = row["arms"]
        lines.append(
            f"| {rank} | {row['concept_id']} | {row['concept']} | "
            f"{arms['av_edit']['hit_rate']:.1%} | {arms['unsteered']['hit_rate']:.1%} | "
            f"{arms['random']['hit_rate']:.1%} | {arms['av_edit']['prompt_coverage']:.1%} | "
            f"{row['discovery_score']:.3f} | {row['failure_cause']} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print(f"merged 500 concepts; {len(strict)} strict candidates; selected {len(selected)} -> {out}")


if __name__ == "__main__":
    main()
