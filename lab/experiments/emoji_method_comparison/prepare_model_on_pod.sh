#!/usr/bin/env bash
# Resume/download one registered model trio on the persistent volume, then
# stage only that trio on fast container-local disk. No generation is run.
set -euo pipefail

cd "$(dirname "$0")/../.."

case "${NLA_MODEL:-}" in
  qwen)
    MODEL_ROWS=(
      "Qwen/Qwen2.5-7B-Instruct|a09a35458c702b33eeacc393d103063234e8bc28|qwen2.5-7b-instruct"
      "kitft/nla-qwen2.5-7b-L20-av|b88469162777ae6553bc14208eb0cb579336f8f4|nla-av"
      "kitft/nla-qwen2.5-7b-L20-ar|e2c9e57eac213d37a31612087f645ab6332c1bb6|nla-ar"
    )
    ;;
  gemma3-12b)
    MODEL_ROWS=(
      "google/gemma-3-12b-it|96b6f1eccf38110c56df3a15bffe176da04bfd80|gemma-3-12b-it"
      "kitft/nla-gemma3-12b-L32-av|7aec22599e8a9cd533564868999b443fcc963cf4|nla-gemma3-12b-av"
      "kitft/nla-gemma3-12b-L32-ar|3d6901d8243182d642af9dea452ca91549a94615|nla-gemma3-12b-ar"
    )
    ;;
  *)
    echo "export NLA_MODEL=qwen or NLA_MODEL=gemma3-12b" >&2
    exit 1
    ;;
esac

export HF_HOME="${HF_HOME:-/workspace/hf}"
PERSISTED_ROOT="${NLA_PERSISTED_MODELS_DIR:-/workspace/models}"
HOT_ROOT="${NLA_MODELS_DIR:-/root/models}"
mkdir -p "$PERSISTED_ROOT" "$HOT_ROOT"

if [[ ! -x /venv/bin/python ]]; then
    python3 -m venv --system-site-packages /venv
fi
/venv/bin/pip install -q -r requirements.txt
/venv/bin/python -m pip check
command -v rsync >/dev/null || { echo "rsync is required" >&2; exit 1; }

for row in "${MODEL_ROWS[@]}"; do
    repo="${row%%|*}"
    rest="${row#*|}"
    revision="${rest%%|*}"
    name="${row##*|}"
    export EMOJI_PREP_REPO="$repo"
    export EMOJI_PREP_REVISION="$revision"
    export EMOJI_PREP_DEST="$PERSISTED_ROOT/$name"
    echo "== resume canonical snapshot: $repo -> $EMOJI_PREP_DEST =="
    /venv/bin/python - <<'PY'
import os
from huggingface_hub import snapshot_download

token = os.environ.get("HF_TOKEN") or None
snapshot_download(
    os.environ["EMOJI_PREP_REPO"],
    local_dir=os.environ["EMOJI_PREP_DEST"],
    revision=os.environ["EMOJI_PREP_REVISION"],
    token=token,
)
PY
    /venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["EMOJI_PREP_DEST"]) / "nla_source_receipt.json"
payload = {
    "repository": os.environ["EMOJI_PREP_REPO"],
    "revision": os.environ["EMOJI_PREP_REVISION"],
}
path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
PY
    echo "== stage hot copy: $name =="
    mkdir -p "$HOT_ROOT/$name"
    rsync -a --info=progress2 "$PERSISTED_ROOT/$name/" "$HOT_ROOT/$name/"
done

echo "== canonical/hot marker verification =="
for row in "${MODEL_ROWS[@]}"; do
    name="${row##*|}"
    for marker in config.json tokenizer_config.json nla_meta.yaml nla_source_receipt.json; do
        if [[ -f "$PERSISTED_ROOT/$name/$marker" ]]; then
            persisted_hash="$(sha256sum "$PERSISTED_ROOT/$name/$marker" | cut -d' ' -f1)"
            hot_hash="$(sha256sum "$HOT_ROOT/$name/$marker" | cut -d' ' -f1)"
            [[ "$persisted_hash" == "$hot_hash" ]] || {
                echo "marker mismatch after staging: $name/$marker" >&2
                exit 1
            }
        fi
    done
done

echo "prepared NLA_MODEL=$NLA_MODEL"
df -h /workspace | tail -1
df -h /root | tail -1
