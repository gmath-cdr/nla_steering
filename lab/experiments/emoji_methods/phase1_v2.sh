#!/usr/bin/env bash
# Wave 2 Phase 1: environment, models, target-relevant singles, faithful
# decomposition. Run pod-side via nohup; logs speak in markers.
set -euo pipefail
echo "=== PHASE1 ENV ==="
if [ ! -f /venv/bin/activate ]; then python3 -m venv --system-site-packages /venv; fi
/venv/bin/pip install -q --no-cache-dir -r /workspace/lab/requirements.txt scipy
/venv/bin/python -c 'import transformers, scipy; print("venv ok:", transformers.__version__, "scipy", scipy.__version__)'
echo "=== PHASE1 MODELS ==="
mkdir -p /root/models
/venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
for repo, dest in [("Qwen/Qwen2.5-7B-Instruct", "qwen2.5-7b-instruct"),
                   ("kitft/nla-qwen2.5-7b-L20-av", "nla-av"),
                   ("kitft/nla-qwen2.5-7b-L20-ar", "nla-ar")]:
    print("downloading", repo, flush=True)
    snapshot_download(repo, local_dir="/root/models/" + dest)
print("models ready", flush=True)
PY
echo "=== PHASE1 SINGLES ==="
cd /workspace/lab
NLA_MODELS_DIR=/root/models /venv/bin/python -u experiments/emoji_methods/build_singles_v2.py
echo "=== PHASE1 DECOMPOSE ==="
EMOJIMX_BASE=/root/models/qwen2.5-7b-instruct /venv/bin/python -u experiments/emoji_methods/decompose_v2.py
echo "=== PHASE1 COMPLETE ==="
