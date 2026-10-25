"""Create one RunPod pod and wait for its SSH endpoint.

The account credential is read through ``runpod_guard.load_api_key`` and is
never printed, logged, or written. The pod's environment is never echoed.
This tool does not stop pods; ``runpod_guard.py`` owns shutdown.

PowerShell example, from the repository root::

    python lab/tools/runpod_pods.py create --name nla-random50-qwen-a `
      --secrets-file ..\\..\\..\\secrets.txt --public-key $HOME\\.ssh\\id_ed25519.pub `
      --output C:\\tmp\\pod.json
    python lab/tools/runpod_pods.py wait-ssh --pod-id <id> `
      --secrets-file ..\\..\\..\\secrets.txt --output C:\\tmp\\pod.ssh.json
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from runpod_guard import API_ROOT, USER_AGENT, GuardError, get_pod, load_api_key


# One 80 GB card is required: the target, verbalizer, and reconstructor are
# loaded together. Only A100 types are listed so the hourly price is known.
GPU_TYPES = ["NVIDIA A100-SXM4-80GB", "NVIDIA A100 80GB PCIe"]
IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
NETWORK_VOLUME_ID = "sk1wth7dnv"
SHOWN_FIELDS = (
    "id", "name", "desiredStatus", "costPerHr", "containerDiskInGb",
    "networkVolumeId", "imageName", "machineId",
)


def create_pod(name: str, public_key: str, api_key: str) -> dict[str, Any]:
    payload = {
        "name": name,
        "computeType": "GPU",
        "cloudType": "SECURE",
        "gpuTypeIds": GPU_TYPES,
        "gpuTypePriority": "availability",
        "gpuCount": 1,
        "containerDiskInGb": 120,
        "imageName": IMAGE,
        "networkVolumeId": NETWORK_VOLUME_ID,
        "volumeMountPath": "/workspace",
        "ports": ["22/tcp"],
        "env": {"PUBLIC_KEY": public_key},
    }
    request = urllib.request.Request(
        API_ROOT + "/pods",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode("utf-8", "replace")
        raise GuardError(f"RunPod create returned HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001 - redact network/client details
        raise GuardError(f"RunPod create failed: {type(exc).__name__}") from exc
    if not isinstance(result, dict) or not result.get("id"):
        raise GuardError("RunPod create returned no pod id")
    return result


def ssh_endpoint(pod: dict[str, Any]) -> tuple[str, int] | None:
    host = str(pod.get("publicIp") or "").strip()
    port = (pod.get("portMappings") or {}).get("22")
    if host and port:
        return host, int(port)
    return None


def shown(pod: dict[str, Any]) -> dict[str, Any]:
    return {field: pod.get(field) for field in SHOWN_FIELDS if pod.get(field) not in (None, "")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create one A100 80 GB pod")
    create.add_argument("--name", required=True)
    create.add_argument("--public-key", type=Path, required=True)
    create.add_argument("--secrets-file", type=Path)
    create.add_argument("--output", type=Path, required=True)
    wait = sub.add_parser("wait-ssh", help="wait until the pod exposes SSH")
    wait.add_argument("--pod-id", required=True)
    wait.add_argument("--secrets-file", type=Path)
    wait.add_argument("--output", type=Path, required=True)
    wait.add_argument("--timeout-minutes", type=float, default=15)
    args = parser.parse_args()

    api_key = load_api_key(args.secrets_file)
    if args.command == "create":
        public_key = args.public_key.read_text(encoding="utf-8").strip()
        if not public_key.startswith("ssh-ed25519 "):
            raise SystemExit("unexpected SSH public-key format")
        record = shown(create_pod(args.name, public_key, api_key))
        args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(record))
        return 0

    deadline = time.monotonic() + args.timeout_minutes * 60
    while time.monotonic() < deadline:
        try:
            pod = get_pod(args.pod_id, api_key)
        except GuardError as exc:
            print(f"poll failed: {exc}", flush=True)
            time.sleep(10)
            continue
        endpoint = ssh_endpoint(pod)
        if endpoint:
            record = {**shown(pod), "host": endpoint[0], "port": endpoint[1]}
            args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(record))
            return 0
        time.sleep(10)
    raise SystemExit("pod did not expose SSH before the timeout")


if __name__ == "__main__":
    raise SystemExit(main())
