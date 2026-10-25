"""Stage B: build all vectors, screen everything, spend nothing on sweeps.

    python experiments/emoji_methods/build_vectors.py

Reads data/substrate.json (stage A) and data/edits.json (the ten edits,
written against the actual paragraph, format
{"single_1": {"find": "...", "replace": "..."}, ...}). Builds every arm
vector, echoes every edited sentence, lens-reads every vector, and
saves data/vectors.npz (unit vectors, preserved from birth) plus
data/build_report.json (edits verbatim, delta ratios, echoes, jgains,
top readouts, split shares). Needs load=("av", "ar"); reads the lens
from ../jspace/data/lens_L20.pt and unembedding rows from the base
checkpoint's safetensors on local disk.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nla_lab import NLASession  # noqa: E402

FREEFORM_POS = ("Casual, excited chat reply packed with emojis and "
                "exclamation marks, playful upbeat tone, informal register.")
FREEFORM_NEG = ("Plain, formal, measured chat reply with no emojis and no "
                "exclamation marks, neutral professional tone.")
QWEN_DIR = Path(os.environ.get("EMOJIMX_BASE", "/root/models/qwen2.5-7b-instruct"))
LAYER = 20
TOP_K_SPAN = 50   # freeform's own top-readout tokens define the readable span
TOP_K_SHOW = 12


def load_base_tensor(name: str) -> torch.Tensor:
    from safetensors import safe_open
    index = json.loads(
        (QWEN_DIR / "model.safetensors.index.json").read_text(encoding="utf-8"))
    with safe_open(QWEN_DIR / index["weight_map"][name],
                   framework="pt", device="cpu") as f:
        return f.get_tensor(name)


def main() -> None:
    here = Path(__file__).resolve().parent
    data = here / "data"
    substrate = json.loads((data / "substrate.json").read_text(encoding="utf-8"))
    edits = json.loads((data / "edits.json").read_text(encoding="utf-8"))
    S = substrate["verbalization_greedy"]

    lab = NLASession(load=("av", "ar"))

    def ar(text: str) -> np.ndarray:
        return lab.reconstruct(text).float().flatten().cpu().numpy()

    base_vec = ar(S)
    report: dict = {"substrate": substrate, "edits": edits,
                    "freeform_sentences": {"positive": FREEFORM_POS,
                                           "negative": FREEFORM_NEG},
                    "per_arm": {}}
    unit_vectors: dict[str, np.ndarray] = {}

    def unit(v: np.ndarray) -> np.ndarray:
        return v / max(float(np.linalg.norm(v)), 1e-12)

    d_free = ar(FREEFORM_POS) - ar(FREEFORM_NEG)
    unit_vectors["freeform"] = unit(d_free)
    report["per_arm"]["freeform"] = {"raw_norm": float(np.linalg.norm(d_free))}

    for name, e in edits.items():
        assert S.count(e["find"]) == 1, f"{name}: find-string not unique in S"
        edited = S.replace(e["find"], e["replace"])
        assert edited != S
        delta = ar(edited) - base_vec
        ratio = float(np.linalg.norm(delta)
                      / ((np.linalg.norm(base_vec)
                          + np.linalg.norm(base_vec + delta)) / 2))
        unit_vectors[name] = unit(delta)
        report["per_arm"][name] = {
            "sentence": edited,
            "delta_norm_ratio": ratio,
            "echo": lab.echo(edited),
        }
        print(f"{name:<10} ratio={ratio:.3f}")

    g = torch.Generator().manual_seed(0)   # bit-identical to run.py seed 0
    r = torch.randn(3584, generator=g)
    unit_vectors["random"] = (r / r.norm()).numpy()

    # Lens: readouts, jgain, and the freeform split.
    state = torch.load(here.parent / "jspace" / "data" / "lens_L20.pt",
                       map_location="cpu", weights_only=True)
    J = state["J"][LAYER].float()
    gamma = load_base_tensor("model.norm.weight").float()
    W_U = load_base_tensor("lm_head.weight").float()
    readout_rows = W_U * gamma
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(QWEN_DIR))

    def lens(d: np.ndarray, k: int = TOP_K_SHOW):
        t = J @ torch.from_numpy(d)
        scores = readout_rows @ t
        top = torch.topk(scores, k)
        toks = [tokenizer.decode([int(i)]) for i in top.indices]
        return float(t.norm()), list(zip(toks, [round(float(v), 3)
                                                for v in top.values])), top.indices

    # split BEFORE adding readable/leftover to the dict
    jgain_free, top_free, top_ids = lens(unit_vectors["freeform"], TOP_K_SPAN)
    span = (J.T @ (readout_rows[top_ids].T)).T          # [K, 3584] token vectors
    span = span / span.norm(dim=1, keepdim=True)
    A = span.T.numpy()                                   # [3584, K]
    d = unit_vectors["freeform"]
    beta, *_ = np.linalg.lstsq(A, d, rcond=None)
    fitted = A @ beta
    share = float(np.linalg.norm(fitted) / np.linalg.norm(d))
    unit_vectors["readable"] = unit(fitted)
    unit_vectors["leftover"] = unit(d - fitted)
    report["split"] = {"span": f"top-{TOP_K_SPAN} readout tokens of freeform",
                       "readable_share_of_length": share}

    for name, vec in unit_vectors.items():
        jg, top, _ = lens(vec)
        report["per_arm"].setdefault(name, {})
        report["per_arm"][name]["jgain"] = jg
        report["per_arm"][name]["top_readout"] = top
        print(f"{name:<10} jgain={jg:.2f}  "
              + " ".join((t.strip() or "_") for t, _ in top[:6]))

    np.savez_compressed(data / "vectors.npz", **unit_vectors)
    (data / "build_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved vectors.npz ({len(unit_vectors)} vectors) and "
          f"build_report.json; readable share = {share:.2f}")


if __name__ == "__main__":
    main()
