"""J-space geometry for prompt-specific vectors from an AV-edit alpha sweep."""

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
    unit = normalize(vector).cpu()
    projection = jspace.project(unit, directions)
    return {
        "projection_fraction": float(projection.norm()),
        "jacobian_alignment": float((jacobian @ unit).norm() / jacobian.norm()),
        "top_basis_cosines": [float(unit @ direction) for direction in directions[:5]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcripts", required=True)
    parser.add_argument("--lens", required=True)
    parser.add_argument("--layer", type=int, required=True)
    parser.add_argument("--k", type=int, default=32)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import jlens

    rows = json.loads(Path(args.transcripts).read_text())
    if not rows or any(row.get("status") != "complete" for row in rows):
        raise ValueError("transcripts must contain only completed prompt records")
    lens = jlens.JacobianLens.load(args.lens)
    if args.layer not in lens.jacobians:
        raise ValueError(f"lens does not contain layer {args.layer}")
    print(f"computing rank-{args.k} J-space basis", flush=True)
    directions = jspace.basis(lens, args.layer, args.k).cpu()
    jacobian = lens.jacobians[args.layer].float().cpu()

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["concept_id"]), []).append(row)

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    centroid_dir = target.parent / "centroids"
    centroid_dir.mkdir(exist_ok=True)
    concepts = []
    all_prompt_projection = []
    all_prompt_alignment = []
    all_pair_cosines = []
    for concept_id, concept_rows in sorted(grouped.items()):
        concept_rows.sort(key=lambda row: int(row["prompt_index"]))
        prompt_vectors = [normalize(vectors.load(row["vector_path"])).cpu()
                          for row in concept_rows]
        centroid = normalize(torch.stack(prompt_vectors).mean(0))
        pair_cosines = [float(left @ right)
                        for index, left in enumerate(prompt_vectors)
                        for right in prompt_vectors[index + 1:]]
        prompt_geometry = [geometry(vector, directions, jacobian)
                           for vector in prompt_vectors]
        centroid_path = centroid_dir / f"c{concept_id}.mean_av_edit.pt"
        vectors.save(centroid, str(centroid_path))
        all_pair_cosines.extend(pair_cosines)
        all_prompt_projection.extend(row["projection_fraction"] for row in prompt_geometry)
        all_prompt_alignment.extend(row["jacobian_alignment"] for row in prompt_geometry)
        concepts.append({
            "concept_id": concept_id,
            "concept": concept_rows[0]["concept"],
            "n_prompt_vectors": len(prompt_vectors),
            "pairwise_prompt_cosines": pair_cosines,
            "within_concept_pair_cosine": stats(pair_cosines),
            "centroid_path": str(centroid_path),
            "centroid_geometry": geometry(centroid, directions, jacobian),
            "prompt_projection_fraction": stats(
                [row["projection_fraction"] for row in prompt_geometry]),
            "prompt_jacobian_alignment": stats(
                [row["jacobian_alignment"] for row in prompt_geometry]),
            "per_prompt_geometry": prompt_geometry,
        })

    output = {
        "protocol_version": "av-edit-sweep-jspace-v1",
        "transcripts": args.transcripts,
        "lens": args.lens,
        "lens_n_prompts": lens.n_prompts,
        "layer": args.layer,
        "basis_rank": args.k,
        "n_concepts": len(concepts),
        "n_prompt_vectors": len(rows),
        "aggregate": {
            "within_concept_pair_cosine": stats(all_pair_cosines),
            "prompt_projection_fraction": stats(all_prompt_projection),
            "prompt_jacobian_alignment": stats(all_prompt_alignment),
        },
        "concepts": concepts,
        "caution": (
            "Geometry is descriptive. Behavioral full/projection/complement "
            "ablations are required to attribute steering to J-space."
        ),
    }
    target.write_text(json.dumps(output, indent=2) + "\n")

    lines = [
        "# Gemma AV-edit J-space geometry", "",
        f"Lens prompts: {lens.n_prompts}; layer: {args.layer}; rank: {args.k}.", "",
        "| ID | Concept | Prompt cosine | Centroid projection | Jacobian alignment |",
        "|---:|---|---:|---:|---:|",
    ]
    for row in concepts:
        lines.append(
            f"| {row['concept_id']} | {row['concept']} | "
            f"{row['within_concept_pair_cosine']['mean']:.4f} | "
            f"{row['centroid_geometry']['projection_fraction']:.4f} | "
            f"{row['centroid_geometry']['jacobian_alignment']:.6f} |"
        )
    target.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(f"J-space geometry -> {target}")


if __name__ == "__main__":
    main()
