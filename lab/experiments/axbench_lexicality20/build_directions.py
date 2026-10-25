"""Build all approved DiffMean, CAA, and prompt-specific NLA directions.

The three model stages are intentionally sequential so an 80 GB pod never
needs the base, AV, and AR checkpoints resident together.  Every intermediate
is written atomically and can be resumed after process or pod interruption.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import io
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Iterable

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
REPO = LAB.parent
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.axbench_lexicality20.protocol import (  # noqa: E402
    append_concept_edit, assignments, canonical_sha256, concept_by_id,
    file_sha256, load_bundle, stable_seed,
)
from nla_lab.session import NLASession  # noqa: E402
from nla_lab.study import canonical_text_file_sha256  # noqa: E402


SCHEMA = "axb44-direction-build/v1"
MODEL_ENV = {"qwen": "qwen", "gemma": "gemma3-12b"}
EXPECTED_CONTRAST_STEERING_COMMIT = "7d88ee6833a27d24b44484ad95df0890317ff39b"
CAA_PARITY_CONCEPT_ID = 11
# The historical vector also contains target-model neutral completions that were
# generated under the teammate run's Torch runtime.  Those response strings
# were not archived, so this is a historical reproduction check rather than a
# source-code parity proof.  0.99 rejects unrelated directions while allowing
# small deterministic-generation drift; the exact observed cosine is retained.
CAA_PARITY_MIN_COSINE = 0.99


class BuildError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
        suffix=".tmp", delete=False,
    ) as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def atomic_npz(path: Path, arrays: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, prefix=path.name + ".", suffix=".npz.tmp",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
    try:
        with temporary.open("wb") as stream:
            np.savez(stream, **arrays)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def unit(array: np.ndarray) -> np.ndarray:
    value = np.asarray(array, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(value))
    require(np.isfinite(value).all() and norm > 1e-12, "invalid zero/nonfinite vector")
    return (value / np.float32(norm)).astype(np.float32)


def tensor_hash(array: np.ndarray) -> str:
    value = np.ascontiguousarray(np.asarray(array, dtype="<f4").reshape(-1))
    digest = hashlib.sha256()
    digest.update(b"steering-tensor-f32-le/v1\0")
    digest.update(json.dumps(list(value.shape), separators=(",", ":")).encode())
    digest.update(b"\0")
    digest.update(value.tobytes())
    return digest.hexdigest()


def release(value: Any) -> None:
    del value
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=REPO, text=True, capture_output=True,
            timeout=20, check=False,
        )
        require(result.returncode == 0, f"git {' '.join(args)} failed")
        return result.stdout.strip()
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def decoder_layers(model: Any) -> Any:
    for dotted in (
        "model.layers", "model.language_model.layers", "language_model.model.layers"
    ):
        current = model
        try:
            for name in dotted.split("."):
                current = getattr(current, name)
            return current
        except AttributeError:
            continue
    raise BuildError("could not locate decoder layers")


def _last_token_mean_difference(
    model: Any, tokenizer: Any, contrast_pairs: list[dict[str, str]],
    layer_index: int, _one_based: bool = False, max_length: int = 1024,
    normalize: bool = False,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """Vendored CAA fallback: mean paired final-token activation difference.

    This implementation is only permitted after a model-time parity check
    against the exact archived team vector.  It is not accepted merely because
    it is a conventional CAA implementation.
    """
    require(not _one_based, "axb44 uses zero-based layer indices")
    module = decoder_layers(model)[layer_index]

    def capture(texts: list[str]) -> torch.Tensor:
        rows: list[torch.Tensor] = []
        for start in range(0, len(texts), 4):
            encoded = tokenizer(
                texts[start:start + 4], return_tensors="pt", padding=True,
                truncation=True, max_length=max_length,
            ).to(model.device)
            found: list[torch.Tensor] = []
            handle = module.register_forward_hook(
                lambda _m, _i, output: found.append(hidden(output).detach().float().cpu())
            )
            try:
                with torch.inference_mode():
                    model(**encoded, use_cache=False)
            finally:
                handle.remove()
            require(len(found) == 1, "CAA hook fired an unexpected number of times")
            mask = encoded.attention_mask.detach().bool().cpu()
            for index in range(found[0].shape[0]):
                eligible = found[0][index][mask[index]]
                require(eligible.shape[0] > 0, "CAA example has no eligible token")
                rows.append(eligible[-1])
        return torch.stack(rows)

    positive = capture([str(pair["positive"]) for pair in contrast_pairs])
    neutral = capture([str(pair["neutral"]) for pair in contrast_pairs])
    vector = (positive - neutral).mean(dim=0)
    raw_norm = float(vector.norm())
    require(raw_norm > 1e-12 and torch.isfinite(vector).all().item(), "invalid CAA vector")
    if normalize:
        vector = vector / vector.norm().clamp_min(1e-12)
    return vector, {
        "method": "caa", "layer_requested": layer_index,
        "layer_index": layer_index, "one_based": False,
        "pairs": len(contrast_pairs), "raw_norm": raw_norm,
        "normalized": bool(normalize),
        "implementation": "vendored_final_token_paired_mean_difference",
    }


def load_caa_extractor() -> tuple[Any, dict[str, Any]]:
    """Prefer the pinned team package; otherwise require archived-vector parity."""
    try:
        from contrast_steering.pipeline import extract_vector
    except ImportError:
        return _last_token_mean_difference, {
            "implementation": "vendored_fallback_pending_mandatory_parity",
            "approved_team_commit": EXPECTED_CONTRAST_STEERING_COMMIT,
        }
    source = Path(inspect.getfile(extract_vector)).resolve()
    candidates = [source.parent, *source.parents]
    repository = next((path for path in candidates if (path / ".git").exists()), None)
    require(repository is not None, "contrast_steering source is not bound to a Git checkout")
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
        capture_output=True, check=False, timeout=20,
    )
    require(result.returncode == 0, "cannot resolve contrast_steering Git commit")
    commit = result.stdout.strip()
    require(commit == EXPECTED_CONTRAST_STEERING_COMMIT,
            f"contrast_steering commit {commit} != approved {EXPECTED_CONTRAST_STEERING_COMMIT}")
    return extract_vector, {
        "source_file": str(source), "repository": str(repository), "commit": commit,
        "source_sha256": file_sha256(source),
    }


def archived_caa_reference(model_key: str) -> tuple[torch.Tensor, dict[str, str]]:
    """Load the exact historical concept-11 team vector from the Git object DB."""
    reference_root = os.environ.get("AXB44_CAA_REFERENCE_DIR", "").strip()
    if reference_root:
        path = Path(reference_root) / model_key / f"c{CAA_PARITY_CONCEPT_ID}.caa.pt"
        require(path.is_file(), f"missing staged CAA parity reference {path}")
        payload = path.read_bytes()
        vector = torch.load(io.BytesIO(payload), map_location="cpu", weights_only=True)
        require(isinstance(vector, torch.Tensor), "staged CAA reference is not a tensor")
        return vector.detach().float().reshape(-1), {
            "staged_path": str(path),
            "serialized_sha256": hashlib.sha256(payload).hexdigest(),
        }
    path = (
        REPO / "artifacts" / "paper_runs" / "data" / "baseline_panel14"
        / model_key / f"c{CAA_PARITY_CONCEPT_ID}.caa.pt"
    )
    require(path.is_file(), f"missing bundled CAA parity reference {path}")
    payload = path.read_bytes()
    vector = torch.load(io.BytesIO(payload), map_location="cpu", weights_only=True)
    require(isinstance(vector, torch.Tensor), "archived CAA reference is not a tensor")
    return vector.detach().float().reshape(-1), {
        "bundled_path": str(path.relative_to(REPO)),
        "serialized_sha256": hashlib.sha256(payload).hexdigest(),
    }


def validate_caa_parity(
    lab: NLASession, train: Any, extractor: Any, *, model_key: str,
    layer_index: int,
) -> dict[str, Any]:
    """Check close historical reproduction of the team's prior CAA vector.

    This comparison jointly reflects the extraction formula and regenerated
    neutral completions.  It cannot prove source-code identity because the
    private dependency and original neutral response strings were not archived.
    """
    rows = positive_rows(train, CAA_PARITY_CONCEPT_ID)
    prompts = [str(value) for value in rows.input]
    neutral = generate_neutral(lab, prompts)
    pairs = [
        {
            "positive": chat_text(lab.tok, prompt, str(answer)),
            "neutral": chat_text(lab.tok, prompt, neutral_answer),
        }
        for prompt, answer, neutral_answer in zip(prompts, rows.output, neutral)
    ]
    candidate, metadata = extractor(
        lab.base, lab.tok, pairs, layer_index, False, 1024, False
    )
    candidate = torch.as_tensor(candidate).detach().float().reshape(-1)
    reference, reference_receipt = archived_caa_reference(model_key)
    require(candidate.shape == reference.shape, "CAA parity vector width mismatch")
    cosine = float(torch.nn.functional.cosine_similarity(candidate, reference, dim=0))
    require(
        np.isfinite(cosine) and cosine >= CAA_PARITY_MIN_COSINE,
        f"CAA extractor failed archived parity: cosine={cosine:.9f} < {CAA_PARITY_MIN_COSINE}",
    )
    return {
        "concept_id": CAA_PARITY_CONCEPT_ID,
        "minimum_cosine": CAA_PARITY_MIN_COSINE,
        "observed_cosine": cosine,
        "interpretation": (
            "historical reproduction check over regenerated neutral answers; "
            "not a source-code-identity claim"
        ),
        "candidate_raw_norm": float(candidate.norm()),
        "reference_raw_norm": float(reference.norm()),
        "extractor_metadata": metadata,
        "reference": reference_receipt,
    }


def hidden(output: Any) -> torch.Tensor:
    return output[0] if isinstance(output, tuple) else output


def chat_text(tokenizer: Any, prompt: str, answer: str | None = None) -> str:
    messages = [{"role": "user", "content": prompt}]
    if answer is not None:
        messages.append({"role": "assistant", "content": answer})
    return tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=answer is None
    )


def axbench_trim(tokenizer: Any) -> tuple[int, int]:
    first = tokenizer.apply_chat_template(
        [{"role": "user", "content": "1"}], tokenize=True
    )
    second = tokenizer.apply_chat_template(
        [{"role": "user", "content": "2"}], tokenize=True
    )
    first = list(getattr(first, "input_ids", first))
    second = list(getattr(second, "input_ids", second))
    prefix = next((i for i, pair in enumerate(zip(first, second)) if pair[0] != pair[1]), 0)
    suffix = next((i for i, pair in enumerate(zip(reversed(first), reversed(second)))
                   if pair[0] != pair[1]), 0)
    return prefix, suffix


@torch.inference_mode()
def capture_text_tokens(
    lab: NLASession, texts: list[str], *, layer_index: int,
    trim: tuple[int, int], batch_size: int = 4,
) -> list[np.ndarray]:
    """Capture eligible layer outputs for already-rendered chat strings."""
    output_rows: list[np.ndarray] = []
    layer = decoder_layers(lab.base)[layer_index]
    for start in range(0, len(texts), batch_size):
        encoded = lab.tok(
            texts[start:start + batch_size], return_tensors="pt", padding=True,
            truncation=True, max_length=1024,
        ).to(lab.device)
        found: list[torch.Tensor] = []
        handle = layer.register_forward_hook(
            lambda _m, _i, out: found.append(hidden(out).detach().float().cpu())
        )
        try:
            lab.base(**encoded, use_cache=False)
        finally:
            handle.remove()
        require(len(found) == 1, "activation hook fired an unexpected number of times")
        values = found[0]
        masks = encoded.attention_mask.detach().bool().cpu()
        left, right = trim
        for index in range(values.shape[0]):
            row = values[index][masks[index]]
            row = row[left:len(row) - right if right else None]
            require(row.numel() > 0, "AxBench trim removed every token")
            output_rows.append(row.numpy().astype(np.float32))
    return output_rows


@torch.inference_mode()
def capture_last_chat(lab: NLASession, prompts: list[str], *, layer_index: int) -> list[np.ndarray]:
    rows = []
    for prompt in prompts:
        acts, _tokens = lab.extract(prompt)
        require(acts.shape[1] == load_bundle()["campaign"]["models"][
            "qwen" if lab.n_layers < 40 else "gemma"]["hidden_width"], "hidden width drift")
        rows.append(acts[-1].numpy().astype(np.float32))
    return rows


@torch.inference_mode()
def generate_neutral(lab: NLASession, prompts: list[str]) -> list[str]:
    return [str(lab.generate(prompt, temperature=0, max_new_tokens=96)) for prompt in prompts]


def load_train(path: Path) -> Any:
    require(path.is_file(), f"missing Concept500 construction parquet: {path}")
    import pandas as pd
    return pd.read_parquet(path)


def positive_rows(train: Any, concept_id: int) -> Any:
    rows = train[(train.concept_id == concept_id) & (train.category == "positive")].head(72)
    require(len(rows) == 72, f"concept {concept_id} does not have 72 positive rows")
    return rows


def negative_rows(train: Any) -> Any:
    rows = train[(train.concept_id == -1) & (train.category == "negative")].head(72)
    require(len(rows) == 72, "shared construction negative set is not 72 rows")
    return rows


def build_fixed_and_activations(
    lab: NLASession, bundle: dict[str, Any], train: Any, out: Path,
    *, model_key: str, caa_extractor: Any,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    profile = bundle["campaign"]["models"][model_key]
    require(lab.n_layers > int(profile["layer"]), "active base layer count drift")
    trim = axbench_trim(lab.tok)
    partial_arrays = out / "base_stage.partial.npz"
    partial_report = out / "base_stage.partial.json"
    arrays: dict[str, np.ndarray] = (
        {key: value for key, value in np.load(partial_arrays).items()}
        if partial_arrays.exists() else {}
    )
    report: dict[str, Any] = (
        json.loads(partial_report.read_text(encoding="utf-8"))
        if partial_report.exists() else {"diffmean_trim": list(trim), "concepts": {}}
    )
    require(report.get("diffmean_trim") == list(trim), "resumed base trim drift")
    if "caa_parity" not in report:
        report["caa_parity"] = validate_caa_parity(
            lab, train, caa_extractor, model_key=model_key,
            layer_index=int(profile["layer"]),
        )
        atomic_json(partial_report, report)
    shared_negatives = negative_rows(train)
    neg_texts = [chat_text(lab.tok, str(row.input), str(row.output))
                 for _, row in shared_negatives.iterrows()]
    layer_index = int(profile["layer"])
    neg_acts = capture_text_tokens(lab, neg_texts, layer_index=layer_index, trim=trim)
    neg_mean = np.concatenate(neg_acts).astype(np.float64).mean(axis=0)
    carrier_rows = [
        (int(concept["concept_id"]), prompt["prompt_id"], prompt["text"])
        for concept in bundle["campaign"]["concepts"]
        for split in ("calibration", "evaluation")
        for prompt in assignments(
            bundle, concept_id=int(concept["concept_id"]), split=split
        )
    ]
    for concept in bundle["campaign"]["concepts"]:
        cid = int(concept["concept_id"])
        if str(cid) in report["concepts"]:
            require(f"c{cid}.diffmean" in arrays and f"c{cid}.caa" in arrays,
                    f"partial base artifacts missing vectors for concept {cid}")
            continue
        rows = positive_rows(train, cid)
        prompts = [str(value) for value in rows.input]
        positives = [chat_text(lab.tok, str(row.input), str(row.output))
                     for _, row in rows.iterrows()]
        pos_acts = capture_text_tokens(
            lab, positives, layer_index=layer_index, trim=trim
        )
        diffmean = (np.concatenate(pos_acts).astype(np.float64).mean(axis=0) - neg_mean).astype(np.float32)
        neutral = generate_neutral(lab, prompts)
        positive_full = [chat_text(lab.tok, prompt, str(answer))
                         for prompt, answer in zip(prompts, rows.output)]
        neutral_full = [chat_text(lab.tok, prompt, answer)
                        for prompt, answer in zip(prompts, neutral)]
        caa_pairs = [
            {"positive": positive, "neutral": neutral_text}
            for positive, neutral_text in zip(positive_full, neutral_full)
        ]
        caa_tensor, caa_metadata = caa_extractor(
            lab.base, lab.tok, caa_pairs, layer_index, False, 1024, False
        )
        caa = np.asarray(
            caa_tensor.detach().float().cpu().numpy()
            if hasattr(caa_tensor, "detach") else caa_tensor,
            dtype=np.float32,
        ).reshape(-1)
        require(caa.size == int(profile["hidden_width"]), "CAA width drift")
        arrays[f"c{cid}.diffmean"] = diffmean
        arrays[f"c{cid}.caa"] = caa
        report["concepts"][str(cid)] = {
            "diffmean_raw_norm": float(np.linalg.norm(diffmean)),
            "caa_raw_norm": float(np.linalg.norm(caa)),
            "construction_pairs": 72,
            "caa_semantics": (
                "paired final-token mean activation difference; exact team package "
                "or archived-vector-parity-validated vendored implementation"
            ),
            "caa_extraction_metadata": caa_metadata,
            "neutral_answers": neutral,
        }
        atomic_npz(partial_arrays, arrays)
        atomic_json(partial_report, report)
        atomic_json(out / "base_stage.progress.json", {
            "schema": SCHEMA, "model_key": model_key,
            "completed_concepts": sorted(int(key) for key in report["concepts"]),
        })
    missing_carriers = [
        row for row in carrier_rows
        if f"c{row[0]}.{row[1]}.activation" not in arrays
    ]
    activations = capture_last_chat(
        lab, [row[2] for row in missing_carriers], layer_index=int(profile["layer"])
    ) if missing_carriers else []
    for (cid, prompt_id, _text), activation in zip(missing_carriers, activations):
        arrays[f"c{cid}.{prompt_id}.activation"] = activation
    require(all(
        f"c{cid}.{prompt_id}.activation" in arrays
        for cid, prompt_id, _text in carrier_rows
    ), "carrier activation stage is incomplete")
    report["carrier_activations"] = len(carrier_rows)
    return arrays, report


def build_av_stage(
    lab: NLASession, bundle: dict[str, Any], base_arrays: dict[str, np.ndarray],
    out: Path, *, model_key: str,
) -> dict[str, Any]:
    progress_path = out / "av_stage.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {
        "schema": SCHEMA, "model_key": model_key, "rows": {},
    }
    for concept in bundle["campaign"]["concepts"]:
        cid = int(concept["concept_id"])
        for split in ("calibration", "evaluation"):
            for prompt in assignments(bundle, concept_id=cid, split=split):
                key = f"c{cid}.{prompt['prompt_id']}"
                if key in progress["rows"]:
                    continue
                activation = base_arrays[key + ".activation"]
                originals = []
                for rollout in range(3):
                    torch.manual_seed(stable_seed("av", bundle["campaign"]["campaign_id"],
                                                  model_key, cid, prompt["prompt_id"], rollout))
                    inner = lab.verbalize(activation, temperature=1.0, max_new_tokens=200)
                    original = inner if "<explanation>" in inner else f"<explanation>{inner}</explanation>"
                    originals.append(original)
                progress["rows"][key] = {
                    "concept_id": cid, "prompt_id": prompt["prompt_id"],
                    "split": split, "original_explanations": originals,
                }
                atomic_json(progress_path, progress)
    return progress


def build_ar_stage(
    lab: NLASession, bundle: dict[str, Any], av_report: dict[str, Any], out: Path,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    partial_arrays = out / "ar_stage.partial.npz"
    partial_report = out / "ar_stage.partial.json"
    arrays: dict[str, np.ndarray] = (
        {key: value for key, value in np.load(partial_arrays).items()}
        if partial_arrays.exists() else {}
    )
    report: dict[str, Any] = (
        json.loads(partial_report.read_text(encoding="utf-8"))
        if partial_report.exists() else {"rows": {}}
    )
    for key, row in av_report["rows"].items():
        if key in report["rows"]:
            require(key + ".nla" in arrays, f"partial AR artifact lacks {key}.nla")
            continue
        concept = concept_by_id(bundle, int(row["concept_id"]))
        unit_deltas = []
        rollout_report = []
        for original in row["original_explanations"]:
            edited = append_concept_edit(original, concept["nla_hidden_addition"])
            original_vector = lab.reconstruct(original).float().cpu().numpy()
            edited_vector = lab.reconstruct(edited).float().cpu().numpy()
            raw = edited_vector - original_vector
            unit_deltas.append(unit(raw))
            rollout_report.append({
                "original": original, "edited": edited,
                "raw_delta_norm": float(np.linalg.norm(raw)),
                "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                "edited_sha256": hashlib.sha256(edited.encode("utf-8")).hexdigest(),
            })
        direction = unit(np.sum(np.stack(unit_deltas), axis=0))
        arrays[key + ".nla"] = direction
        report["rows"][key] = {
            **{field: row[field] for field in ("concept_id", "prompt_id", "split")},
            "rollouts": rollout_report,
            "unit_direction_f32_sha256": tensor_hash(direction),
        }
        atomic_npz(partial_arrays, arrays)
        atomic_json(partial_report, report)
    return arrays, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--models-root", type=Path, default=Path("/workspace/models"))
    parser.add_argument("--train-parquet", type=Path, required=True)
    parser.add_argument("--model-verification", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    require(MODEL_ENV[args.model_key] == __import__("os").environ.get("NLA_MODEL"),
            "NLA_MODEL must explicitly match --model-key")
    bundle = load_bundle()
    state = git_state()
    require(not state["dirty"], "direction build requires a clean committed worktree")
    verification = json.loads(args.model_verification.read_text(encoding="utf-8"))
    require(verification.get("schema") == "axb44-existing-model-verification/v1",
            "model verification schema mismatch")
    require(verification.get("model_key") == args.model_key, "model verification identity mismatch")
    require(verification.get("review_payload_sha256") == bundle["review_payload_sha256"],
            "model verification review lock mismatch")
    expected_train = bundle["campaign"]["sources"]["concept500"]["train_parquet_sha256"]
    require(file_sha256(args.train_parquet) == expected_train, "construction parquet hash mismatch")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    train = load_train(args.train_parquet)
    caa_extractor, caa_dependency = load_caa_extractor()

    print("stage 1/3: base model -> DiffMean, CAA, and carrier activations")
    base_archive = args.output_dir / "base_stage.npz"
    base_receipt = args.output_dir / "base_stage.json"
    if base_archive.exists() and base_receipt.exists():
        base_arrays = {key: value for key, value in np.load(base_archive).items()}
        base_report = json.loads(base_receipt.read_text(encoding="utf-8"))
        print("resuming completed base stage")
    else:
        base = NLASession(models_root=args.models_root, load=("base",))
        base_arrays, base_report = build_fixed_and_activations(
            base, bundle, train, args.output_dir, model_key=args.model_key,
            caa_extractor=caa_extractor,
        )
        atomic_npz(base_archive, base_arrays)
        atomic_json(base_receipt, base_report)
        del base
        release(None)

    print("stage 2/3: AV model -> three frozen explanations per carrier")
    av = NLASession(models_root=args.models_root, load=("av",))
    av_report = build_av_stage(
        av, bundle, base_arrays, args.output_dir, model_key=args.model_key
    )
    del av
    release(None)

    print("stage 3/3: AR model -> edited-minus-original NLA directions")
    ar = NLASession(models_root=args.models_root, load=("ar",))
    nla_arrays, ar_report = build_ar_stage(ar, bundle, av_report, args.output_dir)
    del ar
    release(None)

    final_arrays = {
        key: value for key, value in base_arrays.items()
        if not key.endswith(".activation")
    } | nla_arrays
    archive = args.output_dir / "directions.npz"
    atomic_npz(archive, final_arrays)
    member_receipts = {
        key: {
            "numel": int(value.size), "raw_norm": float(np.linalg.norm(value)),
            "unit_direction_f32_sha256": tensor_hash(unit(value)),
        }
        for key, value in final_arrays.items()
    }
    code_paths = [
        HERE / "build_directions.py", HERE / "protocol.py",
        LAB / "nla_lab" / "session.py", LAB / "nla_lab" / "vendor_nla.py",
    ]
    report = {
        "schema": SCHEMA, "campaign_id": bundle["campaign"]["campaign_id"],
        "review_payload_sha256": bundle["review_payload_sha256"],
        "model_key": args.model_key, "git": state,
        "construction_parquet_sha256": expected_train,
        "model_verification_sha256": file_sha256(args.model_verification),
        "archive": str(archive), "archive_sha256": file_sha256(archive),
        "members": member_receipts, "base_stage": base_report,
        "av_stage_sha256": file_sha256(args.output_dir / "av_stage.json"),
        "ar_stage": ar_report,
        "caa_dependency": caa_dependency,
        "code_sha256": {
            path.relative_to(REPO).as_posix(): canonical_text_file_sha256(path)
            for path in code_paths
        },
        "caa_reference": {
            "team_dependency_commit": EXPECTED_CONTRAST_STEERING_COMMIT,
            "implementation": caa_dependency["implementation"] if "implementation" in caa_dependency
                              else "direct pinned contrast_steering.pipeline.extract_vector call",
            "mandatory_model_time_parity": base_report["caa_parity"],
        },
    }
    atomic_json(args.output_dir / "direction_build_report.json", report)
    print(f"built {len(final_arrays)} directions -> {archive}")


if __name__ == "__main__":
    try:
        main()
    except BuildError as exc:
        raise SystemExit(f"direction build failed: {exc}") from exc
