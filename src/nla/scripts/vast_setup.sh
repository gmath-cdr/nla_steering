#!/usr/bin/env bash
# Bootstrap a one-80GB-A100 Vast.ai instance for native NLA steering runs.
#
# This script deliberately never stores a Hugging Face token. Authenticate with
# `hf auth login` (and accept the Gemma licence, when applicable) before use.
# Example:
#   bash scripts/vast_setup.sh --profile qwen2.5-7b --workspace /workspace/nla
#
# Re-running is safe: Hugging Face resumes existing local directories and an
# already healthy AV server is left running.
set -euo pipefail

PROFILE=""
WORKSPACE="/workspace/nla"
GPU="0"
PORT="30000"
SKIP_INSTALL=0
SKIP_DOWNLOAD=0
SKIP_SERVER=0
SKIP_PREFLIGHT=0
STOP_SERVER=0
SKIP_JLENS=0
RUN_AV_EDIT_SMOKE=0
JLENS_DIR="/workspace/steering/jacobian-lens"
METADATA=""
MAPPING=""
SGLANG_EXTRA_ARGS=()

usage() {
  cat <<'EOF'
Usage: bash scripts/vast_setup.sh --profile {qwen2.5-7b|gemma3-12b} [options]

Options:
  --workspace PATH       Persistent Vast volume location (default: /workspace/nla)
  --gpu INDEX            Physical GPU index to reserve (default: 0)
  --port PORT            SGLang AV server port (default: 30000)
  --skip-install         Do not install Python dependencies
  --skip-download        Do not download Hugging Face checkpoints
  --skip-server          Do not launch the SGLang AV server
  --skip-preflight       Do not run the experiment preflight check
  --skip-jlens           Do not install the already-cloned Jacobian Lens checkout
  --jlens-dir PATH       Jacobian Lens checkout (default: /workspace/steering/jacobian-lens)
  --run-av-edit-smoke    Run the one-concept AV-edit smoke after setup
  --metadata PATH        AxBench metadata JSONL (required with --run-av-edit-smoke)
  --mapping PATH         AxBench prompt mapping JSONL (required with --run-av-edit-smoke)
  --stop-server          Stop this profile's previously launched AV server and exit
  -h, --help             Show this help

Prerequisites:
  * One idle 80GB A100 and a persistent volume with enough free space.
  * `hf auth login` completed; Gemma users must accept its Hugging Face licence.
  * Run from the root of this repository in a PyTorch Vast template.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --gpu) GPU="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --skip-download) SKIP_DOWNLOAD=1; shift ;;
    --skip-server) SKIP_SERVER=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    --skip-jlens) SKIP_JLENS=1; shift ;;
    --jlens-dir) JLENS_DIR="$2"; shift 2 ;;
    --run-av-edit-smoke) RUN_AV_EDIT_SMOKE=1; shift ;;
    --metadata) METADATA="$2"; shift 2 ;;
    --mapping) MAPPING="$2"; shift 2 ;;
    --stop-server) STOP_SERVER=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$PROFILE" ]] || { echo "--profile is required" >&2; usage >&2; exit 2; }
if [[ "$RUN_AV_EDIT_SMOKE" -eq 1 ]] && [[ -z "$METADATA" || -z "$MAPPING" ]]; then
  echo "--run-av-edit-smoke requires --metadata and --mapping." >&2
  exit 2
fi

case "$PROFILE" in
  qwen2.5-7b)
    TARGET_REPO="Qwen/Qwen2.5-7B-Instruct"
    AR_REPO="kitft/nla-qwen2.5-7b-L20-ar"
    AV_REPO="kitft/nla-qwen2.5-7b-L20-av"
    LAYER="20"
    MIN_FREE_GB="110"
    ;;
  gemma3-12b)
    TARGET_REPO="google/gemma-3-12b-it"
    AR_REPO="kitft/nla-gemma3-12b-L32-ar"
    AV_REPO="kitft/nla-gemma3-12b-L32-av"
    LAYER="32"
    MIN_FREE_GB="150"
    # SGLang's default breakable prefill CUDA graph currently crashes Gemma 3
    # on ragged prefill with q/qo_indptr shape mismatches. Decode graphs remain
    # enabled; only the incompatible prefill graph is disabled.
    SGLANG_EXTRA_ARGS+=(--cuda-graph-backend-prefill disabled)
    ;;
  *) echo "Unsupported profile: $PROFILE" >&2; exit 2 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

