"""Wave 2 stage A: the five TARGET-RELEVANT single-token edits. Pod.

    python experiments/emoji_methods/build_singles_v2.py

Design review outcome (2026-08-14): wave 1's singles were excitement
words; only one named the target. Wave 2 singles all name it: emoji,
emojis, emoticon, smiley, and the bare glyph. Same substrate, same
site, same one-word insertion, everything else byte-identical.

Substrate is read from data/build_report.json (single source of truth,
committed). Saves data/vectors_v2.npz (unit vectors) and
data/build_v2_report.json (sentences, delta ratios, echoes, plus the
reproducibility check: cosine of s2_emoji against wave 1's single_5,
expected ~1.0 since the reconstructor is deterministic).
Needs load=("av", "ar").
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nla_lab import NLASession  # noqa: E402

SITE = "a playful response format"
WORDS = {"s2_emoji": "emoji", "s2_emojis": "emojis",
         "s2_emoticon": "emoticon", "s2_smiley": "smiley",
         "s2_glyph": "\U0001F60A"}


def main() -> None:
    data = Path(__file__).resolve().parent / "data"
    report_v1 = json.loads((data / "build_report.json").read_text(encoding="utf-8"))
    S = report_v1["substrate"]["verbalization_greedy"]
    assert S.count(SITE) == 1

    lab = NLASession(load=("av", "ar"))

    def ar(text: str) -> np.ndarray:
        return lab.reconstruct(text).float().flatten().cpu().numpy()

    base = ar(S)
    vectors: dict[str, np.ndarray] = {}
    report = {"substrate_source": "build_report.json", "site": SITE,
              "per_arm": {}}
    for name, word in WORDS.items():
        edited = S.replace(SITE, SITE.replace("a playful",
                                              f"a playful {word}"))
        assert edited != S and edited.count(word) >= 1
        delta = ar(edited) - base
        ratio = float(np.linalg.norm(delta)
                      / ((np.linalg.norm(base)
                          + np.linalg.norm(base + delta)) / 2))
        vectors[name] = delta / max(float(np.linalg.norm(delta)), 1e-12)
        report["per_arm"][name] = {"word": word, "sentence": edited,
                                   "delta_norm_ratio": ratio,
                                   "echo": lab.echo(edited)}
        print(f"{name:<12} ratio={ratio:.3f}")

    old = np.load(data / "vectors.npz")
    repro = float(vectors["s2_emoji"] @ old["single_5"])
    report["repro_check_cos_s2emoji_vs_single5"] = repro
    print(f"reproducibility: cos(s2_emoji, wave-1 single_5) = {repro:.6f}")

    np.savez_compressed(data / "vectors_v2.npz", **vectors)
    (data / "build_v2_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"saved vectors_v2.npz and build_v2_report.json to {data}")


if __name__ == "__main__":
    main()
