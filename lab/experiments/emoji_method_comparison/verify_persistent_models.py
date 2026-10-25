"""Verify existing persistent checkpoints against pinned HF revisions without downloading.

Only repository metadata is requested. Every remote safetensor and required
configuration/tokenizer marker must already exist locally and match its LFS
SHA-256 or Git blob ID before the small ``nla_source_receipt.json`` marker is
written. Missing data fails closed; this program never calls snapshot_download.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from experiments.emoji_method_comparison.method_lock import load_method_lock


MARKERS = {
    "config.json", "generation_config.json", "model.safetensors.index.json",
    "tokenizer.json", "tokenizer_config.json", "tokenizer.model",
    "special_tokens_map.json", "added_tokens.json", "chat_template.jinja",
    "nla_meta.yaml",
}


class PersistentModelError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_id(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1()  # noqa: S324 - Git object identity, not security
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def verify_one(path: Path, source: dict[str, str], *, token: str | None) -> dict[str, Any]:
    if not path.is_dir():
        raise PersistentModelError(f"persistent checkpoint is absent: {path}")
    from huggingface_hub import HfApi

    info = HfApi().model_info(
        source["repository"], revision=source["revision"],
        files_metadata=True, token=token,
    )
    observed_sha = str(_field(info, "sha", ""))
    if observed_sha != source["revision"]:
        raise PersistentModelError(
            f"HF resolved {source['repository']} to {observed_sha}, expected {source['revision']}"
        )
    remote: dict[str, Any] = {}
    for sibling in _field(info, "siblings", []) or []:
        name = str(_field(sibling, "rfilename", ""))
        if name.endswith(".safetensors") or name in MARKERS:
            remote[name] = sibling
    weight_names = sorted(name for name in remote if name.endswith(".safetensors"))
    if not weight_names:
        raise PersistentModelError(f"pinned repository has no safetensors: {source['repository']}")
    checked: list[dict[str, Any]] = []
    for name in sorted(remote):
        local = path / name
        if not local.is_file():
            raise PersistentModelError(f"missing existing file (download forbidden): {local}")
        sibling = remote[name]
        expected_size = _field(sibling, "size")
        if expected_size is not None and int(expected_size) != local.stat().st_size:
            raise PersistentModelError(f"size mismatch: {local}")
        lfs = _field(sibling, "lfs")
        expected_lfs = _field(lfs, "sha256") if lfs is not None else None
        blob_id = _field(sibling, "blob_id") or _field(sibling, "blobId")
        if expected_lfs:
            observed = _sha256(local)
            if observed != str(expected_lfs):
                raise PersistentModelError(f"LFS SHA-256 mismatch: {local}")
            identity = {"kind": "lfs_sha256", "digest": observed}
        elif blob_id:
            observed = _git_blob_id(local)
            if observed != str(blob_id):
                raise PersistentModelError(f"Git blob mismatch: {local}")
            identity = {"kind": "git_blob_sha1", "digest": observed}
        else:
            raise PersistentModelError(f"HF supplied no content identity for {name}")
        checked.append({"name": name, "bytes": local.stat().st_size, **identity})
    receipt_path = path / "nla_source_receipt.json"
    payload = dict(source)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=receipt_path.parent,
        prefix=receipt_path.name + ".", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        temporary = Path(handle.name)
    temporary.replace(receipt_path)
    return {
        "repository": source["repository"], "revision": source["revision"],
        "directory": str(path), "verified_files": len(checked),
        "verified_weight_files": len(weight_names),
        "verified_bytes": sum(row["bytes"] for row in checked),
        "files": checked,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma3-12b"), required=True)
    parser.add_argument("--models-root", type=Path, default=Path("/workspace/models"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lock = load_method_lock()
    profile = lock["model_matrix"][args.model_key]
    token = os.environ.get("HF_TOKEN") or None
    reports = {}
    for role in ("base", "av", "ar"):
        directory = args.models_root / profile["checkpoint_dirs"][role]
        reports[role] = verify_one(directory, profile["checkpoint_sources"][role], token=token)
        print(
            f"verified existing {role}: {reports[role]['verified_weight_files']} weights, "
            f"{reports[role]['verified_bytes'] / 1e9:.2f} GB"
        )
    output = {
        "schema": "emoji-method-comparison-existing-model-verification/v1",
        "study_id": lock["study_id"], "model_key": args.model_key,
        "network_downloads": 0, "models_root": str(args.models_root),
        "checkpoints": reports,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=args.output.parent,
        prefix=args.output.name + ".", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(json.dumps(output, indent=2) + "\n")
        temporary = Path(handle.name)
    temporary.replace(args.output)
    print(f"wrote {args.output}; no model files were downloaded")


if __name__ == "__main__":
    try:
        main()
    except PersistentModelError as exc:
        raise SystemExit(f"persistent model verification failed: {exc}") from exc
