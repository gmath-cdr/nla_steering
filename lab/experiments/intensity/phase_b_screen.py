"""Phase B: build the six edited sentences and screen them. GPU, ~5 min.

    python experiments/intensity/phase_b_screen.py

Substrate: the GREEDY verbalization S of "Choose a number between 0 and
100" (the 42-wall), loaded verbatim from data/phase_a2.json, single
source of truth, never retyped. Each edit inserts exactly one word at
one site in S's second paragraph:

    "...If you were to choose a random number between 1 and 100..."
 -> "...If you were to choose a {WORD} random number between 1 and 100..."

Ladder: tiny, small, large, huge, massive. Null control: certain.

Computes and saves EVERYTHING (data/phase_b.json + phase_b_vectors.npz):
  - AR vector for S and all six edits (full vectors on disk)
  - delta = AR(edit) - AR(S): norm, ratio, per-pair cosines between all
    six deltas (the fan-vs-ruler / tiny-vs-huge-opposition diagnostic)
  - echo of every edited sentence (does the adjective survive?)
Needs load=("av", "ar").
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nla_lab import NLASession  # noqa: E402

SUBSTRATE_PROMPT = "Choose a number between 0 and 100. Reply with just the number."
SITE = "to choose a random number between 1 and 100"
WORDS = ["tiny", "small", "large", "huge", "massive", "certain"]  # last = null


def load_substrate() -> str:
    data = json.loads(
        (Path(__file__).resolve().parent / "data" / "phase_a2.json")
        .read_text(encoding="utf-8"))
    s = data["prompts"][SUBSTRATE_PROMPT]["verbalization_greedy"]
    assert s.count(SITE) == 1, "edit site must appear exactly once in S"
    return s


def edited(s: str, word: str) -> str:
    return s.replace(SITE, SITE.replace("a random number",
                                        f"a {word} random number"))


def main() -> None:
    out_dir = Path(__file__).resolve().parent / "data"
    s = load_substrate()
    sentences = {"original": s, **{w: edited(s, w) for w in WORDS}}
    for w in WORDS:  # each edit is exactly one insertion
        assert len(sentences[w]) == len(s) + len(w) + 1

    lab = NLASession(load=("av", "ar"))
    vecs = {name: lab.reconstruct(text).float().flatten().cpu().numpy()
            for name, text in sentences.items()}
    base = vecs["original"]
    deltas = {w: vecs[w] - base for w in WORDS}

    def cos(a, b):
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))

    report = {"substrate_prompt": SUBSTRATE_PROMPT, "site": SITE,
              "sentences": sentences, "words": WORDS,
              "norm_original": float(np.linalg.norm(base)),
              "per_word": {}, "delta_pairwise_cos": {}}
    print(f"{'word':<10}{'ratio':>7}{'cos(edit,S)':>13}")
    for w in WORDS:
        d = deltas[w]
        ratio = float(np.linalg.norm(d)
                      / ((np.linalg.norm(vecs[w])
                          + np.linalg.norm(base)) / 2))
        report["per_word"][w] = {
            "delta_norm_ratio": ratio,
            "cos_edit_vs_original": cos(vecs[w], base),
            "echo": lab.echo(sentences[w]),
        }
        print(f"{w:<10}{ratio:>7.3f}{report['per_word'][w]['cos_edit_vs_original']:>13.3f}")
    for i, a in enumerate(WORDS):
        for b in WORDS[i + 1:]:
            report["delta_pairwise_cos"][f"{a}|{b}"] = cos(deltas[a],
                                                           deltas[b])
    print("\nkey opposition: cos(d_tiny, d_huge) =",
          f"{report['delta_pairwise_cos']['tiny|huge']:.3f}")

    (out_dir / "phase_b.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(out_dir / "phase_b_vectors.npz",
                        **{f"AR_{k}": v for k, v in vecs.items()},
                        **{f"delta_{w}": deltas[w] for w in WORDS})
    print(f"\nsaved phase_b.json and phase_b_vectors.npz to {out_dir}")


if __name__ == "__main__":
    main()
