"""Wordle 2 build stage: all vectors, screens, decompositions. Pod.

    python experiments/wordle2/build_wordle_vectors.py

Builds every vector in README.md's arm list, echoes every sentence,
reads every vector through the lens, runs the reference-inspired greedy-NNLS
k=25 approximation where the design calls for one (dwelling component+remainder,
best-length comp), applies the deterministic best-length selection
rule, and archives everything: data/wordle_vectors.npz (unit vectors +
decomposition bases) and data/wordle_build_report.json (substrate,
sentences verbatim, edit contexts, ratios, echoes, readouts,
decomposition tables, the best-length choice with its evidence).
Needs load=("base", "av", "ar") plus the lens and base safetensors.
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
from decompose_v2 import decompose, load_base_tensor  # noqa: E402

SUBSTRATE_PROMPT = "Think of a word to start a wordle game."
LAYER = 20
SCAFFOLD = "Reply gives {x} as its starting guess to play."
FREEFORM = {
    "ff_a_six": ("a six letter word", "a five letter word"),
    "ff_b_six": ("a word that has six letters", "a word that has five letters"),
    "ff_c_six": ("a 6 letter word", "a 5 letter word"),
    "ff_dwell": ("the five letter word for a building where people live",
                 "the five letter word for a tall machine that lifts heavy loads"),
    "ff_poor": ("the word for the large maned wild cat known as the king of the jungle",
                "the word for a tall machine that lifts heavy loads"),
    "anchor_five": ("a five letter word", "a word of any length"),
}
NUMBER_SWAPS = {"ve1_four": "four", "ve1_six": "six", "ve1_seven": "seven"}
LENGTH_CANDIDATES = ["ve1_four", "ve1_six", "ve1_seven", "vephrase_six",
                     "ff_a_six", "ff_b_six", "ff_c_six"]
TARGET_WORD = {"ve1_four": "four", "ve1_six": "six", "ve1_seven": "seven",
               "vephrase_six": "six", "ff_a_six": "six", "ff_b_six": "six",
               "ff_c_six": "six"}


def unit(v: np.ndarray) -> np.ndarray:
    return v / max(float(np.linalg.norm(v)), 1e-12)


def main() -> None:
    data = Path(__file__).resolve().parent / "data"
    data.mkdir(exist_ok=True)
    report: dict = {"substrate_prompt": SUBSTRATE_PROMPT, "per_arm": {},
                    "delegated_notes": "see README.md"}

    lab = NLASession(load=("base", "av", "ar"))

    def ar(text: str) -> np.ndarray:
        return lab.reconstruct(text).float().flatten().cpu().numpy()

    # --- substrate ------------------------------------------------------
    acts, toks = lab.extract(SUBSTRATE_PROMPT)
    state = acts[-1]
    S = lab.verbalize(state, temperature=0.0)
    report["substrate_verbalization"] = S
    np.savez_compressed(data / "wordle_substrate.npz",
                        state=state.float().cpu().numpy())
    print("=== SUBSTRATE ===")
    print(S[:400])

    vectors: dict[str, np.ndarray] = {}
    base_vec = ar(S)

    # --- one-word number swaps -----------------------------------------
    # the paper-canonical minimal edit; needs "five" in the paragraph
    n_five = S.count("five")
    report["substrate_five_count"] = n_five
    for name, word in NUMBER_SWAPS.items():
        if n_five == 0:
            report["per_arm"][name] = {"skipped": "substrate lacks 'five'"}
            print(f"{name}: SKIPPED, no 'five' in substrate")
            continue
        edited = S.replace("five", word, 1)
        idx = S.find("five")
        ctx = S[max(0, idx - 40):idx + 44]
        delta = ar(edited) - base_vec
        ratio = float(np.linalg.norm(delta)
                      / ((np.linalg.norm(base_vec)
                          + np.linalg.norm(base_vec + delta)) / 2))
        vectors[name] = unit(delta)
        report["per_arm"][name] = {"edit_context": ctx, "target": word,
                                   "delta_norm_ratio": ratio,
                                   "echo": lab.echo(edited),
                                   "sentence": edited}
        print(f"{name:<14} ratio={ratio:.3f}")

    # --- clause rewrite for six ----------------------------------------
    clause = None
    for pat in ("five letter word", "five-letter word", "five letters"):
        if S.count(pat) >= 1:
            clause = pat
            break
    if clause:
        edited = S.replace(
            clause, clause.replace("five", "six")
            + ", deliberately one letter longer than the usual", 1)
        delta = ar(edited) - base_vec
        ratio = float(np.linalg.norm(delta)
                      / ((np.linalg.norm(base_vec)
                          + np.linalg.norm(base_vec + delta)) / 2))
        vectors["vephrase_six"] = unit(delta)
        report["per_arm"]["vephrase_six"] = {
            "clause_found": clause, "delta_norm_ratio": ratio,
            "echo": lab.echo(edited), "sentence": edited}
        print(f"vephrase_six  ratio={ratio:.3f}")
    else:
        report["per_arm"]["vephrase_six"] = {"skipped": "no five-letter clause"}
        print("vephrase_six: SKIPPED, clause not found")

    # --- freeform pairs -------------------------------------------------
    for name, (pos_x, neg_x) in FREEFORM.items():
        pos, neg = SCAFFOLD.format(x=pos_x), SCAFFOLD.format(x=neg_x)
        d = ar(pos) - ar(neg)
        vectors[name] = unit(d)
        report["per_arm"][name] = {"positive": pos, "negative": neg,
                                   "echo_positive": lab.echo(pos)}
        print(f"{name:<14} built")

    # --- composition ----------------------------------------------------
    vectors["mix_poor"] = unit(0.5 * vectors["ff_poor"]
                               + 0.5 * vectors["anchor_five"])
    report["per_arm"]["mix_poor"] = {
        "recipe": "unit(0.5*unit(ff_poor) + 0.5*unit(anchor_five))",
        "cos_poor_anchor": float(vectors["ff_poor"] @ vectors["anchor_five"])}

    # --- lens: atoms, readouts, decompositions --------------------------
    here = Path(__file__).resolve().parent
    lens = torch.load(here.parent / "jspace" / "data" / "lens_L20.pt",
                      map_location="cpu", weights_only=True)
    J = lens["J"][LAYER].float().cuda()
    gamma = load_base_tensor("model.norm.weight").float().cuda()
    W_U = load_base_tensor("lm_head.weight").float().cuda()
    raw_rows = W_U * gamma
    atoms = raw_rows @ J
    atoms = atoms / atoms.norm(dim=1, keepdim=True)
    del W_U

    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        os.environ.get("EMOJIMX_BASE", "/root/models/qwen2.5-7b-instruct"))

    def readout(d: np.ndarray, k: int = 10):
        t = J @ torch.from_numpy(d).cuda()
        top = torch.topk(raw_rows @ t, k)
        return [tokenizer.decode([int(i)]) for i in top.indices]

    for name, vec in vectors.items():
        report["per_arm"].setdefault(name, {})
        report["per_arm"][name]["top_readout"] = readout(vec)
        print(f"{name:<14} readout: "
              + " ".join(t.strip() or "_" for t in report['per_arm'][name]['top_readout'][:6]))

    arrays = dict(vectors)
    deco: dict = {}
    for name in ["ff_dwell"] + [c for c in LENGTH_CANDIDATES if c in vectors]:
        snaps = decompose(vectors[name].astype(np.float32), atoms, 25)
        s = snaps[25]
        comp, ids, w = s["component"], s["ids"], s["weights"]
        resid = vectors[name] - comp
        deco[name] = {"share": float(np.linalg.norm(comp)),
                      "token_ids": ids,
                      "tokens": [tokenizer.decode([i]) for i in ids],
                      "weights": [round(float(x), 5) for x in w]}
        arrays[f"comp25_{name}"] = unit(comp)
        arrays[f"resid25_{name}"] = unit(resid)
        arrays[f"basis25_{name}"] = atoms[torch.tensor(ids, device=atoms.device)].cpu().numpy()
    report["decompositions"] = deco

    # best-length selection rule from README (deterministic)
    best, best_w = None, -1.0
    evidence = {}
    for cand in LENGTH_CANDIDATES:
        if cand not in deco:
            continue
        target = TARGET_WORD[cand]
        w_target = max((w for t, w in zip(deco[cand]["tokens"],
                                          deco[cand]["weights"])
                        if t.strip().lower() == target), default=0.0)
        evidence[cand] = w_target
        if w_target > best_w:
            best, best_w = cand, w_target
    report["best_length_choice"] = {"rule": "largest k25 weight on own target number word",
                                    "evidence": evidence, "chosen": best}
    if best:
        arrays["comp25_bestlen"] = arrays[f"comp25_{best}"]
        print(f"best length vector: {best} (target-word weight {best_w:.4f})")

    np.savez_compressed(data / "wordle_vectors.npz", **arrays)
    (data / "wordle_build_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"saved {len(arrays)} arrays to wordle_vectors.npz")


if __name__ == "__main__":
    main()
