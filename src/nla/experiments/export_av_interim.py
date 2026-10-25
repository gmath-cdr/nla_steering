"""Export human-readable original/edited AV traces for confirmed concepts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def block(label: str, text: str) -> list[str]:
    return [f"**{label}:**", "", "~~~text", text.strip(), "~~~", ""]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", default="av_interim_audit.md")
    args = parser.parse_args()
    run = Path(args.run_dir)
    summary = json.loads((run / "confirmation_summary.json").read_text())
    transcripts = json.loads((run / "transcripts.json").read_text())
    confirmed = summary["confirmed_concept_ids"]
    rows_by_concept: dict[int, list[dict[str, Any]]] = {cid: [] for cid in confirmed}
    for row in transcripts:
        cid = int(row["concept_id"])
        if cid in rows_by_concept:
            rows_by_concept[cid].append(row)

    lines = [
        "# AV-edit interim trace audit", "",
        "> These 14 concepts passed the automated frozen gates. They are not all",
        "> manually validated clean successes; this document preserves the complete",
        "> evidence needed to identify lexical false positives and semantic intrusion.",
        "", f"Concepts: {len(confirmed)}", "",
    ]
    for cid in confirmed:
        rows = sorted(rows_by_concept[cid], key=lambda row: int(row["prompt_index"]))
        concept = rows[0]["concept"]
        lines.extend([f"## c{cid}: {concept}", ""])
        for row in rows:
            lines.extend([
                f"### Prompt {int(row['prompt_index']) + 1}", "",
                f"**Prompt:** {row['prompt']}", "",
            ])
            for rollout_index, rollout in enumerate(row["rollouts"], 1):
                lines.extend([f"#### AV rollout {rollout_index}", ""])
                lines.extend(block("Original", rollout["original_explanation"]))
                lines.extend(block("Edited", rollout["edited_explanation"]))
                lines.extend([
                    f"**Raw AR delta norm:** {rollout['raw_delta_norm']:.6f}", "",
                ])
            for arm in ("unsteered", "av_edit", "random"):
                for sample_index, response in enumerate(row["generation_arms"][arm], 1):
                    lines.extend(block(f"{arm} generation {sample_index}", response))
    output = run / args.output
    rendered = "\n".join(lines)
    output.write_text("\n".join(line.rstrip() for line in rendered.splitlines()).rstrip() + "\n")
    print(f"{len(confirmed)} concepts, {sum(len(v) for v in rows_by_concept.values())} prompts -> {output}")


if __name__ == "__main__":
    main()
