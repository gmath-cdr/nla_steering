#!/bin/sh
set -u

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
BUNDLE_ROOT=$(CDPATH= cd -- "$ROOT/../.." && pwd)
ENV=${NLA_VENV:-$BUNDLE_ROOT/.venv}
CONTRAST_SRC=$BUNDLE_ROOT/src/contrast_steering/src
OUT=${NLA_OUTPUT_DIR:-$BUNDLE_ROOT/artifacts/rebuilt/baseline_panel14}
LOG=$OUT/queue_logs
mkdir -p "$LOG"

event() {
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG/queue.log"
  "$ENV/bin/python" "$ROOT/scripts/write_queue_status.py" --log-dir "$LOG" --event "$*"
}

run_stage() {
  name=$1
  shift
  attempt=1
  code=1
  while test "$attempt" -le 3; do
    event "START $name attempt=$attempt"
    "$@" >"$LOG/$name.stdout.attempt$attempt.log" 2>"$LOG/$name.stderr.attempt$attempt.log"
    code=$?
    test "$code" -eq 0 && break
    event "RETRY $name attempt=$attempt exit=$code"
    attempt=$((attempt + 1))
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
  date -u +%FT%TZ >"$LOG/$name.done"
  event "DONE $name"
}

event "RANDOM_NORM_REPAIR_QUEUE_START"
run_stage qwen_random_norm_repair env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$ROOT:$CONTRAST_SRC" \
  "$ENV/bin/python" -m experiments.repair_baseline_randoms \
  --model-key qwen --run-dir "$OUT/qwen"
run_stage qwen_baseline_norm_reverify "$ENV/bin/python" "$ROOT/scripts/verify_panel_stage.py" \
  baseline "$OUT/qwen"
run_stage gemma_random_norm_repair env CUDA_VISIBLE_DEVICES=2 PYTHONPATH="$ROOT:$CONTRAST_SRC" \
  "$ENV/bin/python" -m experiments.repair_baseline_randoms \
  --model-key gemma --run-dir "$OUT/gemma"
run_stage gemma_baseline_norm_reverify "$ENV/bin/python" "$ROOT/scripts/verify_panel_stage.py" \
  baseline "$OUT/gemma"
run_stage final_report_after_norm_repair "$ENV/bin/python" "$ROOT/scripts/summarize_panel_queue.py"

if find "$LOG" -maxdepth 1 -name '*.failed' | grep -q .; then
  event "QUEUE_FINISHED_WITH_FAILURES"
else
  event "QUEUE_COMPLETE"
fi
