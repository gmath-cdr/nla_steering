"""Idle watchdog: stops this RunPod pod after N idle minutes, so a forgotten
pod cannot burn money overnight.

"Idle" requires BOTH, for the full window:
  - GPU utilization at or below --gpu-threshold (default 5%), AND
  - zero live SSH connections (a connected VS Code window counts as one,
    so this never stops the pod while someone is attached and tinkering)

A long experiment keeps the GPU busy, so the pod survives after you
disconnect; once the run finishes and nobody is attached, the countdown
starts. Net effect: pay-per-use behavior on a persistent pod.

Optional secondary backstop, enabled by ``RUNPOD_SELF_STOP_ENABLED=1`` in
``tools/session_start.sh`` only after its stop credential has been verified.
Manual use:

    setsid nohup python3 -u tools/auto_stop.py --idle-minutes 45 \
      > /tmp/wd.log 2>&1 < /dev/null &

Requires: running inside a RunPod pod (``RUNPOD_POD_ID`` is set there) and an
API key authorized for lifecycle control or an authenticated ``runpodctl``.
The default pod-provided key may not have that permission.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


def gpu_util_percent() -> int | None:
    """Highest GPU utilization, or ``None`` when the query is not trustworthy."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    fields = result.stdout.split()
    if not fields or any(not field.strip().isdigit() for field in fields):
        return None
    return max(int(field) for field in fields)


def ssh_session_count() -> int:
    """Live SSH connections. Per-connection sshd processes show as 'sshd: user@tty'."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "args"], capture_output=True, text=True, timeout=20
        ).stdout
    except Exception:
        return 1  # if we cannot tell, err on the side of NOT stopping
    return sum(
        1 for line in out.splitlines()
        if line.strip().startswith("sshd:") and "@" in line
    )


def request_pod_stop(pod_id: str) -> tuple[bool, str]:
    """Request a stop without ever placing an API key in argv or log output."""
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    rest_detail: str | None = None
    if api_key:
        safe_id = urllib.parse.quote(pod_id, safe="")
        request = urllib.request.Request(
            f"https://rest.runpod.io/v1/pods/{safe_id}/stop",
            data=b"",
            # The default Python-urllib client name is rejected with HTTP 403.
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "nla-steering-lab-auto-stop/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = int(getattr(response, "status", 200))
            if 200 <= status < 300:
                return True, f"REST HTTP {status}"
            rest_detail = f"REST HTTP {status}"
        except urllib.error.HTTPError as exc:
            rest_detail = f"REST HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - retry loop handles failure
            rest_detail = f"REST {type(exc).__name__}"

    if not shutil.which("runpodctl"):
        if rest_detail is not None:
            return False, f"{rest_detail}; runpodctl unavailable"
        return False, "neither RUNPOD_API_KEY nor runpodctl is available"

    # Current CLI syntax first; the second form supports older RunPod images.
    cli_details: list[str] = []
    for command in (
        ["runpodctl", "pod", "stop", pod_id],
        ["runpodctl", "stop", "pod", pod_id],
    ):
        try:
            stopped = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - retry loop handles failure
            cli_details.append(f"{type(exc).__name__}")
            continue
        if stopped.returncode == 0:
            syntax = "current" if command[1] == "pod" else "legacy"
            return True, f"runpodctl accepted stop ({syntax} syntax)"
        cli_details.append(f"exit {stopped.returncode}")
    prefix = f"{rest_detail}; " if rest_detail is not None else ""
    return False, prefix + "runpodctl " + "/".join(cli_details)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--idle-minutes", type=float, default=45,
                    help="stop after this many continuous idle minutes")
    ap.add_argument("--gpu-threshold", type=int, default=5,
                    help="GPU %% at or below this counts as idle")
    ap.add_argument("--interval", type=int, default=60,
                    help="seconds between checks")
    ap.add_argument("--ignore-ssh", action="store_true",
                    help="do not let an open SSH session suppress idle shutdown")
    ap.add_argument("--max-runtime-minutes", type=float,
                    help="absolute stop ceiling, even if GPU or SSH stays busy")
    ap.add_argument("--dry-run", action="store_true",
                    help="log what would happen, never actually stop the pod")
    args = ap.parse_args()

    pod_id = os.environ.get("RUNPOD_POD_ID")
    if not pod_id:
        raise SystemExit("RUNPOD_POD_ID not set. Not inside a RunPod pod?")
    if (not os.environ.get("RUNPOD_API_KEY")
            and not shutil.which("runpodctl") and not args.dry_run):
        raise SystemExit(
            "neither RUNPOD_API_KEY nor runpodctl is available; cannot stop pod"
        )

    print(f"watchdog up: pod={pod_id} idle_limit={args.idle_minutes}min "
          f"gpu_threshold={args.gpu_threshold}% interval={args.interval}s "
          f"ignore_ssh={args.ignore_ssh} "
          f"max_runtime={args.max_runtime_minutes}",
          flush=True)

    started_at = time.time()
    idle_since: float | None = None
    while True:
        util = gpu_util_percent()
        ssh = ssh_session_count()
        # A failed GPU query is unknown, never evidence of idleness. This is
        # deliberately conservative because a false idle decision costs a run.
        busy = (util is None or util > args.gpu_threshold
                or (ssh > 0 and not args.ignore_ssh))

        now = time.time()
        if busy:
            idle_since = None
        elif idle_since is None:
            idle_since = now
        idle_min = 0.0 if idle_since is None else (now - idle_since) / 60

        util_label = "unknown" if util is None else f"{util}%"
        print(f"{datetime.now():%H:%M:%S} gpu={util_label} ssh={ssh} "
              f"idle={idle_min:.0f}/{args.idle_minutes:g}min", flush=True)

        runtime_min = (now - started_at) / 60
        hit_idle = idle_since is not None and idle_min >= args.idle_minutes
        hit_ceiling = (args.max_runtime_minutes is not None
                       and runtime_min >= args.max_runtime_minutes)
        if hit_idle or hit_ceiling:
            reason = ("absolute runtime ceiling" if hit_ceiling
                      else "idle limit")
            print(f"{reason} reached -> stopping pod {pod_id}", flush=True)
            if args.dry_run:
                print("(dry run: not stopping)", flush=True)
                return
            accepted, detail = request_pod_stop(pod_id)
            if accepted:
                print(f"stop accepted: {detail}", flush=True)
                return
            print(f"pod stop failed: {detail}; will retry", flush=True)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
