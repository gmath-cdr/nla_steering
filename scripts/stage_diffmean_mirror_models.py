#!/usr/bin/env python3
"""Stage complete pinned inference assets and validate templates before GPU work."""
from pathlib import Path
import hashlib
import json
import shutil
import sys

from huggingface_hub import snapshot_download
from transformers import AutoTokenizer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/experiments/diffmean_self_selection_mirror"
REVISIONS = {
    "target": ("Qwen/Qwen2.5-7B-Instruct", "a09a35458c702b33eeacc393d103063234e8bc28"),
    "av": ("kitft/nla-qwen2.5-7b-L20-av", "b88469162777ae6553bc14208eb0cb579336f8f4"),
    "ar": ("kitft/nla-qwen2.5-7b-L20-ar", "e2c9e57eac213d37a31612087f645ab6332c1bb6"),
}


def main():
    runtime = ROOT / "artifacts/cache/mirror_runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    source = OUT / "provenance/archived-nla_inference.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "1acc8c2793d5278fcf8fd8422e82282c5c2ab9738f9218500ff8c1a2d9e0903b"
    shutil.copyfile(source, runtime / "nla_inference.py")
    sys.path.insert(0, str(runtime))
    from nla_inference import load_nla_config
    models = {}
    for role, (repo, revision) in REVISIONS.items():
        path = snapshot_download(repo, revision=revision, cache_dir=str(ROOT / "artifacts/models/hub"),
            allow_patterns=["*.json", "*.yaml", "*.safetensors", "*.txt", "*.model", "*.py", "*.jinja"],
            max_workers=4)
        tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
        assert tokenizer.chat_template, f"Missing pinned chat template for {role}"
        assert tokenizer.apply_chat_template([{"role": "user", "content": "Test"}],
                                            tokenize=True, add_generation_prompt=True)
        if role == "av":
            load_nla_config(path, tokenizer)
        models[role] = {"repo": repo, "revision": revision, "path": path}
        print(f"{role}: pinned checkpoint and tokenizer preflight passed", flush=True)
    if (OUT / "protocol.json").exists():
        assert json.loads((OUT / "protocol.json").read_text())["models"] == models
    (OUT / "model_paths.json").write_text(json.dumps(models, indent=2) + "\n")


if __name__ == "__main__":
    main()
