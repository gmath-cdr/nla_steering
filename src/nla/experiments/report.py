"""Write a concise control-aware Markdown report from experiment JSON."""

from __future__ import annotations

import argparse
from pathlib import Path

from experiments import results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    rows = results.load(args.input)
    baseline = next((row for row in rows if row["label"] == "unsteered"), None)
    lines = ["# Steering report", ""]
    if baseline is None:
        lines.append("No unsteered control was found.")
    else:
        random_by_suffix = {}
        for row in rows:
            if row["label"].startswith("random__"):
                random_by_suffix["__".join(row["label"].split("__")[1:])] = row
        lines += [f"Unsteered: concept={baseline['mean_concept']:.2f}, fluency={baseline['mean_fluency']:.2f}", "",
                  "| Arm | Concept | Fluency | Supported |", "|---|---:|---:|---|"]
        for row in rows:
            if row is baseline:
                continue
            suffix = "__".join(row["label"].split("__")[1:])
            random = random_by_suffix.get(suffix)
            beats_random = random is None or row["mean_concept"] > random["mean_concept"]
            supported = (not row["label"].startswith("random__") and beats_random and
                         row["mean_concept"] > baseline["mean_concept"] and
                         abs(row["mean_fluency"] - baseline["mean_fluency"]) <= 0.5)
            lines.append(f"| {row['label']} | {row['mean_concept']:.2f} | "
                         f"{row['mean_fluency']:.2f} | {'yes' if supported else 'no'} |")
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