need() { command -v "$1" >/dev/null || { echo "Missing command: $1" >&2; exit 1; }; }
need python
need nvidia-smi

if [[ "$STOP_SERVER" -eq 1 ]]; then
  PID_FILE="$WORKSPACE/logs/$PROFILE/sglang-av.pid"
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")"
    echo "Stopped $PROFILE AV server (PID $(cat "$PID_FILE"))."
  else
    echo "No running $PROFILE AV server recorded at $PID_FILE."
  fi
  rm -f "$PID_FILE" "$WORKSPACE/sglang-port-$PORT.model"
  exit 0
fi

TOTAL_MIB="$(nvidia-smi -i "$GPU" --query-gpu=memory.total --format=csv,noheader,nounits | tr -d ' ')"
FREE_MIB="$(nvidia-smi -i "$GPU" --query-gpu=memory.free --format=csv,noheader,nounits | tr -d ' ')"
if [[ "$TOTAL_MIB" -lt 70000 ]]; then
  echo "GPU $GPU has ${TOTAL_MIB} MiB total VRAM; this script requires one 80GB GPU." >&2
  echo "Rent one A100 80GB (PCIe or SXM), or adapt the experiment for multi-GPU first." >&2
  exit 1
fi
if [[ "$FREE_MIB" -lt 65000 ]]; then
  echo "GPU $GPU has only ${FREE_MIB} MiB free; select an idle GPU with --gpu." >&2
  exit 1
fi

mkdir -p "$WORKSPACE"
FREE_GB="$(df -Pk "$WORKSPACE" | awk 'NR==2 {print int($4/1024/1024)}')"
if [[ "$SKIP_DOWNLOAD" -eq 0 && "$FREE_GB" -lt "$MIN_FREE_GB" ]]; then
  echo "Only ${FREE_GB} GB free at $WORKSPACE; $PROFILE needs at least ${MIN_FREE_GB} GB." >&2
  exit 1
fi

export CUDA_VISIBLE_DEVICES="$GPU"
# Redirect model caches without moving the token that hf auth login wrote.
# huggingface_hub otherwise derives the token path from the new HF_HOME and the
# authenticated session appears to vanish.
if [[ -z "${HF_TOKEN_PATH:-}" ]]; then
  if [[ -n "${HF_HOME:-}" ]]; then
    HF_TOKEN_PATH="$HF_HOME/token"
  elif [[ -n "${XDG_CACHE_HOME:-}" ]]; then
    HF_TOKEN_PATH="$XDG_CACHE_HOME/huggingface/token"
  else
    HF_TOKEN_PATH="$HOME/.cache/huggingface/token"
  fi
fi
export HF_TOKEN_PATH
export HF_HOME="$WORKSPACE/hf-cache"
export HF_XET_HIGH_PERFORMANCE=1
export HF_XET_NUM_CONCURRENT_RANGE_GETS=32
export TOKENIZERS_PARALLELISM=false

MODELS="$WORKSPACE/models/$PROFILE"
TARGET="$MODELS/target"
AR="$MODELS/ar"
AV="$MODELS/av"
RESULTS="$WORKSPACE/results/$PROFILE"
VECTORS="$WORKSPACE/vectors/$PROFILE"
CHECKPOINTS="$WORKSPACE/checkpoints/$PROFILE"
LOGS="$WORKSPACE/logs/$PROFILE"
SERVER_MODEL_FILE="$WORKSPACE/sglang-port-$PORT.model"
mkdir -p "$HF_HOME" "$TARGET" "$AR" "$AV" "$RESULTS" "$VECTORS" "$CHECKPOINTS" "$LOGS"

