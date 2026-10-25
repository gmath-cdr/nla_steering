#!/usr/bin/env python3
"""Concept-sharded orchestration of the archived steering and scoring functions.

No model, direction construction, lexical scorer, or quality scorer is reimplemented.
Every completed prompt/arm/strength batch is saved atomically for resumption.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("DIFFMEAN_MIRROR_OUT", str(ROOT / "artifacts/experiments/diffmean_self_selection_mirror")))
sys.path[:0] = [str(ROOT / "artifacts/cache/mirror_runtime"), str(ROOT / "src/nla"),
                str(ROOT / "src/contrast_steering/src")]

import torch
from experiments import steering, vectors
from experiments.confirmation_metrics import aggregate, evaluate_prompt
from experiments.discovery500 import append_edit, bootstrap_lower, capture_batch, save_json
from experiments.run_panel_baselines import generate_many
from nla_inference import NLACritic, NLAStandalone

GRID = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.75]
RANDOM_SEEDS = [17, 42, 101]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_tensor(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    torch.save(value, temporary)
    temporary.replace(path)


def seed(cid, prompt_index, domain=0):
    value = 42 + cid * 10000 + prompt_index * 10 + domain
    torch.manual_seed(value)
    torch.cuda.manual_seed_all(value)
    return value


def unload():
    gc.collect()
    torch.cuda.empty_cache()


def load(cid):
    models = json.loads((OUT / "model_paths.json").read_text())
    protocol = json.loads((OUT / "protocol.json").read_text())
    candidates = json.loads((OUT / "original_candidates.json").read_text())["concepts"]
    candidate = next(c for c in candidates if c["concept_id"] == cid)
    directory = OUT / "concepts" / f"c{cid}"
    directory.mkdir(parents=True, exist_ok=True)
    return models, protocol, candidate, directory


def verified_cache_root():
    protocol = json.loads((OUT / "protocol.json").read_text())
    source = protocol.get("reuse_verified_cache_from")
    if not source:
        return None
    source = ROOT / source
    old = json.loads((source / "protocol.json").read_text())
    for key in ["models", "generation_root_seed", "generation_seed_formula", "av_seed_formula",
                "max_new_tokens", "temperature", "av_rollouts", "av_temperature", "av_max_new_tokens",
                "layer", "runtime_inference_sha256"]:
        assert old[key] == protocol[key], f"Cache setting mismatch: {key}"
    assert sha(source / "provenance/mirror-runtime-pip-freeze.txt") == sha(OUT / "provenance/mirror-runtime-pip-freeze.txt")
    return source


def check_cached_file(source, path):
    expected = dict(line.split("  ", 1)[::-1] for line in (source / "SHA256SUMS").read_text().splitlines())
    assert sha(path) == expected[str(path.relative_to(source))], f"Cache hash mismatch: {path}"


def cached_generation(cid, phase, pi, prompt, arm, direction, alpha, implementation):
    source = verified_cache_root()
    if source is None:
        return None
    path = source / "concepts" / f"c{cid}" / f"{phase}_transcripts.json"
    if not path.exists():
        return None
    check_cached_file(source, path)
    record = next((row for row in json.loads(path.read_text()) if row["prompt_index"] == pi), None)
    if record is None or record["prompt"] != prompt or arm not in record["generation_arms"]:
        return None
    metadata = record["arm_metadata"][arm]
    if metadata["alpha"] != alpha or metadata["implementation"] != implementation:
        return None
    assert metadata["generation_batch_seed"] == 42 + cid * 10000 + pi * 10
    if direction is not None:
        if implementation == "nla":
            vector_path = source / "concepts" / f"c{cid}" / "nla_vectors" / f"p{pi}.pt"
        elif arm.startswith("diffmean"):
            vector_path = source / "directions" / f"c{cid}.diffmean.pt"
        else:
            # The confirmation controls use a different injection window in the strict run.
            return None
        if not vector_path.exists():
            return None
        check_cached_file(source, vector_path)
        if not torch.equal(vectors.load(str(vector_path)), direction):
            return None
    responses = record["generation_arms"][arm]
    assert len(responses) == 2
    return responses, {**metadata, "elapsed_seconds": 0.0,
        "reused_from": {"path": str(path.relative_to(ROOT)), "sha256": sha(path),
                        "original_elapsed_seconds": metadata["elapsed_seconds"]}}


def generate_phase(cid, phase, model, tokenizer, directory, prompts, terms, arms):
    path = directory / f"{phase}_transcripts.json"
    rows = json.loads(path.read_text()) if path.exists() else []
    records = {r["prompt_index"]: r for r in rows}
    signature = sha(OUT / "protocol.json")
    for pi, prompt in prompts:
        record = records.setdefault(pi, {"concept_id": cid, "prompt_index": pi,
                     "prompt": prompt, "protocol_sha256": signature, "generation_arms": {},
                     "arm_metadata": {}})
        assert record["prompt"] == prompt and record["protocol_sha256"] == signature
        for arm, (direction, alpha, implementation) in arms(pi).items():
            if arm in record["generation_arms"]:
                assert len(record["generation_arms"][arm]) == 2
                continue
            cached = cached_generation(cid, phase, pi, prompt, arm, direction, alpha, implementation)
            if cached is not None:
                record["generation_arms"][arm], record["arm_metadata"][arm] = cached
                save_json(path, sorted(records.values(), key=lambda r: r["prompt_index"]))
                print(f"c{cid} {phase} prompt={pi} arm={arm}: verified cache reused", flush=True)
                continue
            rng_seed = seed(cid, pi)
            started = time.monotonic()
            if implementation == "nla":
                responses = steering.generate(model, tokenizer, [{"role": "user", "content": prompt}],
                    20, direction, -1, alpha, n_samples=2, max_new_tokens=96, temperature=0.7)
            else:
                responses = generate_many(model, tokenizer, [prompt, prompt],
                    steering.decoder_layers(model)[20], direction, alpha, batch_size=2, do_sample=True)
            assert len(responses) == 2
            record["generation_arms"][arm] = responses
            record["arm_metadata"][arm] = {"alpha": alpha, "generation_batch_seed": rng_seed,
                "sample_indices": [0, 1], "implementation": implementation,
                "elapsed_seconds": time.monotonic() - started,
                "peak_gpu_memory_bytes": torch.cuda.max_memory_allocated()}
            save_json(path, sorted(records.values(), key=lambda r: r["prompt_index"]))
            print(f"c{cid} {phase} prompt={pi} arm={arm} alpha={alpha} saved", flush=True)
    return sorted(records.values(), key=lambda r: r["prompt_index"])


def score(rows, terms):
    scored = [evaluate_prompt(row, terms) for row in rows]
    arms = list(rows[0]["generation_arms"])
    metrics = {}
    for arm in arms:
        samples = [s for row in scored for s in row["arms"][arm]]
        metrics[arm] = aggregate(samples) | {
            "prompt_coverage": sum(any(s["concept_hit"] for s in row["arms"][arm])
                                   for row in scored) / len(scored)}
    return scored, metrics


def confirmation(cid, models, candidate, directory, split, model_tokenizer=None):
    model, tokenizer = model_tokenizer or steering.load_target(models["target"]["path"], "cuda:0")
    direction = vectors.load(str(OUT / "directions" / f"c{cid}.diffmean.pt"))
    assert direction.numel() == 3584 and abs(float(direction.norm()) - 1) < 1e-5
    protocol = json.loads((OUT / "protocol.json").read_text())
    control_seeds = protocol.get("confirmation_random_seeds", RANDOM_SEEDS)
    implementation = protocol.get("confirmation_implementation", "diffmean")
    def arms(pi):
        return {"unsteered": (None, 0., "diffmean"), "diffmean": (direction, .5, implementation),
                **{f"random_{s}": (vectors.random_like(direction, s * 1000000 + cid * 100 + pi - 2),
                                   .5, implementation) for s in control_seeds}}
    rows = generate_phase(cid, "confirmation", model, tokenizer, directory,
                          list(enumerate(split, 2)), candidate["terms"], arms)
    scored, metrics = score(rows, candidate["terms"])
    strongest = max(control_seeds, key=lambda s: (metrics[f"random_{s}"]["hit_rate"], -s))
    flags = lambda arm: [s["concept_hit"] for row in scored for s in row["arms"][arm]]
    target = metrics["diffmean"]
    random = metrics[f"random_{strongest}"]
    lb_base = bootstrap_lower(flags("diffmean"), flags("unsteered"))
    lb_random = bootstrap_lower(flags("diffmean"), flags(f"random_{strongest}"))
    causal = (target["hit_rate"] - metrics["unsteered"]["hit_rate"] >= .2 and
              target["hit_rate"] - random["hit_rate"] >= .2 and target["prompt_coverage"] >= .5
              and lb_base > 0 and lb_random > 0)
    quality = (target["quality_pass_rate"] >= .75 and target["mean_reference_similarity"] >= .12
               and target["mean_repeated_trigram_rate"] <= .1 and target["mean_target_term_count"] <= 4)
    result = {"concept_id": cid, "confirmed": causal and quality, "causal_pass": causal,
              "quality_pass": quality, "metrics": metrics, "strongest_random_seed": strongest,
              "bootstrap_lower_vs_unsteered": lb_base, "bootstrap_lower_vs_random": lb_random,
              "decision": "CONFIRMED" if causal and quality else "QUALITY_FAIL" if causal else "CAUSAL_FAIL",
              "score": min(target["hit_rate"] - metrics["unsteered"]["hit_rate"],
                           target["hit_rate"] - random["hit_rate"]) * target["prompt_coverage"] * target["quality_pass_rate"]}
    save_json(directory / "confirmation_metrics.json", result)
    print(json.dumps(result), flush=True)


def prepare_nla(cid, models, directory, prompts):
    vdir = directory / "nla_vectors"
    vdir.mkdir(exist_ok=True)
    cache_protocol = vdir / "protocol_sha256.json"
    signature = sha(OUT / "protocol.json")
    if cache_protocol.exists():
        assert json.loads(cache_protocol.read_text()) == signature
    else:
        save_json(cache_protocol, signature)
    source = verified_cache_root()
    if source is not None:
        old_dir = source / "concepts" / f"c{cid}" / "nla_vectors"
        for pi, prompt in prompts:
            if (vdir / f"p{pi}.receipt.json").exists() or not (old_dir / f"p{pi}.receipt.json").exists():
                continue
            old_receipt = json.loads((old_dir / f"p{pi}.receipt.json").read_text())
            assert old_receipt["prompt"] == prompt and old_receipt["protocol_sha256"] == sha(source / "protocol.json")
            assert old_receipt["direction_sha256"] == sha(old_dir / f"p{pi}.pt")
            for path in old_dir.glob(f"p{pi}.*"):
                if path.suffix == ".tmp" or path.name == f"p{pi}.receipt.json":
                    continue
                check_cached_file(source, path)
                shutil.copyfile(path, vdir / path.name)
            activation_receipt = json.loads((vdir / f"p{pi}.activation.receipt.json").read_text())
            activation_receipt["protocol_sha256"] = signature
            activation_receipt["reused_from"] = str(old_dir.relative_to(ROOT))
            save_json(vdir / f"p{pi}.activation.receipt.json", activation_receipt)
            save_json(vdir / f"p{pi}.receipt.json", {**old_receipt, "protocol_sha256": signature,
                      "reused_from": str(old_dir.relative_to(ROOT))})
    for pi, prompt in prompts:
        path = vdir / f"p{pi}.pt"
        if path.exists() and (vdir / f"p{pi}.receipt.json").exists():
            receipt = json.loads((vdir / f"p{pi}.receipt.json").read_text())
            assert receipt["direction_sha256"] == sha(path)
            assert receipt["protocol_sha256"] == signature and receipt["prompt"] == prompt
    target, tokenizer = steering.load_target(models["target"]["path"], "cuda:0")
    pending = [(pi, prompt) for pi, prompt in prompts
               if not ((vdir / f"p{pi}.pt").exists() and (vdir / f"p{pi}.receipt.json").exists())]
    for pi, prompt in pending:
        path = vdir / f"p{pi}.activation.pt"
        receipt_path = vdir / f"p{pi}.activation.receipt.json"
        if not path.exists() or not receipt_path.exists():
            activation = capture_batch(target, tokenizer, [prompt], 20)[0]
            save_tensor(path, activation)
            save_json(receipt_path, {"sha256": sha(path), "protocol_sha256": signature, "prompt": prompt})
        else:
            receipt = json.loads(receipt_path.read_text())
            assert all(receipt[key] == value for key, value in
                       {"sha256": sha(path), "protocol_sha256": signature, "prompt": prompt}.items())
    del target, tokenizer
    unload()
    candidate = next(c for c in json.loads((OUT / "original_candidates.json").read_text())["concepts"]
                     if c["concept_id"] == cid)
    if pending:
        actor = NLAStandalone(models["av"]["path"], "cuda:0")
        for pi, prompt in pending:
            path = vdir / f"p{pi}.explanations.json"
            explanations = json.loads(path.read_text()) if path.exists() else []
            for rollout in range(len(explanations), 3):
                rng = seed(cid, pi, rollout + 1)
                raw = actor.generate(torch.load(vdir / f"p{pi}.activation.pt", weights_only=True),
                                     extract_explanation=False, temperature=1.0, max_new_tokens=200)
                original = raw if "<explanation>" in raw else f"<explanation>{raw}</explanation>"
                explanations.append({"original_explanation": original,
                    "edited_explanation": append_edit(original, candidate["concept"]), "seed": rng})
                save_json(path, explanations)
                print(f"c{cid} AV prompt={pi} rollout={rollout} saved", flush=True)
        del actor
        unload()
        critic = NLACritic(models["ar"]["path"], device="cuda:0")
        for pi, prompt in pending:
            path = vdir / f"p{pi}.explanations.json"
            explanations = json.loads(path.read_text())
            deltas = []
            for ri, row in enumerate(explanations):
                pair_path = vdir / f"p{pi}.rollout{ri}.reconstructions.pt"
                if pair_path.exists():
                    pair = torch.load(pair_path, weights_only=True)
                else:
                    pair = {"edited": critic.reconstruct(row["edited_explanation"]),
                            "original": critic.reconstruct(row["original_explanation"])}
                    save_tensor(pair_path, pair)
                delta = pair["edited"] - pair["original"]
                row["raw_delta_norm"] = float(delta.norm())
                deltas.append(steering.normalize(delta))
            save_tensor(vdir / f"p{pi}.pt", steering.normalize(sum(deltas, torch.zeros_like(deltas[0]))))
            save_json(path, explanations)
            save_json(vdir / f"p{pi}.receipt.json", {"prompt": prompt, "prompt_index": pi,
                "protocol_sha256": sha(OUT / "protocol.json"), "direction_sha256": sha(vdir / f"p{pi}.pt")})
        del critic
        unload()


def calibration(cid, models, candidate, directory, split):
    prompts = list(enumerate(split, 10))
    prepare_nla(cid, models, directory, prompts)
    model, tokenizer = steering.load_target(models["target"]["path"], "cuda:0")
    direction = vectors.load(str(OUT / "directions" / f"c{cid}.diffmean.pt"))
    def arms(pi):
        nla = vectors.load(str(directory / "nla_vectors" / f"p{pi}.pt"))
        return {"unsteered": (None, 0., "diffmean"),
                **{f"{method}@{alpha}": (direction if method == "diffmean" else nla, alpha, method)
                   for method in ["diffmean", "nla"] for alpha in GRID}}
    rows = generate_phase(cid, "calibration", model, tokenizer, directory, prompts, candidate["terms"], arms)
    _, metrics = score(rows, candidate["terms"])
    selected = {}
    for method in ["diffmean", "nla"]:
        eligible = [(m["hit_rate"], -alpha, alpha) for alpha in GRID
                    if (m := metrics[f"{method}@{alpha}"])["quality_pass_rate"] >= .75
                    and m["mean_reference_similarity"] >= .12 and m["mean_repeated_trigram_rate"] <= .1
                    and m["mean_target_term_count"] <= 4.]
        selected[method] = {"alpha": max(eligible)[2] if eligible else .5,
                           "status": "quality_eligible" if eligible else "fixed_fallback_no_quality_eligible_alpha"}
    save_json(directory / "calibration_metrics.json", {"concept_id": cid, "metrics": metrics, "selected": selected})


def evaluation(cid, models, candidate, directory, split):
    assert (OUT / "strengths_frozen.json").exists()
    selected = json.loads((directory / "calibration_metrics.json").read_text())["selected"]
    frozen = json.loads((OUT / "strengths_frozen.json").read_text())
    assert frozen["selected"][str(cid)] == selected
    assert frozen["calibration_metrics_sha256"][str(cid)] == sha(directory / "calibration_metrics.json")
    assert frozen["panel_sha256"] == sha(OUT / "panel_frozen.json")
    prompts = list(enumerate(split, 14))
    prepare_nla(cid, models, directory, prompts)
    model, tokenizer = steering.load_target(models["target"]["path"], "cuda:0")
    direction = vectors.load(str(OUT / "directions" / f"c{cid}.diffmean.pt"))
    def arms(pi):
        nla = vectors.load(str(directory / "nla_vectors" / f"p{pi}.pt"))
        return {"unsteered": (None, 0., "diffmean"),
                "diffmean": (direction, selected["diffmean"]["alpha"], "diffmean"),
                "nla": (nla, selected["nla"]["alpha"], "nla"),
                **{f"random_{s}": (vectors.random_like(direction, s * 1000000 + cid * 100 + pi - 14),
                                   .5, "diffmean") for s in RANDOM_SEEDS}}
    rows = generate_phase(cid, "evaluation", model, tokenizer, directory, prompts, candidate["terms"], arms)
    scored, metrics = score(rows, candidate["terms"])
    save_json(directory / "evaluation_metrics.json", {"concept_id": cid, "metrics": metrics, "prompt_metrics": scored})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concept", type=int)
    parser.add_argument("--concepts", default="")
    parser.add_argument("--phase", choices=["confirmation", "calibration", "evaluation"], required=True)
    args = parser.parse_args()
    concept_ids = [int(x) for x in args.concepts.split(",") if x] or [args.concept]
    assert all(cid is not None for cid in concept_ids)
    shared_target = None
    if args.phase == "confirmation":
        models = json.loads((OUT / "model_paths.json").read_text())
        shared_target = steering.load_target(models["target"]["path"], "cuda:0")
    for cid in concept_ids:
        run_concept(cid, args.phase, shared_target)


def run_concept(cid, phase, shared_target=None):
    models, protocol, candidate, directory = load(cid)
    if phase != "confirmation":
        panel = json.loads((OUT / "panel_frozen.json").read_text())
        assert cid in panel["concept_ids"]
        assert panel["protocol_sha256"] == sha(OUT / "protocol.json")
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    split = json.loads((OUT / "prompt_splits.json").read_text())[
        "confirmation" if phase == "confirmation" else "calibration" if phase == "calibration" else "evaluation"
    ][str(cid)]
    receipt = {"concept_id": cid, "phase": phase, "status": "running",
               "pid": os.getpid(), "gpu": os.environ.get("CUDA_VISIBLE_DEVICES"), "started_unix": time.time()}
    path = directory / f"{phase}_runtime.json"
    if path.exists():
        previous = json.loads(path.read_text())
        if previous["status"] != "complete":
            attempt = len(list(directory.glob(f"{phase}_runtime.attempt*.json"))) + 1
            save_json(directory / f"{phase}_runtime.attempt{attempt}.json", previous)
    save_json(path, receipt)
    try:
        if phase == "confirmation":
            confirmation(cid, models, candidate, directory, split, shared_target)
        else:
            {"calibration": calibration, "evaluation": evaluation}[phase](
                cid, models, candidate, directory, split)
        receipt["status"] = "complete"
    except BaseException:
        receipt["status"] = "failed"
        receipt["failure"] = traceback.format_exc()
        raise
    finally:
        receipt.update(elapsed_seconds=time.monotonic() - started,
                       peak_gpu_memory_bytes=torch.cuda.max_memory_allocated(), finished_unix=time.time())
        save_json(path, receipt)


if __name__ == "__main__":
    main()
