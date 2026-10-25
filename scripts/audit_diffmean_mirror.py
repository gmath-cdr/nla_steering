#!/usr/bin/env python3
"""Audit archived mirror inputs without loading models or modifying the archive.

Uses the existing pure lexical, quality, and resampling functions verbatim.
AST loading avoids importing unavailable GPU dependencies for an offline audit.
This is an input audit, not a replacement generation pipeline.
"""
from __future__ import annotations

import ast
import argparse
import collections
import csv
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/experiments/diffmean_self_selection_mirror"
ARCHIVE = ROOT / "artifacts/paper_runs/data/qwen2.5-7b/diffmean_discovery500_2026-08-15_merged"


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")
                          if path.suffix in {".json", ".jsonl", ".md", ".py", ".csv"}
                          else path.read_bytes()).hexdigest()


def pure_functions(path, names, namespace):
    tree = ast.parse(path.read_text())
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names
             or isinstance(node, ast.Assign) and any(
                 isinstance(target, ast.Name) and target.id in names for target in node.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


def save(name, value):
    path = OUT / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def check_prompt_splits(manifest, candidates, transcripts):
    import pyarrow
    import pyarrow.parquet as pq
    source = ROOT / "src/axbench/axbench/concept500/prod_9b_l20_v1"
    ids = {r["concept_id"] for r in candidates}
    prompts = {cid: [] for cid in ids}
    for row in pq.read_table(source / "inference/latent_eval_data.parquet",
                            columns=["input", "concept_id", "category", "dataset_category"]).to_pylist():
        cid, prompt = row["concept_id"], str(row["input"]).strip()
        if (cid in ids and row["category"] == "negative" and row["dataset_category"] == "instruction"
                and prompt and prompt not in prompts[cid]):
            prompts[cid].append(prompt)
    assert all(len(ps) >= 22 and len(set(ps[:22])) == 22 for ps in prompts.values())
    assert all(r["prompt"] == prompts[r["concept_id"]][r["prompt_index"]]
               for r in transcripts if r["concept_id"] in ids)
    save("prompt_splits.json", {name: {str(cid): ps[a:b] for cid, ps in prompts.items()}
                               for name, (a, b) in manifest["prompt_splits"].items()})
    metadata = {r["concept_id"]: r["concept"] for r in
                map(json.loads, (source / "generate/metadata.jsonl").read_text().splitlines())}
    assert all(metadata[r["concept_id"]] == r["concept"] for r in candidates)
    construction = pq.read_table(source / "generate/train_data.parquet",
                                columns=["concept_id", "category"]).to_pylist()
    counts = collections.Counter((r["concept_id"], r["category"]) for r in construction)
    assert all(counts[cid, "positive"] >= 72 for cid in ids) and counts[-1, "negative"] >= 72
    manifest["prompt_splits_sha256"] = digest(OUT / "prompt_splits.json")
    manifest["leakage_check"]["full_prompt_content_disjointness"] = "verified within each candidate across all four splits"
    manifest["integrity"]["construction_minimum_72_examples_verified"] = True
    manifest["integrity"]["candidate_labels_match_concept500_metadata"] = True
    manifest["audit_package_versions"] = {"pyarrow": pyarrow.__version__}


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-parquet", action="store_true")
    parser.add_argument("--out-dir")
    args = parser.parse_args()
    if args.out_dir:
        OUT = ROOT / args.out_dir
    elif (OUT / "protocol.json").exists():
        OUT = ROOT / "artifacts/rebuilt/diffmean_mirror_input_recheck"
    started = time.monotonic()
    OUT.mkdir(parents=True, exist_ok=True)
    ns = {"re": re, "Any": Any}
    discovery = ROOT / "src/nla/experiments/discovery500.py"
    quality = ROOT / "src/nla/experiments/confirmation_metrics.py"
    pure_functions(discovery, {"STOPWORDS", "lexical_terms", "matches", "bootstrap_lower"}, ns)
    pure_functions(ROOT / "src/nla/experiments/diffmean_discovery500.py", {"summarize"}, ns)
    # Quality has its own STOPWORDS, so use a separate function namespace.
    qns = {"re": re, "Any": Any, "matches": ns["matches"]}
    pure_functions(quality, {"STOPWORDS", "tokens", "jaccard", "surface_quality", "evaluate_prompt", "aggregate"}, qns)
    summaries = json.loads((ARCHIVE / "summary.json").read_text())["concepts"]
    transcripts = json.loads((ARCHIVE / "transcripts.json").read_text())
    assert len(summaries) == 500 and {r["concept_id"] for r in summaries} == set(range(500))
    assert len(transcripts) == 1000
    by_concept = collections.defaultdict(list)
    primary_arms = {"unsteered", "diffmean", "random_17", "random_42", "random_101"}
    for row in transcripts:
        assert row["status"] == "complete"
        assert primary_arms <= set(row["generation_arms"])
        assert all(len(row["generation_arms"][arm]) == 2 for arm in primary_arms | {"random"})
        assert row["generation_arms"]["random"] == row["generation_arms"][f'random_{row["strongest_random_seed"]}']
        by_concept[row["concept_id"]].append(row)
    source_hashes = {str(p.relative_to(ROOT)): digest(p) for p in [
        ARCHIVE / "summary.json", ARCHIVE / "transcripts.json", discovery, quality,
        ROOT / "src/nla/experiments/diffmean_discovery500.py",
        ROOT / "src/axbench/axbench/concept500/prod_9b_l20_v1/generate/metadata.jsonl",
        ROOT / "src/axbench/axbench/concept500/prod_9b_l20_v1/generate/train_data.parquet",
        ROOT / "src/axbench/axbench/concept500/prod_9b_l20_v1/inference/latent_eval_data.parquet",
        ROOT / "sources.lock", ROOT / "uv.lock"]}
    expected = dict((line.split("  ", 1)[1], line.split("  ", 1)[0]) for line in
                    (ROOT / "artifacts/paper_runs/paper-sha256sums.txt").read_text().splitlines())
    mismatches = [name for name, value in expected.items() if digest(ROOT / name) != value]
    assert all(source_hashes[str((ARCHIVE / name).relative_to(ROOT))] ==
               expected[str((ARCHIVE / name).relative_to(ROOT))] for name in ["summary.json", "transcripts.json"])
    for archived in summaries:
        cid = archived["concept_id"]
        rows = sorted(by_concept[cid], key=lambda r: r["prompt_index"])
        assert [r["prompt_index"] for r in rows] == [0, 1]
        recomputed = ns["summarize"](cid, archived["concept"], ns["lexical_terms"](archived["concept"]), rows, 2)
        assert recomputed == archived, f"summary mismatch c{cid}"
    candidates = [r for r in summaries if r["discovery_candidate"]]
    assert len(candidates) == 65
    save("original_candidates.json", {"source": str(ARCHIVE.relative_to(ROOT)),
         "source_hashes": {k: v for k, v in source_hashes.items() if k.startswith(str(ARCHIVE.relative_to(ROOT)))},
         "concept_ids": [r["concept_id"] for r in candidates], "concepts": candidates})
    candidate_hash = digest(OUT / "original_candidates.json")
    (OUT / "original_candidates.sha256").write_text(candidate_hash + "  original_candidates.json\n")
    flow = []
    for rank, candidate in enumerate(candidates, 1):
        cid = candidate["concept_id"]
        metrics = [qns["evaluate_prompt"](r, candidate["terms"]) for r in by_concept[cid]]
        samples = [s for r in metrics for s in r["arms"]["diffmean"]]
        aggregate = qns["aggregate"](samples)
        flow.append({"concept_id": cid, "concept": candidate["concept"], "discovery_rank": rank,
                     "pass1": "candidate", "archived_alpha": by_concept[cid][0]["alpha"],
                     **aggregate, "pass2": "pending_filter_provenance",
                     "pass3": "not_run", "final_panel": "not_frozen",
                     "reason": "Historical discovery-quality ranking implementation and run provenance must be verified before confirmation."})
    save("discovery_quality_metrics.json", flow)
    with (OUT / "candidate_flow.csv").open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(flow[0]))
        writer.writeheader(); writer.writerows(flow)
    versions = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    manifest = {
        "status": "awaiting_historical_provenance_no_gpu_generation_started",
        "repository_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "artifact_source_commit": "93c61e8fbae9e083c862e15a38adc69acad02745",
        "concept500_vendored_source_commit": "e003aed69c0c4306388d97e13bd34cfb77adc693",
        "historical_generation_code_commit": None,
        "historical_target_revision": None, "historical_generation_seed": None,
        "historical_package_versions": None,
        "nla_av_revision": "b88469162777ae6553bc14208eb0cb579336f8f4",
        "nla_ar_revision": "e2c9e57eac213d37a31612087f645ab6332c1bb6",
        "installed_package_versions": versions,
        "source_hashes": source_hashes, "candidate_list_sha256": candidate_hash,
        "prompt_splits": {"discovery": [0, 2], "confirmation": [2, 10], "calibration": [10, 14], "evaluation": [14, 22]},
        "split_convention": "half-open ranges of unique negative instruction prompts in source order",
        "alpha_grid": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.75],
        "archived_discovery_alpha": sorted({r["alpha"] for r in transcripts}),
        "nla_confirmation_alpha": 0.5, "layer": 20,
        "response_max_new_tokens": 96, "response_temperature": 0.7, "samples_per_prompt": 2,
        "random_direction_seeds": [17, 42, 101],
        "resampling": {"source": str(discovery.relative_to(ROOT)), "resamples": 2000, "seed": 0, "one_sided_quantile": 0.05},
        "hardware": subprocess.check_output(["nvidia-smi", "--query-gpu=index,name,memory.total,uuid,driver_version", "--format=csv,noheader"], text=True).splitlines(),
        "workspace_is_persistent_volume": False,
        "integrity": {"concepts": 500, "prompt_records": 1000, "primary_generations": 10000,
                      "random_alias_generations_excluded": 2000, "candidate_count": 65,
                      "all_archived_summaries_recomputed_exactly": True, "global_checksum_mismatches": mismatches},
        "leakage_check": {"new_calibration_responses": 0, "new_evaluation_responses": 0, "panel_frozen": False,
                          "full_prompt_content_disjointness": "pending parquet and source-provenance verification"},
        "runtime": {"audit_elapsed_seconds": time.monotonic() - started, "gpu_generation_seconds": 0,
                    "peak_gpu_memory": None, "eta": "unavailable before provenance resolution and GPU pilot"},
    }
    receipts = OUT / "provenance"
    if receipts.exists():
        manifest["upstream_provenance_receipt_hashes"] = {
            p.name: digest(p) for p in sorted(receipts.iterdir()) if p.is_file()}
        manifest["panel14_historical_axbench_commit"] = (receipts / "panel14-axbench.commit").read_text().strip()
        manifest["panel14_historical_nla_commit"] = (receipts / "panel14-nla.commit").read_text().strip()
        manifest["panel14_package_receipt"] = str((receipts / "panel14-pip-freeze.txt").relative_to(ROOT))
        manifest["package_receipt_scope"] = "panel14 queue; not proven to describe the DiffMean discovery environment"
    if args.check_parquet:
        check_prompt_splits(manifest, candidates, transcripts)
    save("run_manifest.json", manifest)
    print(json.dumps({"candidates": len(candidates), "discovery_alpha": manifest["archived_discovery_alpha"],
                      "global_checksum_mismatches": mismatches, "audit_seconds": manifest["runtime"]["audit_elapsed_seconds"]}))


if __name__ == "__main__":
    main()
