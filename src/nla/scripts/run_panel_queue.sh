#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
BUNDLE_ROOT=$(CDPATH= cd -- "$ROOT/../.." && pwd)
ENV=${NLA_VENV:-$BUNDLE_ROOT/.venv}
CONTRAST_SRC=$BUNDLE_ROOT/src/contrast_steering/src
AXB=$BUNDLE_ROOT/src/axbench/axbench/concept500/prod_9b_l20_v1
OUT=${NLA_OUTPUT_DIR:-$BUNDLE_ROOT/artifacts/rebuilt/baseline_panel14}
LOG=$OUT/queue_logs
IDS=116,222,446,11,359,406,216,287,354,30,172,171,186,90
mkdir -p "$LOG"

event() {
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG/queue.log"
  $ENV/bin/python "$ROOT/scripts/write_queue_status.py" --log-dir "$LOG" --event "$*"
}
run_stage() {
  name=$1; shift
  donefile=$LOG/$name.done
  if test -f "$donefile"; then event "SKIP $name already complete"; return; fi
  attempt=1
  code=1
  while test "$attempt" -le 3; do
    event "START $name attempt=$attempt"
    set +e
    "$@" >"$LOG/$name.stdout.attempt$attempt.log" 2>"$LOG/$name.stderr.attempt$attempt.log"
    code=$?
    set -e
    test "$code" -eq 0 && break
    event "RETRY $name attempt=$attempt exit=$code"
    attempt=$((attempt + 1))
    sleep 30
  done
  printf '%s\n' "$code" >"$LOG/$name.exit_code"
  if test "$code" -ne 0; then
    date -u +%FT%TZ >"$LOG/$name.failed"
    event "FAILED $name exit=$code CONTINUING_QUEUE"
    return 0
  fi
  rm -f "$LOG/$name.failed"
  cp "$LOG/$name.stdout.attempt$attempt.log" "$LOG/$name.stdout.log"
  cp "$LOG/$name.stderr.attempt$attempt.log" "$LOG/$name.stderr.log"
  date -u +%FT%TZ >"$donefile"
  event "DONE $name"
}

event "QUEUE_START"
cp "$BUNDLE_ROOT/sources.lock" "$LOG/sources.lock"
$ENV/bin/pip freeze >"$LOG/pip_freeze.txt"
nvidia-smi -q >"$LOG/nvidia_smi.txt"

event "WAIT qwen_baseline"
while ! test -f "$OUT/qwen/manifest.json"; do
  test -f "$LOG/qwen_baseline.failed" && break
  if ! pgrep -f 'run_panel_baselines.py --model-key qwen' >/dev/null 2>&1; then
    event "RELAUNCH qwen_baseline after missing process"
    run_stage qwen_baseline env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$CONTRAST_SRC" \
      $ENV/bin/python "$ROOT/experiments/run_panel_baselines.py" --model-key qwen --output "$OUT/qwen"
  fi
  sleep 30
done
date -u +%FT%TZ >"$LOG/qwen_baseline.done"
event "DONE qwen_baseline"
run_stage qwen_baseline_verify $ENV/bin/python "$ROOT/scripts/verify_panel_stage.py" baseline "$OUT/qwen"

run_stage qwen_av_download $ENV/bin/hf download kitft/nla-qwen2.5-7b-L20-av
run_stage qwen_ar_download $ENV/bin/hf download kitft/nla-qwen2.5-7b-L20-ar
QAV=$(tail -1 "$LOG/qwen_av_download.stdout.log" 2>/dev/null || true)
QAR=$(tail -1 "$LOG/qwen_ar_download.stdout.log" 2>/dev/null || true)
run_stage qwen_nla_calibration_vectors env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$ROOT" $ENV/bin/python -m experiments.discovery500 \
  --target Qwen/Qwen2.5-7B-Instruct --av "$QAV" --ar "$QAR" \
  --metadata "$AXB/generate/metadata.jsonl" --parquet "$AXB/inference/latent_eval_data.parquet" \
  --out-dir "$OUT/qwen_nla_calibration" --layer 20 --alpha 0.5 --n-prompts 4 --prompt-offset 10 \
  --n-samples 2 --av-rollouts 3 --activation-batch-size 8 --av-concurrency 1 \
  --random-seeds 17,42,101 --concept-ids "$IDS" --standalone-actor
run_stage qwen_nla_alpha_sweep env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$ROOT" $ENV/bin/python -m experiments.calibrate_nla_alpha \
  --run-dir "$OUT/qwen_nla_calibration" --target Qwen/Qwen2.5-7B-Instruct --layer 20 \
  --alphas 0.1,0.2,0.3,0.4,0.5,0.6,0.75 --fallback-alpha 0.5
