"""Build four frozen emoji directions in one registered model space.

Run on one 80 GB RunPod GPU from ``lab/`` after committing the frozen data::

    export NLA_MODEL=qwen  # or gemma3-12b
    /venv/bin/python experiments/emoji_method_comparison/build_directions.py \
      --model-key "$NLA_MODEL" \
      --models-root /root/models \
      --output-dir /workspace/nla-artifacts/emoji-method-comparison/$NLA_MODEL-build-01

The job loads the selected base model plus its matching AR once. It builds the 300-pair CAA
mean, the two exact AV-edit-AR deltas, and the direct `` emoji`` J-lens atom.
It fails before writing if Git is dirty, checkpoint identities are incomplete,
tokenization drifts, a registered lens hash fails, or split-half atom cosine is
below the frozen threshold. It creates vectors and receipts, never responses.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

import numpy as np
import yaml

try:  # Keep receipt/data validation importable on the CPU-only laptop.
    import torch
except ModuleNotFoundError:  # pragma: no cover - exercised by local test env
    torch = None  # type: ignore[assignment]


HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
REPO = LAB.parent
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.emoji_method_comparison.freeze_data import (  # noqa: E402
    CAA_PATH,
    CALIBRATION_PATH,
    load_yaml,
    validate_caa_manifest,
    validate_calibration_bank,
)
from experiments.emoji_method_comparison.method_lock import (  # noqa: E402
    LOCK_PATH,
    PROMPT_BANK_PATH,
    load_method_lock,
    load_prompt_bank,
)
from nla_lab.jlens_atoms import (  # noqa: E402
    ATOM_FORMULA_ID,
    atom_from_readout,
    effective_scale_from_norm_response,
    tensor_f32_sha256,
    unit,
)
from nla_lab.study import canonical_sha256, canonical_text_file_sha256  # noqa: E402


BUILD_SCHEMA = "emoji-method-comparison-direction-build/v2"
ARCHIVE_NAME = "directions.npz"
REPORT_NAME = "direction_build_report.json"
MARKER_NAMES = (
    "config.json",
    "generation_config.json",
    "model.safetensors.index.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "tokenizer.model",
    "special_tokens_map.json",
    "added_tokens.json",
    "chat_template.jinja",
    "nla_meta.yaml",
    "nla_source_receipt.json",
)
CODE_PATHS = (
    HERE / "build_directions.py",
    HERE / "freeze_data.py",
    HERE / "method_lock.py",
    LAB / "nla_lab" / "jlens_atoms.py",
    LAB / "nla_lab" / "session.py",
    LAB / "nla_lab" / "vendor_nla.py",
    LAB / "requirements.txt",
)


class DirectionBuildError(RuntimeError):
    """Raised before artifacts are accepted as frozen directions."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DirectionBuildError(message)


