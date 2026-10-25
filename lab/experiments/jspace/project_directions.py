"""Project every archived steering direction through the fitted J_20. Pod.

    NLA_MODELS_DIR=/root/models /venv/bin/python -u experiments/jspace/project_directions.py

Runs in the NLA venv (/venv, transformers<5): the lens file is a plain
dict, so no jlens import is needed. Needs data/lens_L20.pt and
data/jlens_tokens.npz from fit_jlens.py, the base checkpoint at
/root/qwen (unembed weights + tokenizer, read directly from safetensors,
model never loaded), and the AR checkpoint under $NLA_MODELS_DIR.

For each direction d (unit vector at layer 20) it computes:
  - jgain = |J_20 d|: how strongly the direction transports into the
    final-layer basis at all. Reference: random unit vectors, and
    |J|_F / sqrt(d_model) = the expected jgain of an arbitrary direction.
  - top-20 J-lens readout of d: logits (gamma * W_U) @ (J_20 d), i.e.
    which vocabulary tokens the model "hears" in the direction.
  - cos and dot against every curated J-lens token vector.

Directions covered (all rebuilt or replayed exactly as run):
  - every specs/*.yaml with a sentence direction (AR difference, unit)
  - the seeded random controls (seeds 0 and 1, bit-identical to run.py)
  - intensity phase B / B2 deltas from the archived npz (unit-normalized,
    raw norm recorded)

Saves results/jspace_projections.json and data/direction_vectors.npz
(every unit direction, preserved from birth per lab convention).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import torch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nla_lab import NLASession  # noqa: E402

QWEN_DIR = Path(os.environ.get("JLENS_MODEL", "/root/qwen"))
LAYER = 20
TOP_K = 20


def load_base_tensor(name: str) -> torch.Tensor:
    from safetensors import safe_open
    index = json.loads(
        (QWEN_DIR / "model.safetensors.index.json").read_text(encoding="utf-8"))
    with safe_open(QWEN_DIR / index["weight_map"][name],
                   framework="pt", device="cpu") as f:
        return f.get_tensor(name)


def collect_directions(lab: NLASession) -> dict[str, dict]:
    """name -> {vec (unit fp32 numpy), source, raw_norm, sentences?}"""
    out: dict[str, dict] = {}
    specs_dir = Path(__file__).resolve().parents[1] / "specs"
    for spec_path in sorted(specs_dir.glob("*.yaml")):
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        direction = spec.get("direction")
        if not isinstance(direction, dict):
            continue
        d = lab.direction(direction["positive"], direction["negative"])
        out[spec["name"]] = {
            "vec": d.float().flatten().cpu().numpy(), "source": "spec",
            "positive": direction["positive"],
            "negative": direction["negative"],
        }
        print(f"  spec {spec['name']}")

    for seed in (0, 1):  # bit-identical to run.py's direction: random
        g = torch.Generator().manual_seed(seed)
        d = torch.randn(3584, generator=g)
        out[f"random_seed{seed}"] = {
            "vec": (d / d.norm()).numpy(), "source": "random"}

    intensity = Path(__file__).resolve().parent.parent / "intensity" / "data"
    for phase, npz_name in (("b", "phase_b_vectors.npz"),
                            ("b2", "phase_b2_vectors.npz")):
        path = intensity / npz_name
        if not path.exists():
            print(f"  (skipping {npz_name}: not found)")
            continue
        with np.load(path) as z:
            for key in z.files:
                if not key.startswith("delta_"):
                    continue
                raw = z[key].astype(np.float32)
                norm = float(np.linalg.norm(raw))
                out[f"intensity_{phase}_{key.removeprefix('delta_')}"] = {
                    "vec": raw / max(norm, 1e-12), "source": "intensity",
                    "raw_norm": norm}
    return out


def main() -> None:
    here = Path(__file__).resolve().parent
    data_dir = here / "data"
    results_dir = here.parents[1] / "results"
    results_dir.mkdir(exist_ok=True)

    state = torch.load(data_dir / "lens_L20.pt", map_location="cpu",
                       weights_only=True)
    J = state["J"][LAYER].float()                       # [d, d]
    gamma = load_base_tensor("model.norm.weight").float()
    W_U = load_base_tensor("lm_head.weight").float()    # [vocab, d]
    readout = W_U * gamma                               # rows: gamma * W_U[t]
    frob_ref = float(J.norm() / np.sqrt(J.shape[0]))

    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(QWEN_DIR))

    curated = dict(np.load(data_dir / "jlens_tokens.npz"))

    lab = NLASession(load=("ar",))
    print("rebuilding directions:")
    directions = collect_directions(lab)

    report: dict = {"config": {"layer": LAYER, "top_k": TOP_K,
                               "n_directions": len(directions),
                               "frobenius_over_sqrt_d": frob_ref},
                    "directions": {}}
    vectors: dict[str, np.ndarray] = {}
    print(f"\n{'direction':<28}{'jgain':>8}  top readout tokens")
    for name, info in directions.items():
        d = torch.from_numpy(info["vec"])
        t = J @ d
        logits = readout @ t
        top = torch.topk(logits, TOP_K)
        top_tokens = [[tokenizer.decode([int(i)]), round(float(v), 3)]
                      for i, v in zip(top.indices, top.values)]
        entry = {
            "source": info["source"],
            "jgain": float(t.norm()),
            "top_readout": top_tokens,
            "curated": {key: {"cos": float(np.dot(vec, info["vec"])
                                           / (np.linalg.norm(vec) + 1e-12)),
                              "dot": float(np.dot(vec, info["vec"]))}
                        for key, vec in curated.items()},
        }
        for extra in ("raw_norm", "positive", "negative"):
            if extra in info:
                entry[extra] = info[extra]
        report["directions"][name] = entry
        vectors[name] = info["vec"]
        preview = " ".join(tok.strip() or "_" for tok, _ in top_tokens[:6])
        print(f"{name:<28}{entry['jgain']:>8.3f}  {preview}")

    (results_dir / "jspace_projections.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(data_dir / "direction_vectors.npz", **vectors)
    print(f"\nsaved {results_dir / 'jspace_projections.json'} and "
          f"{data_dir / 'direction_vectors.npz'}")
    print(f"reference: |J|_F/sqrt(d) = {frob_ref:.3f} "
          f"(expected jgain of an arbitrary direction)")


if __name__ == "__main__":
    main()
