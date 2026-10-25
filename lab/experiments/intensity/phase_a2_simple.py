"""Phase A2: stupid-simple prompts, no 'random'. GPU, ~10 min.

    python experiments/intensity/phase_a2_simple.py

Phase A1 taught us 'random' derails the model (it simulates random()
calls) and destabilizes the verbalizer. A2 uses bare choose-a-number
prompts across three ranges, censuses each at BOTH temperatures, and
takes one GREEDY verbalization per prompt (the canonical, reproducible
S). Everything saved: data/phase_a2.json + data/phase_a2_vectors.npz.
Needs load=("base", "av").
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
    "Choose a number between 0 and 10. Reply with just the number.",
    "Choose a number between 0 and 100. Reply with just the number.",
    "Choose a number between 0 and 1000. Reply with just the number.",
]
N_SAMPLED = 30   # T=0.7 census
N_GREEDY = 5     # T=0.0 census (expected identical; 5 confirms)
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def first_number(text: str) -> float | None:
    m = _NUM.search(text)
    return float(m.group(0)) if m else None


def census(lab, prompt: str, temperature: float, n: int) -> list[dict]:
    out = []
    for s in range(n):
        text = lab.generate(prompt, temperature=temperature,
                            max_new_tokens=12, seed=s)
        out.append({"seed": s, "text": text, "number": first_number(text)})
    return out


def summarize(samples: list[dict]) -> dict:
    nums = [x["number"] for x in samples if x["number"] is not None]
    from collections import Counter
    return {"n_numeric": len(nums), "n_total": len(samples),
            "median": float(np.median(nums)) if nums else None,
            "counts": {str(k): v for k, v in
                       Counter(nums).most_common(8)}}


def main() -> None:
    out_dir = Path(__file__).resolve().parent / "data"
    out_dir.mkdir(exist_ok=True)
    lab = NLASession(load=("base", "av"))

    report = {"config": {"prompts": PROMPTS, "n_sampled": N_SAMPLED,
                         "n_greedy": N_GREEDY, "max_new_tokens": 12},
              "prompts": {}}
    vectors: dict[str, np.ndarray] = {}

    for prompt in PROMPTS:
        entry: dict = {}
        entry["census_t07"] = census(lab, prompt, 0.7, N_SAMPLED)
        entry["census_t07_summary"] = summarize(entry["census_t07"])
        entry["census_t00"] = census(lab, prompt, 0.0, N_GREEDY)
        entry["census_t00_summary"] = summarize(entry["census_t00"])

        acts, toks = lab.extract(prompt)
        v = acts[-1]
        vectors[prompt] = v.float().cpu().numpy()
        entry["state"] = {"position": "last_prompt_token",
                          "n_prompt_tokens": len(toks),
                          "norm": float(v.norm())}
        entry["verbalization_greedy"] = lab.verbalize(v, temperature=0.0)

        print(f"\n=== {prompt}")
        print(f"  T=0.7: {entry['census_t07_summary']}")
        print(f"  T=0.0: {entry['census_t00_summary']}")
        print(f"  S: {entry['verbalization_greedy'][:180]}")
        report["prompts"][prompt] = entry

    (out_dir / "phase_a2.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(out_dir / "phase_a2_vectors.npz",
                        **{p: vectors[p] for p in PROMPTS})
    print(f"\nsaved {out_dir / 'phase_a2.json'} and phase_a2_vectors.npz")


if __name__ == "__main__":
    main()
