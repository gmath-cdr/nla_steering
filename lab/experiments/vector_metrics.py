"""Compute reconstructor-side vector geometry for EVERY direction spec,
plus the actual layer-20 state of every task prompt. GPU, ~3 minutes:

    python experiments/vector_metrics.py

Deterministic: sentences come from the spec files and the reconstructor
has no sampling, so this reproduces the vectors every past run used.
Saves METRICS and 2D map coordinates, not the raw 3584-dim vectors:

  results/vector_metrics.json
    directions[name]: cos_pair, delta_norm_ratio, norms, unit-direction
                      PCA xy for AR(+) and AR(-), cos to each prompt state
    prompts[text]:    norm and PCA xy of the last-token layer-20 state
    direction_cos:    pairwise cosine matrix between all unit directions
                      (do different experiments push in related ways?)

Render with analysis/geometry.py on the laptop afterwards.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nla_lab import NLASession  # noqa: E402


def cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def main() -> None:
    lab_dir = Path(__file__).resolve().parents[1]
    pairs: dict[str, tuple[str, str]] = {}
    prompts: set[str] = set()
    for f in sorted((lab_dir / "experiments" / "specs").glob("*.yaml")):
        spec = yaml.safe_load(f.read_text(encoding="utf-8"))
        if isinstance(spec.get("direction"), dict):
            pairs[spec["name"]] = (spec["direction"]["positive"],
                                   spec["direction"]["negative"])
        prompts.update(spec.get("prompts", []))
    print(f"{len(pairs)} direction pairs, {len(prompts)} distinct prompts")

    lab = NLASession(load=("base", "ar"))
    recon: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for name, (pos, neg) in sorted(pairs.items()):
        vp = lab.reconstruct(pos).float().flatten().cpu().numpy()
        vn = lab.reconstruct(neg).float().flatten().cpu().numpy()
        recon[name] = (vp, vn)
        print(f"  AR pair done: {name}")
    states: dict[str, np.ndarray] = {}
    for p in sorted(prompts):
        acts, _ = lab.extract(p)
        states[p] = acts[-1].float().flatten().cpu().numpy()
        print(f"  prompt state done: {p[:50]}")

    names = sorted(recon)
    units = {}
    for n in names:
        d = recon[n][0] - recon[n][1]
        units[n] = d / (np.linalg.norm(d) + 1e-12)

    # one shared 2D map over every AR output and prompt state
    mat = np.stack([v for n in names for v in recon[n]]
                   + [states[p] for p in sorted(states)])
    center = mat - mat.mean(axis=0)
    _, _, vt = np.linalg.svd(center, full_matrices=False)
    xy = center @ vt[:2].T
    xy_iter = iter(xy.tolist())

    out = {"directions": {}, "prompts": {}, "names": names,
           "direction_cos": [[cos(units[a], units[b]) for b in names]
                             for a in names]}
    for n in names:
        vp, vn = recon[n]
        d = vp - vn
        out["directions"][n] = {
            "cos_pair": cos(vp, vn),
            "delta_norm_ratio": float(np.linalg.norm(d)
                                      / ((np.linalg.norm(vp)
                                          + np.linalg.norm(vn)) / 2)),
            "norm_pos": float(np.linalg.norm(vp)),
            "norm_neg": float(np.linalg.norm(vn)),
            "xy_pos": next(xy_iter), "xy_neg": next(xy_iter),
            "cos_to_prompts": {p: cos(units[n], states[p])
                               for p in sorted(states)},
        }
    for p in sorted(states):
        out["prompts"][p] = {"norm": float(np.linalg.norm(states[p])),
                             "xy": next(xy_iter)}
    dest = lab_dir / "results" / "vector_metrics.json"
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
