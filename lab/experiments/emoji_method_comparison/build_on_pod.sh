#!/usr/bin/env bash
# One guarded, non-behavioral RunPod job for one registered model: smoke-test,
# build four directions, hash every checkpoint shard, validate, and stop.
set -euo pipefail

cd "$(dirname "$0")/../.."

case "${NLA_MODEL:-}" in
  qwen)
    SPLIT_A="3ae3b903083e55137564e0ea90baf000cfd452f8fe140d3cf00db48b741c781d  experiments/jspace/data/lens_L20_A.pt"
    SPLIT_B="e323df23c755f9d400dc9eb7197574d148f473bc65dee94df4aa784f44483c1e  experiments/jspace/data/lens_L20_B.pt"
    ;;
  gemma3-12b)
    SPLIT_A="419f2945ee88b17ac06b87c43343514821e481816c4c65017990b79252aa1111  experiments/jspace/data/lens_L32_A.pt"
    SPLIT_B="53cb71bd6cb6f1b4e02219ca2dce0b25a6f09baae988543f1cd4d3be2a83849b  experiments/jspace/data/lens_L32_B.pt"
    ;;
  *)
    echo "export NLA_MODEL=qwen or NLA_MODEL=gemma3-12b before this build" >&2
    exit 1
    ;;
esac
export NLA_MODELS_DIR="${NLA_MODELS_DIR:-/root/models}"
ARTIFACT_DIR="${EMOJI_METHOD_ARTIFACT_DIR:-/workspace/nla-artifacts/emoji-method-comparison/${NLA_MODEL}-build-01}"

if [[ "${NLA_EXTERNAL_GUARD_CONFIRMED:-}" != "1" ]]; then
    echo "external RunPod lease guard is not confirmed" >&2
    exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
    echo "worktree must be clean before the frozen build" >&2
    exit 1
fi

echo "== session preflight =="
export NLA_DIRECTION_BUILD_ONLY=1
bash tools/session_start.sh

echo "== registered ${NLA_MODEL} split-half lenses =="
echo "$SPLIT_A" | sha256sum --check
echo "$SPLIT_B" | sha256sum --check

echo "== build all four directions and receipts =="
"${NLA_PYTHON:-/venv/bin/python}" -u experiments/emoji_method_comparison/build_directions.py \
  --model-key "$NLA_MODEL" \
  --models-root "$NLA_MODELS_DIR" \
  --output-dir "$ARTIFACT_DIR"

echo "== independent CPU receipt validation =="
"${NLA_PYTHON:-/venv/bin/python}" -m experiments.emoji_method_comparison.validate_build \
  "$ARTIFACT_DIR" --model-key "$NLA_MODEL"

echo "== BUILD COMPLETE: no behavioral generations were run =="
echo "artifact_dir=$ARTIFACT_DIR"
echo "operator: pull artifacts, verify locally, then explicitly stop the pod"
