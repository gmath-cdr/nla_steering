"""Wordle redesign round B: enriched sentences for the arms that failed
the readout screen. Pod. One round only, per phase-gate discipline;
every change documented here for morning review.

    python experiments/wordle2/build_v2b.py

Screen verdicts from round A (data/wordle_build_report.json): dwelling
healthy; all length pairs junk except a digit glimmer in ff_c_six;
lion and anchor junk. Diagnosis: the failed contrasts differ by one
number word, the known too-small-edit regime. Round B makes each
contrast meaning-bearing (longer-ness described, not just named),
mirroring the phase-B2 lesson from the intensity work.

Adds keys to data/wordle_vectors.npz (suffix _b) and appends to the
build report. Also recomputes the best-length rule over old + new
candidates and refreshes comp25_bestlen if a candidate finally puts
weight on its own number word.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "emoji_methods"))

from nla_lab import NLASession  # noqa: E402
from decompose_v2 import decompose, load_base_tensor  # noqa: E402

SCAFFOLD = "Reply gives {x} as its starting guess to play."
PAIRS_B = {
    "len_six_b": (
        "an unusually long six letter word, one letter more than the normal opener",
        "a normal five letter word of the usual length"),
    "len_seven_b": (
        "an extra long seven letter word, two letters more than the normal opener",
        "a normal five letter word of the usual length"),
    "len_four_b": (
        "an unusually short four letter word, one letter less than the normal opener",
        "a normal five letter word of the usual length"),
    "len_digit_six_b": (
        "a 6 letter word, longer than the standard 5",
        "a standard 5 letter word"),
    "poor_lion_b": (
        "the word for the great maned wild cat that rules the jungle as the king of beasts",
        "the word for a tall machine that lifts heavy loads"),
    "anchor_five_b": (
        "a standard wordle word of exactly five letters, no more and no less",
        "a word of any length whatsoever, short or long"),
}
TARGET_WORD_B = {"len_six_b": "six", "len_seven_b": "seven",
                 "len_four_b": "four", "len_digit_six_b": "6"}
LAYER = 20


def unit(v):
    return v / max(float(np.linalg.norm(v)), 1e-12)


def main() -> None:
    data = Path(__file__).resolve().parent / "data"
    report = json.loads((data / "wordle_build_report.json").read_text(encoding="utf-8"))
    old = dict(np.load(data / "wordle_vectors.npz"))

    lab = NLASession(load=("av", "ar"))

    def ar(text):
        return lab.reconstruct(text).float().flatten().cpu().numpy()

    vectors = dict(old)
    round_b: dict = {"pairs": {}, "note": "enriched contrasts, round B"}
    for name, (pos_x, neg_x) in PAIRS_B.items():
        pos, neg = SCAFFOLD.format(x=pos_x), SCAFFOLD.format(x=neg_x)
        vectors[name] = unit(ar(pos) - ar(neg))
        round_b["pairs"][name] = {"positive": pos, "negative": neg,
                                  "echo_positive": lab.echo(pos)}
        print(f"{name:<16} built")

    # mix rebuilt from round-B parents
    vectors["mix_poor_b"] = unit(0.5 * vectors["poor_lion_b"]
                                 + 0.5 * vectors["anchor_five_b"])

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
    tokenizer = AutoTokenizer.from_pretrained("/root/models/qwen2.5-7b-instruct")

    def readout(d, k=10):
        t = J @ torch.from_numpy(d).cuda()
        top = torch.topk(raw_rows @ t, k)
        return [tokenizer.decode([int(i)]) for i in top.indices]

    for name in list(PAIRS_B) + ["mix_poor_b"]:
        round_b["pairs"].setdefault(name, {})
        round_b["pairs"][name]["top_readout"] = readout(vectors[name])
        print(f"{name:<16} readout: "
              + " ".join(t.strip() or "_" for t in round_b['pairs'][name]['top_readout'][:8]))

    # best-length rule over old + new candidates
    candidates = {**{c: w for c, w in
                     {"ff_a_six": "six", "ff_b_six": "six", "ff_c_six": "6"}.items()},
                  **TARGET_WORD_B}
    best, best_w, evidence = None, 0.0, {}
    deco_b = {}
    for cand, target in candidates.items():
        if cand not in vectors:
            continue
        snaps = decompose(vectors[cand].astype(np.float32), atoms, 25)
        s = snaps.get(25)
        if not s:
            continue
        toks = [tokenizer.decode([i]) for i in s["ids"]]
        w_t = max((w for t, w in zip(toks, s["weights"])
                   if t.strip().lower() == target), default=0.0)
        evidence[cand] = round(float(w_t), 5)
        deco_b[cand] = {"tokens": toks, "weights": [round(float(x), 5) for x in s["weights"]]}
        if w_t > best_w:
            best, best_w = cand, w_t
            comp = s["component"]
            vectors["comp25_bestlen"] = unit(comp)
            vectors["resid25_bestlen"] = unit(vectors[cand] - comp)
    round_b["best_length"] = {"evidence": evidence, "chosen": best,
                              "weight": best_w}
    print("best length candidate:", best, "weight", best_w)

    np.savez_compressed(data / "wordle_vectors.npz", **vectors)
    report["round_b"] = round_b
    report["round_b"]["decompositions"] = deco_b
    (data / "wordle_build_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"npz now holds {len(vectors)} arrays")


if __name__ == "__main__":
    main()
