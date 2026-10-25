"""Three-arm factuality comparison: does steering degrade factual accuracy?

The calibration rubric contains only the av_edit arm, so its absolute
factual-correctness score is uninterpretable -- a low score could reflect the
prompts or the token-cap truncation rather than any steering cost. This script
pulls the matched unsteered and random completions for the same
(concept, prompt, sample) triples and judges all three arms identically.

Question it answers: is av_edit factual accuracy below matched unsteered?
  - Yes -> the quality gate (length / repetition / Jaccard drift) is blind to a
    real cost, and that belongs in Limitations.
  - No  -> the 0.87 absolute score is a property of the prompts and truncation,
    not of steering, and no such claim should be made.

Both outcomes are reportable.

Usage:
  export OPENROUTER_API_KEY=...
  python -m experiments.factuality_three_arm --per-arm 60
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from pathlib import Path

from experiments import openrouter_client
from experiments.rubric_two_judge import (
    JUDGE_A, JUDGE_B, SYSTEM, TEMPLATE, parse_labels,
)

TRANSCRIPTS = Path("artifacts/baseline_panel14/qwen_nla_calibration/transcripts.json")
OUT = Path("artifacts/baseline_panel14/reliability_v1/factuality_three_arm.json")
ARMS = ("unsteered", "av_edit", "random_17")



def complete_resilient(prompts, *, model, max_tokens, concurrency,
                       chunk=30, attempts=4):
    """openrouter_client raises on body-level errors (e.g. 200 + {"error":
    "Overloaded", "code": 503}) without retrying, which kills the whole
    asyncio.gather and discards completed work. Chunk the batch and retry
    failed chunks so a transient upstream blip costs one chunk, not the run."""
    import time
    out: list[str | None] = []
    for i in range(0, len(prompts), chunk):
        part = prompts[i:i + chunk]
        got = None
        for a in range(attempts):
            try:
                got = openrouter_client.complete_batch(
                    part, model=model, max_tokens=max_tokens,
                    concurrency=concurrency)
                break
            except Exception as e:
                wait = min(2 ** a * 5, 60)
                print(f"  chunk {i//chunk} attempt {a+1}/{attempts} failed "
                      f"({type(e).__name__}); retrying in {wait}s", flush=True)
                time.sleep(wait)
        if got is None:
            print(f"  chunk {i//chunk} permanently failed; marking None", flush=True)
            got = [None] * len(part)
        out.extend(got)
        print(f"  {min(i+chunk, len(prompts))}/{len(prompts)} done", flush=True)
    return out


def build_items(per_arm: int) -> list[dict]:
    """Matched triples: same concept and prompt across all three arms."""
    tr = json.loads(TRANSCRIPTS.read_text())
    items: list[dict] = []
    for t in tr:
        ga = t.get("generation_arms") or {}
        if not all(a in ga for a in ARMS):
            continue
        for si in range(3):
            row = {}
            for arm in ARMS:
                v = ga[arm]
                seq = v if isinstance(v, list) else [v]
                if si >= len(seq):
                    row = {}
                    break
                s = seq[si]
                txt = s.get("text") if isinstance(s, dict) else str(s)
                if not txt or len(txt.strip()) < 30:
                    row = {}
                    break
                row[arm] = txt
            if not row:
                continue
            for arm, txt in row.items():
                items.append({
                    "concept_id": t["concept_id"],
                    "prompt_index": t["prompt_index"],
                    "sample_index": si,
                    "arm": arm,
                    "prompt": t["prompt"],
                    "output": txt,
                })
    # keep a balanced sample across arms
    out: list[dict] = []
    for arm in ARMS:
        sel = [x for x in items if x["arm"] == arm][:per_arm]
        out.extend(sel)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-arm", type=int, default=60)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--judge-a", default=JUDGE_A)
    ap.add_argument("--judge-b", default=JUDGE_B)
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("set OPENROUTER_API_KEY")

    items = build_items(args.per_arm)
    counts = {a: sum(1 for x in items if x["arm"] == a) for a in ARMS}
    print(f"items per arm: {counts}", flush=True)
    if min(counts.values()) == 0:
        raise SystemExit("could not build matched arms from transcripts")

    prompts = [(SYSTEM, TEMPLATE.format(prompt=x["prompt"], output=x["output"]))
               for x in items]

    scored: dict[str, list] = {}
    for tag, model in (("a", args.judge_a), ("b", args.judge_b)):
        print(f"judging {len(prompts)} items with {model} ...", flush=True)
        raw = complete_resilient(
            prompts, model=model, max_tokens=args.max_tokens,
            concurrency=args.concurrency,
        )
        scored[tag] = [parse_labels(t) if t else None for t in raw]
        print(f"  parsed {sum(1 for x in scored[tag] if x)}/{len(raw)}", flush=True)

    rows = []
    for it, la, lb in zip(items, scored["a"], scored["b"]):
        rows.append({**{k: it[k] for k in
                        ("concept_id", "prompt_index", "sample_index", "arm")},
                     "judge_a": la, "judge_b": lb})

    summary: dict = {"arms": {}, "judge_a": args.judge_a, "judge_b": args.judge_b}
    for arm in ARMS:
        sel = [r for r in rows if r["arm"] == arm and r["judge_a"] and r["judge_b"]]
        if not sel:
            continue
        stats = {"n": len(sel)}
        for ax in ("factual_correctness", "relevance", "fluency", "repetition", "usable"):
            vals = [(r["judge_a"][ax] + r["judge_b"][ax]) / 2 for r in sel]
            stats[ax] = round(statistics.mean(vals), 3)
        summary["arms"][arm] = stats

    a = summary["arms"]
    if "av_edit" in a and "unsteered" in a:
        d = a["av_edit"]["factual_correctness"] - a["unsteered"]["factual_correctness"]
        summary["av_minus_unsteered_factual"] = round(d, 3)
        summary["interpretation"] = (
            "av_edit scores BELOW matched unsteered on factual correctness; the "
            "quality gate does not capture this cost." if d < -0.1 else
            "no clear factual gap between av_edit and matched unsteered; the low "
            "absolute score reflects the prompts and truncation, not steering.")

    summary["caveat"] = (
        "Two automated judges, not human validation. Completions are truncated "
        "mid-sentence by a generation-time token cap; judges were instructed to "
        "disregard the truncated ending. Qwen panel-14 calibration split only.")

    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
