"""Read-only sanitized inventory of account Pods for experiment recovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request

from runpod_guard import API_ROOT, USER_AGENT, GuardError, load_api_key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets-file", type=Path, required=True)
    args = parser.parse_args()
    api_key = load_api_key(args.secrets_file)
    request = urllib.request.Request(
        API_ROOT + "/pods",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise GuardError(f"RunPod GET returned HTTP {exc.code}") from exc
    if isinstance(payload, dict):
        rows = payload.get("items", payload.get("pods", []))
    else:
        rows = payload
    if not isinstance(rows, list):
        raise GuardError("RunPod inventory returned an unexpected shape")
    safe = []
    for pod in rows:
        if not isinstance(pod, dict):
            continue
        machine = pod.get("machine") or {}
        gpu = pod.get("gpu") or {}
        safe.append({
            "id": pod.get("id"), "name": pod.get("name"),
            "desired_status": pod.get("desiredStatus"),
            "gpu": machine.get("gpuTypeId") or gpu.get("id"),
            "cost_per_hour": pod.get("costPerHr") or pod.get("adjustedCostPerHr"),
            "network_volume_id": pod.get("networkVolumeId") or
                (pod.get("networkVolume") or {}).get("id"),
        })
    print(json.dumps(sorted(safe, key=lambda row: str(row["id"])), indent=2))


if __name__ == "__main__":
    main()
