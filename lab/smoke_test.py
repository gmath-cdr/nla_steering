"""Day-one smoke test. Run on the GPU pod after setup_pod.sh:

    python smoke_test.py

Checks the full loop end to end in ~3 minutes on an A100 80GB:
  1. load all three models
  2. extract activations at the active checkpoint family's registered layer
  3. verbalize one activation (greedy, reproducible)
  4. round-trip fidelity: does AR(explanation) point back at the original?
  5. a free-form steering direction actually changes a deterministic probe

Steps 1-4 are hard PASS/FAIL gates (they verify the plumbing).
Step 5 is informational: steering is the research question, not the plumbing.
"""

from __future__ import annotations

import sys
import traceback

import torch

from nla_lab import NLASession, probes
from nla_lab.session import HIDDEN, LAYER, NLA_MODEL

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{PASS if ok else FAIL}] {name}  {detail}")


def cjk_fraction(s: str) -> float:
    if not s:
        return 1.0
    return sum("一" <= c <= "鿿" for c in s) / len(s)


def main() -> int:
    # 1. load ──────────────────────────────────────────────────────────────
    lab = NLASession()
    check("load models", True)

    # 2. extract ───────────────────────────────────────────────────────────
    acts, toks = lab.extract("What is the capital of France?")
    check(
        "extract shape",
        acts.ndim == 2 and acts.shape[1] == HIDDEN and acts.shape[0] == len(toks),
        f"model={NLA_MODEL} layer={LAYER} acts={tuple(acts.shape)} tokens={len(toks)}",
    )
    # typical Qwen layer-20 norms are ~100-170 (authors' worked example);
    # use the median so newline outliers (~14k) don't trip the check
    med = acts.norm(dim=-1).median().item()
    check("activation norms plausible", 30 < med < 1000, f"median ||v||={med:.1f}")

    # 3. verbalize the last prompt token (assistant header: in-distribution) ─
    sent = lab.verbalize(acts[-1], temperature=0.0)
    print(f"\n  explanation: {sent[:300]}{'...' if len(sent) > 300 else ''}\n")
    check("verbalize returns text", len(sent) > 40, f"{len(sent)} chars")
    check(
        "output not CJK-dominated (injection sanity)",
        cjk_fraction(sent) < 0.3,
        f"cjk={cjk_fraction(sent):.0%}",
    )

    # 4. round trip: AR(explanation) vs original activation ────────────────
    mse, cos = lab.fidelity(sent, acts[-1])
    # authors: cos ~0.7-0.9 on clean in-distribution positions; below 0.5
    # on a clean position means the plumbing (not the science) is broken
    check("round-trip fidelity", cos > 0.5, f"cos={cos:.3f} mse={mse:.3f}")

    # 5. free-form steering (informational) ────────────────────────────────
    pos = (
        "Casual, excited chat reply packed with emojis and exclamation marks, "
        "playful upbeat tone, informal register."
    )
    neg = (
        "Plain, formal, measured chat reply with no emojis and no exclamation "
        "marks, neutral professional tone."
    )
    delta = lab.direction(pos, neg)
    base_txt = lab.generate("Tell me about your day.", temperature=0.7, seed=0)
    steer_txt = lab.generate(
        "Tell me about your day.", direction=delta, alpha=1.0, temperature=0.7, seed=0
    )
    b = probes.emojis(base_txt) + probes.exclamations(base_txt)
    s = probes.emojis(steer_txt) + probes.exclamations(steer_txt)
    print(f"\n  baseline (emoji+excl={b}): {base_txt[:160]!r}")
    print(f"  steered  (emoji+excl={s}): {steer_txt[:160]!r}\n")
    print(
        f"[info] steering probe moved {b} -> {s} at alpha=1. "
        "No hard pass/fail: whether NLA steering works is the research question. "
        "If nothing moves across alpha in {0.5, 1, 2}, see README troubleshooting."
    )

    failed = [n for n, ok in results if not ok]
    print("\n" + ("all plumbing checks passed" if not failed else f"FAILED: {failed}"))
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print("\nsmoke test crashed; fix before running anything else")
        sys.exit(2)
