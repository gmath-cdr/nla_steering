#!/usr/bin/env bash
# Gemma pod setup: venvs + the gated trio to local disk. HF_TOKEN must be
# exported by the caller (read from secrets on the laptop, never stored here).
set -euo pipefail
echo "=== GSETUP VENV ==="
if [ ! -f /venv/bin/activate ]; then python3 -m venv --system-site-packages /venv; fi
/venv/bin/pip install -q --no-cache-dir -r /workspace/lab/requirements.txt scipy
/venv/bin/python -c 'import transformers, scipy; print("nla venv:", transformers.__version__)'
if [ ! -f /venv_jlens/bin/activate ]; then python3 -m venv --system-site-packages /venv_jlens; fi
cp -r /workspace/jacobian-lens /root/jacobian-lens 2>/dev/null || true
if [ -d /root/jacobian-lens ]; then /venv_jlens/bin/pip install -q --no-cache-dir /root/jacobian-lens accelerate scipy 2>&1 | tail -1; fi
echo "=== GSETUP MODELS ==="
mkdir -p /root/models
/venv/bin/python - <<'PY'
import os
from huggingface_hub import snapshot_download
token = os.environ.get("HF_TOKEN")
assert token, "HF_TOKEN missing"
for repo, dest in [("google/gemma-3-12b-it", "gemma-3-12b-it"),
                   ("kitft/nla-gemma3-12b-L32-av", "nla-gemma3-12b-av"),
                   ("kitft/nla-gemma3-12b-L32-ar", "nla-gemma3-12b-ar")]:
    print("downloading", repo, flush=True)
    snapshot_download(repo, local_dir="/root/models/" + dest, token=token)
print("models ready", flush=True)
PY
df -h / | tail -1
echo "=== GSETUP COMPLETE ==="
