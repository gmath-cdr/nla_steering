"""Verify a persistent base checkpoint from the authenticated prior receipt.

The sensitivity run needs only the target model because every steering vector
already exists in the sealed direction archive.  This verifier hashes the
complete base checkpoint against the original AxB44 receipt without contacting
Hugging Face and without loading or rebuilding the AV/AR checkpoints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

from experiments.axbench_lexicality20.lexical_select_alpha import (
    EXPECTED_CAMPAIGN_ID,
    EXPECTED_REVIEW_SHA256,
    EXPECTED_SOURCE,
    file_sha256,
)


MODEL_DIRS = {"qwen": "qwen2.5-7b-instruct", "gemma": "gemma-3-12b-it"}


class BaseVerificationError(RuntimeError):
    """Raised when the persistent checkpoint differs from the prior run."""


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaseVerificationError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BaseVerificationError(f"receipt root is not a mapping: {path}")
    return value


def _git_blob(path: Path) -> str:
    digest = hashlib.sha1()  # noqa: S324 - verifying Git object identity
    digest.update(f"blob {path.stat().st_size}\0".encode("ascii"))
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(
    *, model_key: str, source_run_root: Path, models_root: Path
) -> dict[str, Any]:
    complete_path = source_run_root / "COMPLETE.json"
    if file_sha256(complete_path) != EXPECTED_SOURCE[model_key]["complete_sha256"]:
        raise BaseVerificationError("source COMPLETE hash mismatch")
    complete = _load(complete_path)
    if (
        complete.get("status") != "complete"
        or complete.get("campaign_id") != EXPECTED_CAMPAIGN_ID
        or complete.get("review_payload_sha256") != EXPECTED_REVIEW_SHA256
        or complete.get("model_key") != model_key
    ):
        raise BaseVerificationError("source COMPLETE identity mismatch")
    relative = "model_verification.json"
    registered = complete.get("files", {}).get(relative)
    source_verification = source_run_root / relative
    if (
        not isinstance(registered, dict)
        or file_sha256(source_verification) != registered.get("sha256")
        or source_verification.stat().st_size != int(registered.get("bytes", -1))
    ):
        raise BaseVerificationError("source model verification is not authenticated")
    source = _load(source_verification)
    base = source.get("checkpoints", {}).get("base", {})
    if source.get("model_key") != model_key or source.get("network_model_downloads") != 0:
        raise BaseVerificationError("source model verification identity mismatch")

    model_dir = models_root / MODEL_DIRS[model_key]
    if not model_dir.is_dir() or model_dir.is_symlink():
        raise BaseVerificationError(f"persistent base checkpoint is absent: {model_dir}")
    checked: list[dict[str, Any]] = []
    for row in base.get("files", []):
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise BaseVerificationError("malformed source checkpoint file receipt")
        path = model_dir / row["name"]
        if not path.is_file() or path.stat().st_size != int(row.get("bytes", -1)):
            raise BaseVerificationError(f"checkpoint file missing or wrong size: {path}")
        if "lfs_sha256" in row:
            observed = file_sha256(path)
            expected = row["lfs_sha256"]
            identity = "lfs_sha256"
        elif "git_blob_sha1" in row:
            observed = _git_blob(path)
            expected = row["git_blob_sha1"]
            identity = "git_blob_sha1"
        else:
            raise BaseVerificationError(f"no content identity for checkpoint file: {path}")
        if observed != expected:
            raise BaseVerificationError(f"checkpoint content mismatch: {path}")
        checked.append(
            {"name": row["name"], "bytes": path.stat().st_size, identity: observed}
        )
    if not checked or len(checked) != int(base.get("verified_files", -1)):
        raise BaseVerificationError("checkpoint file count differs from source receipt")
    if sum(int(row["bytes"]) for row in checked) != int(base.get("verified_bytes", -1)):
        raise BaseVerificationError("checkpoint byte count differs from source receipt")
    return {
        "schema": "axb44-lexical-alpha-base-verification/v1",
        "status": "verified",
        "model_key": model_key,
        "model_directory": str(model_dir),
        "source_complete_sha256": EXPECTED_SOURCE[model_key]["complete_sha256"],
        "source_model_verification_sha256": registered["sha256"],
        "source_revision": base.get("source", {}).get("revision"),
        "verified_files": len(checked),
        "verified_bytes": sum(int(row["bytes"]) for row in checked),
        "files": checked,
        "network_model_downloads": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", choices=("qwen", "gemma"), required=True)
    parser.add_argument("--source-run-root", type=Path, required=True)
    parser.add_argument("--models-root", type=Path, default=Path("/workspace/models"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = verify(
            model_key=args.model_key,
            source_run_root=args.source_run_root.resolve(),
            models_root=args.models_root.resolve(),
        )
    except BaseVerificationError as exc:
        raise SystemExit(f"base checkpoint verification failed: {exc}") from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=args.output.parent,
        prefix=args.output.name + ".",
        suffix=".tmp",
        delete=False,
    ) as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(args.output)
    print(
        f"verified {args.model_key} base: {payload['verified_files']} files / "
        f"{payload['verified_bytes'] / 1e9:.2f} GB; zero downloads"
    )


if __name__ == "__main__":
    main()
