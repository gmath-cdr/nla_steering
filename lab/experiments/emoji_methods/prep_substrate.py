"""Stage A: extract + verbalize the substrate state. Pod, ~4 min.

    python experiments/emoji_methods/prep_substrate.py

Substrate prompt = the first test prompt. Saves data/substrate.json
(paragraph + metadata) and data/substrate_vector.npz. The paragraph is
the text the ten edits get written against. Needs load=("base", "av").
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nla_lab import NLASession  # noqa: E402

SUBSTRATE_PROMPT = "Tell me about your day."


def main() -> None:
    out_dir = Path(__file__).resolve().parent / "data"
    out_dir.mkdir(exist_ok=True)
    lab = NLASession(load=("base", "av"))
    acts, toks = lab.extract(SUBSTRATE_PROMPT)
    v = acts[-1]
    paragraph = lab.verbalize(v, temperature=0.0)
    report = {
        "prompt": SUBSTRATE_PROMPT,
        "position": "last_prompt_token",
        "n_prompt_tokens": len(toks),
        "state_norm": float(v.norm()),
        "verbalization_greedy": paragraph,
    }
    (out_dir / "substrate.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(out_dir / "substrate_vector.npz",
                        state=v.float().cpu().numpy())
    print("=== SUBSTRATE PARAGRAPH ===")
    print(paragraph)
    print("===========================")
    print(f"saved to {out_dir}")


if __name__ == "__main__":
    main()
