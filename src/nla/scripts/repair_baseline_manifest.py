"""Backfill CAA provenance for a completed run made before metadata persistence landed."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--layer", type=int, required=True)
    parser.add_argument("--hook-path", default="model.layers")
    args = parser.parse_args()

    manifest_path = args.run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    concept_ids = manifest["protocol"]["concept_ids"]
    metadata = {}
    for concept_id in concept_ids:
        vector = torch.load(
            args.run_dir / f"c{concept_id}.caa.pt",
            map_location="cpu",
            weights_only=True,
        )
        metadata[str(concept_id)] = {
            "method": "caa",
            "layer_requested": args.layer,
            "layer_index": args.layer,
            "one_based": False,
            "hook_path": args.hook_path,
            "pairs": manifest["protocol"]["construction_examples_per_concept"],
            "raw_norm": float(vector.float().norm()),
            "normalized": False,
            "provenance_backfilled_from_saved_vector": True,
        }
    manifest["caa_extraction_metadata"] = metadata
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(manifest_path)


if __name__ == "__main__":
    main()