def _inference_mode():
    if torch is None:
        return lambda function: function
    return torch.inference_mode()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    _require(denominator > 1e-12, "cosine received a zero vector")
    return float(np.dot(a, b) / denominator)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=REPO, text=True, capture_output=True,
            check=False, timeout=15,
        )
        _require(completed.returncode == 0, f"git {' '.join(args)} failed")
        return completed.stdout.strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _package_versions() -> dict[str, str | None]:
    output: dict[str, str | None] = {}
    for name in (
        "torch", "transformers", "tokenizers", "accelerate", "numpy",
        "safetensors", "huggingface_hub", "PyYAML", "orjson",
    ):
        try:
            output[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            output[name] = None
    return output


def _gpu_runtime() -> dict[str, Any]:
    _require(torch is not None and torch.cuda.is_available(), "CUDA runtime is unavailable")
    index = torch.cuda.current_device()
    properties = torch.cuda.get_device_properties(index)
    return {
        "device_index": int(index),
        "name": torch.cuda.get_device_name(index),
        "compute_capability": [int(properties.major), int(properties.minor)],
        "total_memory_bytes": int(properties.total_memory),
        "torch_cuda_version": torch.version.cuda,
        "device_count": int(torch.cuda.device_count()),
    }


def _code_hashes() -> dict[str, str]:
    return {
        path.resolve().relative_to(REPO.resolve()).as_posix():
        canonical_text_file_sha256(path)
        for path in CODE_PATHS
    }


def _checkpoint_receipt(path: Path, source: Mapping[str, Any]) -> dict[str, Any]:
    _require(path.is_dir(), f"checkpoint directory is missing: {path}")
    markers = {
        name: _sha256_file(path / name)
        for name in MARKER_NAMES if (path / name).is_file()
    }
    _require("config.json" in markers, f"checkpoint lacks config.json: {path.name}")
    _require(
        "nla_source_receipt.json" in markers,
        f"checkpoint lacks pinned source receipt: {path.name}",
    )
    observed_source = json.loads(
        (path / "nla_source_receipt.json").read_text(encoding="utf-8")
    )
    _require(observed_source == dict(source), f"checkpoint source receipt drifted: {path.name}")
    weights: list[dict[str, Any]] = []
    for weight in sorted(path.glob("*.safetensors")):
        row = {"name": weight.name, "bytes": weight.stat().st_size}
        row["sha256"] = _sha256_file(weight)
        weights.append(row)
    _require(weights, f"checkpoint has no safetensor weights: {path.name}")
    return {
        "directory_name": path.name,
        "source": dict(source),
        "marker_sha256": markers,
        "weights_hashed": True,
        "weight_files": weights,
        "snapshot_identity_sha256": canonical_sha256({
            "source": dict(source),
            "marker_sha256": markers,
            "weight_files": weights,
        }),
    }


def _resolve_path(relative: str) -> Path:
    path = LAB / relative
    _require(path.is_file(), f"locked source file is missing: {relative}")
    return path


def _get_nested(value: Mapping[str, Any], dotted: str) -> Any:
    current: Any = value
    for key in dotted.split("."):
        _require(isinstance(current, Mapping) and key in current, f"missing field {dotted}")
        current = current[key]
    return current


def _render_caa_prompt(row: Mapping[str, Any], construction: Mapping[str, Any]) -> str:
    positive = str(construction["positive_requirement"])
    negative = str(construction["negative_requirement"])
    if row["positive_option"] == "A":
        option_a, option_b = positive, negative
    else:
        option_a, option_b = negative, positive
    return str(construction["prompt_template"]).format(
        input=row["text"], option_a=option_a, option_b=option_b
    )


@_inference_mode()
def build_caa_direction(
    lab: Any,
    rows: list[dict[str, Any]],
    construction: Mapping[str, Any],
    *,
    batch_size: int,
    layer: int,
    hidden_width: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    tokenizer = lab.tok
    letter_ids: dict[str, int] = {}
    for letter in ("A", "B"):
        ids = list(tokenizer.encode(letter, add_special_tokens=False))
        _require(len(ids) == 1, f"CAA completion {letter!r} is not one token: {ids}")
        _require(tokenizer.decode(ids) == letter, f"CAA completion {letter!r} does not round-trip")
        letter_ids[letter] = int(ids[0])

    encoded: list[dict[str, Any]] = []
    for row in rows:
        prompt = _render_caa_prompt(row, construction)
        prefix = list(tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            tokenize=True,
            add_generation_prompt=True,
        ))
        for letter in ("A", "B"):
            encoded.append({
                "row_id": row["id"],
                "positive_option": row["positive_option"],
                "letter": letter,
                "ids": prefix + [letter_ids[letter]],
            })

    activations: dict[tuple[str, str], np.ndarray] = {}
    pad_id = tokenizer.pad_token_id
    if pad_id is None:
        pad_id = tokenizer.eos_token_id
    _require(isinstance(pad_id, int), "tokenizer has no usable pad/eos token")
    for start in range(0, len(encoded), batch_size):
        batch = encoded[start:start + batch_size]
        maximum = max(len(item["ids"]) for item in batch)
        ids = torch.full(
            (len(batch), maximum), pad_id, dtype=torch.long, device=lab.device
        )
        attention = torch.zeros_like(ids)
        positions: list[int] = []
        for index, item in enumerate(batch):
            length = len(item["ids"])
            ids[index, :length] = torch.tensor(item["ids"], device=lab.device)
            attention[index, :length] = 1
            positions.append(length - 1)
        output = lab.base(
            input_ids=ids, attention_mask=attention,
            output_hidden_states=True, use_cache=False,
        )
        hidden = output.hidden_states[layer + 1]
        _require(hidden.shape[-1] == hidden_width, "CAA hidden width drifted")
        for index, item in enumerate(batch):
            vector = hidden[index, positions[index]].float().cpu().numpy()
            activations[(str(item["row_id"]), str(item["letter"]))] = vector
        del output, hidden, ids, attention

    differences: list[np.ndarray] = []
    positive_a: list[np.ndarray] = []
    positive_b: list[np.ndarray] = []
    odd_indexed: list[np.ndarray] = []
    even_indexed: list[np.ndarray] = []
    for index, row in enumerate(rows):
        a = activations[(str(row["id"]), "A")]
        b = activations[(str(row["id"]), "B")]
        oriented = a - b if row["positive_option"] == "A" else b - a
        differences.append(oriented)
        (positive_a if row["positive_option"] == "A" else positive_b).append(oriented)
        (even_indexed if index % 2 == 0 else odd_indexed).append(oriented)
    raw = np.mean(np.stack(differences).astype(np.float64), axis=0).astype(np.float32)
    mean_a = np.mean(np.stack(positive_a).astype(np.float64), axis=0).astype(np.float32)
    mean_b = np.mean(np.stack(positive_b).astype(np.float64), axis=0).astype(np.float32)
    mean_even = np.mean(np.stack(even_indexed).astype(np.float64), axis=0).astype(np.float32)
    mean_odd = np.mean(np.stack(odd_indexed).astype(np.float64), axis=0).astype(np.float32)
    _require(np.isfinite(raw).all() and np.linalg.norm(raw) > 1e-12, "CAA mean is invalid")
    return raw, {
        "pair_count": len(rows),
        "positive_A_count": len(positive_a),
        "positive_B_count": len(positive_b),
        "completion_token_ids": letter_ids,
        "positive_A_vs_B_half_cosine": _cos(mean_a, mean_b),
        "even_vs_odd_half_cosine": _cos(mean_even, mean_odd),
        "positive_A_half_raw_norm": float(np.linalg.norm(mean_a)),
        "positive_B_half_raw_norm": float(np.linalg.norm(mean_b)),
        "mean_accumulator": "float64_then_cast_float32",
        "batch_size": batch_size,
    }


def build_nla_direction(
    lab: Any,
    arm: Mapping[str, Any],
    substrate: Mapping[str, Any],
    *,
    hidden_width: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    source_path = _resolve_path(str(substrate["source"]))
    _require(_sha256_file(source_path) == substrate["source_sha256"], "substrate source hash mismatch")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    original = _get_nested(source, str(substrate["verbalization_field"]))
    _require(isinstance(original, str) and original, "original AV verbalization is empty")
    _require(
        hashlib.sha256(original.encode("utf-8")).hexdigest()
        == substrate["verbalization_sha256"],
        "substrate verbalization hash mismatch",
    )
    edited = original + str(arm["append_separator"]) + str(arm["appended_text"])
    original_vector = lab.reconstruct(original).float().reshape(-1).cpu().numpy()
    edited_vector = lab.reconstruct(edited).float().reshape(-1).cpu().numpy()
    raw = (edited_vector - original_vector).astype(np.float32)
    _require(raw.size == hidden_width and np.linalg.norm(raw) > 1e-12, "NLA delta is invalid")
    return raw, {
        "substrate_source": substrate["source"],
        "substrate_source_sha256": _sha256_file(source_path),
        "substrate_prompt": substrate["prompt"],
        "substrate_position": substrate["position"],
        "original_text_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "edited_text_sha256": hashlib.sha256(edited.encode("utf-8")).hexdigest(),
        "appended_text": arm["appended_text"],
        "append_separator": arm["append_separator"],
        "original_reconstruction_f32_sha256": tensor_f32_sha256(original_vector),
        "edited_reconstruction_f32_sha256": tensor_f32_sha256(edited_vector),
        "cos_original_edited": _cos(original_vector, edited_vector),
        "delta_norm_ratio": float(
            np.linalg.norm(raw)
            / ((np.linalg.norm(original_vector) + np.linalg.norm(edited_vector)) / 2)
        ),
    }


def _final_norm(base: Any) -> Any:
    for dotted in ("model.norm", "model.language_model.norm", "language_model.model.norm"):
        current = base
        try:
            for name in dotted.split("."):
                current = getattr(current, name)
            return current
        except AttributeError:
            continue
    raise DirectionBuildError("could not locate the active model's final norm")


@_inference_mode()
def build_jlens_direction(
    lab: Any,
    arm: Mapping[str, Any],
    lens_profile: Mapping[str, Any],
    *,
    layer: int,
    hidden_width: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    lens_path = _resolve_path(str(lens_profile["artifact"]))
    fit_path = _resolve_path(str(arm["fit_corpus"]))
    _require(_sha256_file(lens_path) == lens_profile["sha256"], "merged J-lens hash mismatch")
    fit_corpus_sha256 = canonical_text_file_sha256(fit_path)
    _require(fit_corpus_sha256 == arm["fit_corpus_sha256"], "J-lens fit-corpus hash mismatch")
    surface = str(arm["token_surface"])
    ids = list(lab.tok.encode(surface, add_special_tokens=False))
    _require(len(ids) == 1, f"direct J surface {surface!r} tokenizes as {ids}")
    token_id = int(ids[0])
    decoded = lab.tok.decode([token_id])
    _require(decoded == surface, f"direct J token round-trip {decoded!r} != {surface!r}")

    final_norm = _final_norm(lab.base)
    device = next(final_norm.parameters()).device
    response = final_norm(torch.ones((1, hidden_width), device=device, dtype=torch.float32))
    scale = effective_scale_from_norm_response(response.float().cpu().numpy(), hidden_width)
    head = lab.base.get_output_embeddings()
    if head is None:
        head = lab.base.get_input_embeddings()
    _require(head is not None, "base model has no output/input embedding head")
    row = head.weight[token_id].detach().float().cpu().numpy()
    state = torch.load(lens_path, map_location="cpu", weights_only=True)
    J = state["J"][layer].float().cpu().numpy()
    _require(J.shape == (hidden_width, hidden_width), f"merged J-lens shape drifted: {J.shape}")
    raw = atom_from_readout(J, scale, row)

    split_vectors: list[np.ndarray] = []
    split_hashes: dict[str, str] = {}
    for index, split in enumerate(lens_profile["split_half_lenses"]):
        path = _resolve_path(str(split["path"]))
        observed = _sha256_file(path)
        _require(observed == split["sha256"], f"split-half lens {index} hash mismatch")
        split_hashes["AB"[index]] = observed
        split_state = torch.load(path, map_location="cpu", weights_only=True)
        split_J = split_state["J"][layer].float().cpu().numpy()
        _require(split_J.shape == (hidden_width, hidden_width), "split-half J-lens shape drifted")
        split_vectors.append(atom_from_readout(split_J, scale, row))
    split_cosine = _cos(split_vectors[0], split_vectors[1])
    minimum = float(arm["minimum_split_half_atom_cosine"])
    _require(split_cosine >= minimum, f"J atom split-half cosine {split_cosine:.4f} < {minimum:.4f}")

    glyph = str(arm["glyph_diagnostic"]["surface"])
    glyph_ids = [int(value) for value in lab.tok.encode(glyph, add_special_tokens=False)]
    return raw, {
        "formula": ATOM_FORMULA_ID,
        "lens_sha256": _sha256_file(lens_path),
        "fit_corpus_sha256": fit_corpus_sha256,
        "fit_corpus_hash_rule": arm["fit_corpus_hash_rule"],
        "split_lens_sha256": split_hashes,
        "split_half_atom_cosine": split_cosine,
        "minimum_split_half_atom_cosine": minimum,
        "token": {
            "surface": surface,
            "token_id": token_id,
            "decoded": decoded,
            "ids": ids,
            "single_token_round_trip": True,
        },
        "glyph_diagnostic": {
            "surface": glyph,
            "ids": glyph_ids,
            "decoded_pieces": [lab.tok.decode([value]) for value in glyph_ids],
            "single_token": len(glyph_ids) == 1,
            "behavioral_arm": False,
        },
        "effective_norm_class": type(final_norm).__name__,
        "effective_norm_response_f32_sha256": tensor_f32_sha256(scale),
        "unembedding_row_f32_sha256": tensor_f32_sha256(row),
    }


def _vector_receipt(raw: np.ndarray, details: Mapping[str, Any]) -> dict[str, Any]:
    value = np.asarray(raw, dtype=np.float32).reshape(-1)
    normalized = unit(value)
    return {
        "hidden_width": int(value.size),
        "raw_norm": float(np.linalg.norm(value)),
        "source_member_f32_sha256": tensor_f32_sha256(value),
        "unit_direction_sha256": tensor_f32_sha256(normalized),
        "details": dict(details),
    }


def _atomic_npz(path: Path, vectors: Mapping[str, np.ndarray]) -> None:
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=path.name + ".", suffix=".npz", delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        np.savez_compressed(temporary, **vectors)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    rendered = json.dumps(dict(value), indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(path)


def validate_build_report(report: Mapping[str, Any], archive: Path) -> None:
    lock = load_method_lock()
    _require(report.get("schema") == BUILD_SCHEMA, "wrong build-report schema")
    _require(report.get("study_id") == lock["study_id"], "wrong build study")
    runtime = report.get("runtime", {})
    model_key = runtime.get("model_key")
    _require(model_key in ("qwen", "gemma3-12b"), "unknown build model")
    profile = lock["model_matrix"][model_key]
    _require(runtime.get("space_id") == profile["space_id"], "build residual space drifted")
    _require(runtime.get("layer") == profile["layer"], "build layer drifted")
    _require(runtime.get("hidden_width") == profile["hidden_width"], "build width drifted")
    payload = dict(report)
    receipt_hash = payload.pop("receipt_sha256", None)
    _require(receipt_hash == canonical_sha256(payload), "build receipt digest mismatch")
    _require(report.get("git", {}).get("dirty") is False, "build receipt came from a dirty worktree")
    checkpoints = report.get("checkpoint_receipts", {})
    _require(set(checkpoints) == {"base", "av", "ar"}, "checkpoint receipt roles drifted")
    for role, checkpoint in checkpoints.items():
        _require(
            checkpoint.get("source") == profile["checkpoint_sources"][role],
            f"{role} checkpoint source/revision drifted",
        )
        _require(
            "nla_source_receipt.json" in checkpoint.get("marker_sha256", {}),
            f"{role} checkpoint did not hash its pinned source receipt",
        )
        _require(checkpoint.get("weights_hashed") is True, f"{role} weights were not hashed")
        _require(
            checkpoint.get("snapshot_identity_sha256")
            == canonical_sha256({
                "source": checkpoint.get("source"),
                "marker_sha256": checkpoint.get("marker_sha256"),
                "weight_files": checkpoint.get("weight_files"),
            }),
            f"{role} snapshot receipt digest mismatch",
        )
    _require(_sha256_file(archive) == report.get("archive_sha256"), "direction archive hash mismatch")
    with np.load(archive) as data:
        _require(set(data.files) == {"caa", "nla_phrase", "nla_glyph", "jlens_word_atom"}, "direction member set drifted")
        for name in data.files:
            raw = np.asarray(data[name], dtype=np.float32).reshape(-1)
            receipt = report["directions"][name]
            _require(raw.size == profile["hidden_width"], f"{name} has wrong width")
            _require(tensor_f32_sha256(raw) == receipt["source_member_f32_sha256"], f"{name} raw hash mismatch")
            _require(tensor_f32_sha256(unit(raw)) == receipt["unit_direction_sha256"], f"{name} unit hash mismatch")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma3-12b"), required=True)
    parser.add_argument("--models-root", type=Path, default=Path("/root/models"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--caa-batch-size", type=int, default=24)
    args = parser.parse_args()
    _require(torch is not None, "PyTorch is required for the GPU direction build")
    _require(args.caa_batch_size > 0, "CAA batch size must be positive")

    lock = load_method_lock()
    heldout = load_prompt_bank()
    calibration = load_yaml(CALIBRATION_PATH)
    caa_manifest = load_yaml(CAA_PATH)
    validate_calibration_bank(calibration, heldout=heldout)
    validate_caa_manifest(caa_manifest, calibration=calibration, heldout=heldout)
    state = _git_state()
    _require(state["dirty"] is False, "direction build requires a clean Git worktree")
    _require(
        os.environ.get("NLA_MODEL") == args.model_key,
        f"set NLA_MODEL={args.model_key} explicitly",
    )

    profile = lock["model_matrix"][args.model_key]
    layer = int(profile["layer"])
    hidden_width = int(profile["hidden_width"])

    from nla_lab import NLASession
    from nla_lab.session import HIDDEN, LAYER, MODEL_DIRS

    _require(LAYER == layer, "active session layer disagrees with frozen profile")
    _require(HIDDEN == hidden_width, "active session width disagrees with frozen profile")
    _require(MODEL_DIRS == profile["checkpoint_dirs"], "active checkpoint directories drifted")

    checkpoints = {
        role: _checkpoint_receipt(
            args.models_root / directory,
            profile["checkpoint_sources"][role],
        )
        for role, directory in MODEL_DIRS.items()
    }
    lab = NLASession(models_root=args.models_root, load=("base", "ar"))
    _require(lab.n_layers > layer, f"base model has no layer {layer}")

    vectors: dict[str, np.ndarray] = {}
    details: dict[str, dict[str, Any]] = {}
    # Resolve and stability-gate the direct atom before the longer 300-pair CAA
    # pass. If a model does not have the frozen single token, fail closed and
    # spend no CAA compute; do not substitute a multi-token combination.
    vectors["jlens_word_atom"], details["jlens_word_atom"] = build_jlens_direction(
        lab,
        lock["construction"]["jlens_word_atom"],
        profile["jlens"],
        layer=layer,
        hidden_width=hidden_width,
    )
    vectors["caa"], details["caa"] = build_caa_direction(
        lab, caa_manifest["rows"], lock["construction"]["caa"],
        batch_size=args.caa_batch_size,
        layer=layer,
        hidden_width=hidden_width,
    )
    for name in ("nla_phrase", "nla_glyph"):
        vectors[name], details[name] = build_nla_direction(
            lab,
            lock["construction"][name],
            profile["nla_substrate"],
            hidden_width=hidden_width,
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / ARCHIVE_NAME
    _atomic_npz(archive, vectors)
    receipts = {
        name: _vector_receipt(vector, details[name])
        for name, vector in vectors.items()
    }
    for name, receipt in receipts.items():
        receipt["archive_member"] = name
    report: dict[str, Any] = {
        "schema": BUILD_SCHEMA,
        "study_id": lock["study_id"],
        "built_utc": _utc_now(),
        "git": state,
        "runtime": {
            "model_key": args.model_key,
            "base_model": profile["base_model"],
            "space_id": profile["space_id"],
            "layer": layer,
            "hidden_width": hidden_width,
            "packages": _package_versions(),
            "gpu": _gpu_runtime(),
        },
        "code_sha256": _code_hashes(),
        "lock_files": {
            "methods.lock.yaml": canonical_text_file_sha256(LOCK_PATH),
            "prompts.calibration.yaml": canonical_text_file_sha256(CALIBRATION_PATH),
            "prompts.heldout.yaml": canonical_text_file_sha256(PROMPT_BANK_PATH),
            "caa.construction.yaml": canonical_text_file_sha256(CAA_PATH),
        },
        "input_payload_sha256": {
            "calibration": calibration["selection"]["prompt_payload_sha256"],
            "heldout": lock["common_intervention"]["heldout_prompt_payload_sha256"],
            "caa": caa_manifest["selection"]["payload_sha256"],
        },
        "checkpoint_receipts": checkpoints,
        "archive_path": ARCHIVE_NAME,
        "archive_sha256": _sha256_file(archive),
        "directions": receipts,
    }
    report["receipt_sha256"] = canonical_sha256(report)
    report_path = args.output_dir / REPORT_NAME
    _atomic_json(report_path, report)
    validate_build_report(report, archive)
    print(f"built {archive}")
    print(f"archive_sha256={report['archive_sha256']}")
    for name, receipt in receipts.items():
        print(
            f"{name}: raw_norm={receipt['raw_norm']:.6f} "
            f"unit_sha256={receipt['unit_direction_sha256']}"
        )
    print(f"receipt={report_path} sha256={report['receipt_sha256']}")


if __name__ == "__main__":
    try:
        main()
    except DirectionBuildError as exc:
        raise SystemExit(f"direction build failed: {exc}") from exc
