"""
Phase 2: decode the target activation through the real AV.

Run this WHILE SGLANG IS RUNNING.

Usage:
    python3 ar_edit_phase2_decode.py \\
        --checkpoint ~/NLASteering/models/nla-qwen2.5-7b-L20-av \\
        --sglang-url http://localhost:30000
"""

from __future__ import annotations

import argparse

import numpy as np

from nla_inference import NLAClient


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--checkpoint", default="kitft/nla-qwen2.5-7b-L20-av")
    ap.add_argument("--sglang-url", default="http://localhost:30000")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--max-new-tokens", type=int, default=200)
    args = ap.parse_args()

    data = np.load("ar_edit_target.npz")
    target_activation = data["target_activation"]

    print(f"Loaded target activation, norm={np.linalg.norm(target_activation):.2f}")
    print(f"Connecting to NLA actor at {args.sglang_url} ...")
    client = NLAClient(args.checkpoint, sglang_url=args.sglang_url)

    explanation = client.generate(
        target_activation,
        temperature=args.temperature,
        max_new_tokens=args.max_new_tokens,
    )

    print("\n" + "=" * 80)
    print("ORIGINAL EXPLANATION")
    print("=" * 80)
    print(explanation)

    with open("ar_edit_original_explanation.txt", "w", encoding="utf-8") as f:
        f.write(explanation)

    print("\nWrote ar_edit_original_explanation.txt")
    print(
        "Next: hand-edit this text to inject eval-awareness framing, save "
        "as ar_edit_edited_explanation.txt, stop SGLang, then run "
        "ar_edit_phase3_patch.py"
    )


if __name__ == "__main__":
    main()
