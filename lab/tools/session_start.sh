#!/usr/bin/env bash
# Run this at the START of every pod session (from the lab/ directory):
#
#     bash tools/session_start.sh
#
# It (1) verifies a clean, explicit runtime, (2) requires confirmation that the
# laptop-side lease guard is already live, (3) optionally starts an independently
# authorized in-pod backstop, and (4) runs the smoke test. It deliberately does
# not pull moving code: prospective specs bind generation to committed hashes.
set -euo pipefail
cd "$(dirname "$0")/.."

NLA_PYTHON="${NLA_PYTHON:-/venv/bin/python}"
if [[ ! -x "$NLA_PYTHON" ]]; then
    echo "missing $NLA_PYTHON; rebuild the NLA environment first" >&2
    exit 1
fi

# SSH sessions do not inherit RunPod's container env (RUNPOD_POD_ID etc.,
# needed by the watchdog to stop the right pod); the image parks it here
[ -f /etc/rp_environment ] && source /etc/rp_environment

if [[ -z "${NLA_MODEL:-}" ]]; then
    echo "set NLA_MODEL explicitly (qwen or gemma3-12b)" >&2
    exit 1
fi
export NLA_MODELS_DIR="${NLA_MODELS_DIR:-/root/models}"

echo "== runtime identity =="
git rev-parse HEAD
if [[ -n "$(git status --porcelain)" ]]; then
    echo "worktree is dirty; commit or restore changes before a prospective run" >&2
    exit 1
fi
echo "NLA_MODEL=$NLA_MODEL"
echo "NLA_MODELS_DIR=$NLA_MODELS_DIR"

echo "== external lifecycle guard =="
if [[ "${NLA_EXTERNAL_GUARD_CONFIRMED:-}" != "1" ]]; then
    echo "laptop-side RunPod lease guard is not confirmed" >&2
    echo "start tools/runpod_guard.py externally, inspect its live log, then:" >&2
    echo "  export NLA_EXTERNAL_GUARD_CONFIRMED=1" >&2
    exit 1
fi
echo "   operator confirmed a live external lease guard"

echo "== optional in-pod backstop =="
if [[ "${RUNPOD_SELF_STOP_ENABLED:-0}" == "1" ]]; then
    if pgrep -f "python.*tools/auto_stop.py" > /dev/null; then
        echo "   already running"
    else
        : > /tmp/wd.log
        setsid nohup "$NLA_PYTHON" -u tools/auto_stop.py --idle-minutes 45 \
            > /tmp/wd.log 2>&1 < /dev/null &
        sleep 1
        if ! pgrep -af "python.*tools/auto_stop.py"; then
            echo "in-pod watchdog failed to remain running" >&2
            tail -n 50 /tmp/wd.log >&2 || true
            exit 1
        fi
        echo "   started; this is secondary to the external guard"
    fi
    echo "   log: /tmp/wd.log"
    tail -n 5 /tmp/wd.log || true
else
    echo "   disabled: this pod template has no verified self-stop credential"
fi

if [[ "${NLA_DIRECTION_BUILD_ONLY:-0}" == "1" ]]; then
    echo "== direction-build GPU preflight (no decoding) =="
    "$NLA_PYTHON" -c 'import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))'
else
    echo "== smoke test =="
    "$NLA_PYTHON" smoke_test.py
fi
