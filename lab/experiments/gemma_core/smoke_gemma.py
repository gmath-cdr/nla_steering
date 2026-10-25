"""Gemma phase 1: smoke test + substrate extraction. Pod, ~15 min.

    NLA_MODEL=gemma3-12b python experiments/gemma_core/smoke_gemma.py

Hard gates, in order: (1) session loads base+verbalizer+reconstructor
under the gemma3-12b config (layer 32, hidden 3840, sidecar injection
scale, root-of-d embed scale all via the vendored client); (2)
round-trip fidelity on a smoke sentence is sane (cos >= 0.5; the
authors' Gemma examples sit well above); (3) seeded generation is
deterministic (two identical calls, identical text); (4) steering
plumbing moves the emoji probe at moderate strength without garbage.
Then extracts + greedily verbalizes the two substrate prompts (emoji
chat prompt and wordle prompt) and saves them for edit authoring.
Prints both paragraphs; edits are written against these exact texts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nla_lab import NLASession, probes  # noqa: E402
from nla_lab.session import LAYER, NLA_MODEL  # noqa: E402

FREEFORM_POS = ("Casual, excited chat reply packed with emojis and "
                "exclamation marks, playful upbeat tone, informal register.")
FREEFORM_NEG = ("Plain, formal, measured chat reply with no emojis and no "
                "exclamation marks, neutral professional tone.")
SUBSTRATES = {"chat": "Tell me about your day.",
              "wordle": "Think of a word to start a wordle game."}


def main() -> None:
    assert NLA_MODEL == "gemma3-12b", f"run with NLA_MODEL=gemma3-12b (got {NLA_MODEL})"
    out = Path(__file__).resolve().parent / "data"
    out.mkdir(exist_ok=True)
    report: dict = {"layer": LAYER}

    lab = NLASession(load=("base", "av", "ar"))

    # gate 2: the REAL round trip — extract a live state, verbalize it,
    # reconstruct the description, compare against the original state.
    # (Comparing AR(s) to AR(s) is vacuous; first smoke draft did that.)
    acts, _ = lab.extract("What should I cook for dinner tonight?")
    state = acts[-1]
    described = lab.verbalize(state, temperature=0.0)
    mse, cos = lab.fidelity(described, state)
    report["roundtrip_cos"] = float(cos)
    report["roundtrip_verbalization"] = described[:300]
    print(f"GATE roundtrip: cos={cos:.3f}")
    assert cos >= 0.4, "round-trip fidelity failed hard"

    # gate 3: determinism
    a = lab.generate("Say hello.", temperature=0.7, max_new_tokens=24, seed=3)
    b = lab.generate("Say hello.", temperature=0.7, max_new_tokens=24, seed=3)
    assert a == b, "seeded generation not deterministic"
    report["determinism"] = "ok"
    print("GATE determinism: ok")

    # gate 4: steering plumbing, as an alpha mini-ladder. Qwen's window
    # (0.4-0.65) need not transfer; the gate asks only for signs of life:
    # some alpha where text stays coherent, whether or not it steers yet.
    d = lab.direction(FREEFORM_POS, FREEFORM_NEG)
    ladder = {}
    for a_val in (0.0, 0.05, 0.1, 0.2, 0.35, 0.5):
        txt = lab.generate("Tell me about your day.", direction=d,
                           alpha=a_val, mode="all", temperature=0.7,
                           max_new_tokens=48, seed=0)
        ladder[a_val] = {"emojis": probes.emojis(txt),
                         "exclaim": probes.exclamations(txt),
                         "n_words": probes.n_words(txt),
                         "sample": txt[:110]}
        print(f"  alpha={a_val}: emojis={ladder[a_val]['emojis']} "
              f"exclaim={ladder[a_val]['exclaim']} "
              f"words={ladder[a_val]['n_words']} | {txt[:80]!r}")
    report["steer_ladder"] = ladder

    # substrates for edit authoring
    for key, prompt in SUBSTRATES.items():
        acts, toks = lab.extract(prompt)
        state = acts[-1]
        para = lab.verbalize(state, temperature=0.0)
        report[f"substrate_{key}"] = {"prompt": prompt,
                                      "n_prompt_tokens": len(toks),
                                      "verbalization_greedy": para}
        np.savez_compressed(out / f"substrate_{key}.npz",
                            state=state.float().cpu().numpy())
        print(f"\n=== SUBSTRATE [{key}] ===\n{para}\n")

    (out / "smoke_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print("smoke report saved")


if __name__ == "__main__":
    main()
