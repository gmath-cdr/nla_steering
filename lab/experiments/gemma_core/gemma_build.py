"""Gemma build: every confirmatory-core vector, screened + decomposed.

    NLA_MODEL=gemma3-12b NLA_MODELS_DIR=/root/models \
        python experiments/gemma_core/gemma_build.py

Requires the fitted Gemma lens (jspace/data/lens_L32.pt, gate passed)
and smoke_report.json (substrate source of truth). Builds: freeform
emoji pair; 5 target-relevant singles + 2 phrase edits against Gemma's
own substrate sentence (data/gemma_edits.json, authored for review);
the dwelling, lion, and seven-letter pairs (verbatim Qwen wave
wordings); reference-inspired greedy-NNLS k25 approximations (component +
approximation remainder) for freeform, the glyph phrase, and dwelling, with
component-norm ratios (legacy artifact key ``share``) for every
content vector. Echoes and lens readouts for everything. Archives
data/gemma_vectors.npz + data/gemma_build_report.json.

Tied embeddings: Gemma has no separate unembedding tensor; the loader
below resolves the language tower's embed_tokens and final norm by
key-suffix search across the safetensors index.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "emoji_methods"))

from nla_lab import NLASession  # noqa: E402
from nla_lab.session import LAYER, NLA_MODEL  # noqa: E402
from decompose_v2 import decompose  # noqa: E402

BASE_DIR = Path(os.environ.get("GEMMA_BASE", "/root/models/gemma-3-12b-it"))
SCAFFOLD = "Reply gives {x} as its starting guess to play."
FREEFORM = {
    "g_freeform": (
        "Casual, excited chat reply packed with emojis and exclamation marks, "
        "playful upbeat tone, informal register.",
        "Plain, formal, measured chat reply with no emojis and no exclamation "
        "marks, neutral professional tone."),
}
WORDLE_PAIRS = {
    "g_dwell": ("the five letter word for a building where people live",
                "the five letter word for a tall machine that lifts heavy loads"),
    "g_lion": ("the word for the great maned wild cat that rules the jungle as the king of beasts",
               "the word for a tall machine that lifts heavy loads"),
    "g_len_seven": ("an extra long seven letter word, two letters more than the normal opener",
                    "a normal five letter word of the usual length"),
}
DECOMPOSE_ARMS = ["g_freeform", "g_phrase_glyph", "g_dwell"]


def load_base_tensor_tied(suffixes: list[str]) -> torch.Tensor:
    """Suffix-search the safetensors index; prefer language-tower keys."""
    from safetensors import safe_open
    index = json.loads(
        (BASE_DIR / "model.safetensors.index.json").read_text(encoding="utf-8"))
    wmap = index["weight_map"]
    candidates = [k for k in wmap
                  if any(k.endswith(s) for s in suffixes)]
    candidates.sort(key=lambda k: ("language" not in k, len(k)))
    assert candidates, f"no tensor matching {suffixes}"
    key = candidates[0]
    with safe_open(BASE_DIR / wmap[key], framework="pt", device="cpu") as f:
        return f.get_tensor(key)


def unit(v: np.ndarray) -> np.ndarray:
    return v / max(float(np.linalg.norm(v)), 1e-12)


def main() -> None:
    assert NLA_MODEL == "gemma3-12b"
    data = Path(__file__).resolve().parent / "data"
    smoke = json.loads((data / "smoke_report.json").read_text(encoding="utf-8"))
    S = smoke["substrate_chat"]["verbalization_greedy"]
    edits = json.loads((data / "gemma_edits.json").read_text(encoding="utf-8"))
    edits.pop("_site_note", None)

    lab = NLASession(load=("av", "ar"))

    def ar(text: str) -> np.ndarray:
        return lab.reconstruct(text).float().flatten().cpu().numpy()

    report: dict = {"substrate_chat": S, "per_arm": {}}
    vectors: dict[str, np.ndarray] = {}

    base_vec = ar(S)
    for name, e in edits.items():
        assert S.count(e["find"]) == 1, f"{name}: find not unique in substrate"
        edited = S.replace(e["find"], e["replace"])
        delta = ar(edited) - base_vec
        ratio = float(np.linalg.norm(delta)
                      / ((np.linalg.norm(base_vec)
                          + np.linalg.norm(base_vec + delta)) / 2))
        vectors[name] = unit(delta)
        report["per_arm"][name] = {"sentence": edited,
                                   "delta_norm_ratio": ratio,
                                   "echo": lab.echo(edited)}
        print(f"{name:<16} ratio={ratio:.3f}")

    for name, (pos, neg) in FREEFORM.items():
        vectors[name] = unit(ar(pos) - ar(neg))
        report["per_arm"][name] = {"positive": pos, "negative": neg,
                                   "echo_positive": lab.echo(pos)}
        print(f"{name:<16} built")
    for name, (pos_x, neg_x) in WORDLE_PAIRS.items():
        pos, neg = SCAFFOLD.format(x=pos_x), SCAFFOLD.format(x=neg_x)
        vectors[name] = unit(ar(pos) - ar(neg))
        report["per_arm"][name] = {"positive": pos, "negative": neg,
                                   "echo_positive": lab.echo(pos)}
        print(f"{name:<16} built")

    # lens: readouts + decompositions (tied embeddings)
    lens = torch.load(Path(__file__).resolve().parent.parent / "jspace" / "data"
                      / f"lens_L{LAYER}.pt", map_location="cpu",
                      weights_only=True)
    J = lens["J"][LAYER].float().cuda()
    # Gemma/Gemma3 RMSNorm applies (1 + weight), unlike Qwen's raw weight.
    # Historical Gemma J-readouts/decompositions built before this correction
    # are under audit; the merged J itself and whole-parent vectors are valid.
    gamma = (1.0 + load_base_tensor_tied(["model.norm.weight"]).float()).cuda()
    W_U = load_base_tensor_tied(["embed_tokens.weight"]).float().cuda()
    raw_rows = W_U * gamma
    report["j_atom_formula"] = "j-lens-atom-effective-final-norm/v2"
    atoms = raw_rows @ J
    atoms = atoms / atoms.norm(dim=1, keepdim=True)
    del W_U
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(BASE_DIR))

    def readout(d: np.ndarray, k: int = 10):
        t = J @ torch.from_numpy(d).cuda()
        top = torch.topk(raw_rows @ t, k)
        return [tokenizer.decode([int(i)]) for i in top.indices]

    for name, vec in vectors.items():
        report["per_arm"][name]["top_readout"] = readout(vec)
        print(f"{name:<16} readout: "
              + " ".join(t.strip() or "_"
                         for t in report['per_arm'][name]['top_readout'][:6]))

    deco: dict = {}
    for name in list(vectors):
        snaps = decompose(vectors[name].astype(np.float32), atoms, 25)
        s = snaps.get(25)
        if not s:
            continue
        comp = s["component"]
        deco[name] = {"share": float(np.linalg.norm(comp)),
                      "tokens": [tokenizer.decode([i]) for i in s["ids"]],
                      "weights": [round(float(w), 5) for w in s["weights"]]}
        if name in DECOMPOSE_ARMS:
            vectors[f"comp25_{name}"] = unit(comp)
            vectors[f"resid25_{name}"] = unit(vectors[name] - comp)
            vectors[f"basis25_{name}"] = atoms[
                torch.tensor(s["ids"], device=atoms.device)].cpu().numpy()
    report["decompositions"] = deco

    np.savez_compressed(data / "gemma_vectors.npz", **vectors)
    (data / "gemma_build_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"saved {len(vectors)} arrays; shares: "
          + ", ".join(f"{k}={v['share']:.2f}" for k, v in deco.items()))


if __name__ == "__main__":
    main()
