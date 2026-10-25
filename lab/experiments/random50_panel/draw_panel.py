"""Draw the random 50-concept Concept500 panel and write its word lists.

This module performs no model or network work. The draw is a seeded simple
random sample from every Concept500 concept that has not appeared in an earlier
panel. Earlier panels are read from their own source files, never typed here.

Run once from the repository root::

    python lab/experiments/random50_panel/draw_panel.py

The output files are written next to this script and are refused if they
already exist, so the panel cannot be redrawn by accident.
"""

from __future__ import annotations

import hashlib
import json
import platform
import random
import re
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SEED = 20261012
PANEL_SIZE = 50

METADATA = ROOT / "src/axbench/axbench/concept500/prod_9b_l20_v1/generate/metadata.jsonl"
CONFIGS = ROOT / "src/nla/experiments/configs"
LEXICALITY = ROOT / "lab/experiments/axbench_lexicality20"
DIFFMEAN_TOP6 = (
    ROOT / "artifacts/paper_runs/data/qwen2.5-7b/av_edit_confirm_diffmean_top6_2026/protocol.json"
)
ARCHIVED_TERMS = (
    CONFIGS / "gemma_qwen_positive14_terms.json",
    CONFIGS / "gemma_confirmed10_terms.json",
)

# Mirrors ``lexical_terms`` in src/nla/experiments/discovery500.py, which cannot
# be imported here because that module loads model code. ``check_word_rule``
# fails the draw unless this copy reproduces every archived word list.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "related", "references", "specifically",
    "terms", "that", "the", "their", "these", "to", "topics", "various", "with",
}


def lexical_terms(concept: str) -> list[str]:
    words = [word.lower() for word in re.findall(r"[A-Za-z][A-Za-z'-]+", concept)]
    return sorted({word for word in words if len(word) >= 4 and word not in STOPWORDS})


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_labels() -> dict[int, str]:
    labels: dict[int, str] = {}
    for line in METADATA.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            labels[int(row["concept_id"])] = row["concept"]
    if len(labels) != 500:
        raise SystemExit(f"expected 500 concepts, found {len(labels)}")
    return labels


def earlier_panels() -> dict[str, list[int]]:
    """Return every earlier panel, keyed by the file that defines it."""
    panels: dict[str, list[int]] = {}
    for name in ("qwen_selected14_baseline_protocol.json", "gemma_confirmed10_baseline_protocol.json"):
        path = CONFIGS / name
        panels[path.relative_to(ROOT).as_posix()] = json.loads(path.read_text(encoding="utf-8"))["concept_ids"]
    campaign = LEXICALITY / "campaign.review.yaml"
    panels[campaign.relative_to(ROOT).as_posix()] = [
        int(row["concept_id"])
        for row in yaml.safe_load(campaign.read_text(encoding="utf-8"))["concepts"]
    ]
    frozen = LEXICALITY / "panel.frozen.yaml"
    prior = yaml.safe_load(frozen.read_text(encoding="utf-8"))["prior_work_exclusions"]
    for key, value in prior.items():
        if isinstance(value, list) and all(isinstance(item, int) for item in value):
            panels[f"{frozen.relative_to(ROOT).as_posix()}#prior_work_exclusions.{key}"] = value
    panels[DIFFMEAN_TOP6.relative_to(ROOT).as_posix()] = json.loads(
        DIFFMEAN_TOP6.read_text(encoding="utf-8")
    )["concept_ids"]
    return {source: sorted(int(item) for item in ids) for source, ids in panels.items()}


def check_word_rule(labels: dict[int, str]) -> int:
    checked = 0
    for path in ARCHIVED_TERMS:
        for concept_id, archived in json.loads(path.read_text(encoding="utf-8")).items():
            if lexical_terms(labels[int(concept_id)]) != archived:
                raise SystemExit(f"word rule does not reproduce {path.name} concept {concept_id}")
            checked += 1
    return checked


def main() -> None:
    panel_path = HERE / "panel.random50.json"
    terms_path = HERE / "terms.random50.json"
    for path in (panel_path, terms_path):
        if path.exists():
            raise SystemExit(f"{path.name} already exists; the panel is drawn once")

    labels = load_labels()
    reproduced = check_word_rule(labels)
    panels = earlier_panels()
    excluded = sorted({concept_id for ids in panels.values() for concept_id in ids})
    pool = sorted(set(labels) - set(excluded))
    drawn = random.Random(SEED).sample(pool, PANEL_SIZE)

    concepts = [
        {"concept_id": concept_id, "label": labels[concept_id], "terms": lexical_terms(labels[concept_id])}
        for concept_id in drawn
    ]
    if any(not row["terms"] for row in concepts):
        raise SystemExit("a drawn concept has an empty word list")

    panel = {
        "schema": "nla-random-panel/v1",
        "rule": (
            "seeded simple random sample, without replacement, from the sorted "
            "Concept500 ids that appear in no earlier panel"
        ),
        "seed": SEED,
        "sampler": "random.Random(seed).sample(sorted_pool, 50)",
        "python": platform.python_version(),
        "metadata_sha256": sha256_file(METADATA),
        "pool_size": len(pool),
        "excluded_count": len(excluded),
        "excluded_sources": panels,
        "word_rule": "lexical_terms(label), as in src/nla/experiments/discovery500.py",
        "word_rule_archived_lists_reproduced": reproduced,
        "concept_ids": drawn,
        "concepts": concepts,
    }
    panel_path.write_text(json.dumps(panel, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    terms = {str(row["concept_id"]): row["terms"] for row in concepts}
    terms_path.write_text(json.dumps(terms, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    print(f"pool {len(pool)} of 500 after excluding {len(excluded)} earlier-panel concepts")
    print(f"word rule reproduced {reproduced} archived lists")
    print(f"drew {len(drawn)} concepts with seed {SEED}")
    print(f"panel sha256 {sha256_file(panel_path)}")


if __name__ == "__main__":
    sys.exit(main())
