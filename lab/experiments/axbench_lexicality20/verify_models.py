"""Verify already-persistent model checkpoints against the approved HF revisions.

This program requests repository metadata but never downloads model content.
It writes the small source marker required by the execution receipts only after
all required local files match the pinned snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from experiments.axbench_lexicality20.protocol import load_bundle


MARKERS = {
    "config.json", "generation_config.json", "model.safetensors.index.json",
    "tokenizer.json", "tokenizer_config.json", "tokenizer.model",
    "special_tokens_map.json", "added_tokens.json", "chat_template.jinja",
    "nla_meta.yaml",
}
DIRS = {
    "qwen": {"base": "qwen2.5-7b-instruct", "av": "nla-av", "ar": "nla-ar"},
    "gemma": {
        "base": "gemma-3-12b-it", "av": "nla-gemma3-12b-av",
        "ar": "nla-gemma3-12b-ar",
    },
}


class VerificationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_blob(path: Path) -> str:
    digest = hashlib.sha1()  # noqa: S324 - Git content identity, not security
    digest.update(f"blob {path.stat().st_size}\0".encode("ascii"))
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def verify_one(path: Path, source: dict[str, str], token: str | None) -> dict[str, Any]:
    if not path.is_dir():
        raise VerificationError(f"persistent checkpoint is absent: {path}")
    from huggingface_hub import HfApi

    info = HfApi().model_info(
        source["repository"], revision=source["revision"],
        files_metadata=True, token=token,
    )
    if str(field(info, "sha", "")) != source["revision"]:
        raise VerificationError(f"HF revision resolution drift: {source['repository']}")
    remote = {
        str(field(item, "rfilename", "")): item
        for item in field(info, "siblings", []) or []
        if str(field(item, "rfilename", "")).endswith(".safetensors")
        or str(field(item, "rfilename", "")) in MARKERS
    }
    weights = sorted(name for name in remote if name.endswith(".safetensors"))
    if not weights:
        raise VerificationError(f"pinned repository has no safetensors: {source['repository']}")
    checked = []
    for name in sorted(remote):
        local = path / name
        if not local.is_file():
            raise VerificationError(f"missing local file; download forbidden: {local}")
        expected_size = field(remote[name], "size")
        if expected_size is not None and int(expected_size) != local.stat().st_size:
            raise VerificationError(f"size mismatch: {local}")
        lfs = field(remote[name], "lfs")
        lfs_sha = field(lfs, "sha256") if lfs is not None else None
        blob_id = field(remote[name], "blob_id") or field(remote[name], "blobId")
        if lfs_sha:
            observed, kind = sha256(local), "lfs_sha256"
            expected = str(lfs_sha)
        elif blob_id:
            observed, kind = git_blob(local), "git_blob_sha1"
            expected = str(blob_id)
        else:
            raise VerificationError(f"HF supplied no content identity for {name}")
        if observed != expected:
            raise VerificationError(f"content mismatch: {local}")
        checked.append({"name": name, "bytes": local.stat().st_size, kind: observed})
    marker = path / "nla_source_receipt.json"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path, prefix=marker.name + ".",
        suffix=".tmp", delete=False,
    ) as stream:
        json.dump(source, stream, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(marker)
    return {
        "directory": str(path), "source": source,
        "verified_files": len(checked), "verified_weight_files": len(weights),
        "verified_bytes": sum(row["bytes"] for row in checked), "files": checked,
        "marker_sha256": {
            name: sha256(path / name) for name in sorted(remote)
            if name in MARKERS
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--models-root", type=Path, default=Path("/workspace/models"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = load_bundle()
    sources = bundle["campaign"]["models"][args.model_key]["checkpoints"]
    reports = {}
    for role in ("base", "av", "ar"):
        reports[role] = verify_one(
            args.models_root / DIRS[args.model_key][role], sources[role],
            os.environ.get("HF_TOKEN") or None,
        )
        print(f"verified existing {args.model_key}/{role}: "
              f"{reports[role]['verified_bytes'] / 1e9:.2f} GB")
    payload = {
        "schema": "axb44-existing-model-verification/v1",
        "campaign_id": bundle["campaign"]["campaign_id"],
        "review_payload_sha256": bundle["review_payload_sha256"],
        "model_key": args.model_key, "network_model_downloads": 0,
        "models_root": str(args.models_root), "checkpoints": reports,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=args.output.parent,
        prefix=args.output.name + ".", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(args.output)
    print(f"wrote {args.output}; no model content downloaded")


if __name__ == "__main__":
    try:
        main()
    except VerificationError as exc:
        raise SystemExit(f"persistent model verification failed: {exc}") from exc
