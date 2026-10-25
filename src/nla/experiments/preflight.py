"""Fail fast on missing dependencies, checkpoints, CUDA, or SGLang service.

Run before a GPU allocation:
    python -m experiments.preflight --target /models/qwen --av /models/qwen-av \
      --ar /models/qwen-ar --sglang-url http://localhost:30000
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}{': ' + detail if detail else ''}")
    return condition


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--av", required=True)
    parser.add_argument("--ar", required=True)
    parser.add_argument("--sglang-url", default="http://localhost:30000")
    parser.add_argument("--require-cuda", action="store_true")
    args = parser.parse_args(argv)
    ok = True
    for package in ("torch", "accelerate", "transformers", "safetensors", "httpx", "yaml", "numpy"):
        try:
            importlib.import_module(package)
            ok &= check(f"import {package}", True)
        except ImportError as exc:
            ok &= check(f"import {package}", False, str(exc))
    for name, directory in (("target", args.target), ("AV", args.av), ("AR", args.ar)):
        path = Path(directory)
        ok &= check(f"{name} checkpoint", path.is_dir(), str(path))
        if path.is_dir():
            ok &= check(f"{name} config", (path / "config.json").exists())
            if name in ("AV", "AR"):
                ok &= check(f"{name} sidecar", (path / "nla_meta.yaml").exists())
    try:
        import torch
        cuda = torch.cuda.is_available()
        detail = torch.cuda.get_device_name(0) if cuda else "no CUDA device"
        ok &= check("CUDA", cuda or not args.require_cuda, detail)
    except ImportError:
        ok = False
    try:
        import httpx
        response = httpx.get(args.sglang_url.rstrip("/") + "/health", timeout=5)
        ok &= check("SGLang health", response.is_success, str(response.status_code))
    except Exception as exc:
        ok &= check("SGLang health", False, str(exc))
    print("PRE-FLIGHT PASSED" if ok else "PRE-FLIGHT FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
