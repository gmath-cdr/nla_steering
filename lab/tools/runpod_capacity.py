"""Read-only RunPod capacity monitor using the official ``runpodctl`` CLI.

This process never creates, starts, stops, or modifies a Pod. The account
credential is passed to a child process through its environment, never through
an argument, URL, output file, or log line. ``runpodctl datacenter list`` is
the same working interface used during the 2026-08-24 A100 allocation.

PowerShell example, from the repository root::

    $env:RUNPOD_SECRETS_FILE = '..\\secrets.txt'
    $env:RUNPODCTL = 'C:\\tools\\runpodctl.exe'
    python lab/tools/runpod_capacity.py watch --data-center-id US-MD-1 `
      --signal-file C:\\tmp\\nla-capacity-ready.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runpod_guard import load_api_key


DEFAULT_GPU_TYPES = (
    "NVIDIA A100-SXM4-80GB",
    "NVIDIA A100 80GB PCIe",
    "NVIDIA H100 80GB HBM3",
    "NVIDIA H100 PCIe",
    "NVIDIA H100 NVL",
)
READY_STATES = {"low", "medium", "high"}


class CapacityError(RuntimeError):
    """A sanitized official-CLI capacity failure."""


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_runpodctl(explicit: str | None = None) -> str:
    candidate = explicit or os.environ.get("RUNPODCTL", "").strip()
    if candidate:
        path = Path(candidate)
        if not path.is_file():
            raise CapacityError("configured runpodctl executable does not exist")
        return str(path.resolve())
    found = shutil.which("runpodctl")
    if found is None:
        raise CapacityError("runpodctl is not installed or configured")
    return found


def parse_capacity(
    decoded: Any, *, data_center_id: str,
    gpu_types: tuple[str, ...] = DEFAULT_GPU_TYPES,
) -> list[dict[str, Any]]:
    if not isinstance(decoded, list):
        raise CapacityError("runpodctl returned an unexpected JSON shape")
    regions = [
        row for row in decoded
        if isinstance(row, dict) and row.get("id") == data_center_id
    ]
    if len(regions) != 1:
        raise CapacityError("requested data center was not uniquely returned")
    availability = regions[0].get("gpuAvailability", [])
    if not isinstance(availability, list):
        raise CapacityError("runpodctl returned malformed GPU availability")
    wanted = set(gpu_types)
    selected: list[dict[str, Any]] = []
    for row in availability:
        if not isinstance(row, dict) or row.get("gpuId") not in wanted:
            continue
        selected.append({
            "id": str(row["gpuId"]),
            "display_name": str(row.get("displayName", row["gpuId"])),
            "stock_status": str(row.get("stockStatus") or "None"),
        })
    found = {row["id"] for row in selected}
    for gpu_id in sorted(wanted - found):
        selected.append({
            "id": gpu_id,
            "display_name": gpu_id,
            "stock_status": "Missing",
        })
    return sorted(selected, key=lambda row: row["id"])


def query_capacity(
    api_key: str,
    *,
    data_center_id: str,
    gpu_types: tuple[str, ...] = DEFAULT_GPU_TYPES,
    runpodctl: str | None = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    executable = resolve_runpodctl(runpodctl)
    environment = os.environ.copy()
    environment["RUNPOD_API_KEY"] = api_key
    try:
        completed = subprocess.run(
            [executable, "datacenter", "list", "-o", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except Exception as exc:  # noqa: BLE001 - keep credential context private
        raise CapacityError(f"runpodctl failed: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise CapacityError(f"runpodctl returned exit code {completed.returncode}")
    try:
        decoded = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CapacityError("runpodctl returned malformed JSON") from exc
    return parse_capacity(
        decoded, data_center_id=data_center_id, gpu_types=gpu_types
    )


def ready(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if str(row["stock_status"]).casefold() in READY_STATES
    ]


def summary(rows: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{row['display_name']}={row['stock_status']}" for row in rows
    )


def write_signal(path: Path, data_center_id: str, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "runpod-capacity-signal/v1",
        "observed_at": stamp(),
        "data_center_id": data_center_id,
        "ready": ready(rows),
        "all_targets": rows,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("command", choices=("once", "watch"))
    root.add_argument("--data-center-id", required=True)
    root.add_argument("--gpu-type", action="append", dest="gpu_types")
    root.add_argument("--interval-seconds", type=float, default=60)
    root.add_argument("--max-minutes", type=float, default=360)
    root.add_argument("--signal-file", type=Path)
    root.add_argument("--secrets-file", type=Path)
    root.add_argument("--runpodctl")
    return root


def main() -> int:
    args = parser().parse_args()
    if args.interval_seconds <= 0 or args.max_minutes <= 0:
        raise SystemExit("interval and maximum duration must be positive")
    gpu_types = tuple(args.gpu_types or DEFAULT_GPU_TYPES)
    api_key = load_api_key(args.secrets_file)
    started = time.monotonic()
    while True:
        try:
            rows = query_capacity(
                api_key, data_center_id=args.data_center_id,
                gpu_types=gpu_types, runpodctl=args.runpodctl,
            )
            print(f"{stamp()} {summary(rows)}", flush=True)
            if ready(rows):
                if args.signal_file is not None:
                    write_signal(args.signal_file, args.data_center_id, rows)
                return 0
        except CapacityError as exc:
            print(f"{stamp()} capacity query failed: {exc}", flush=True)
            if args.command == "once":
                return 2
        if args.command == "once":
            return 1
        elapsed_minutes = (time.monotonic() - started) / 60
        if elapsed_minutes >= args.max_minutes:
            print(f"{stamp()} monitor duration ended without capacity", flush=True)
            return 1
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
