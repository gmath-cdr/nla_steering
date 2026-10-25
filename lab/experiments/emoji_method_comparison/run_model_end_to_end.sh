#!/usr/bin/env bash
# Run one frozen model stratum end to end from existing persistent checkpoints.
# This script never invokes snapshot_download. A laptop-side controller must
# stop the pod in a finally block whether this script succeeds or fails.
set -euo pipefail

cd "$(dirname "$0")/../.."

case "${NLA_MODEL:-}" in
  qwen|gemma3-12b) ;;
  *) echo "set NLA_MODEL=qwen or gemma3-12b" >&2; exit 1 ;;
esac

if [[ "${NLA_EXTERNAL_GUARD_CONFIRMED:-}" != "1" ]]; then
  echo "external lifecycle controller is not confirmed" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "worktree must be clean" >&2
  exit 1
fi

export NLA_MODELS_DIR="${NLA_MODELS_DIR:-/workspace/models}"
export NLA_PYTHON="${NLA_PYTHON:-/workspace/venvs/nla/bin/python}"
RUN_ROOT="${EMOJI_METHOD_RUN_ROOT:-/workspace/nla-artifacts/emoji-method-comparison/${NLA_MODEL}-emoji-v4-end-to-end-01}"
mkdir -p "$RUN_ROOT"

status_file="$RUN_ROOT/RUN_STATUS.json"
finish_status() {
  code=$?
  if [[ $code -eq 0 ]]; then status="complete"; else status="failed"; fi
  printf '{"status":"%s","exit_code":%d,"model_key":"%s","finished_utc":"%s"}\n' \
    "$status" "$code" "$NLA_MODEL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$status_file.tmp"
  mv "$status_file.tmp" "$status_file"
}
trap finish_status EXIT

echo "== persistent storage inventory (no downloads) =="
df -h /workspace
du -sh "$NLA_MODELS_DIR"/* 2>/dev/null || true

if [[ ! -x "$NLA_PYTHON" ]]; then
  python3 -m venv --system-site-packages "$(dirname "$(dirname "$NLA_PYTHON")")"
fi
"$NLA_PYTHON" -m pip install -q -r requirements.txt
"$NLA_PYTHON" -m pip check

echo "== verify pinned persistent model bytes; download path is forbidden =="
"$NLA_PYTHON" -u -m experiments.emoji_method_comparison.verify_persistent_models \
  --model-key "$NLA_MODEL" --models-root "$NLA_MODELS_DIR" \
  --output "$RUN_ROOT/model_verification.json"

export NLA_EXTERNAL_GUARD_CONFIRMED=1
export EMOJI_METHOD_ARTIFACT_DIR="$RUN_ROOT/build"
if [[ -f "$RUN_ROOT/build/directions.npz" && -f "$RUN_ROOT/build/direction_build_report.json" ]]; then
  "$NLA_PYTHON" -m experiments.emoji_method_comparison.validate_build \
    "$RUN_ROOT/build" --model-key "$NLA_MODEL"
else
  bash experiments/emoji_method_comparison/build_on_pod.sh
fi

echo "== excluded two-record canary =="
"$NLA_PYTHON" -m experiments.emoji_method_comparison.gen_specs canary \
  --model-key "$NLA_MODEL" --build-dir "$RUN_ROOT/build" \
  --output-dir "$RUN_ROOT/specs/canary"
"$NLA_PYTHON" experiments/run_specs.py "$RUN_ROOT/specs/canary" \
  --output-dir "$RUN_ROOT/results/canary" --models-root "$NLA_MODELS_DIR" --resume
"$NLA_PYTHON" -m experiments.emoji_method_comparison.validate_canary \
  --model-key "$NLA_MODEL" --results-dir "$RUN_ROOT/results/canary"

echo "== frozen calibration =="
"$NLA_PYTHON" -m experiments.emoji_method_comparison.gen_specs calibration \
  --model-key "$NLA_MODEL" --build-dir "$RUN_ROOT/build" \
  --output-dir "$RUN_ROOT/specs/calibration"
"$NLA_PYTHON" experiments/run_specs.py "$RUN_ROOT/specs/calibration" \
  --output-dir "$RUN_ROOT/results/calibration" --models-root "$NLA_MODELS_DIR" --resume
"$NLA_PYTHON" -m experiments.emoji_method_comparison.select_alpha \
  --model-key "$NLA_MODEL" --results-dir "$RUN_ROOT/results/calibration" \
  --output "$RUN_ROOT/alpha_receipt.json"

echo "== one-shot held-out family in resumable five-prompt chunks =="
"$NLA_PYTHON" -m experiments.emoji_method_comparison.gen_specs heldout \
  --model-key "$NLA_MODEL" --build-dir "$RUN_ROOT/build" \
  --alpha-receipt "$RUN_ROOT/alpha_receipt.json" \
  --output-dir "$RUN_ROOT/specs/heldout"
"$NLA_PYTHON" experiments/run_specs.py "$RUN_ROOT/specs/heldout" \
  --output-dir "$RUN_ROOT/results/heldout" --models-root "$NLA_MODELS_DIR" --resume

echo "== locked analysis and blinded quality sheet =="
"$NLA_PYTHON" -m experiments.emoji_method_comparison.evaluate \
  --model-key "$NLA_MODEL" --results-dir "$RUN_ROOT/results/heldout" \
  --alpha-receipt "$RUN_ROOT/alpha_receipt.json" \
  --output "$RUN_ROOT/heldout_summary.json" \
  --rating-sheet "$RUN_ROOT/instruction_quality_blinded.csv" \
  --rating-key "$RUN_ROOT/instruction_quality_key.json"
"$NLA_PYTHON" -m experiments.emoji_method_comparison.finalize_run \
  --model-key "$NLA_MODEL" --run-root "$RUN_ROOT"

echo "END-TO-END COMPLETE: $RUN_ROOT"
