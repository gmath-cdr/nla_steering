"""Reproduce the paper's steering demo, as faithfully as possible, on Qwen.

The original demo (run on Claude Opus 4.6, NOT on these open checkpoints,
which is exactly why this reproduction matters):

  1. Prompt a rhyming-couplet continuation; the model plans an end rhyme
     ("... grab it," -> next line will end in "rabbit"/"habit").
  2. Extract the activation at the newline token (the planning site).
  3. Verbalize it, then edit the sentence: rabbit->mouse, habit->house,
     carrot->cheese.
  4. Steering vector = AR(edited) - AR(original)   (a DIFFERENCE, not the
     raw reconstruction of the edited sentence).
  5. Inject h -> h + alpha * ||h|| * delta_unit at the newline position only.
  6. At sufficient alpha, completions flip from rabbit/habit to mouse/house.
     The authors report ~50% success and occasional incoherence. That is the
     published ceiling on their strongest model; expect less here.

Run on the pod:

    python experiments/repro_rhyme.py
    python experiments/repro_rhyme.py --alphas 0 2 4 8 12 16 --n 20
    python experiments/repro_rhyme.py --position -1   # steer last token instead

Outputs a table: alpha vs rabbit-family and mouse-family completion counts.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from nla_lab import NLASession

PROMPT = "A rhyming couplet:\nHe saw a carrot and had to grab it,\n"

# the paper's exact word swaps
EDITS = [
    ("rabbit", "mouse"),
    ("habit", "house"),
    ("carrot", "cheese"),
    ("Rabbit", "Mouse"),
    ("Habit", "House"),
    ("Carrot", "Cheese"),
]

RABBIT_FAMILY = re.compile(r"\b(rabbit|habit)\b", re.IGNORECASE)
MOUSE_FAMILY = re.compile(r"\b(mouse|house)\b", re.IGNORECASE)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    # team calibration on Qwen (July 2026): single-position effects switch on
    # near alpha 1; past ~2 the text degrades
    ap.add_argument("--alphas", type=float, nargs="+", default=[0, 0.5, 1, 1.5, 2, 3])
    ap.add_argument("--n", type=int, default=10, help="samples per alpha")
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument(
        "--position",
        type=int,
        default=None,
        help="prompt token index to steer (default: last newline, as in the "
        "paper; -1 = last prompt token)",
    )
    ap.add_argument("--max-new-tokens", type=int, default=24)
    args = ap.parse_args()

    lab = NLASession()

    # ── extract at the planning site ───────────────────────────────────────
    acts, toks = lab.extract(PROMPT, raw=True)
    print("prompt tokens (tail):")
    lab.show_tokens(acts, toks, tail=12)

    pos = args.position
    if pos is None:
        pos = lab.find_token(toks, "\n", from_end=True)
    if pos < 0:
        pos = len(toks) + pos
    print(f"\nsteering position: [{pos}] {toks[pos]!r}  ||v||={acts[pos].norm():.1f}")
    if acts[pos].norm() > 1000:
        print(
            "NOTE: outlier-norm activation (Qwen newlines can hit ~14k). These "
            "often verbalize poorly. If the explanation below looks unrelated "
            "to rhymes/rabbits, try --position -1 (last token) or the comma."
        )

    # ── verbalize and check fidelity BEFORE editing ────────────────────────
    sent = lab.verbalize(acts[pos], temperature=0.0)
    mse, cos = lab.fidelity(sent, acts[pos])
    print(f"\nverbalization (cos={cos:.3f}, mse={mse:.3f}):\n{sent}\n")
    if cos < 0.5:
        print(
            "WARNING: poor round-trip fidelity at this position. The edit may "
            "not carry signal. Consider another position; continuing anyway."
        )

    # ── the paper's edit ───────────────────────────────────────────────────
    edited = sent
    for a, b in EDITS:
        edited = edited.replace(a, b)
    if edited == sent:
        print(
            "NOTE: none of rabbit/habit/carrot appear in the verbalization, so "
            "the template edit changed nothing. Hand-edit the sentence in a "
            "REPL/notebook instead; proceeding with an appended-hint edit."
        )
        edited = sent + " The next line will end with the word 'mouse'."
    print(f"edited:\n{edited}\n")

    # ── difference-of-reconstructions steering vector ──────────────────────
    delta = lab.direction(edited, sent)

    # ── sweep alpha, sample completions, count rhyme families ──────────────
    print(f"{'alpha':>6}  {'rabbit/habit':>13}  {'mouse/house':>12}  {'other':>6}   example")
    print("-" * 100)
    for alpha in args.alphas:
        counts: Counter[str] = Counter()
        example = ""
        for i in range(args.n):
            out = lab.generate(
                PROMPT,
                direction=delta,
                alpha=alpha,
                mode="position",
                position=pos,
                raw=True,
                temperature=args.temperature,
                max_new_tokens=args.max_new_tokens,
                seed=i,
            )
            line = out.splitlines()[0] if out.splitlines() else out
            if MOUSE_FAMILY.search(line):
                counts["mouse"] += 1
                example = example or line.strip()
            elif RABBIT_FAMILY.search(line):
                counts["rabbit"] += 1
            else:
                counts["other"] += 1
            if not example and i == 0:
                example = line.strip()
        print(
            f"{alpha:>6g}  {counts['rabbit']:>13}  {counts['mouse']:>12}  "
            f"{counts['other']:>6}   {example[:50]!r}"
        )

    print(
        "\nread-out: success looks like the mouse/house column growing with "
        "alpha while completions stay coherent. rabbit column unchanged at "
        "every alpha = no effect (also a real, reportable result)."
    )


if __name__ == "__main__":
    main()