run_stage qwen_nla env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$ROOT" $ENV/bin/python -m experiments.discovery500 \
  --target Qwen/Qwen2.5-7B-Instruct --av "$QAV" --ar "$QAR" \
  --metadata "$AXB/generate/metadata.jsonl" --parquet "$AXB/inference/latent_eval_data.parquet" \
  --out-dir "$OUT/qwen_nla" --layer 20 --alpha 0.5 \
  --alpha-map "$OUT/qwen_nla_calibration/selected_alphas.json" --n-prompts 8 --prompt-offset 14 \
  --n-samples 2 --av-rollouts 3 --activation-batch-size 8 --av-concurrency 1 \
  --random-seeds 17,42,101 --concept-ids "$IDS" --standalone-actor
run_stage qwen_nla_score env PYTHONPATH="$ROOT" $ENV/bin/python -m experiments.confirmation_metrics \
  --run-dir "$OUT/qwen_nla" --target-count 14
run_stage qwen_nla_verify $ENV/bin/python "$ROOT/scripts/verify_panel_stage.py" nla "$OUT/qwen_nla" \
  --calibration-run "$OUT/qwen_nla_calibration"
run_stage qwen_cache_cleanup $ENV/bin/python "$ROOT/scripts/cleanup_hf_repos.py" \
  Qwen/Qwen2.5-7B-Instruct kitft/nla-qwen2.5-7b-L20-av kitft/nla-qwen2.5-7b-L20-ar

run_stage gemma_baseline env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$CONTRAST_SRC" \
  $ENV/bin/python "$ROOT/experiments/run_panel_baselines.py" --model-key gemma --output "$OUT/gemma"
run_stage gemma_baseline_verify $ENV/bin/python "$ROOT/scripts/verify_panel_stage.py" baseline "$OUT/gemma"
run_stage gemma_av_download $ENV/bin/hf download kitft/nla-gemma3-12b-L32-av
run_stage gemma_ar_download $ENV/bin/hf download kitft/nla-gemma3-12b-L32-ar
GAV=$(tail -1 "$LOG/gemma_av_download.stdout.log" 2>/dev/null || true)
GAR=$(tail -1 "$LOG/gemma_ar_download.stdout.log" 2>/dev/null || true)
run_stage gemma_nla_calibration_vectors env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$ROOT" $ENV/bin/python -m experiments.discovery500 \
  --target google/gemma-3-12b-it --av "$GAV" --ar "$GAR" \
  --metadata "$AXB/generate/metadata.jsonl" --parquet "$AXB/inference/latent_eval_data.parquet" \
  --out-dir "$OUT/gemma_nla_calibration" --layer 32 --alpha 0.3 --n-prompts 4 --prompt-offset 10 \
  --n-samples 2 --av-rollouts 3 --activation-batch-size 4 --av-concurrency 1 \
  --random-seeds 17,42,101 --concept-ids "$IDS" --standalone-actor
run_stage gemma_nla_alpha_sweep env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$ROOT" $ENV/bin/python -m experiments.calibrate_nla_alpha \
  --run-dir "$OUT/gemma_nla_calibration" --target google/gemma-3-12b-it --layer 32 \
  --alphas 0.1,0.15,0.2,0.25,0.3,0.4,0.5 --fallback-alpha 0.3
run_stage gemma_nla env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$ROOT" $ENV/bin/python -m experiments.discovery500 \
  --target google/gemma-3-12b-it --av "$GAV" --ar "$GAR" \
  --metadata "$AXB/generate/metadata.jsonl" --parquet "$AXB/inference/latent_eval_data.parquet" \
  --out-dir "$OUT/gemma_nla" --layer 32 --alpha 0.3 \
  --alpha-map "$OUT/gemma_nla_calibration/selected_alphas.json" --n-prompts 8 --prompt-offset 14 \
  --n-samples 2 --av-rollouts 3 --activation-batch-size 4 --av-concurrency 1 \
  --random-seeds 17,42,101 --concept-ids "$IDS" --standalone-actor
run_stage gemma_nla_score env PYTHONPATH="$ROOT" $ENV/bin/python -m experiments.confirmation_metrics \
  --run-dir "$OUT/gemma_nla" --target-count 14
run_stage gemma_nla_verify $ENV/bin/python "$ROOT/scripts/verify_panel_stage.py" nla "$OUT/gemma_nla" \
  --calibration-run "$OUT/gemma_nla_calibration"
run_stage final_report $ENV/bin/python "$ROOT/scripts/summarize_panel_queue.py"
if find "$LOG" -maxdepth 1 -name '*.failed' | grep -q .; then
  event "QUEUE_FINISHED_WITH_FAILURES"
else
  event "QUEUE_COMPLETE"
fi
