#!/usr/bin/env bash
# One shard of the random 50-concept panel on one GPU.
#
# This is the selected-panel evaluation pointed at the concept ids in $IDS.
# The baseline and NLA commands and flags are copied from
# src/nla/scripts/run_panel_queue.sh. The direct-prompting stages are not in
# that queue; they call the packaged prompting scripts with the same protocol.
# Only the paths, the concept list, and the stage bookkeeping differ.
# A laptop-side guard MUST own the pod lifecycle.
#
#   MODEL     qwen or gemma
#   IDS       comma-separated concept ids, all present in $PROTOCOL
#   OUT       this shard's output directory, absolute, on pod-local disk
#   PROTOCOL  base protocol JSON (default: the random 50-concept protocol)
#   PY        python interpreter, absolute (its directory supplies `hf`)
#
# Re-running with the same OUT and IDS skips finished stages.
set -euo pipefail

MODEL=${MODEL:?set MODEL=qwen or gemma}
IDS=${IDS:?set IDS to a comma-separated concept id list}
OUT=${OUT:?set OUT to this shard output directory}
PY=${PY:?set PY to the virtual environment python}

if [[ "$OUT" != /* || "$PY" != /* ]]; then
  echo "OUT and PY must be absolute paths" >&2
  exit 2
fi
# Bind every tool to the same environment as $PY.
PATH="$(dirname "$PY"):$PATH"
export PATH

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
NLA="$REPO/src/nla"
AXB="$REPO/src/axbench/axbench/concept500/prod_9b_l20_v1"
PROTOCOL=${PROTOCOL:-$REPO/lab/experiments/random50_panel/protocol.random50.json}
LOG="$OUT/queue_logs"
HUB="${HF_HOME:?set HF_HOME to the pod-local model cache}/hub"

# Checkpoint revisions recorded by the archived 14-concept run.
case "$MODEL" in
  qwen)
    TARGET=Qwen/Qwen2.5-7B-Instruct
    TARGET_REV=a09a35458c702b33eeacc393d103063234e8bc28
    AV_REPO=kitft/nla-qwen2.5-7b-L20-av
    AV_REV=b88469162777ae6553bc14208eb0cb579336f8f4
    AR_REPO=kitft/nla-qwen2.5-7b-L20-ar
    AR_REV=e2c9e57eac213d37a31612087f645ab6332c1bb6
    LAYER=20; ALPHA=0.5; GRID=0.1,0.2,0.3,0.4,0.5,0.6,0.75; ACTIVATION_BATCH=8 ;;
  gemma)
    TARGET=google/gemma-3-12b-it
    TARGET_REV=96b6f1eccf38110c56df3a15bffe176da04bfd80
    AV_REPO=kitft/nla-gemma3-12b-L32-av
    AV_REV=7aec22599e8a9cd533564868999b443fcc963cf4
    AR_REPO=kitft/nla-gemma3-12b-L32-ar
    AR_REV=3d6901d8243182d642af9dea452ca91549a94615
    LAYER=32; ALPHA=0.3; GRID=0.1,0.15,0.2,0.25,0.3,0.4,0.5; ACTIVATION_BATCH=4 ;;
  *) echo "MODEL must be qwen or gemma" >&2; exit 2 ;;
esac

mkdir -p "$LOG"

STAGE=setup
FINISHED=0
event() {
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG/queue.log"
}
status() {
  printf '{"status":"%s","stage":"%s","model":"%s","utc":"%s"}\n' \
    "$1" "$2" "$MODEL" "$(date -u +%FT%TZ)" >"$OUT/STATUS.json.tmp"
  mv "$OUT/STATUS.json.tmp" "$OUT/STATUS.json"
}
# Any exit before the final line is recorded as a failure of the current stage.
on_exit() {
  if [[ $FINISHED -ne 1 ]]; then
    event "FAILED $STAGE"
    status failed "$STAGE"
  fi
}
trap on_exit EXIT

# Unlike the team queue, a failed stage stops the shard. Later stages read the
# outputs of earlier ones, so continuing would score incomplete data.
run_stage() {
  local name=$1; shift
  STAGE=$name
  if [[ -f "$LOG/$name.done" ]]; then
    event "SKIP $name already complete"
    return 0
  fi
  event "START $name"
  status running "$name"
  "$@" >"$LOG/$name.stdout.log" 2>"$LOG/$name.stderr.log"
  date -u +%FT%TZ >"$LOG/$name.done"
  event "DONE $name"
}
# The team scripts load checkpoints by repository name. Fail closed if the
# cache holds anything other than the recorded revision.
require_revision() {
  local repo=$1 revision=$2
  local snapshots="$HUB/models--${repo//\//--}/snapshots"
  local found
  found=$(ls "$snapshots" | tr '\n' ' ')
  if [[ "$found" != "$revision " ]]; then
    echo "$repo: expected only revision $revision, found: $found" >&2
    return 1
  fi
}

COUNT=$(awk -F, '{print NF}' <<<"$IDS")
if [[ -f "$OUT/shard.ids" && "$(cat "$OUT/shard.ids")" != "$IDS" ]]; then
  echo "OUT already holds a different concept list" >&2
  exit 2
fi
printf '%s\n' "$IDS" >"$OUT/shard.ids"
event "QUEUE_START model=$MODEL concepts=$COUNT"
status running setup
git -C "$REPO" rev-parse HEAD >"$LOG/repo.commit"
"$PY" -m pip freeze >"$LOG/pip_freeze.txt"
nvidia-smi -q >"$LOG/nvidia_smi.txt"

# The shard protocol is the base protocol with only its concept list narrowed.
"$PY" - "$PROTOCOL" "$OUT/protocol.shard.json" "$IDS" <<'PYEOF'
import json
import sys
from pathlib import Path

source, destination, ids = sys.argv[1:4]
protocol = json.loads(Path(source).read_text(encoding="utf-8"))
wanted = [int(item) for item in ids.split(",")]
outside = [concept for concept in wanted if concept not in protocol["concept_ids"]]
if outside or len(set(wanted)) != len(wanted):
    raise SystemExit(f"concept ids outside the base protocol or repeated: {outside}")
protocol["concept_ids"] = wanted
Path(destination).write_text(json.dumps(protocol, indent=2) + "\n", encoding="utf-8")
PYEOF

cd "$NLA"

run_stage "${MODEL}_target_download" hf download "$TARGET" --revision "$TARGET_REV"

run_stage "${MODEL}_baseline" env PYTHONPATH="$REPO/src/contrast_steering/src" \
  "$PY" "$NLA/experiments/run_panel_baselines.py" --model-key "$MODEL" \
  --config "$OUT/protocol.shard.json" --output "$OUT/$MODEL"

run_stage "${MODEL}_prompting" env PYTHONPATH="$REPO/src/contrast_steering/src" \
  "$PY" "$NLA/experiments/prompting_baseline.py" --model-key "$MODEL" \
  --config "$OUT/protocol.shard.json" --metadata "$AXB/generate/metadata.jsonl" \
  --output "$OUT/${MODEL}_prompting" --unsteered-results "$OUT/$MODEL/results.json"
# Output names follow the archived prompting_baseline_2026-08-15 folders.
run_stage "${MODEL}_prompting_rescore" \
  "$PY" "$NLA/experiments/rescore_prompting_quality.py" \
  --prompting-results "$OUT/${MODEL}_prompting/results.json" \
  --unsteered-results "$OUT/$MODEL/results.json" \
  --output-results "$OUT/${MODEL}_prompting/results_matched_unsteered.json" \
  --output-summary "$OUT/${MODEL}_prompting/quality_matched_unsteered_summary.json"

run_stage "${MODEL}_av_download" hf download "$AV_REPO" --revision "$AV_REV"
run_stage "${MODEL}_ar_download" hf download "$AR_REPO" --revision "$AR_REV"
STAGE=checkpoint_revisions
AV=$(tail -1 "$LOG/${MODEL}_av_download.stdout.log")
AR=$(tail -1 "$LOG/${MODEL}_ar_download.stdout.log")
[[ -d "$AV" && -d "$AR" ]]
require_revision "$TARGET" "$TARGET_REV"
require_revision "$AV_REPO" "$AV_REV"
require_revision "$AR_REPO" "$AR_REV"

run_stage "${MODEL}_nla_calibration_vectors" env PYTHONPATH="$NLA" "$PY" -m experiments.discovery500 \
  --target "$TARGET" --av "$AV" --ar "$AR" \
  --metadata "$AXB/generate/metadata.jsonl" --parquet "$AXB/inference/latent_eval_data.parquet" \
  --out-dir "$OUT/${MODEL}_nla_calibration" --layer "$LAYER" --alpha "$ALPHA" --n-prompts 4 --prompt-offset 10 \
  --n-samples 2 --av-rollouts 3 --activation-batch-size "$ACTIVATION_BATCH" --av-concurrency 1 \
  --random-seeds 17,42,101 --concept-ids "$IDS" --standalone-actor
run_stage "${MODEL}_nla_alpha_sweep" env PYTHONPATH="$NLA" "$PY" -m experiments.calibrate_nla_alpha \
  --run-dir "$OUT/${MODEL}_nla_calibration" --target "$TARGET" --layer "$LAYER" \
  --alphas "$GRID" --fallback-alpha "$ALPHA"
run_stage "${MODEL}_nla" env PYTHONPATH="$NLA" "$PY" -m experiments.discovery500 \
  --target "$TARGET" --av "$AV" --ar "$AR" \
  --metadata "$AXB/generate/metadata.jsonl" --parquet "$AXB/inference/latent_eval_data.parquet" \
  --out-dir "$OUT/${MODEL}_nla" --layer "$LAYER" --alpha "$ALPHA" \
  --alpha-map "$OUT/${MODEL}_nla_calibration/selected_alphas.json" --n-prompts 8 --prompt-offset 14 \
  --n-samples 2 --av-rollouts 3 --activation-batch-size "$ACTIVATION_BATCH" --av-concurrency 1 \
  --random-seeds 17,42,101 --concept-ids "$IDS" --standalone-actor
run_stage "${MODEL}_nla_score" env PYTHONPATH="$NLA" "$PY" -m experiments.confirmation_metrics \
  --run-dir "$OUT/${MODEL}_nla" --target-count "$COUNT"

STAGE=checkpoint_revisions
require_revision "$TARGET" "$TARGET_REV"
run_stage "${MODEL}_verify_counts" "$PY" "$REPO/lab/experiments/random50_panel/verify_shard.py" \
  --out "$OUT" --model "$MODEL" --ids "$IDS"

FINISHED=1
status complete all
event "QUEUE_COMPLETE"
