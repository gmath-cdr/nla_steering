#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
for path in \
  src/nla/experiments/runner.py \
  src/nla/experiments/rescore_prompting_quality.py \
  src/nla/experiments/rubric_two_judge.py \
  src/nla/experiments/factuality_three_arm.py \
  src/nla/LICENSE \
  src/nla_inference/nla_inference.py \
  src/nla_inference/LICENSE \
  src/axbench/axbench/models/steering_vector.py \
  src/axbench/LICENSE \
  src/jacobian_lens/jlens/lens.py \
  src/jacobian_lens/LICENSE \
  src/contrast_steering/src/contrast_steering/pipeline.py; do
  test -f "$root/$path" || { echo "missing: $path" >&2; exit 1; }
done

for path in \
  artifacts/release/prompting_quality/panel14_qwen_matched_panel.json \
  artifacts/release/prompting_quality/panel14_qwen_prompting.json \
  artifacts/release/prompting_quality/panel14_gemma_matched_panel.json \
  artifacts/release/prompting_quality/panel14_gemma_prompting.json \
  artifacts/release/prompting_quality/panel10_gemma_matched_panel.json \
  artifacts/release/prompting_quality/panel10_gemma_prompting.json \
  artifacts/release/reliability_v1/human_scores_only_56.json \
  artifacts/release/reliability_v1/two_judge_scores.json \
  artifacts/release/reliability_v1/factuality_three_arm.json; do
  test -s "$root/$path" || { echo "missing or empty: $path" >&2; exit 1; }
done

(cd "$root" && shasum -a 256 -c artifacts/release/sha256sums.txt)

if find "$root/src" -type l -print -quit | grep -q .; then
  echo "source tree contains a symlink; expected ordinary source copies" >&2
  exit 1
fi
echo "source tree and frozen release inputs are complete"
