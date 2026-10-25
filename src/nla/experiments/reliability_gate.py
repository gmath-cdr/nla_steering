"""Versioned external reliability-gated AV-edit utilities."""
from __future__ import annotations
import argparse, json, shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "artifacts" / "baseline_panel14"
def load(path: Path) -> Any: return json.loads(path.read_text())
def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value, indent=2) + "\n")
def snapshot(root: Path) -> None:
    out = root / "k3_historical"
    for name in ("qwen_nla", "qwen_nla_calibration"):
        src, dst = root / name, out / name
        if not dst.exists(): shutil.copytree(src, dst)
    dump(out / "manifest.json", {"source": "baseline_panel14", "immutable": True, "purpose": "protocol development only", "rollouts": 3})
def inspect_k3(root: Path) -> dict[str, Any]:
    report = {"rollout_count": {}, "missing_reliability_fields": {}}
    for name in ("qwen_nla_calibration", "qwen_nla"):
        rows = load(root / name / "transcripts.json"); counts = [len(r.get("rollouts", [])) for r in rows]
        report["rollout_count"][name] = {"prompts": len(rows), "min": min(counts), "max": max(counts), "total": sum(counts)}
        fields = set().union(*(x.keys() for r in rows for x in r.get("rollouts", [])))
        report["missing_reliability_fields"][name] = sorted({"reconstructed_original", "reconstructed_edited", "target_activation", "delta_vector", "pairwise_delta_cosine"} - fields)
    return report
def make_label_set(root: Path, out: Path) -> None:
    rows = load(root / "qwen_nla_calibration" / "transcripts.json"); records = []
    for row in rows:
        for i, output in enumerate(row.get("generation_arms", {}).get("av_edit__ratio_0.5", row.get("generation_arms", {}).get("av_edit", []))):
            records.append({"review_id": f"r{len(records):04d}", "concept_id": row["concept_id"], "prompt_index": row["prompt_index"], "prompt": row["prompt"], "output": output, "sample_index": i, "labels": {"target_hit": None, "relevance": None, "factual_correctness": None, "repetition": None, "fluency": None, "usable": None}})
    dump(out, {"purpose": "calibration-only rubric validation", "frozen_alpha": 0.5, "records": records})
def main() -> None:
    p = argparse.ArgumentParser(); p.add_argument("command", choices=["snapshot", "inspect", "labels"]); p.add_argument("--root", type=Path, default=DEFAULT_ROOT); p.add_argument("--out", type=Path); a = p.parse_args()
    if a.command == "snapshot": snapshot(a.root)
    elif a.command == "inspect": print(json.dumps(inspect_k3(a.root), indent=2))
    else: make_label_set(a.root, a.out or a.root / "reliability_v1" / "calibration_review.json")
if __name__ == "__main__": main()
