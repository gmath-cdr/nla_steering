"""J-space geometry for prompt-specific vectors from AV-edit confirmation."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import torch

from experiments import jspace, vectors
from experiments.steering import normalize


def stats(values: list[float]) -> dict[str, float]:
    mean = sum(values) / len(values)
    return {
        "mean": mean,
        "std": math.sqrt(sum((value - mean) ** 2 for value in values) / len(values)),
        "min": min(values),
        "max": max(values),
    }


def geometry(vector: torch.Tensor, directions: torch.Tensor,
             jacobian: torch.Tensor) -> dict[str, Any]:
    vector = normalize(vector).cpu()
    projection = jspace.project(vector, directions)
    return {
        "projection_fraction": float(projection.norm()),
        "jacobian_alignment": float((jacobian @ vector).norm() / jacobian.norm()),
        "top_basis_cosines": [float(vector @ direction) for direction in directions[:5]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--lens", required=True)
    parser.add_argument("--layer", type=int, default=20)
    parser.add_argument("--k", type=int, default=32)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import jlens

    run = Path(args.run_dir)
    summary = json.loads((run / "confirmation_summary.json").read_text())
    transcripts = json.loads((run / "transcripts.json").read_text())
    confirmed = summary["confirmed_concept_ids"]
    summary_by_id = {int(row["concept_id"]): row for row in summary["concepts"]}
    lens = jlens.JacobianLens.load(args.lens)
    if args.layer not in lens.jacobians:
        raise ValueError(f"lens does not contain layer {args.layer}")
    print(f"computing rank-{args.k} J-space basis", flush=True)
    directions = jspace.basis(lens, args.layer, args.k).cpu()
    jacobian = lens.jacobians[args.layer].float().cpu()

    concepts = []
    centroids: dict[int, torch.Tensor] = {}
    for cid in confirmed:
        rows = sorted(
            (row for row in transcripts if int(row["concept_id"]) == cid),
            key=lambda row: int(row["prompt_index"]),
        )
        prompt_vectors = [normalize(vectors.load(row["vector_path"])).cpu() for row in rows]
        centroid = normalize(torch.stack(prompt_vectors).mean(0))
        centroids[cid] = centroid
        similarities = [float(vector @ centroid) for vector in prompt_vectors]
        prompt_geometry = [geometry(vector, directions, jacobian) for vector in prompt_vectors]
        behavioral = summary_by_id[cid]
        concepts.append({
            "concept_id": cid,
            "concept": behavioral["concept"],
            "n_prompt_vectors": len(prompt_vectors),
            "within_concept_cosine_to_centroid": stats(similarities),
            "centroid_geometry": geometry(centroid, directions, jacobian),
            "prompt_projection_fraction": stats(
                [row["projection_fraction"] for row in prompt_geometry]),
            "prompt_jacobian_alignment": stats(
                [row["jacobian_alignment"] for row in prompt_geometry]),
            "behavior": {
                "av_hit_rate": behavioral["arms"]["av_edit"]["hit_rate"],
                "lift_vs_unsteered": behavioral["lift_vs_unsteered"],
                "lift_vs_random": behavioral["lift_vs_random"],
                "prompt_coverage": behavioral["prompt_coverage"],
                "quality_pass_rate": behavioral["arms"]["av_edit"]["quality_pass_rate"],
            },
            "per_prompt_geometry": prompt_geometry,
        })

    pairwise = []
    for left_index, left_id in enumerate(confirmed):
        for right_id in confirmed[left_index + 1:]:
            pairwise.append({
                "left": left_id,
                "right": right_id,
                "cosine": float(centroids[left_id] @ centroids[right_id]),
            })
    pairwise.sort(key=lambda row: abs(row["cosine"]), reverse=True)
    concepts.sort(
        key=lambda row: row["centroid_geometry"]["projection_fraction"], reverse=True)
    output = {
        "protocol_version": "qwen-av-edit-confirmation-jspace-v1",
        "lens": args.lens,
        "lens_n_prompts": lens.n_prompts,
        "layer": args.layer,
        "basis_rank": args.k,
        "n_concepts": len(concepts),
        "n_prompt_vectors": sum(row["n_prompt_vectors"] for row in concepts),
        "concepts": concepts,
        "top_absolute_centroid_cosines": pairwise[:20],
        "caution": (
            "Selected-set mechanistic analysis of automated passes. Geometry does not "
            "repair lexical false positives or independently establish clean steering."
        ),
    }
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2) + "\n")
    lines = [
        "# J-space geometry of 14 AV-edit automated passes", "",
        f"Lens prompts: {lens.n_prompts}; layer: {args.layer}; basis rank: {args.k}.", "",
        "| ID | Concept | Within-concept cosine | J-space projection | Jacobian alignment | AV lift/base | Quality |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in concepts:
        behavior = row["behavior"]
        lines.append(
            f"| {row['concept_id']} | {row['concept']} | "
            f"{row['within_concept_cosine_to_centroid']['mean']:.3f} | "
            f"{row['centroid_geometry']['projection_fraction']:.3f} | "
            f"{row['centroid_geometry']['jacobian_alignment']:.5f} | "
            f"{behavior['lift_vs_unsteered']:.1%} | {behavior['quality_pass_rate']:.1%} |"
        )
    report = target.with_suffix(".md")
    report.write_text("\n".join(lines) + "\n")
    centroid_dir = target.parent / "centroids"
    centroid_dir.mkdir(exist_ok=True)
    for cid, centroid in centroids.items():
        vectors.save(centroid, str(centroid_dir / f"c{cid}.mean_av_edit.pt"))
    print(f"J-space geometry -> {target}")


if __name__ == "__main__":
    main()
