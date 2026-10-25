"""One-time data prep for the panel-14 LoReFT baseline (SAE_REFT_BASELINE_PLAN.md item #8).

AxBench's ``scripts/train.py`` trains every concept it finds in
``{data_dir}/train_data.parquet`` (``data_generator()`` groups by ``concept_id``
in ascending order) and indexes ``metadata.jsonl`` *positionally* by
``concept_id`` (``metadata[concept_id]["concept"]``, ``prepare_df``'s
``concept_genres_map`` lookup). That means:

- ``metadata.jsonl`` must stay the full, untouched 500-row file -- concept_id
  446 has to resolve as index 446 into that list.
- ``train_data.parquet`` is what we restrict to the panel-14 concepts, by
  filtering rows to ``concept_id in PANEL14 or concept_id == -1`` (the
  ``concept_id == -1`` rows are the shared negative pool every concept's
  ``prepare_df`` negative split draws from, matched by ``concept_genre``).

The filtered parquet is model-agnostic (it's the same AxBench construction
text every other panel-14 baseline reads), so one copy serves both the Qwen
and Gemma LoReFT configs.

Concept training order matters downstream: ``LoReFT.save()`` appends each
concept's weight slice to a stacked ``[n_concepts, embed_dim, low_rank_dim]``
tensor in the order ``data_generator()`` yields them -- ascending sorted
``concept_id``, since a single-GPU ``torchrun --nproc_per_node=1`` run has no
rank partitioning to reorder them. This script records that order
(``concept_order.json``) so the post-training generation script
(``reft_baseline_generate.py``) can map concept_id -> stacked position
correctly instead of assuming they're equal.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd

PANEL14 = [116, 222, 446, 11, 359, 406, 216, 287, 354, 30, 172, 171, 186, 90]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir",
                        default="axbench/concept500/prod_9b_l20_v1/generate",
                        help="original AxBench generate/ dir (relative to --axbench-root)")
    parser.add_argument("--axbench-root", default=".", help="axbench repo root (cwd when this runs from there)")
    parser.add_argument("--out-dir", default="axbench/concept500/panel14/generate",
                        help="destination generate/ dir (relative to --axbench-root)")
    parser.add_argument("--concept-ids", default="",
                        help="comma-separated subset to prepare instead of the full panel-14 "
                             "(e.g. a weak-concept subset for a follow-up sweep)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing --out-dir")
    args = parser.parse_args()

    concept_ids = [int(v) for v in args.concept_ids.split(",") if v.strip()] if args.concept_ids else PANEL14

    root = Path(args.axbench_root)
    source = root / args.source_dir
    out = root / args.out_dir
    if out.exists() and not args.force:
        existing_order = json.loads((out / "concept_order.json").read_text())
        if existing_order == sorted(concept_ids):
            print(f"already prepared -> {out} (use --force to rebuild)")
            return
        raise FileExistsError(f"{out} exists with a different concept order; pass --force to overwrite")
    out.mkdir(parents=True, exist_ok=True)

    train = pd.read_parquet(source / "train_data.parquet")
    filtered = train[train.concept_id.isin(concept_ids) | (train.concept_id == -1)]
    filtered.to_parquet(out / "train_data.parquet")

    shutil.copy(source / "metadata.jsonl", out / "metadata.jsonl")

    concept_order = sorted(concept_ids)
    (out / "concept_order.json").write_text(json.dumps(concept_order, indent=2) + "\n")

    print(f"{len(concept_ids)}-concept rows: {len(filtered)} (of {len(train)} total, "
         f"{(filtered.concept_id == -1).sum()} shared-negative)")
    print(f"concept training order (stacked-tensor position -> concept_id): {concept_order}")
    print(f"prepared -> {out}")


if __name__ == "__main__":
    main()
