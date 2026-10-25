"""Fail closed unless a baseline or NLA panel stage is structurally complete."""
from __future__ import annotations

import argparse, json
from pathlib import Path

import torch

IDS = [116, 222, 446, 11, 359, 406, 216, 287, 354, 30, 172, 171, 186, 90]
SEEDS = [17, 42, 101]


def load(path):
    assert path.exists(), f"missing {path}"
    return json.loads(path.read_text())


def verify_baseline(run):
    manifest = load(run / "manifest.json")
    rows = load(run / "results.json")
    calibration = load(run / "calibration.json")
    selections = load(run / "alpha_selections.json")
    assert manifest["rows"] == 1568 == len(rows)
    assert set(manifest["caa_extraction_metadata"]) == {str(x) for x in IDS}
    assert len(manifest["diffmean_prefix_suffix_trim"]) == 2
    methods = {"unsteered", "diffmean", "caa_fixed", "caa_calibrated",
               "random_17", "random_42", "random_101"}
    for cid in IDS:
        subset = [x for x in rows if x["concept_id"] == cid]
        assert len(subset) == 112
        assert {x["method"] for x in subset} == methods
        assert {x["prompt_index"] for x in subset} == set(range(14, 22))
        for method in methods:
            arm = [x for x in subset if x["method"] == method]
            assert len(arm) == 16 and {x["sample_index"] for x in arm} == {0, 1}
            if method.startswith("random_"):
                assert all(x.get("norm_matched") is True for x in arm)
                assert all(abs(x["vector_norm"] - x["reference_vector_norm"])
                           <= 1e-5 * max(1.0, x["reference_vector_norm"]) for x in arm)
        cal = [x for x in calibration if x["concept_id"] == cid]
        assert len(cal) == 120
        assert len([x for x in selections if x["concept_id"] == cid]) == 2
        for method in ("diffmean", "caa"):
            vector = torch.load(run / f"c{cid}.{method}.pt", map_location="cpu", weights_only=True)
            assert vector.ndim == 1 and torch.isfinite(vector).all() and vector.norm() > 0


def verify_nla(run, calibration_run):
    transcript = load(run / "transcripts.json")
    summary = load(run / "confirmation_summary.json")
    protocol = load(run / "protocol.json")
    alpha_map = load(calibration_run / "selected_alphas.json")
    alpha_rows = load(calibration_run / "alpha_calibration.json")
    cal_transcript = load(calibration_run / "transcripts.json")
    assert protocol["prompt_offset"] == 14 and protocol["n_prompts"] == 8
    assert len(transcript) == 112 and len(cal_transcript) == 56
    assert set(alpha_map) == {str(x) for x in IDS}
    assert len(alpha_rows) == 784
    assert len(summary["concepts"]) == 14
    for record in transcript:
        assert record["status"] == "complete"
        assert len(record["rollouts"]) == 3
        for rollout in record["rollouts"]:
            assert rollout["original_explanation"] and rollout["edited_explanation"]
        arms = record["generation_arms"]
        expected = {"unsteered", "av_edit", "random", *(f"random_{s}" for s in SEEDS)}
        assert expected <= set(arms)
        assert all(len(arms[name]) == 2 for name in expected)
        assert record["alpha"] == alpha_map[str(record["concept_id"])]
        vector = torch.load(record["vector_path"], map_location="cpu", weights_only=True)
        assert vector.ndim == 1 and torch.isfinite(vector).all() and vector.norm() > 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("kind", choices=("baseline", "nla"))
    p.add_argument("run_dir")
    p.add_argument("--calibration-run")
    args = p.parse_args()
    run = Path(args.run_dir)
    if args.kind == "baseline":
        verify_baseline(run)
    else:
        assert args.calibration_run
        verify_nla(run, Path(args.calibration_run))
    print(f"VERIFIED {args.kind} {run}")


if __name__ == "__main__":
    main()
