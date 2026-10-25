"""Phase A of the intensity-ladder experiment: the look phase. GPU.

    python experiments/intensity/phase_a_look.py

Does three things and stops (the edit template gets designed by humans
AFTER reading this output, never before):

  1. Census both task prompts: 30 samples each at T=0.7, extract the
     numeric answer, tally the distribution. "Random" will have a mode.
  2. Extract the layer-20 state at the answer position (last prompt
     token) for each prompt. Save the FULL vectors.
  3. Verbalize each state: greedy (the canonical S for editing) plus
     three T=0.7 samples (to see how stable the phrasing is).

Everything is written to experiments/intensity/data/: phase_a.json
(all texts, numbers, config) and phase_a_vectors.npz (the vectors).
Needs load=("base", "av"): ~30 GB.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nla_lab import NLASession  # noqa: E402

PROMPTS = [
    "Add a random number to 0. Reply with just the result.",
    "Add a random number to 50. Reply with just the result.",
]
N_CENSUS = 30
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def first_number(text: str) -> float | None:
    m = _NUM.search(text)
    return float(m.group(0)) if m else None


def main() -> None:
    out_dir = Path(__file__).resolve().parent / "data"
    out_dir.mkdir(exist_ok=True)
    lab = NLASession(load=("base", "av"))

    report: dict = {"config": {"prompts": PROMPTS, "n_census": N_CENSUS,
                               "census_temperature": 0.7,
                               "max_new_tokens": 16,
                               "verbalize_greedy_plus_samples": 3},
                    "prompts": {}}
    vectors: dict[str, np.ndarray] = {}

    for prompt in PROMPTS:
        entry: dict = {}
        # 1. census
        samples = []
        for s in range(N_CENSUS):
            text = lab.generate(prompt, temperature=0.7,
                                max_new_tokens=16, seed=s)
            samples.append({"seed": s, "text": text,
                            "number": first_number(text)})
        nums = [x["number"] for x in samples if x["number"] is not None]
        entry["census"] = samples
        entry["census_summary"] = {
            "n_numeric": len(nums),
            "median": float(np.median(nums)) if nums else None,
            "distinct": sorted({n for n in nums}),
        }
        print(f"\n{prompt}\n  numeric {len(nums)}/{N_CENSUS}, "
              f"median {entry['census_summary']['median']}, "
              f"distinct {entry['census_summary']['distinct'][:12]}")

        # 2. extract the state at the answer position (last prompt token)
        acts, toks = lab.extract(prompt)
        v = acts[-1]
        vectors[prompt] = v.float().cpu().numpy()
        entry["state"] = {"position": "last_prompt_token",
                          "n_prompt_tokens": len(toks),
                          "norm": float(v.norm())}

        # 3. verbalize: greedy canon + stability samples
        entry["verbalization_greedy"] = lab.verbalize(v, temperature=0.0)
        entry["verbalization_samples"] = [
            lab.verbalize(v, temperature=0.7) for _ in range(3)
        ]
        print(f"  S (greedy): {entry['verbalization_greedy'][:200]}")
        report["prompts"][prompt] = entry

    (out_dir / "phase_a.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(out_dir / "phase_a_vectors.npz",
                        **{p: vectors[p] for p in PROMPTS})
    print(f"\nsaved {out_dir / 'phase_a.json'} and phase_a_vectors.npz")
    print("PHASE A COMPLETE. Review the verbalizations and fix "
          "the edit template before touching phase B.")


if __name__ == "__main__":
    main()
