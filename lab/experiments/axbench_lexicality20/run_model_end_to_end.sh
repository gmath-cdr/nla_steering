#!/usr/bin/env bash
# One model stratum from existing persistent checkpoints through exact judging.
# A laptop-side controller MUST stop the pod in a finally block.
set -euo pipefail

cd "$(dirname "$0")/../.."

case "${AXB44_MODEL:-}" in
  qwen) export NLA_MODEL=qwen ;;
  gemma) export NLA_MODEL=gemma3-12b ;;
  *) echo "set AXB44_MODEL=qwen or gemma" >&2; exit 1 ;;
esac

if [[ "${NLA_EXTERNAL_GUARD_CONFIRMED:-}" != "1" ]]; then
  echo "external lifecycle controller is not confirmed" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "worktree must be clean" >&2
  exit 1
fi
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY must be supplied ephemerally by the controller" >&2
  exit 1
fi

export NLA_MODELS_DIR="${NLA_MODELS_DIR:-/workspace/models}"
export NLA_PYTHON="${NLA_PYTHON:-/workspace/venvs/nla/bin/python}"
export AXB44_TRAIN_PARQUET="${AXB44_TRAIN_PARQUET:-/workspace/axb44-source/train_data.parquet}"
export AXB44_CAA_REFERENCE_DIR="${AXB44_CAA_REFERENCE_DIR:-/workspace/axb44-source/caa_refs}"
export CONTRAST_STEERING_ROOT="${CONTRAST_STEERING_ROOT:-/workspace/contrast-steering}"
if [[ -d "$CONTRAST_STEERING_ROOT/.git" ]]; then
  test "$(git -C "$CONTRAST_STEERING_ROOT" rev-parse HEAD)" = "7d88ee6833a27d24b44484ad95df0890317ff39b"
  export PYTHONPATH="$PWD:${CONTRAST_STEERING_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
else
  # The vendored fallback is accepted only if its model-time vector matches
  # the exact archived team CAA vector at cosine >= .999.
  export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
fi
RUN_ROOT="${AXB44_RUN_ROOT:-/workspace/nla-artifacts/axb44/${AXB44_MODEL}-run-01}"
mkdir -p "$RUN_ROOT"

status_file="$RUN_ROOT/RUN_STATUS.json"
finish_status() {
  code=$?
  if [[ $code -eq 0 ]]; then status="complete"; else status="failed"; fi
  printf '{"status":"%s","exit_code":%d,"model_key":"%s","finished_utc":"%s"}\n' \
    "$status" "$code" "$AXB44_MODEL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$status_file.tmp"
  mv "$status_file.tmp" "$status_file"
}
trap finish_status EXIT

echo "== fail-closed environment and persistent storage =="
test -x "$NLA_PYTHON"
test -f "$AXB44_TRAIN_PARQUET"
available_kb=$(df -Pk /workspace | awk 'NR==2 {print $4}')
if [[ -z "$available_kb" || "$available_kb" -lt 10485760 ]]; then
  echo "less than 10 GiB free on persistent storage" >&2
  exit 1
fi
df -h /workspace
"$NLA_PYTHON" -m pip check
"$NLA_PYTHON" -c 'import torch, transformers, pyarrow, pandas, yaml, huggingface_hub; assert torch.cuda.is_available()'

echo "== zero-download exact checkpoint verification =="
"$NLA_PYTHON" -u -m experiments.axbench_lexicality20.verify_models \
  --model-key "$AXB44_MODEL" --models-root "$NLA_MODELS_DIR" \
  --output "$RUN_ROOT/model_verification.json"

echo "== resumable model-specific direction construction =="
"$NLA_PYTHON" -u -m experiments.axbench_lexicality20.build_directions \
  --model-key "$AXB44_MODEL" --models-root "$NLA_MODELS_DIR" \
  --train-parquet "$AXB44_TRAIN_PARQUET" \
  --model-verification "$RUN_ROOT/model_verification.json" \
  --output-dir "$RUN_ROOT/build"

echo "== bounded two-record infrastructure canary =="
if [[ ! -d "$RUN_ROOT/specs/canary" ]]; then
  "$NLA_PYTHON" -m experiments.axbench_lexicality20.gen_specs canary \
    --model-key "$AXB44_MODEL" --model-verification "$RUN_ROOT/model_verification.json" \
    --direction-report "$RUN_ROOT/build/direction_build_report.json" \
    --output-dir "$RUN_ROOT/specs/canary"
fi
"$NLA_PYTHON" experiments/run_specs.py "$RUN_ROOT/specs/canary" \
  --output-dir "$RUN_ROOT/results/canary" --models-root "$NLA_MODELS_DIR" --resume
"$NLA_PYTHON" -m experiments.axbench_lexicality20.validate_canary \
  --results-dir "$RUN_ROOT/results/canary"

echo "== frozen calibration: 920 generations =="
if [[ ! -d "$RUN_ROOT/specs/calibration" ]]; then
  "$NLA_PYTHON" -m experiments.axbench_lexicality20.gen_specs calibration \
    --model-key "$AXB44_MODEL" --model-verification "$RUN_ROOT/model_verification.json" \
    --direction-report "$RUN_ROOT/build/direction_build_report.json" \
    --output-dir "$RUN_ROOT/specs/calibration"
fi
"$NLA_PYTHON" experiments/run_specs.py "$RUN_ROOT/specs/calibration" \
  --output-dir "$RUN_ROOT/results/calibration" --models-root "$NLA_MODELS_DIR" --resume

echo "== exact three-call AxBench calibration judge and sealed alpha receipt =="
"$NLA_PYTHON" -u -m experiments.axbench_lexicality20.judge \
  --results-dir "$RUN_ROOT/results/calibration" --phase calibration \
  --output "$RUN_ROOT/judge_calibration.json" --workers 8
"$NLA_PYTHON" -m experiments.axbench_lexicality20.select_alpha \
  --model-key "$AXB44_MODEL" --judge-artifact "$RUN_ROOT/judge_calibration.json" \
  --output "$RUN_ROOT/alpha_receipt.json"

echo "== untouched evaluation released only by sealed receipt =="
if [[ ! -d "$RUN_ROOT/specs/evaluation" ]]; then
  "$NLA_PYTHON" -m experiments.axbench_lexicality20.gen_specs evaluation \
    --model-key "$AXB44_MODEL" --model-verification "$RUN_ROOT/model_verification.json" \
    --direction-report "$RUN_ROOT/build/direction_build_report.json" \
    --alpha-receipt "$RUN_ROOT/alpha_receipt.json" \
    --output-dir "$RUN_ROOT/specs/evaluation"
fi
"$NLA_PYTHON" experiments/run_specs.py "$RUN_ROOT/specs/evaluation" \
  --output-dir "$RUN_ROOT/results/evaluation" --models-root "$NLA_MODELS_DIR" --resume

echo "== exact held-out AxBench judge and immutable completion manifest =="
"$NLA_PYTHON" -u -m experiments.axbench_lexicality20.judge \
  --results-dir "$RUN_ROOT/results/evaluation" --phase evaluation \
  --output "$RUN_ROOT/judge_evaluation.json" --workers 8
"$NLA_PYTHON" -m experiments.axbench_lexicality20.finalize_run \
  --model-key "$AXB44_MODEL" --run-root "$RUN_ROOT"

echo "END-TO-END COMPLETE: $RUN_ROOT"
