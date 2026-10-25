#!/usr/bin/env bash
# Emoji method-comparison batch (14 sweeps). Run from lab/:
#
#     bash experiments/emoji_methods/run_night.sh
#
# Random first: it needs only the base model, so a broken environment
# fails cheaply. File-direction sweeps also load base only.
set -euo pipefail
specs=(
  random
  freeform
  single_1 single_2 single_3 single_4 single_5
  phrase_1 phrase_2 phrase_3 phrase_4 phrase_5
  readable leftover
)
for s in "${specs[@]}"; do
    echo "==== $s ===="
    python experiments/run.py "experiments/emoji_methods/specs/$s.yaml"
done
echo "emoji_methods batch complete"
