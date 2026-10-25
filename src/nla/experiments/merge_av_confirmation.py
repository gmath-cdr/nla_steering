"""Merge completed prompt-specific AV-edit confirmation shards."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.discovery500 import save_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-dirs", nargs="+", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    shards = [Path(value) for value in args.shard_dirs]
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    transcripts, summaries, protocols = [], [], []
    prompts, lexicons = {}, {}
    for shard in shards:
        transcripts.extend(json.loads((shard / "transcripts.json").read_text()))
        summaries.extend(json.loads((shard / "summary.json").read_text())["concepts"])
        prompts.update(json.loads((shard / "prompts.json").read_text()))
        lexicons.update(json.loads((shard / "lexicons.json").read_text()))
        protocols.append(json.loads((shard / "protocol.json").read_text()))

    concept_ids = [int(row["concept_id"]) for row in summaries]
    keys = [(int(row["concept_id"]), int(row["prompt_index"])) for row in transcripts]
    if len(concept_ids) != 30 or len(set(concept_ids)) != 30:
        raise ValueError(f"expected 30 unique concepts, got {len(set(concept_ids))}/{len(concept_ids)}")
    if len(keys) != 240 or len(set(keys)) != 240:
        raise ValueError(f"expected 240 unique prompt records, got {len(set(keys))}/{len(keys)}")
    if any(row.get("status") != "complete" for row in transcripts):
        raise ValueError("all confirmation records must be complete")
    if set(map(str, concept_ids)) != set(prompts) or set(prompts) != set(lexicons):
        raise ValueError("summary, prompt, and lexicon concept IDs do not match")

    summaries.sort(key=lambda row: (-float(row["discovery_score"]), int(row["concept_id"])))
    concept_ids = [int(row["concept_id"]) for row in summaries]
    protocol = {
        "protocol_version": "gemma3-12b-av-edit-confirmation-30-v1-merged",
        "concept_ids": concept_ids,
        "n_prompts": 8,
        "n_samples": 2,
        "prompt_offset": 2,
        "source_shards": [str(path) for path in shards],
        "source_protocols": protocols,
    }
    save_json(out / "protocol.json", protocol)
    save_json(out / "summary.json", {"protocol_version": protocol["protocol_version"],
                                      "concepts": summaries})
    save_json(out / "transcripts.json", sorted(
        transcripts, key=lambda row: (int(row["concept_id"]), int(row["prompt_index"]))))
    save_json(out / "prompts.json", prompts)
    save_json(out / "lexicons.json", lexicons)
    print(f"merged {len(concept_ids)} concepts and {len(keys)} records -> {out}")


if __name__ == "__main__":
    main()
