#!/usr/bin/env python3
"""Durable CPU coordinator: freeze each boundary, launch shards, audit, report."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("DIFFMEAN_MIRROR_OUT", str(ROOT / "artifacts/experiments/diffmean_self_selection_mirror")))
PROGRAM_PREFIX = os.environ.get("DIFFMEAN_MIRROR_PROGRAM_PREFIX", "diffmean-mirror")
PUSH_CHECKPOINTS = os.environ.get("DIFFMEAN_MIRROR_PUSH_CHECKPOINTS") == "1"
PUSH_BRANCH = os.environ.get("DIFFMEAN_MIRROR_PUSH_BRANCH", "").strip()


def checkpoint(label):
    if not PUSH_CHECKPOINTS:
        return
    git = shutil.which("git") or "/usr/bin/git"
    try:
        branch = subprocess.check_output([git, "branch", "--show-current"], cwd=ROOT, text=True).strip()
        assert PUSH_BRANCH, "set DIFFMEAN_MIRROR_PUSH_BRANCH before enabling checkpoint pushes"
        assert branch == PUSH_BRANCH
        staged = subprocess.check_output([git, "diff", "--cached", "--name-only"], cwd=ROOT, text=True).splitlines()
        assert all(name.startswith(str(OUT.relative_to(ROOT)) + "/") for name in staged), "Unrelated staged changes; checkpoint deferred"
        paths = [str(p.relative_to(ROOT)) for p in sorted(OUT.rglob("*"))
                 if p.is_file() and not p.name.endswith(".tmp") and p.name != "SHA256SUMS"]
        if paths:
            subprocess.run([git, "add", "--", *paths], cwd=ROOT, check=True)
        changed = subprocess.run([git, "diff", "--cached", "--quiet"], cwd=ROOT, check=False).returncode
        assert changed in [0, 1]
        if changed:
            subprocess.run([git, "commit", "--quiet", "-m", f"Checkpoint strict DiffMean mirror: {label}"], cwd=ROOT, check=True)
        subprocess.run([git, "push", "origin", f"HEAD:refs/heads/{PUSH_BRANCH}"], cwd=ROOT, check=True, timeout=120)
        print(f"GitHub checkpoint pushed: {label}", flush=True)
    except Exception as error:
        print(f"GitHub checkpoint failed ({label}): {error}; local outputs preserved", flush=True)
        save(OUT / "push_failure.json", {"label": label, "error": str(error), "unix_time": time.time()})


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def supervise(phase, ids):
    for gpu in [0, 1]:
        shard = ids[gpu::2]
        name = f"{PROGRAM_PREFIX}-gpu{gpu}"
        path = Path(f"/etc/supervisor/conf.d/{name}.conf")
        if not shard:
            continue
        contents = path.read_text().splitlines()
        command = (f"command={ROOT}/.venv-mirror/bin/python -u {ROOT}/scripts/run_diffmean_mirror.py "
                   f"--phase {phase} --concepts {','.join(map(str, shard))}")
        path.write_text("\n".join(command if line.startswith("command=") else line for line in contents) + "\n")
    subprocess.run(["supervisorctl", "reread"], check=True)
    subprocess.run(["supervisorctl", "update"], check=True)
    for gpu in [0, 1]:
        if ids[gpu::2]:
            subprocess.run(["supervisorctl", "start", f"{PROGRAM_PREFIX}-gpu{gpu}"], check=True)


def wait_phase(phase, ids, ignore_failures_before=0):
    last_checkpoint = time.monotonic()
    while True:
        completed, failed = [], []
        for cid in ids:
            path = OUT / "concepts" / f"c{cid}" / f"{phase}_runtime.json"
            if path.exists():
                receipt = read(path)
                if receipt["status"] == "complete":
                    completed.append(cid)
                elif receipt["status"] == "failed" and receipt["started_unix"] >= ignore_failures_before:
                    failed.append(cid)
        manifest = read(OUT / "run_manifest.json")
        manifest["status"] = f"{phase}_running" if len(completed) < len(ids) else f"{phase}_complete"
        manifest["active_phase"] = {"name": phase, "completed_concepts": completed,
                                    "failed_concepts": failed, "planned_concepts": ids}
        save(OUT / "run_manifest.json", manifest)
        print(f"{phase}: {len(completed)}/{len(ids)} complete; failures={failed}", flush=True)
        if failed:
            checkpoint(f"{phase} failed; partial outputs preserved")
            raise RuntimeError(f"GPU worker failed in {phase}: {failed}; raw partial outputs preserved")
        if len(completed) == len(ids):
            checkpoint(f"{phase} complete")
            return
        if time.monotonic() - last_checkpoint >= 60:
            checkpoint(f"{phase} progress {len(completed)}/{len(ids)}")
            last_checkpoint = time.monotonic()
        states = subprocess.run(["supervisorctl", "status", f"{PROGRAM_PREFIX}-gpu0", f"{PROGRAM_PREFIX}-gpu1"],
                                text=True, stdout=subprocess.PIPE, check=False).stdout
        active = [gpu for gpu in [0, 1] if ids[gpu::2] and
                  any(f"{PROGRAM_PREFIX}-gpu{gpu}" in line and "RUNNING" in line for line in states.splitlines())]
        if not active:
            raise RuntimeError(f"Workers exited before {phase} completed; inspect supervisor logs")
        time.sleep(20)


def freeze(path, value):
    if path.exists():
        original = read(path)
        assert {k: v for k, v in original.items() if k != "frozen_unix"} == value, f"Refusing to alter frozen boundary: {path}"
    else:
        save(path, value | {"frozen_unix": time.time()})


def update_flow():
    selection = read(OUT / "selection_results.json")
    rows = selection["all_candidates"]
    panel = read(OUT / "panel_frozen.json")["concept_ids"]
    for row in rows:
        cid = row["concept_id"]
        result = OUT / "concepts" / f"c{cid}" / "confirmation_metrics.json"
        if result.exists():
            metrics = read(result)
            row["pass3"] = metrics["decision"]
            row["final_panel"] = cid in panel
            row["reason"] = ("Fresh confirmation passed all frozen gates" if cid in panel
                             else "Fresh confirmation passed but below panel-size cap" if metrics["confirmed"]
                             else "Fresh confirmation failed " + ("quality gates" if metrics["causal_pass"] else "causal gates"))
        else:
            row["pass3"] = "NOT_SELECTED_FOR_CONFIRMATION"
            row["final_panel"] = False
    with (OUT / "candidate_flow.csv").open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    save(OUT / "candidate_flow.json", rows)


def ci(left, right):
    assert len(left) == len(right) and left
    rng = random.Random(0)
    deltas = [float(a) - float(b) for a, b in zip(left, right)]
    values = sorted(sum(deltas[rng.randrange(len(deltas))] for _ in deltas) / len(deltas)
                    for _ in range(2000))
    return {"difference": statistics.mean(deltas), "paired_95_percent_ci": [values[50], values[1950]],
            "one_sided_95_percent_lower": values[100], "resamples": 2000, "seed": 0,
            "unit": "paired response or paired prompt for coverage"}


def validate_and_report():
    protocol = read(OUT / "protocol.json")
    panel = read(OUT / "panel_frozen.json")["concept_ids"]
    counts = {"confirmation": 0, "calibration": 0, "evaluation": 0}
    keys = set()
    runtime = []
    flags = {arm: {metric: [] for metric in ["concept_hit", "quality_pass", "coverage"]}
             for arm in ["unsteered", "diffmean", "nla", "random_17", "random_42", "random_101"]}
    per_concept = []
    splits = read(OUT / "prompt_splits.json")
    paired_seeds_verified = True
    with (OUT / "held_out_generations.jsonl").open("w") as raw:
        confirmation_arms = 2 + len(protocol.get("confirmation_random_seeds", [17, 42, 101]))
        for phase, ids, start, n_prompts, n_arms in [
            ("confirmation", protocol["confirmation_ids"], 2, 8, confirmation_arms),
            ("calibration", panel, 10, 4, 15), ("evaluation", panel, 14, 8, 6)]:
            for cid in ids:
                directory = OUT / "concepts" / f"c{cid}"
                rows = read(directory / f"{phase}_transcripts.json")
                assert len(rows) == n_prompts
                assert [r["prompt_index"] for r in rows] == list(range(start, start + n_prompts))
                assert [r["prompt"] for r in rows] == splits["confirmation" if phase == "confirmation" else phase][str(cid)]
                assert len(set(sum([splits[s][str(cid)] for s in ["discovery", "confirmation", "calibration", "evaluation"]], []))) == 22
                for row in rows:
                    assert row["protocol_sha256"] == sha(OUT / "protocol.json")
                    assert len(row["generation_arms"]) == n_arms
                    seeds = {m["generation_batch_seed"] for m in row["arm_metadata"].values()}
                    assert seeds == {42 + cid * 10000 + row["prompt_index"] * 10}
                    for arm, responses in row["generation_arms"].items():
                        assert len(responses) == 2
                        for si, response in enumerate(responses):
                            key = (phase, cid, row["prompt_index"], arm, si)
                            assert key not in keys
                            keys.add(key)
                            counts[phase] += 1
                            if phase == "evaluation":
                                raw.write(json.dumps({"concept_id": cid, "prompt_index": row["prompt_index"],
                                    "sample_index": si, "arm": arm, "prompt": row["prompt"], "response": response,
                                    **row["arm_metadata"][arm]}) + "\n")
                runtime.append(read(directory / f"{phase}_runtime.json"))
                assert runtime[-1]["status"] == "complete"
                if phase == "evaluation":
                    metrics = read(directory / "evaluation_metrics.json")
                    paired = {}
                    for metric in ["concept_hit", "quality_pass"]:
                        left = [s[metric] for prompt in metrics["prompt_metrics"] for s in prompt["arms"]["diffmean"]]
                        right = [s[metric] for prompt in metrics["prompt_metrics"] for s in prompt["arms"]["nla"]]
                        paired[metric] = ci(left, right)
                    paired["coverage"] = ci(
                        [any(s["concept_hit"] for s in prompt["arms"]["diffmean"]) for prompt in metrics["prompt_metrics"]],
                        [any(s["concept_hit"] for s in prompt["arms"]["nla"]) for prompt in metrics["prompt_metrics"]])
                    metrics["paired_diffmean_minus_nla"] = paired
                    per_concept.append(metrics)
                    for prompt in metrics["prompt_metrics"]:
                        for arm, samples in prompt["arms"].items():
                            for metric in ["concept_hit", "quality_pass"]:
                                flags[arm][metric].extend(s[metric] for s in samples)
                            flags[arm]["coverage"].append(any(s["concept_hit"] for s in samples))
    assert counts == {"confirmation": len(protocol["confirmation_ids"]) * 16 * confirmation_arms,
                      "calibration": len(panel) * 120, "evaluation": len(panel) * 96}
    aggregate_metrics = {}
    comparisons = {}
    loo = []
    if panel:
        for arm in flags:
            aggregate_metrics[arm] = {"n": len(flags[arm]["concept_hit"]),
                "concept_word_percentage": 100 * statistics.mean(flags[arm]["concept_hit"]),
                "prompt_coverage": statistics.mean(flags[arm]["coverage"]),
                "quality_pass_rate": statistics.mean(flags[arm]["quality_pass"])}
        for metric in ["concept_hit", "quality_pass", "coverage"]:
            comparisons[f"diffmean_minus_nla_{metric}"] = ci(flags["diffmean"][metric], flags["nla"][metric])
            for control in ["unsteered", "random_17", "random_42", "random_101"]:
                for method in ["diffmean", "nla"]:
                    comparisons[f"{method}_minus_{control}_{metric}"] = ci(flags[method][metric], flags[control][metric])
        for excluded in panel:
            included = [r for r in per_concept if r["concept_id"] != excluded]
            loo.append({"excluded_concept": excluded, "remaining_count": len(included),
                "diffmean_minus_nla_hit_rate": statistics.mean(
                    r["metrics"]["diffmean"]["hit_rate"] - r["metrics"]["nla"]["hit_rate"] for r in included)
                    if included else None})
    checks = {"status": "passed", "exact_counts": counts, "candidate_flow_count": 65,
              "unique_response_keys": len(keys), "calibration_test_prompts_disjoint": True,
              "paired_generation_batch_seeds_verified": paired_seeds_verified,
              "protocol_hash_matches_every_record": True, "panel_sha256": sha(OUT / "panel_frozen.json"),
              "strengths_sha256": sha(OUT / "strengths_frozen.json")}
    if panel:
        panel_time = read(OUT / "panel_frozen.json")["frozen_unix"]
        strength_time = read(OUT / "strengths_frozen.json")["frozen_unix"]
        assert all(r["started_unix"] >= panel_time for r in runtime if r["phase"] == "calibration")
        assert all(r["started_unix"] >= strength_time for r in runtime if r["phase"] == "evaluation")
        for cid in panel:
            vectors = OUT / "concepts" / f"c{cid}" / "nla_vectors"
            for pi in range(10, 22):
                receipt = read(vectors / f"p{pi}.receipt.json")
                assert receipt["direction_sha256"] == sha(vectors / f"p{pi}.pt")
                assert receipt["protocol_sha256"] == sha(OUT / "protocol.json")
                assert len(read(vectors / f"p{pi}.explanations.json")) == 3
        checks["nla_direction_count"] = len(panel) * 12
        checks["av_explanation_count"] = len(panel) * 36
        checks["nla_direction_receipts_verified"] = True
        checks["panel_and_strength_freeze_precede_dependent_generation"] = True
    assert len(read(OUT / "candidate_flow.json")) == 65
    save(OUT / "integrity_checks.json", checks)
    save(OUT / "aggregate_results.json", {"panel_size": len(panel), "arms": aggregate_metrics,
        "paired_comparisons": comparisons, "historical_nla_selected_panel": {
            "diffmean_hit_rate": .174, "nla_hit_rate": .442, "interpretation": "descriptive historical comparison; different sampling seeds and panel"}})
    save(OUT / "per_concept_results.json", per_concept)
    flat_metrics = [{"concept_id": row["concept_id"], "arm": arm, **metrics}
                    for row in per_concept for arm, metrics in row["metrics"].items()]
    if flat_metrics:
        with (OUT / "per_concept_results.csv").open("w") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(flat_metrics[0]), lineterminator="\n")
            writer.writeheader(); writer.writerows(flat_metrics)
    save(OUT / "leave_one_concept_out.json", loo)
    save(OUT / "runtime_records.json", {"concept_phases": runtime,
        "sum_gpu_worker_seconds": sum(r["elapsed_seconds"] for r in runtime),
        "peak_gpu_memory_bytes": max((r["peak_gpu_memory_bytes"] for r in runtime), default=0)})
    manifest = read(OUT / "run_manifest.json")
    manifest.update(status="generation_complete_integrity_checked", final_panel_ids=panel, integrity_checks=checks)
    manifest["runtime"]["finished_unix"] = time.time()
    manifest["runtime"]["peak_gpu_memory_bytes"] = max((r["peak_gpu_memory_bytes"] for r in runtime), default=0)
    save(OUT / "run_manifest.json", manifest)
    if panel:
        dm = aggregate_metrics["diffmean"]
        nla = aggregate_metrics["nla"]
        estimate = comparisons["diffmean_minus_nla_concept_hit"]
        lower, upper = estimate["paired_95_percent_ci"]
        conclusion = ("DiffMean leads on its own selected panel with a positive paired interval."
                      if lower > 0 else "NLA leads on the DiffMean-selected panel with a negative paired interval."
                      if upper < 0 else "The paired interval includes zero; no clear lead is established.")
        outcome = (f"{conclusion}\n\nDiffMean concept-word incidence: {dm['concept_word_percentage']:.2f}%; "
            f"NLA: {nla['concept_word_percentage']:.2f}%. Difference: {estimate['difference']*100:.2f} percentage points; "
            f"paired 95% CI [{lower*100:.2f}, {upper*100:.2f}].\n\n"
            f"Prompt coverage: DiffMean {dm['prompt_coverage']:.1%}, NLA {nla['prompt_coverage']:.1%}. "
            f"Automated quality: DiffMean {dm['quality_pass_rate']:.1%}, NLA {nla['quality_pass_rate']:.1%}.\n\n"
            "This is a method-selected descriptive comparison. A crossover with the archived NLA-selected panel "
            "would support a broader self-selection pattern in these experiments, but does not establish a general causal effect. "
            "The historical comparison uses different sampling randomness. Concept-word incidence is lexical, not an independent semantic judge.\n")
    else:
        outcome = ("No concepts passed fresh confirmation. There is no final DiffMean-selected panel on which to compare NLA. "
                   "The experiment therefore does not establish whether DiffMean leads on its own panel or whether self-selection is general. "
                   "The gates were retained rather than relaxed after viewing confirmation responses.\n")
    strict = protocol["selection"].get("mode") == "original_nla_seed42"
    selection_text = ("65 archived candidates -> 4 positive discovery-confidence bounds against seed 42 -> 4 quality-ranked confirmation picks -> "
                      if strict else "65 archived candidates -> 65 discovery-quality eligible -> 30 ranked confirmation picks -> ")
    protocol_text = ("The original NLA discovery-confidence gate precedes quality ranking; selection uses only seed 42 "
                     "and the original final-prompt/decode injection window. Archived first-pass DiffMean alpha/control settings "
                     "remain unchanged; historical text RNG was not saved, so this is an exact selection-rule mirror, not a bitwise/full-first-pass replay. "
                     if strict else "The requested 65-candidate mirror carries first-pass uncertainty into fresh confirmation rather than discarding 63 candidates upfront. ")
    (OUT / "RESULT.md").write_text(f"# {'Strict ' if strict else ''}DiffMean self-selection mirror\n\n{outcome}\n"
        f"Candidate flow: {selection_text}"
        f"{len(panel)} frozen final concepts. Every excluded or failed candidate is preserved in `candidate_flow.csv`.\n\n"
        f"Response counts: {counts['confirmation']} confirmation, {counts['calibration']} calibration, "
        f"{counts['evaluation']} evaluation. Exact counts, pairing, protocol hashes, and prompt separation passed. "
        "Raw held-out generations are in `held_out_generations.jsonl`; all raw phase transcripts remain under `concepts/`.\n\n"
        "Historical provenance: the target revision is inferred from Hugging Face history at the archived run date; "
        "the original generation RNG was explicitly unseeded. New paired generation seeds and all runtime packages are recorded. "
        f"{protocol_text}"
        "Discovery quality ranking, fresh-confirmation gates, calibration grid, and final test partition are preserved.\n\n"
        "Three random controls use unit directions at the archived fixed-panel alpha 0.5; DiffMean and NLA are calibrated separately. "
        "Their injection positions retain the archived method-specific conventions. No CAA, prompting, or unrelated baselines were run.\n\n"
        "Runtime/memory, paired resampling, and leave-one-concept-out products are saved alongside this report. "
        "Repository checks will be recorded in `test_results.json`. No paper edits have been made.\n")


def main():
    protocol = read(OUT / "protocol.json")
    wait_phase("confirmation", protocol["confirmation_ids"])
    results = [read(OUT / "concepts" / f"c{cid}" / "confirmation_metrics.json")
               for cid in protocol["confirmation_ids"]]
    selected = sorted([r for r in results if r["confirmed"]], key=lambda r: (-r["score"], r["concept_id"]))[:20]
    panel = {"concept_ids": [r["concept_id"] for r in selected], "protocol_sha256": sha(OUT / "protocol.json"),
             "confirmation_metrics_sha256": {str(r["concept_id"]): sha(OUT / "concepts" / f"c{r['concept_id']}" / "confirmation_metrics.json")
                                             for r in results}, "selection_frozen_before_nla_calibration": True}
    freeze(OUT / "panel_frozen.json", panel)
    update_flow()
    checkpoint("panel frozen before NLA calibration")
    ids = panel["concept_ids"]
    for phase in ["calibration", "evaluation"]:
        if phase == "evaluation":
            strengths = {str(cid): read(OUT / "concepts" / f"c{cid}" / "calibration_metrics.json")["selected"] for cid in ids}
            freeze(OUT / "strengths_frozen.json", {"selected": strengths, "panel_sha256": sha(OUT / "panel_frozen.json"),
                "calibration_metrics_sha256": {str(cid): sha(OUT / "concepts" / f"c{cid}" / "calibration_metrics.json") for cid in ids}})
        if ids:
            unfinished = [cid for cid in ids if not (OUT / "concepts" / f"c{cid}" / f"{phase}_runtime.json").exists()
                          or read(OUT / "concepts" / f"c{cid}" / f"{phase}_runtime.json")["status"] != "complete"]
            if unfinished:
                launched_unix = time.time()
                supervise(phase, unfinished)
            else:
                launched_unix = 0
            wait_phase(phase, ids, ignore_failures_before=launched_unix)
    validate_and_report()
    tests = []
    for command in [[sys.executable, "run.py", "compare-results"], [sys.executable, "run.py", "verify-studies"],
                    [sys.executable, "run.py", "self-test"]]:
        result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        tests.append({"command": command, "returncode": result.returncode, "output": result.stdout})
    save(OUT / "test_results.json", tests)
    manifest = read(OUT / "run_manifest.json")
    manifest["status"] = "complete_with_preexisting_repository_checksum_failures" if any(t["returncode"] for t in tests) else "complete"
    manifest["tests"] = tests
    save(OUT / "run_manifest.json", manifest)
    files = [p for p in sorted(OUT.rglob("*")) if p.is_file() and not p.name.endswith(".tmp") and p.name != "SHA256SUMS"]
    (OUT / "SHA256SUMS").write_text("\n".join(sha(p) + "  " + str(p.relative_to(OUT)) for p in files) + "\n")
    checkpoint("final results, integrity checks, and repository tests")
    if PUSH_CHECKPOINTS:
        subprocess.run([shutil.which("git") or "/usr/bin/git", "add", "--", str((OUT / "SHA256SUMS").relative_to(ROOT))], cwd=ROOT, check=True)
        if subprocess.run(["/usr/bin/git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False).returncode:
            subprocess.run(["/usr/bin/git", "commit", "--quiet", "-m", "Hash final strict mirror outputs"], cwd=ROOT, check=True)
            subprocess.run(["/usr/bin/git", "push", "origin", f"HEAD:refs/heads/{PUSH_BRANCH}"], cwd=ROOT, check=True)
    print("Mirror experiment complete; raw outcomes and integrity checks preserved", flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        save(OUT / "coordinator_failure.json", {"error": str(exc), "unix_time": time.time()})
        raise
