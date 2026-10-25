#!/usr/bin/env bash
# Full Tier 1 + Tier 2 batch (2026-07-31 plan). Run from lab/:
#
#     bash experiments/run_batch.sh
#
# Controls first: they need only the base model, so a broken environment
# fails cheaply before any three-model steered run starts. Every run
# writes its JSON + transcript; a failure stops the chain.
set -euo pipefail
specs=(
  control_std control_howto
  emoji ellipsis exclaim caps parens digits emdash
  bold_down list_down
  shakespeare contractions verbosity hedging sentlen person secondperson allit
  emoji_confirm emoji_confirm_control
)
for s in "${specs[@]}"; do
    echo "==== $s ===="
    python experiments/run.py "experiments/specs/$s.yaml"
done
echo "batch complete: $(ls results/*.json | wc -l) result files"
