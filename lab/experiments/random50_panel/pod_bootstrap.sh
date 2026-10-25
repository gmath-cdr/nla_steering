#!/usr/bin/env bash
# Prepare a fresh RunPod pod for run_shard.sh. Idempotent.
#
# The virtual environment and model cache live on the pod-local disk, which is
# fast and is wiped when the pod stops. Nothing here writes to the shared
# network volume.
#
#   TORCH_VERSION  torch version to install, or "image" to keep the image's
#                  CUDA build (default: 2.13.0, the team's recorded version)
#   TORCH_INDEX    wheel index for that install (default: PyPI, whose build
#                  is the CUDA 13 one in the team's recorded environment; it
#                  needs driver 580 or newer)
set -euo pipefail

VENV=${VENV:-/root/venvs/random50}
TORCH_VERSION=${TORCH_VERSION:-2.13.0}
TORCH_INDEX=${TORCH_INDEX:-https://pypi.org/simple}

if [[ ! -x "$VENV/bin/python" ]]; then
  if [[ "$TORCH_VERSION" == "image" ]]; then
    python3 -m venv --system-site-packages "$VENV"
  else
    python3 -m venv "$VENV"
  fi
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
if [[ "$TORCH_VERSION" != "image" ]]; then
  "$VENV/bin/python" -m pip install --quiet "torch==$TORCH_VERSION" --index-url "$TORCH_INDEX"
fi
# Versions recorded by the team's 14-concept run (queue_logs/pip_freeze.txt).
"$VENV/bin/python" -m pip install --quiet \
  transformers==4.57.6 accelerate==1.14.0 tokenizers==0.22.2 safetensors==0.8.0 \
  "huggingface_hub[hf_xet]==0.36.2" pandas==2.3.3 pyarrow==25.0.1 \
  sentencepiece==0.2.2 protobuf==7.35.1 numpy==2.3.5 orjson==3.11.9   httpx==0.28.1 PyYAML==6.0.3

"$VENV/bin/python" - <<'PYEOF'
import torch
import transformers

assert torch.cuda.is_available(), "no CUDA device"
name = torch.cuda.get_device_name(0)
total_gib = torch.cuda.get_device_properties(0).total_memory / 2**30
probe = torch.ones(8, device="cuda", dtype=torch.bfloat16).sum().item()
assert probe == 8.0, "CUDA arithmetic probe failed"
print(f"torch {torch.__version__} transformers {transformers.__version__} gpu {name} {total_gib:.0f} GiB")
assert total_gib >= 70, "this run needs one 80 GB card"
PYEOF

command -v rsync >/dev/null || { apt-get update -qq && apt-get install -y -qq rsync; }
"$VENV/bin/hf" --help >/dev/null
echo "BOOTSTRAP_OK"
