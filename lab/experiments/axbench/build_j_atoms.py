"""Build namespaced pure J-lens token atoms on the RunPod.

Run in the J-lens environment, not the transformers<5 NLA environment:

    python experiments/axbench/build_j_atoms.py \
      --model-key qwen --model-path /root/models/qwen2.5-7b-instruct

The builder reads the registered merged lens, resolves the requested token
surface exactly, evaluates the model's final norm on an all-ones vector (which
handles Qwen ``weight`` and Gemma ``1 + weight`` parameterizations), and saves
raw fp32 atoms plus receipts consumed by ``gen_specs.py``. No vocabulary-wide
matrix is constructed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import torch
import yaml

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
sys.path.insert(0, str(LAB))

from nla_lab.jlens_atoms import (  # noqa: E402
    ATOM_FORMULA_ID,
    JLensAtomError,
    atom_from_readout,
    effective_scale_from_norm_response,
    resolve_single_token,
    tensor_f32_sha256,
)
from nla_lab.study import canonical_sha256  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a mapping")
    return value


def _space(model_key: str) -> dict:
    rows = _load_yaml(LAB / "registry" / "residual_spaces.yaml")["spaces"]
    matches = [row for row in rows if row["model_key"] == model_key]
    if len(matches) != 1:
        raise ValueError(f"could not uniquely resolve residual space for {model_key}")
    return matches[0]


def _registered_artifact_hash(path: Path) -> str:
    registry = _load_yaml(LAB / "registry" / "artifacts.yaml")
    relative = path.resolve().relative_to(LAB.parent.resolve()).as_posix()
    matches = [row for row in registry["artifacts"] if row.get("path") == relative]
    if len(matches) != 1 or not matches[0].get("sha256"):
        raise ValueError(f"split lens is not uniquely hash-registered: {relative}")
    return str(matches[0]["sha256"])


def _marker_hashes(model_path: Path) -> dict[str, str]:
    names = (
        "config.json", "generation_config.json", "model.safetensors.index.json",
        "tokenizer.json", "tokenizer_config.json", "tokenizer.model",
        "special_tokens_map.json", "added_tokens.json", "chat_template.jinja",
    )
    found = {
        name: _sha256(model_path / name)
        for name in names if (model_path / name).is_file()
    }
    if "config.json" not in found:
        raise ValueError("model checkpoint is missing config.json")
    if not any(name.startswith("tokenizer") for name in found):
        raise ValueError("model checkpoint has no hashed tokenizer identity marker")
    return found


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma3-12b"), required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=HERE / "data")
    args = parser.parse_args()

    if not args.model_path.is_dir():
        raise ValueError(f"model path does not exist: {args.model_path}")
    space = _space(args.model_key)
    layer = int(space["layer_index"])
    hidden = int(space["hidden_width"])
    lens_path = LAB / str(space["j_lens_artifact"])
    if _sha256(lens_path) != space["j_lens_sha256"]:
        raise ValueError("registered merged J-lens hash mismatch")
    lens_state = torch.load(lens_path, map_location="cpu", weights_only=True)
    J_t = lens_state["J"][layer].float().cpu()
    if tuple(J_t.shape) != (hidden, hidden):
        raise ValueError(f"lens shape {tuple(J_t.shape)} != {(hidden, hidden)}")

    from transformers import AutoModelForCausalLM, AutoTokenizer
    from jlens.hf import from_hf

    tokenizer = AutoTokenizer.from_pretrained(str(args.model_path), trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(args.model_path), torch_dtype=torch.bfloat16,
        device_map="cuda", trust_remote_code=True,
    ).eval()
    lm = from_hf(model, tokenizer)
    if lm.n_layers <= layer:
        raise ValueError(f"model has {lm.n_layers} layers; need layer {layer}")
    final_norm = lm._final_norm
    head = lm._lm_head
    device = next(final_norm.parameters()).device
    with torch.no_grad():
        response = final_norm(torch.ones((1, hidden), device=device, dtype=torch.float32))
    scale = effective_scale_from_norm_response(response.float().cpu().numpy(), hidden)
    marker_hashes = _marker_hashes(args.model_path)
    norm_response_hash = tensor_f32_sha256(scale)

    registry = _load_yaml(HERE / "concepts.yaml")
    concept_registry_hash = canonical_sha256(registry)
    concepts = [row for row in registry["concepts"] if "direct_j_atom" in row]
    atoms: dict[str, np.ndarray] = {}
    entries: dict[str, dict] = {}
    J = J_t.numpy()
    for concept in concepts:
        target = concept["direct_j_atom"]
        variants = [target["preferred_variant"], target["fallback_variant"]]
        try:
            resolution = resolve_single_token(tokenizer, target["token_text"], variants)
        except JLensAtomError as exc:
            entries[concept["key"]] = {
                "status": "ineligible_tokenization",
                "model_key": args.model_key,
                "concept_key": concept["key"],
                "space_id": space["space_id"],
                "layer": layer,
                "hidden_width": hidden,
                "lens_sha256": space["j_lens_sha256"],
                "atom_formula": ATOM_FORMULA_ID,
                "checkpoint_marker_sha256": marker_hashes,
                "concept_registry_sha256": concept_registry_hash,
                "token_text": target["token_text"],
                "requested_variants": variants,
                "reason": str(exc),
            }
            continue
        token_id = int(resolution["token_id"])
        row = head.weight[token_id].detach().float().cpu().numpy()
        atom = atom_from_readout(J, scale, row)
        atom_t = torch.from_numpy(atom)
        unit_atom = (atom_t / atom_t.norm().clamp_min(1e-12)).numpy()
        key = f"{concept['key']}__{resolution['chosen_variant']}"
        atoms[key] = atom
        entries[concept["key"]] = {
            "concept_key": concept["key"],
            "space_id": space["space_id"],
            "layer": layer,
            "hidden_width": hidden,
            "lens_sha256": space["j_lens_sha256"],
            "atom_formula": ATOM_FORMULA_ID,
            "effective_norm_class": type(final_norm).__name__,
            "effective_norm_response_f32_sha256": norm_response_hash,
            "checkpoint_marker_sha256": marker_hashes,
            "concept_registry_sha256": concept_registry_hash,
            "unembedding_row_f32_sha256": tensor_f32_sha256(row),
            "archive_member": key,
            "token": resolution,
            "raw_norm": float(np.linalg.norm(atom)),
            "source_member_f32_sha256": tensor_f32_sha256(atom),
            # Match experiments/run.py exactly: torch fp32 norm on CPU.
            "unit_direction_sha256": tensor_f32_sha256(unit_atom),
        }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.model_key}_L{layer}_j_atoms"
    archive = args.output_dir / f"{stem}.npz"
    np.savez_compressed(archive, **atoms)
    archive_hash = _sha256(archive)
    archive_rel = archive.resolve().relative_to(LAB.resolve()).as_posix()
    for concept_key, entry in entries.items():
        if entry.get("status") == "ineligible_tokenization":
            continue
        entry.update({
            "status": "built",
            "model_key": args.model_key,
            "concept_key": concept_key,
            "archive_path": archive_rel,
            "source_archive_sha256": archive_hash,
        })

    split_report = {"available": False, "lens_sha256": {}}
    split_paths = [lens_path.with_name(f"lens_L{layer}_{half}.pt") for half in ("A", "B")]
    if all(path.exists() for path in split_paths):
        registered_split_hashes = {
            half: _registered_artifact_hash(path)
            for half, path in zip(("A", "B"), split_paths)
        }
        for half, path in zip(("A", "B"), split_paths):
            if _sha256(path) != registered_split_hashes[half]:
                raise ValueError(f"registered split-{half} lens hash mismatch")
        split_report = {
            "available": True,
            "lens_sha256": registered_split_hashes,
            "per_concept_cosine": {},
        }
        split_J = [
            torch.load(path, map_location="cpu", weights_only=True)["J"][layer]
            .float().numpy()
            for path in split_paths
        ]
        for concept_key, entry in entries.items():
            if entry.get("status") != "built":
                continue
            token_id = entry["token"]["token_id"]
            row = head.weight[token_id].detach().float().cpu().numpy()
            a = atom_from_readout(split_J[0], scale, row)
            b = atom_from_readout(split_J[1], scale, row)
            split_cos = _cos(a, b)
            split_report["per_concept_cosine"][concept_key] = split_cos
            entry["split_half_available"] = True
            entry["split_half_atom_cosine"] = split_cos
            entry["split_lens_sha256"] = dict(split_report["lens_sha256"])
    else:
        for entry in entries.values():
            if entry.get("status") != "built":
                continue
            entry["split_half_available"] = False
            entry["split_half_atom_cosine"] = None
            entry["split_lens_sha256"] = {}

    report = {
        "schema": "j-lens-atom-build/v2",
        "formula": ATOM_FORMULA_ID,
        "model_key": args.model_key,
        "space_id": space["space_id"],
        "layer": layer,
        "hidden_width": hidden,
        "model_path": str(args.model_path.resolve()),
        "checkpoint_marker_sha256": marker_hashes,
        "lens_path": str(lens_path.relative_to(LAB)),
        "lens_sha256": space["j_lens_sha256"],
        "concept_registry_sha256": concept_registry_hash,
        "effective_norm_class": type(final_norm).__name__,
        "effective_norm_response_f32_sha256": norm_response_hash,
        "split_half": split_report,
        "archive_path": archive_rel,
        "archive_sha256": archive_hash,
        "receipts": entries,
    }
    report_path = args.output_dir / f"{stem}_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {archive} and {report_path}")
    if not split_report["available"]:
        print("NOTE: split-half lenses unavailable; atom-stability gate not claimed")


if __name__ == "__main__":
    main()
