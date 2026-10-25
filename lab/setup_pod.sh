#!/usr/bin/env bash
# One-time GPU pod setup. Run from the lab/ directory:
#
#     bash setup_pod.sh
#
# Installs Python dependencies, keeps canonical checkpoint copies on the
# persistent volume, and stages a hot copy on container-local disk. Direct
# model loading from the network volume stalled during the 2026-08-24 A100
# canary; /root/models loaded all three checkpoints in seconds.
# Safe to re-run: downloads and rsync both resume/skip matching files.
set -euo pipefail

# Caches and canonical checkpoints persist. Runtime checkpoints are copied to
# fast local disk and are disposable when the pod is terminated.
export HF_HOME="${HF_HOME:-/workspace/hf}"
PERSISTED_MODELS_ROOT="${NLA_PERSISTED_MODELS_DIR:-/workspace/models}"
HOT_MODELS_ROOT="${NLA_MODELS_DIR:-/root/models}"
mkdir -p "$PERSISTED_MODELS_ROOT" "$HOT_MODELS_ROOT"

echo "== python environment (venv on container disk, reusing the image's torch) =="
# Ubuntu 24.04 images mark system python externally-managed (PEP 668), so
# pip refuses to install system-wide. The venv lives on the pod's LOCAL
# disk, not the network volume: mfs chokes on pip's thousands of small
# files (slow, and I/O errors). A terminated pod loses it; rerunning this
# script rebuilds it in minutes. Models stay on the volume.
if [ ! -f /venv/bin/activate ]; then
    python3 -m venv --system-site-packages /venv
fi
source /venv/bin/activate

echo "== installing python packages =="
pip install -q -r requirements.txt
python -m pip check

echo "== canonical checkpoints: $PERSISTED_MODELS_ROOT (base ~15GB, AV ~15GB, AR ~10GB) =="
export NLA_PERSISTED_MODELS_DIR="$PERSISTED_MODELS_ROOT"
python - <<'PY'
import os
from huggingface_hub import snapshot_download

repos = {
    "qwen2.5-7b-instruct": "Qwen/Qwen2.5-7B-Instruct",
    "nla-av": "kitft/nla-qwen2.5-7b-L20-av",   # verbalizer: vector -> sentence
    "nla-ar": "kitft/nla-qwen2.5-7b-L20-ar",   # reconstructor: sentence -> vector
}
root = os.environ.get("NLA_PERSISTED_MODELS_DIR", "/workspace/models")
for name, repo in repos.items():
    dest = os.path.join(root, name)
    print(f"  {repo}  ->  {dest}")
    # full snapshot: also fetches nla_meta.yaml and value_head.safetensors,
    # which plain from_pretrained() would NOT download
    snapshot_download(repo, local_dir=dest)
print("downloads complete")
PY

echo "== staging checkpoints on local disk: $HOT_MODELS_ROOT =="
if ! command -v rsync >/dev/null 2>&1; then
    echo "rsync is required to stage model checkpoints" >&2
    exit 1
fi
rsync -a --info=progress2 "$PERSISTED_MODELS_ROOT/" "$HOT_MODELS_ROOT/"

echo "== checkpoint marker verification =="
for relative in \
    qwen2.5-7b-instruct/config.json \
    qwen2.5-7b-instruct/tokenizer.json \
    nla-av/config.json \
    nla-ar/config.json; do
    persisted_hash="$(sha256sum "$PERSISTED_MODELS_ROOT/$relative" | cut -d' ' -f1)"
    hot_hash="$(sha256sum "$HOT_MODELS_ROOT/$relative" | cut -d' ' -f1)"
    if [[ "$persisted_hash" != "$hot_hash" ]]; then
        echo "marker mismatch after local staging: $relative" >&2
        exit 1
    fi
done
echo "   canonical and hot markers match"

echo "== disk usage =="
df -h /workspace | tail -1
df -h /root | tail -1
du -sh "$PERSISTED_MODELS_ROOT" "$HOT_MODELS_ROOT" 2>/dev/null || true

echo
echo "done. next steps:"
echo "  export NLA_MODEL=qwen NLA_MODELS_DIR=$HOT_MODELS_ROOT"
echo "  python smoke_test.py                     # verify the whole loop"
echo "  python experiments/repro_rhyme.py        # paper reproduction"
echo "  jupyter: open playground.ipynb           # interactive loop"