cat > "$WORKSPACE/${PROFILE}.env" <<EOF
export CUDA_VISIBLE_DEVICES=$GPU
export HF_HOME=$HF_HOME
export HF_TOKEN_PATH=$HF_TOKEN_PATH
export HF_XET_HIGH_PERFORMANCE=1
export HF_XET_NUM_CONCURRENT_RANGE_GETS=32
export NLA_TARGET=$TARGET
export NLA_AR=$AR
export NLA_AV=$AV
export NLA_RESULTS=$RESULTS
export NLA_VECTORS=$VECTORS
export NLA_CHECKPOINTS=$CHECKPOINTS
EOF

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  python -m pip install --upgrade pip
  python -m pip install -e . 'sglang[all]>=0.5.6' 'huggingface_hub[hf_xet]>=0.32.0'
fi

if [[ "$SKIP_JLENS" -eq 0 ]]; then
  [[ -d "$JLENS_DIR" ]] || { echo "Jacobian Lens checkout not found: $JLENS_DIR" >&2; exit 1; }
  python -m pip install -e "$JLENS_DIR"
fi

if [[ "$SKIP_DOWNLOAD" -eq 0 ]]; then
  need hf
  if ! hf auth whoami >/dev/null 2>&1; then
    echo "Hugging Face is not authenticated. Run: hf auth login" >&2
    exit 1
  fi
  download() {
    local repo="$1" destination="$2"
    echo "Downloading $repo -> $destination"
    hf download "$repo" --local-dir "$destination"
  }
  download "$TARGET_REPO" "$TARGET"
  download "$AR_REPO" "$AR"
  download "$AV_REPO" "$AV"
fi

health() {
  python - "$PORT" <<'PY'
import sys
import urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=2) as response:
        raise SystemExit(0 if 200 <= response.status < 300 else 1)
except Exception:
    raise SystemExit(1)
PY
}

if [[ "$SKIP_SERVER" -eq 0 ]]; then
  if health; then
    if [[ -f "$SERVER_MODEL_FILE" ]] && [[ "$(cat "$SERVER_MODEL_FILE")" == "$AV" ]]; then
      echo "SGLang already healthy on port $PORT for $PROFILE"
    else
      echo "Port $PORT already serves an unknown or different SGLang model." >&2
      echo "Stop it first, for example:" >&2
      echo "  bash scripts/vast_setup.sh --profile qwen2.5-7b --workspace $WORKSPACE --port $PORT --stop-server" >&2
      exit 1
    fi
  else
    echo "Starting SGLang AV server; log: $LOGS/sglang-av.log"
    nohup python -m sglang.launch_server \
      --model-path "$AV" --port "$PORT" --disable-radix-cache --trust-remote-code \
      --mem-fraction-static 0.35 \
      "${SGLANG_EXTRA_ARGS[@]}" \
      > "$LOGS/sglang-av.log" 2>&1 &
    echo $! > "$LOGS/sglang-av.pid"
    for _ in $(seq 1 90); do
      health && break
      sleep 2
    done
    health || { tail -n 80 "$LOGS/sglang-av.log" >&2; exit 1; }
    printf '%s\n' "$AV" > "$SERVER_MODEL_FILE"
  fi
fi

if [[ "$SKIP_PREFLIGHT" -eq 0 ]]; then
  python -m experiments.preflight --target "$TARGET" --av "$AV" --ar "$AR" \
    --sglang-url "http://127.0.0.1:$PORT" --require-cuda
fi

if [[ "$RUN_AV_EDIT_SMOKE" -eq 1 ]]; then
  SMOKE_OUT="$RESULTS/axbench_av_edit_c3_smoke.json"
  python -m experiments.runner axbench-av-edit \
    --target "$TARGET" --av "$AV" --ar "$AR" --layer "$LAYER" \
    --metadata "$METADATA" --mapping "$MAPPING" --concept-ids 3 \
    --n-prompts 3 --n-samples 3 --alphas 0.25,0.5 \
    --random-seeds 17,42,101,314,2718 --resume --out "$SMOKE_OUT"
fi

cat <<EOF

Ready: $PROFILE (layer $LAYER)
Load this environment in future terminals with:
  source "$WORKSPACE/${PROFILE}.env"

Qwen proofpoint:
  python -m experiments.runner reward --target "$TARGET" --av "$AV" --ar "$AR" \\
    --layer $LAYER --alphas 0.5,1.0 --n-samples 20 --out "$RESULTS/reward.json"
EOF
