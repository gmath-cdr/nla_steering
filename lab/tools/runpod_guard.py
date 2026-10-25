"""External RunPod lease guard.

Run this on the controlling laptop, never inside the Pod.  It uses an account
credential held outside the repository, polls lifecycle state, and requests a
stop when either a renewable local lease expires or an absolute runtime ceiling
is reached.  Logs contain pod state and timing only; the credential is never
printed or placed in a URL/process argument.

Examples (PowerShell, from the repository root):

    $env:RUNPOD_SECRETS_FILE = '..\\secrets.txt'
    python lab/tools/runpod_guard.py start --pod-id POD_ID
    python lab/tools/runpod_guard.py heartbeat --lease-file C:\\tmp\\nla.lease
    python lab/tools/runpod_guard.py guard --pod-id POD_ID `
      --lease-file C:\\tmp\\nla.lease --lease-minutes 20 `
      --max-runtime-minutes 180
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_ROOT = "https://rest.runpod.io/v1"
# RunPod's edge rejects the default ``Python-urllib`` client name with HTTP 403
# (error 1010) before the request reaches the API, so every call names itself.
USER_AGENT = "nla-steering-lab-runpod-guard/1.0"
FINAL_STATES = {"EXITED", "TERMINATED"}


class GuardError(RuntimeError):
    """A sanitized lifecycle-control failure."""


def load_api_key(secrets_file: Path | None) -> str:
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if key:
        return key
    if secrets_file is None:
        configured = os.environ.get("RUNPOD_SECRETS_FILE", "").strip()
        secrets_file = Path(configured) if configured else None
    if secrets_file is None:
        raise GuardError(
            "set RUNPOD_API_KEY or RUNPOD_SECRETS_FILE outside the repository"
        )
    try:
        lines = secrets_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GuardError(f"cannot read secrets file: {type(exc).__name__}") from exc
    for line in lines:
        name, separator, value = line.partition("=")
        if separator and name.strip() == "RUNPOD_API_KEY":
            key = value.strip()
            if key:
                return key
    raise GuardError("RUNPOD_API_KEY entry is absent or empty")


def _request(
    method: str, path: str, api_key: str, *, timeout: int = 30
) -> dict[str, Any]:
    request = urllib.request.Request(
        API_ROOT + path,
        data=b"" if method != "GET" else None,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise GuardError(f"RunPod {method} returned HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - redact network/client details
        raise GuardError(f"RunPod {method} failed: {type(exc).__name__}") from exc
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GuardError("RunPod returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise GuardError("RunPod returned an unexpected response shape")
    return value


def get_pod(pod_id: str, api_key: str) -> dict[str, Any]:
    safe_id = urllib.parse.quote(pod_id, safe="")
    return _request("GET", f"/pods/{safe_id}", api_key)


def stop_pod(pod_id: str, api_key: str) -> None:
    safe_id = urllib.parse.quote(pod_id, safe="")
    _request("POST", f"/pods/{safe_id}/stop", api_key)


def start_pod(pod_id: str, api_key: str) -> None:
    safe_id = urllib.parse.quote(pod_id, safe="")
    _request("POST", f"/pods/{safe_id}/start", api_key)


def heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def lease_age_minutes(path: Path, now: float | None = None) -> float:
    if not path.is_file():
        return float("inf")
    now = time.time() if now is None else now
    return max(0.0, (now - path.stat().st_mtime) / 60)


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_guard(args: argparse.Namespace) -> int:
    api_key = load_api_key(args.secrets_file)
    started = time.time()
    stop_requested = False
    while True:
        age = lease_age_minutes(args.lease_file)
        elapsed = (time.time() - started) / 60
        try:
            pod = get_pod(args.pod_id, api_key)
        except GuardError as exc:
            # A guard that exits during a control-plane outage is no guard at
            # all.  Keep retrying and, once the lease or ceiling has passed,
            # also retry the stop request on every poll until a final state is
            # observed.
            print(
                f"{stamp()} status unavailable: {exc}; "
                f"lease_age={age:.1f}min elapsed={elapsed:.1f}min",
                flush=True,
            )
            if age >= args.lease_minutes or elapsed >= args.max_runtime_minutes:
                try:
                    stop_pod(args.pod_id, api_key)
                    stop_requested = True
                    print(f"{stamp()} stop requested while status unavailable", flush=True)
                except GuardError as stop_exc:
                    print(f"{stamp()} stop request failed: {stop_exc}", flush=True)
            time.sleep(args.poll_seconds)
            continue
        status = str(pod.get("desiredStatus", "UNKNOWN"))
        print(
            f"{stamp()} status={status} lease_age={age:.1f}min "
            f"elapsed={elapsed:.1f}min",
            flush=True,
        )
        if status in FINAL_STATES:
            print(f"{stamp()} verified final state {status}", flush=True)
            return 0
        expired = age >= args.lease_minutes
        ceiling = elapsed >= args.max_runtime_minutes
        if expired or ceiling:
            reason = "lease expired" if expired else "absolute ceiling"
            print(f"{stamp()} {reason}; requesting stop", flush=True)
            try:
                stop_pod(args.pod_id, api_key)
                stop_requested = True
            except GuardError as exc:
                print(f"{stamp()} stop request failed: {exc}", flush=True)
        if stop_requested and elapsed >= args.max_runtime_minutes + args.stop_grace_minutes:
            print(
                f"{stamp()} stop is still unverified; continuing to retry",
                flush=True,
            )
        time.sleep(args.poll_seconds)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    beat = sub.add_parser("heartbeat", help="create or renew a local lease")
    beat.add_argument("--lease-file", type=Path, required=True)

    for command, help_text in (
        ("status", "print the Pod lifecycle state"),
        ("start", "request Pod start"),
        ("stop", "request Pod stop"),
    ):
        lifecycle = sub.add_parser(command, help=help_text)
        lifecycle.add_argument("--pod-id", required=True)
        lifecycle.add_argument("--secrets-file", type=Path)

    guard = sub.add_parser("guard", help="stop a Pod when its lease expires")
    guard.add_argument("--pod-id", required=True)
    guard.add_argument("--lease-file", type=Path, required=True)
    guard.add_argument("--lease-minutes", type=float, required=True)
    guard.add_argument("--max-runtime-minutes", type=float, required=True)
    guard.add_argument("--poll-seconds", type=float, default=15)
    guard.add_argument("--stop-grace-minutes", type=float, default=3)
    guard.add_argument("--secrets-file", type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "heartbeat":
        heartbeat(args.lease_file)
        print(f"renewed {args.lease_file}")
        return 0
    if args.command in {"status", "start", "stop"}:
        api_key = load_api_key(args.secrets_file)
        if args.command == "start":
            start_pod(args.pod_id, api_key)
            print(f"{stamp()} start requested for {args.pod_id}")
            return 0
        if args.command == "stop":
            stop_pod(args.pod_id, api_key)
            print(f"{stamp()} stop requested for {args.pod_id}")
            return 0
        pod = get_pod(args.pod_id, api_key)
        print(str(pod.get("desiredStatus", "UNKNOWN")))
        return 0
    if args.lease_minutes <= 0 or args.max_runtime_minutes <= 0:
        raise SystemExit("lease and maximum runtime must be positive")
    if args.poll_seconds <= 0 or args.stop_grace_minutes < 0:
        raise SystemExit("poll interval must be positive and grace nonnegative")
    return run_guard(args)


if __name__ == "__main__":
    raise SystemExit(main())
