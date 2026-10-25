"""Wave 2 stage B: reference-inspired sparse J-lens approximation. Pod.

    python experiments/emoji_methods/decompose_v2.py

Implements a greedy-NNLS approximation to the workspace paper's sparse
nonnegative objective. It is not paper-exact because the paper's gradient-
pursuit implementation was not available:

  paper (verbatim): "we solve for a sparse nonnegative combination of k
  J-lens vectors that approximate it well using gradient pursuit";
  k "no more than 25" (10 and 16 also used); component = "the point in
  the J-space nearest to it"; residual = "the difference between these
  points".

  implementation choices:
  candidate pool = ALL vocabulary token directions (no preselection);
  greedy atom selection by largest positive correlation with the
  residual, exact nonnegative least squares refit of the active set at
  every step (the one labeled divergence: exact refits instead of
  gradient-pursuit's approximate updates; same objective, same
  constraints); atoms unit-normalized before coding; no other
  preprocessing (inputs are already unit vectors); k in {10, 16, 25}.

Decomposes all 16 content vectors (11 wave-1 + 5 wave-2 singles).
Archives EVERYTHING per the no-lost-bases rule: chosen token ids,
decoded tokens, nonnegative weights, component and approximation-remainder
vectors, component-norm ratios (stored under the legacy ``share`` field), and
the atom basis rows, for every vector at every k. Outputs:
data/decomposition_v2.npz + data/decomposition_v2.json.
Needs no NLA models: only the lens, the base checkpoint's safetensors
(unembedding + final norm), the tokenizer, torch, and scipy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import torch
from scipy.optimize import nnls

QWEN_DIR = Path(os.environ.get("EMOJIMX_BASE", "/root/models/qwen2.5-7b-instruct"))
LAYER = 20
K_VALUES = (10, 16, 25)


def load_base_tensor(name: str) -> torch.Tensor:
    from safetensors import safe_open
    index = json.loads(
        (QWEN_DIR / "model.safetensors.index.json").read_text(encoding="utf-8"))
    with safe_open(QWEN_DIR / index["weight_map"][name],
                   framework="pt", device="cpu") as f:
        return f.get_tensor(name)


def decompose(d: np.ndarray, atoms: torch.Tensor, k_max: int):
    """Greedy sparse nonnegative coding: returns per-step snapshots."""
    device = atoms.device
    target = torch.from_numpy(d).to(device)
    residual = target.clone()
    active: list[int] = []
    snapshots = {}
    weights = np.zeros(0)
    for step in range(1, k_max + 1):
        scores = atoms @ residual                       # [vocab]
        if active:
            scores[torch.tensor(active, device=device)] = -torch.inf
        best = int(torch.argmax(scores))
        if float(scores[best]) <= 0:
            break                                       # nothing left to add
        active.append(best)
        A = atoms[torch.tensor(active, device=device)].T.cpu().numpy()
        weights, _ = nnls(A.astype(np.float64), d.astype(np.float64))
        component = A @ weights
        residual = target - torch.from_numpy(
            component.astype(np.float32)).to(device)
        if step in K_VALUES:
            snapshots[step] = {
                "ids": list(active),
                "weights": weights.astype(np.float32).copy(),
                "component": component.astype(np.float32).copy(),
            }
    # if the loop broke early, backfill remaining k snapshots
    for k in K_VALUES:
        if k not in snapshots and active:
            A = atoms[torch.tensor(active, device=device)].T.cpu().numpy()
            snapshots[k] = {"ids": list(active),
                            "weights": weights.astype(np.float32).copy(),
                            "component": (A @ weights).astype(np.float32).copy()}
    return snapshots


def main() -> None:
    data = Path(__file__).resolve().parent / "data"
    here = Path(__file__).resolve().parent
    lens = torch.load(here.parent / "jspace" / "data" / "lens_L20.pt",
                      map_location="cpu", weights_only=True)
    J = lens["J"][LAYER].float().cuda()
    gamma = load_base_tensor("model.norm.weight").float().cuda()
    W_U = load_base_tensor("lm_head.weight").float().cuda()
    atoms = (W_U * gamma) @ J                            # rows: v_t
    atoms = atoms / atoms.norm(dim=1, keepdim=True)      # unit atoms (approved)
    del W_U

    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(QWEN_DIR))

    content = {}
    v1 = np.load(data / "vectors.npz")
    for name in v1.files:
        if name not in ("readable", "leftover", "random"):
            content[name] = v1[name]
    v2 = np.load(data / "vectors_v2.npz")
    for name in v2.files:
        content[name] = v2[name]
    print(f"decomposing {len(content)} content vectors, k = {K_VALUES}")

    arrays: dict[str, np.ndarray] = {}
    report: dict = {"config": {
        "pool": "full vocabulary", "k_values": list(K_VALUES),
        "solver": "greedy argmax-correlation + exact NNLS refit per step "
                  "(labeled divergence from unspecified gradient pursuit)",
        "atoms": "unit-normalized token directions v_t from lens_L20"},
        "per_vector": {}}
    for name, d in content.items():
        snaps = decompose(d.astype(np.float32), atoms, max(K_VALUES))
        entry = {}
        for k, s in snaps.items():
            comp = s["component"]
            resid = d - comp
            share = float(np.linalg.norm(comp) / np.linalg.norm(d))
            arrays[f"comp{k}_{name}"] = comp / max(np.linalg.norm(comp), 1e-12)
            arrays[f"resid{k}_{name}"] = resid / max(np.linalg.norm(resid), 1e-12)
            arrays[f"basis{k}_{name}"] = atoms[
                torch.tensor(s["ids"], device=atoms.device)].cpu().numpy()
            entry[str(k)] = {
                "share": share,
                "token_ids": s["ids"],
                "tokens": [tokenizer.decode([i]) for i in s["ids"]],
                "weights": [round(float(w), 5) for w in s["weights"]],
            }
        report["per_vector"][name] = entry
        shares = {k: round(v["share"], 3) for k, v in entry.items()}
        top = " ".join(t.strip() or "_" for t in entry["25"]["tokens"][:6])
        print(f"{name:<12} shares(k10/16/25)={shares}  atoms: {top}")

    np.savez_compressed(data / "decomposition_v2.npz", **arrays)
    (data / "decomposition_v2.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"archived {len(arrays)} arrays to decomposition_v2.npz")


if __name__ == "__main__":
    main()
