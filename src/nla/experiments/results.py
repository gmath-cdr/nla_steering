"""Dependency-light evaluation, atomic result persistence, and shard merge."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


def score(concept: str, response: str) -> dict[str, float]:
    """Deterministic offline judge; replace only at analysis time if needed."""
    words = [word for word in concept.lower().replace(",", " ").split() if len(word) > 3]
    hits = sum(word in response.lower() for word in words)
    return {"concept_score": min(5.0, 1 + 2 * hits / max(1, len(words))),
            "fluency_score": min(5.0, 2 + len(response.split()) / 20)}


def arm(label: str, concept: str, responses: list[str]) -> dict[str, Any]:
    rows = [{"idx": index, "response": text, **score(concept, text)}
            for index, text in enumerate(responses)]
    concept_scores = [row["concept_score"] for row in rows]
    fluency_scores = [row["fluency_score"] for row in rows]
    mean_concept = statistics.fmean(concept_scores)
    mean_fluency = statistics.fmean(fluency_scores)
    return {"label": label, "mean_concept": mean_concept,
            "mean_fluency": mean_fluency,
            "combined": mean_concept * min(mean_fluency / 5, 1),
            "n_samples": len(rows), "per_prompt": rows}


def save(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(rows, indent=2) + "\n")
    temporary.replace(target)


def load(path: str | Path) -> list[dict[str, Any]]:
    file = Path(path)
    return json.loads(file.read_text()) if file.exists() else []


def merge(paths: list[str], output: str) -> None:
    rows, labels = [], set()
    for path in paths:
        for row in load(path):
            if row["label"] in labels:
                raise ValueError(f"overlapping shard arm: {row['label']}")
            labels.add(row["label"])
            rows.append(row)
    save(output, rows)


def write_case_study(path: str | Path, title: str, rows: list[dict[str, Any]]) -> Path:
    """Render any arm-based experiment JSON as an inspectable sample transcript."""
    output = Path(path).with_suffix(".case_study.md")
    lines = [f"# {title}", ""]
    for row in rows:
        lines += [f"## {row['label']}", "",
                  f"- Concept score: {row.get('mean_concept', 'n/a')}",
                  f"- Fluency score: {row.get('mean_fluency', 'n/a')}",
                  f"- Samples: {row.get('n_samples', len(row.get('responses', [])))}", ""]
        samples = row.get("per_prompt") or [{"response": value} for value in row.get("responses", [])]
        for index, sample in enumerate(samples):
            lines += [f"sample {index}: {sample.get('response', '')}", ""]
    output.write_text("\n".join(lines))
    return output
