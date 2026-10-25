#!/usr/bin/env bash
# Run one lexical-alpha sensitivity stratum from sealed prior artifacts.
# A laptop-side controller owns retrieval and shutdown in a finally block.
set -euo pipefail

cd "$(dirname "$0")/../.."

case "${AXB44_MODEL:-}" in
  qwen)
    export NLA_MODEL=qwen
    canary_specs=(
      000-axb44-qwen-evaluation-unsteered.yaml
      002-axb44-qwen-evaluation-diffmean-c20-fixed.yaml
      062-axb44-qwen-evaluation-random_17_all_a0p1-c20-alpaca-0041.yaml
    )
    ;;
  gemma)
    export NLA_MODEL=gemma3-12b
    canary_specs=(
      000-axb44-gemma-evaluation-unsteered.yaml
      002-axb44-gemma-evaluation-diffmean-c20-fixed.yaml
      062-axb44-gemma-evaluation-random_17_all_a0p1-c20-alpaca-0041.yaml
    )
    ;;
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

: "${AXB44_SOURCE_RUN_ROOT:?set the authenticated original run root}"
: "${AXB44_SENSITIVITY_ROOT:?set a separate sensitivity output root}"
: "${AXB44_SENSITIVITY_PACKAGE:?set the sealed package root}"

export NLA_MODELS_DIR="${NLA_MODELS_DIR:-/workspace/models}"
export NLA_PYTHON="${NLA_PYTHON:-/root/venvs/axb44-7cef0d4-v1/bin/python}"
source_root="$(readlink -f -- "$AXB44_SOURCE_RUN_ROOT")"
run_root="$(readlink -m -- "$AXB44_SENSITIVITY_ROOT")"
package_root="$(readlink -f -- "$AXB44_SENSITIVITY_PACKAGE")"
spec_dir="$package_root/${AXB44_MODEL}-specs"
manifest="$package_root/${AXB44_MODEL}-materialization-manifest.json"
alpha_receipt="$package_root/${AXB44_MODEL}-lexical-alpha-receipt.json"

[[ -x "$NLA_PYTHON" ]]
[[ -d "$source_root" && -f "$source_root/COMPLETE.json" ]]
[[ -d "$spec_dir" && -f "$manifest" && -f "$alpha_receipt" ]]
[[ "$run_root" != "$source_root" && "$run_root" != "$source_root"/* ]]
[[ "$run_root" == /workspace/nla-artifacts/axb44-lexical-alpha-v1/* ]]
mkdir -p "$run_root/results/canary" "$run_root/results/new"

write_status() {
  code=$?
  if [[ $code -eq 0 ]]; then state=complete; else state=failed; fi
  "$NLA_PYTHON" -m experiments.axbench_lexicality20.lexical_validate_results status \
    --model-key "$AXB44_MODEL" --run-root "$run_root" \
    --state "$state" --exit-code "$code" || true
}
trap write_status EXIT

echo "== persistent storage and exact runtime gate =="
available_kb=$(df -Pk /workspace | awk 'NR==2 {print $4}')
if [[ -z "$available_kb" || "$available_kb" -lt 10485760 ]]; then
  echo "less than 10 GiB free on persistent storage" >&2
  exit 1
fi
"$NLA_PYTHON" -m pip check
"$NLA_PYTHON" - <<'PY'
import torch, transformers, yaml, numpy
assert torch.cuda.is_available()
assert torch.__version__ == "2.8.0+cu128"
assert transformers.__version__ == "4.57.6"
assert numpy.__version__ == "2.1.2"
assert yaml.__version__ == "6.0.3"
name = torch.cuda.get_device_name(0)
capability = torch.cuda.get_device_capability(0)
assert "A100" in name, name
assert capability == (8, 0), capability
assert torch.version.cuda == "12.8", torch.version.cuda
print(f"RUNTIME_OK gpu={name} capability={capability} cuda={torch.version.cuda}")
PY

echo "== exhaustive zero-download base-checkpoint verification =="
"$NLA_PYTHON" -m experiments.axbench_lexicality20.lexical_verify_base \
  --model-key "$AXB44_MODEL" --source-run-root "$source_root" \
  --models-root "$NLA_MODELS_DIR" --output "$run_root/base_verification.json"

echo "== historical execution canary =="
canary_paths=()
for name in "${canary_specs[@]}"; do
  path="$source_root/specs/evaluation/$name"
  [[ -f "$path" ]]
  canary_paths+=("$path")
done
"$NLA_PYTHON" experiments/run_specs.py "${canary_paths[@]}" \
  --output-dir "$run_root/results/canary" \
  --models-root "$NLA_MODELS_DIR" --resume
"$NLA_PYTHON" -m experiments.axbench_lexicality20.lexical_validate_results canary \
  --model-key "$AXB44_MODEL" --source-run-root "$source_root" \
  --canary-results "$run_root/results/canary" \
  --output "$run_root/CANARY.json"

echo "== sealed lexical-alpha delta generation =="
"$NLA_PYTHON" experiments/run_specs.py "$spec_dir" \
  --output-dir "$run_root/results/new" \
  --models-root "$NLA_MODELS_DIR" --resume

echo "== exact generated/reused union validation =="
"$NLA_PYTHON" -m experiments.axbench_lexicality20.lexical_validate_results finalize \
  --model-key "$AXB44_MODEL" --source-run-root "$source_root" \
  --run-root "$run_root" --spec-dir "$spec_dir" \
  --materialization-manifest "$manifest" \
  --lexical-alpha-receipt "$alpha_receipt" \
  --canary-receipt "$run_root/CANARY.json"

echo "LEXICAL SENSITIVITY COMPLETE: $run_root"
