#!/usr/bin/env bash
# Run a slice of the random 50-concept panel on one pod, as sequential shards.
#
# Shards are independent, so a failure costs at most one shard and a re-run
# with the same arguments resumes at the first unfinished stage.
#
#   MODEL     qwen or gemma
#   SLICE     START:END positions in the panel's draw order (END exclusive)
#   CHUNK     concepts per shard (default 10)
#   OUT_ROOT  output root on pod-local disk, absolute
#   PY        virtual environment python, absolute
set -euo pipefail

MODEL=${MODEL:?set MODEL=qwen or gemma}
SLICE=${SLICE:?set SLICE=START:END}
CHUNK=${CHUNK:-10}
OUT_ROOT=${OUT_ROOT:?set OUT_ROOT}
PY=${PY:?set PY}
HERE="$(cd "$(dirname "$0")" && pwd)"

mapfile -t SHARDS < <("$PY" - "$HERE/panel.random50.json" "$SLICE" "$CHUNK" <<'PYEOF'
import json
import sys
from pathlib import Path

panel, piece, chunk = sys.argv[1], sys.argv[2], int(sys.argv[3])
ids = json.loads(Path(panel).read_text(encoding="utf-8"))["concept_ids"]
start, end = (int(part) for part in piece.split(":"))
if not 0 <= start < end <= len(ids) or chunk < 1:
    raise SystemExit("SLICE or CHUNK out of range")
for first in range(start, end, chunk):
    print(f"{first:02d} {','.join(str(item) for item in ids[first:min(first + chunk, end)])}")
PYEOF
)

mkdir -p "$OUT_ROOT"
printf '%s shards for %s slice %s\n' "${#SHARDS[@]}" "$MODEL" "$SLICE" | tee -a "$OUT_ROOT/pod.log"
for shard in "${SHARDS[@]}"; do
  first=${shard%% *}
  ids=${shard#* }
  printf '%s SHARD_START %s %s\n' "$(date -u +%FT%TZ)" "$first" "$ids" | tee -a "$OUT_ROOT/pod.log"
  MODEL="$MODEL" IDS="$ids" OUT="$OUT_ROOT/shard_$first" PY="$PY" bash "$HERE/run_shard.sh"
  printf '%s SHARD_DONE %s\n' "$(date -u +%FT%TZ)" "$first" | tee -a "$OUT_ROOT/pod.log"
done
printf '%s POD_ALL_DONE\n' "$(date -u +%FT%TZ)" | tee -a "$OUT_ROOT/pod.log"
