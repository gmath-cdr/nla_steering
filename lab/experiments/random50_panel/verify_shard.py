"""Check that one finished shard holds every expected record.

Read-only. The expected counts follow from the protocol: 4 tuning prompts and
8 held-out prompts per concept, 2 sampled responses per prompt and arm.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


BASELINE_ARMS = {
    "unsteered", "diffmean", "caa_fixed", "caa_calibrated",
    "random_17", "random_42", "random_101",
}
NLA_ARMS = {"unsteered", "av_edit", "random_17", "random_42", "random_101"}
TUNING_PROMPTS, TEST_PROMPTS, SAMPLES = 4, 8, 2


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--ids", required=True)
    args = parser.parse_args()
    ids = [int(item) for item in args.ids.split(",")]
    problems: list[str] = []

    def expect(label: str, found, wanted) -> None:
        if found != wanted:
            problems.append(f"{label}: found {found}, expected {wanted}")

    base = args.out / args.model
    rows = load(base / "results.json")
    per_cell = Counter((row["concept_id"], row["method"]) for row in rows)
    for concept in ids:
        for arm in sorted(BASELINE_ARMS):
            expect(f"baseline c{concept} {arm}", per_cell[(concept, arm)], TEST_PROMPTS * SAMPLES)
    expect("baseline concepts", sorted({row["concept_id"] for row in rows}), sorted(ids))
    expect("baseline arms", {row["method"] for row in rows}, BASELINE_ARMS)
    selections = Counter(row["concept_id"] for row in load(base / "alpha_selections.json"))
    for concept in ids:
        expect(f"strength choices c{concept}", selections[concept], 2)

    prompting = Counter(row["concept_id"] for row in load(args.out / f"{args.model}_prompting" / "results.json"))
    for concept in ids:
        expect(f"prompting c{concept}", prompting[concept], TEST_PROMPTS * SAMPLES)

    for folder, prompts in ((f"{args.model}_nla_calibration", TUNING_PROMPTS), (f"{args.model}_nla", TEST_PROMPTS)):
        records = load(args.out / folder / "transcripts.json")
        per_concept = Counter(record["concept_id"] for record in records)
        for concept in ids:
            expect(f"{folder} c{concept} prompts", per_concept[concept], prompts)
        for record in records:
            arms = record["generation_arms"]
            missing = NLA_ARMS - set(arms)
            if missing:
                problems.append(f"{folder} c{record['concept_id']} p{record['prompt_index']}: missing arms {sorted(missing)}")
                continue
            for arm in NLA_ARMS:
                expect(
                    f"{folder} c{record['concept_id']} p{record['prompt_index']} {arm}",
                    len(arms[arm]), SAMPLES,
                )
    chosen = load(args.out / f"{args.model}_nla_calibration" / "selected_alphas.json")
    expect("NLA strength choices", sorted(int(key) for key in chosen), sorted(ids))

    if problems:
        print("\n".join(problems))
        print(f"VERIFY FAIL: {len(problems)} problems")
        return 1
    print(f"VERIFY OK: {len(ids)} concepts, {len(rows)} baseline rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
