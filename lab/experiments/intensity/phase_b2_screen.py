"""Phase B2: phrase-level nudge edits of the 42-wall verbalization. GPU.

    python experiments/intensity/phase_b2_screen.py

Phase B taught us one adjective drowns in a 120-word S (ratios 0.06-0.08,
echoes stripped every size word). B2 escalates HOME-style: the SAME
clause gets a meaning-bearing phrase, everything else byte-identical,
the clause skeleton preserved (nudge, not overwrite). Sentences below
were frozen before the run on 2026-08-01.

Gates for advancing any rung to a sweep: delta ratio clears ~0.15 AND
the echo mentions smallness/largeness. Saves data/phase_b2.json +
phase_b2_vectors.npz (all vectors). Needs load=("av", "ar").
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
EDITS = {
    "small_mild": "a fairly small random number, on the lower side",
    "small_extreme": "a very small random number, as close to zero as possible",
    "large_mild": "a fairly large random number, on the higher side",
    "large_extreme": "a very large random number, as close to one hundred as possible",
    "null": "a perfectly ordinary random number, of no particular size",
}


def load_substrate() -> str:
    data = json.loads(
        (Path(__file__).resolve().parent / "data" / "phase_a2.json")
        .read_text(encoding="utf-8"))
    s = data["prompts"][SUBSTRATE_PROMPT]["verbalization_greedy"]
    assert s.count(SITE) == 1
    return s


def main() -> None:
    out_dir = Path(__file__).resolve().parent / "data"
    s = load_substrate()
    sentences = {"original": s}
    for name, phrase in EDITS.items():
        e = s.replace(SITE, f"to choose {phrase} between 1 and 100")
        assert e != s and e.count(phrase) == 1
        sentences[name] = e

    lab = NLASession(load=("av", "ar"))
    vecs = {name: lab.reconstruct(text).float().flatten().cpu().numpy()
            for name, text in sentences.items()}
    base = vecs["original"]
    deltas = {n: vecs[n] - base for n in EDITS}

    def cos(a, b):
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))

    report = {"substrate_prompt": SUBSTRATE_PROMPT, "site": SITE,
              "edits": EDITS, "sentences": sentences,
              "per_rung": {}, "delta_pairwise_cos": {}}
    print(f"{'rung':<15}{'ratio':>7}")
    for n in EDITS:
        d = deltas[n]
        ratio = float(np.linalg.norm(d)
                      / ((np.linalg.norm(vecs[n]) + np.linalg.norm(base)) / 2))
        report["per_rung"][n] = {"delta_norm_ratio": ratio,
                                 "echo": lab.echo(sentences[n])}
        print(f"{n:<15}{ratio:>7.3f}")
    names = list(EDITS)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            report["delta_pairwise_cos"][f"{a}|{b}"] = cos(deltas[a], deltas[b])

    (out_dir / "phase_b2.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(out_dir / "phase_b2_vectors.npz",
                        **{f"AR_{k}": v for k, v in vecs.items()},
                        **{f"delta_{n}": deltas[n] for n in EDITS})
    print(f"\nsaved phase_b2.json and phase_b2_vectors.npz to {out_dir}")


if __name__ == "__main__":
    main()
