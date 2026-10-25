"""Guarded RunPod controller for the AxB44 lexical-alpha sensitivity run.

This controller uses one existing A100 pod and its persistent network volume.
It uploads only a small code delta and sealed specs, runs Qwen and Gemma
sequentially, retrieves and verifies each completed archive, and always asks
RunPod to stop in ``finally``.  Credentials stay in local process memory.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any


BASE_COMMIT = "688f4c1e1b45be856762142a0b1f03031893305d"
RUNTIME_COMMIT = "6027c0299db64401ef62d97c53e628c213bbb299"
TAG = "lexical-6027c02"
EXPECTED_NETWORK_VOLUME = "sk1wth7dnv"
ASSET_HASHES = {
    "runtime-incremental.bundle": "cbf0c0fbbe1fc0c1967ab237a5350a3c8e3180fba4a08daaf168c2faae7098b8",
    "sealed-package.tar.gz": "f9a386477ef66ce9f55a7063fa79281673d8bdfbbbf134b53ead071a8be94e49",
    "source.bundle": "2ca33b356447932aa82823161af7fb0d53f402cf92908e752787ebc6a03905ad",
}
SOURCE_COMPLETE_HASHES = {
    "qwen": "faa6d651ee5f3f3336bf90f47b72c34c4e435282a8706bdf58cf53210570519d",
    "gemma": "d251d0d70d7abcdbb9bb3292296db30f0c59afd4c73044b14ed7a08f812d4912",
}
PACKAGE_HASHES = {
    "qwen-lexical-alpha-receipt.json": "daa2c4595e5d617de084cc4701e80200f2bd9f829a95db898eb58a3daba115e1",
    "qwen-materialization-manifest.json": "67a25d1c8953182dd764c9e49de32173762184c9f850c805401b1d5f4b120037",
    "gemma-lexical-alpha-receipt.json": "80e11370825f40f1797e935df3cf3c20ab3029e678091a4bb82285e9fb78d999",
    "gemma-materialization-manifest.json": "a17cc545bd2b596128b0b71a9c33e1cbea0cdd305afe69f1dc6e1042778b425a",
}
SOURCE_RUNS = {
    "qwen": "/workspace/nla-artifacts/axb44/qwen-run-02",
    "gemma": "/workspace/nla-artifacts/axb44/gemma-run-02",
}


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_secret(path: Path, name: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name and value.strip():
            return value.strip()
    raise RuntimeError(f"required secret entry is absent: {name}")


def acquire_controller_lock(path: Path):
    """Hold one OS-released byte lock for this local Pod controller."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("another controller already has custody of this Pod") from exc
    return handle


def release_controller_lock(handle) -> None:
    """Release a lock acquired by :func:`acquire_controller_lock`."""

    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def endpoint(pod: dict[str, Any]) -> tuple[str, int] | None:
    public_ip = str(pod.get("publicIp") or "").strip()
    mappings = pod.get("portMappings") or {}
    if isinstance(mappings, dict) and public_ip:
        mapped = mappings.get("22") or mappings.get(22)
        if mapped:
            return public_ip, int(mapped)
    for row in pod.get("ports") or []:
        if not isinstance(row, dict):
            continue
        private = row.get("privatePort") or row.get("private_port")
        public = row.get("publicPort") or row.get("public_port")
        host = str(row.get("ip") or public_ip).strip()
        if int(private or -1) == 22 and public and host:
            return host, int(public)
    return None


def safe_extract(archive: Path, target: Path) -> Path:
    if target.exists():
        raise RuntimeError(f"refusing to replace prior extraction: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(
        f"{target.name}.partial-{os.getpid()}-{time.time_ns()}"
    )
    partial.mkdir()
    root = partial.resolve()
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        if any(not (root / member.name).resolve().is_relative_to(root) for member in members):
            raise RuntimeError("unsafe result archive member")
        if any(not (member.isfile() or member.isdir()) for member in members):
            raise RuntimeError("result archive contains a link or special file")
        try:
            stream.extractall(partial, members=members, filter="data")
        except TypeError:
            stream.extractall(partial, members=members)
    complete = list(partial.rglob("COMPLETE.json"))
    if len(complete) != 1:
        raise RuntimeError("result archive does not contain one COMPLETE.json")
    complete_relative = complete[0].relative_to(partial)
    partial.replace(target)
    return target / complete_relative


def verify_complete(path: Path, model_key: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != "axb44-lexical-alpha-complete/v1"
        or payload.get("status") != "complete"
        or payload.get("model_key") != model_key
        or payload.get("evidential_status") != "post_hoc_sensitivity"
    ):
        raise RuntimeError("retrieved COMPLETE identity/status mismatch")
    root = path.parent
    for relative, receipt in payload.get("files", {}).items():
        candidate = root / relative
        if (
            not candidate.is_file()
            or candidate.stat().st_size != int(receipt.get("bytes", -1))
            or sha256(candidate) != receipt.get("sha256")
        ):
            raise RuntimeError(f"retrieved artifact mismatch: {relative}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--secrets", type=Path, required=True)
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--pod-id", required=True)
    parser.add_argument("--accept-running-pod", action="store_true")
    args = parser.parse_args()

    repo = args.repo.resolve()
    assets = args.assets.resolve()
    secrets = args.secrets.resolve()
    ssh_key = args.ssh_key.resolve()
    for name, expected in ASSET_HASHES.items():
        path = assets / name
        if not path.is_file() or sha256(path) != expected:
            raise RuntimeError(f"local controller asset hash mismatch: {name}")
    if not ssh_key.is_file():
        raise RuntimeError("SSH private key is absent")

    sys.path.insert(0, str(repo / "lab" / "tools"))
    import runpod_guard  # type: ignore

    api_key = read_secret(secrets, "RUNPOD_API_KEY")
    local = assets / f"runpod-{TAG}-{args.pod_id}"
    local.mkdir(parents=True, exist_ok=True)
    download = local / "download"
    download.mkdir(exist_ok=True)
    controller_log = local / "controller.log"
    guard_log = local / "guard.log"
    lease = local / "controller.lease"
    known_hosts = local / "known_hosts"
    remote_transfer = f"/workspace/transfers/axb44-{TAG}"
    remote_source = f"/workspace/nla-source/axb44-{TAG}"

    connected: tuple[str, int] | None = None
    stop_required = False
    guard_process: subprocess.Popen[bytes] | None = None
    guard_handle = None
    heartbeat_stop = threading.Event()
    run_succeeded = False
    stop_verified = False

    def log(message: str) -> None:
        line = f"{stamp()} {message}"
        print(line, flush=True)
        with controller_log.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def heartbeat() -> None:
        while not heartbeat_stop.wait(30):
            lease.touch(exist_ok=True)

    def ssh_args(host: str, port: int) -> list[str]:
        return [
            "ssh", "-i", str(ssh_key), "-p", str(port),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}", f"root@{host}",
        ]

    def scp_args(host: str, port: int) -> list[str]:
        return [
            "scp", "-i", str(ssh_key), "-P", str(port),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}",
        ]

    def run_logged(command: list[str]) -> None:
        with controller_log.open("ab") as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            raise RuntimeError(f"child command returned {result.returncode}: {command[0]}")

    def remote_hash(host: str, port: int, path: str, expected: str) -> bool:
        body = f"[[ $(sha256sum {shlex.quote(path)} 2>/dev/null | awk '{{print $1}}') == {expected} ]]"
        return subprocess.run(
            ssh_args(host, port) + ["bash -lc " + shlex.quote(body)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0

    def retrieve(host: str, port: int, model_key: str, suffix: str) -> Path:
        remote_root = f"/workspace/nla-artifacts/axb44-lexical-alpha-v1/{model_key}"
        name = f"axb44-{TAG}-{model_key}-{suffix}.tar.gz"
        body = (
            "set -euo pipefail; "
            "cd /workspace/nla-artifacts/axb44-lexical-alpha-v1; "
            f"test -d {shlex.quote(model_key)}; "
            f"exec 7> {shlex.quote(name + '.pack.lock')}; flock 7; "
            f"tmp={shlex.quote(name + '.tmp')}.$$; "
            f"trap 'rm -f -- \"$tmp\"' EXIT; "
            f"tar -czf \"$tmp\" {shlex.quote(model_key)}; "
            f"mv \"$tmp\" {shlex.quote(name)}; trap - EXIT; "
            f"sha256sum {shlex.quote(name)} > {shlex.quote(name + '.sha256')}"
        )
        run_logged(ssh_args(host, port) + ["bash -lc " + shlex.quote(body)])
        run_logged(
            scp_args(host, port)
            + [
                f"root@{host}:/workspace/nla-artifacts/axb44-lexical-alpha-v1/{name}",
                f"root@{host}:/workspace/nla-artifacts/axb44-lexical-alpha-v1/{name}.sha256",
                str(download),
            ]
        )
        archive = download / name
        expected = (download / f"{name}.sha256").read_text(encoding="utf-8").split()[0]
        if sha256(archive) != expected:
            raise RuntimeError(f"downloaded {model_key} archive SHA-256 mismatch")
        if suffix == "complete":
            # Key the extraction by the downloaded archive.  A controller
            # restart can safely revalidate an identical prior extraction,
            # while a newly packed archive gets a separate directory.
            verified = local / f"verified-{model_key}-{expected[:16]}"
            if verified.exists():
                complete_matches = list(verified.rglob("COMPLETE.json"))
                if len(complete_matches) != 1:
                    raise RuntimeError(
                        f"prior {model_key} extraction is incomplete or ambiguous"
                    )
                complete = complete_matches[0]
            else:
                complete = safe_extract(archive, verified)
            verify_complete(complete, model_key)
        log(f"retrieved {model_key} {suffix} archive sha256={expected}")
        return archive

    def run_remote_job(
        host: str,
        port: int,
        model_key: str,
        experiment: str,
    ) -> None:
        """Run one resumable model job without tying it to one SSH session."""

        run_root = f"/workspace/nla-artifacts/axb44-lexical-alpha-v1/{model_key}"
        # Controller metadata must remain outside run_root.  COMPLETE.json
        # recursively seals run_root, so logs/PIDs there would mutate evidence
        # after the receipt was written.
        job_root = f"/workspace/nla-controller/axb44-lexical-alpha-v1/{model_key}"
        legacy_job_root = f"{run_root}/controller-job"
        job_script = f"{job_root}/run.sh"
        job_log = f"{job_root}/run.log"
        job_pid = f"{job_root}/pid"
        job_status = f"{job_root}/status"
        job_instance = f"{job_root}/instance_id"
        wrapper = f"""#!/usr/bin/env bash
set +e
(
{experiment}
)
rc=$?
instance=$(cat {job_instance})
printf 'INSTANCE=%s EXIT=%s\\n' "$instance" "$rc" > {job_status}.tmp
mv {job_status}.tmp {job_status}
exit "$rc"
"""
        encoded = base64.b64encode(wrapper.encode("utf-8")).decode("ascii")
        launch = f"""
set -euo pipefail
mkdir -p {job_root}
exec 9> {job_root}/launch.lock
flock -n 9 || {{ echo CONTENDED; exit 44; }}
current_instance=$(cat /dev/shm/axb44-controller-instance)
if [[ ! -f {run_root}/COMPLETE.json ]] && [[ -d {legacy_job_root} ]]; then
  mkdir -p {job_root}/legacy
  mv {legacy_job_root} {job_root}/legacy/run-root-controller-job-$(date +%s%N)
fi
if [[ -f {job_status} ]] && ! grep -Eq "^INSTANCE=${{current_instance}} EXIT=[0-9]+$" {job_status}; then
  mv {job_status} {job_status}.interrupted-$(date +%s%N)
fi
if [[ -f {job_instance} ]] && [[ $(cat {job_instance}) != "$current_instance" ]]; then
  rm -f {job_pid} {job_instance}
fi
if [[ -f {job_status} ]]; then
  if grep -Eq "^INSTANCE=${{current_instance}} EXIT=0$" {job_status} \
    && [[ -f {run_root}/COMPLETE.json ]]; then
    echo COMPLETE
  else
    cat {job_status}
  fi
elif [[ -f {run_root}/COMPLETE.json ]]; then
  if [[ -f {job_pid} ]] && [[ -f {job_instance} ]] \
    && [[ $(cat {job_instance}) == "$current_instance" ]] \
    && kill -0 "$(cat {job_pid})" 2>/dev/null; then
    echo FINISHING
  else
    echo COMPLETE
  fi
elif [[ -f {job_pid} ]] && [[ -f {job_instance} ]] \
  && [[ $(cat {job_instance}) == "$current_instance" ]] \
  && kill -0 "$(cat {job_pid})" 2>/dev/null \
  && tr '\\0' ' ' < "/proc/$(cat {job_pid})/cmdline" | grep -Fq {job_script}; then
  echo RUNNING
else
  printf %s {shlex.quote(encoded)} | base64 -d > {job_script}.tmp
  mv {job_script}.tmp {job_script}
  chmod 700 {job_script}
  rm -f {job_status}
  printf '%s\\n' "$current_instance" > {job_instance}
  nohup bash {job_script} 9>&- > {job_log} 2>&1 < /dev/null &
  echo $! > {job_pid}
  echo STARTED
fi
"""

        launch_deadline = time.monotonic() + 15 * 60
        while True:
            try:
                result = subprocess.run(
                    ssh_args(host, port) + ["bash -lc " + shlex.quote(launch)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                result = None
            if result is not None and result.returncode == 0:
                output = result.stdout.strip()
                if not output:
                    result = None
                else:
                    state = output.splitlines()[-1]
                    if " EXIT=" in state and not state.endswith(" EXIT=0"):
                        raise RuntimeError(f"prior {model_key} remote job failed: {state}")
                    log(f"{model_key} detached remote job state={state}")
                    break
            if time.monotonic() >= launch_deadline:
                raise RuntimeError(f"could not launch or find {model_key} remote job")
            log(f"{model_key} remote launch probe unavailable; retrying")
            time.sleep(20)

        poll = f"""
set -euo pipefail
current_instance=$(cat /dev/shm/axb44-controller-instance)
if [[ -f {job_status} ]]; then
  if grep -Eq "^INSTANCE=${{current_instance}} EXIT=0$" {job_status} \
    && [[ -f {run_root}/COMPLETE.json ]]; then
    echo COMPLETE
  else
    cat {job_status}
  fi
elif [[ -f {run_root}/COMPLETE.json ]]; then
  if [[ -f {job_pid} ]] && [[ -f {job_instance} ]] \
    && [[ $(cat {job_instance}) == "$current_instance" ]] \
    && kill -0 "$(cat {job_pid})" 2>/dev/null; then
    echo FINISHING
  else
    echo COMPLETE
  fi
elif [[ -f {job_pid} ]] && [[ -f {job_instance} ]] \
  && [[ -s /dev/shm/axb44-controller-instance ]] \
  && [[ $(cat {job_instance}) == $(cat /dev/shm/axb44-controller-instance) ]] \
  && kill -0 "$(cat {job_pid})" 2>/dev/null \
  && tr '\\0' ' ' < "/proc/$(cat {job_pid})/cmdline" | grep -Fq {job_script}; then
  if [[ -d {run_root}/results/new ]]; then
    count=$(find {run_root}/results/new -maxdepth 1 -type f -name '*.json' | wc -l)
  else
    count=0
  fi
  echo RUNNING_RESULTS=$count
else
  echo LOST
fi
"""
        deadline = time.monotonic() + 6 * 60 * 60
        last_state = ""
        last_report = 0.0
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    ssh_args(host, port) + ["bash -lc " + shlex.quote(poll)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                result = None
            now = time.monotonic()
            if result is None or result.returncode != 0:
                if now - last_report >= 60:
                    log(f"{model_key} remote status unavailable; job remains detached")
                    last_report = now
                time.sleep(20)
                continue
            output = result.stdout.strip()
            if not output:
                if now - last_report >= 60:
                    log(f"{model_key} remote status empty; job remains detached")
                    last_report = now
                time.sleep(20)
                continue
            state = output.splitlines()[-1]
            if state == "COMPLETE":
                log(f"{model_key} detached remote job completed")
                return
            if " EXIT=" in state and state.endswith(" EXIT=0"):
                raise RuntimeError(f"{model_key} job exited without COMPLETE.json")
            if " EXIT=" in state:
                raise RuntimeError(f"{model_key} remote job failed: {state}")
            if state == "LOST":
                raise RuntimeError(f"{model_key} remote job disappeared")
            if state != last_state or now - last_report >= 300:
                log(f"{model_key} detached remote job state={state}")
                last_state = state
                last_report = now
            time.sleep(20)
        raise RuntimeError(f"{model_key} remote job exceeded six-hour controller ceiling")

    controller_lock = acquire_controller_lock(local / "controller.lock")
    try:
        while True:
            try:
                pod = runpod_guard.get_pod(args.pod_id, api_key)
                break
            except runpod_guard.GuardError as exc:
                log(f"initial pod status unavailable ({exc}); retrying")
                time.sleep(15)
        if pod.get("networkVolumeId") != EXPECTED_NETWORK_VOLUME:
            raise RuntimeError("pod is not attached to the expected persistent volume")
        if str(pod.get("volumeMountPath") or "") != "/workspace":
            raise RuntimeError("pod has an unexpected persistent-volume mount path")
        state = str(pod.get("desiredStatus"))
        if state == "RUNNING":
            if not args.accept_running_pod:
                raise RuntimeError("refusing to take custody of an already-running pod")
            stop_required = True
            log("accepted custody of explicitly approved running pod")
        else:
            stop_required = True
            while True:
                try:
                    runpod_guard.start_pod(args.pod_id, api_key)
                    log("pod start accepted")
                    break
                except runpod_guard.GuardError as exc:
                    log(f"pod start response unavailable ({exc}); checking state")
                    time.sleep(15)
                    try:
                        pod = runpod_guard.get_pod(args.pod_id, api_key)
                    except runpod_guard.GuardError:
                        continue
                    if str(pod.get("desiredStatus")) == "RUNNING":
                        log("pod start externally observed after ambiguous response")
                        break
                    time.sleep(45)

        lease.touch(exist_ok=True)
        guard_env = os.environ.copy()
        guard_env["RUNPOD_SECRETS_FILE"] = str(secrets)
        guard_handle = guard_log.open("ab")
        guard_process = subprocess.Popen(
            [
                sys.executable,
                str(repo / "lab" / "tools" / "runpod_guard.py"),
                "guard",
                "--pod-id", args.pod_id,
                "--lease-file", str(lease),
                "--lease-minutes", "10",
                "--max-runtime-minutes", "360",
                "--poll-seconds", "15",
                "--stop-grace-minutes", "5",
            ],
            stdout=guard_handle,
            stderr=subprocess.STDOUT,
            env=guard_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=heartbeat, daemon=True).start()
        log("independent dead-controller lease guard active")

        deadline = time.monotonic() + 20 * 60
        while time.monotonic() < deadline:
            try:
                pod = runpod_guard.get_pod(args.pod_id, api_key)
            except runpod_guard.GuardError as exc:
                log(f"SSH endpoint status unavailable ({exc}); retrying")
                time.sleep(15)
                continue
            candidate = endpoint(pod)
            if candidate:
                host, port = candidate
                probe = subprocess.run(
                    ssh_args(host, port) + ["true"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
                if probe.returncode == 0:
                    connected = candidate
                    break
            time.sleep(15)
        if connected is None:
            raise RuntimeError("pod did not expose working SSH within 20 minutes")
        host, port = connected
        log("SSH ready at API-provided endpoint")

        preflight = (
            "set -euo pipefail; "
            "gpu=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1); "
            "[[ $gpu == *A100* ]] || { echo WRONG_GPU=$gpu; exit 41; }; "
            "available=$(df -Pk /workspace | awk 'NR==2 {print $4}'); "
            "[[ -n $available && $available -ge 10485760 ]]; "
            "command -v flock >/dev/null; "
            "if [[ ! -s /dev/shm/axb44-controller-instance ]]; then "
            "cat /proc/sys/kernel/random/uuid > /dev/shm/axb44-controller-instance; fi; "
            f"mkdir -p {shlex.quote(remote_transfer)} /workspace/nla-source "
            "/workspace/nla-artifacts/axb44-lexical-alpha-v1 "
            "/workspace/nla-controller/axb44-lexical-alpha-v1; "
            "echo PREFLIGHT_OK gpu=$gpu available_kb=$available"
        )
        run_logged(ssh_args(host, port) + ["bash -lc " + shlex.quote(preflight)])

        uploads: list[str] = []
        for name in ("runtime-incremental.bundle", "sealed-package.tar.gz"):
            if not remote_hash(host, port, f"{remote_transfer}/{name}", ASSET_HASHES[name]):
                uploads.append(str(assets / name))
        remote_existing_source = "/workspace/nla-source/axb44-688f4c1/.git"
        source_probe = subprocess.run(
            ssh_args(host, port)
            + ["bash -lc " + shlex.quote(f"test -d {remote_existing_source}")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if source_probe.returncode and not remote_hash(
            host, port, f"{remote_transfer}/source.bundle", ASSET_HASHES["source.bundle"]
        ):
            uploads.append(str(assets / "source.bundle"))
        if uploads:
            run_logged(scp_args(host, port) + uploads + [f"root@{host}:{remote_transfer}/"])
        for name in ("runtime-incremental.bundle", "sealed-package.tar.gz"):
            if not remote_hash(host, port, f"{remote_transfer}/{name}", ASSET_HASHES[name]):
                raise RuntimeError(f"remote controller asset hash mismatch: {name}")
        log("code delta and sealed package staged and hash-verified")

        setup_source = f"""
set -euo pipefail
exec 8> {shlex.quote(remote_source + '.prepare.lock')}
flock 8
if [[ ! -d {shlex.quote(remote_source + '/.git')} ]]; then
  temporary={shlex.quote(remote_source + '.prepare')}.$$
  trap 'rm -rf -- "$temporary"' EXIT
  if [[ -d /workspace/nla-source/axb44-688f4c1/.git ]]; then
    git clone --no-hardlinks /workspace/nla-source/axb44-688f4c1 "$temporary"
  else
    [[ $(sha256sum {remote_transfer}/source.bundle | awk '{{print $1}}') == {ASSET_HASHES['source.bundle']} ]]
    git clone {remote_transfer}/source.bundle "$temporary"
  fi
  cd "$temporary"
  git checkout --detach {BASE_COMMIT}
  git fetch {remote_transfer}/runtime-incremental.bundle axb44-lexical-runtime-base
  git checkout --detach {RUNTIME_COMMIT}
  [[ -z $(git status --porcelain) ]]
  test "$(git rev-parse HEAD)" = {RUNTIME_COMMIT}
  mv "$temporary" {shlex.quote(remote_source)}
  trap - EXIT
fi
cd {shlex.quote(remote_source)}
[[ -z $(git status --porcelain) ]]
test "$(git rev-parse HEAD)" = {RUNTIME_COMMIT}
"""
        run_logged(ssh_args(host, port) + ["bash -lc " + shlex.quote(setup_source)])
        log(f"clean runtime source prepared at {RUNTIME_COMMIT[:8]}")

        # Reuse the historical environment when it survives.  RunPod's root
        # disk may be reset while the persistent /workspace volume remains;
        # in that case recreate the environment with the exact command and
        # versions used by the original AxB44 controller.
        runtime_probe = """
set -euo pipefail
py=/root/venvs/axb44-7cef0d4-v1/bin/python
if [[ ! -x $py ]]; then
  venv=/root/venvs/axb44-7cef0d4-v1
  [[ ! -e $venv ]] || { echo UNHEALTHY_CAMPAIGN_VENV_ALREADY_EXISTS; exit 43; }
  python3 -m venv --system-site-packages $venv
  py=$venv/bin/python
  $py -m pip install --disable-pip-version-check \
    transformers==4.57.6 tokenizers==0.22.2 accelerate==1.14.0 \
    numpy==2.1.2 safetensors==0.8.0 huggingface_hub==0.36.2 \
    PyYAML==6.0.3 orjson==3.12.0 pyarrow==25.0.1 pandas==3.0.5 \
    httpx==0.28.1
fi
$py -m pip check
$py - <<'PY'
import torch, transformers, numpy, yaml
assert torch.__version__ == '2.8.0+cu128'
assert transformers.__version__ == '4.57.6'
assert numpy.__version__ == '2.1.2'
assert yaml.__version__ == '6.0.3'
assert torch.cuda.is_available()
assert 'A100' in torch.cuda.get_device_name(0)
assert torch.cuda.get_device_capability(0) == (8, 0)
assert torch.version.cuda == '12.8'
PY
"""
        run_logged(ssh_args(host, port) + ["bash -lc " + shlex.quote(runtime_probe)])
        log("historical A100 software environment verified")

        for model_key in ("qwen", "gemma"):
            source_root = SOURCE_RUNS[model_key]
            run_root = f"/workspace/nla-artifacts/axb44-lexical-alpha-v1/{model_key}"
            package_checks = "\n".join(
                f"[[ $(sha256sum {run_root}/package/{name} | awk '{{print $1}}') == {digest} ]]"
                for name, digest in PACKAGE_HASHES.items()
            )
            prepare = f"""
set -euo pipefail
[[ $(sha256sum {source_root}/COMPLETE.json | awk '{{print $1}}') == {SOURCE_COMPLETE_HASHES[model_key]} ]]
mkdir -p {run_root}/package
if [[ ! -f {run_root}/package/qwen-materialization-manifest.json ]]; then
  tar -xzf {remote_transfer}/sealed-package.tar.gz -C {run_root}/package
fi
{package_checks}
"""
            run_logged(ssh_args(host, port) + ["bash -lc " + shlex.quote(prepare)])
            experiment = f"""
set -euo pipefail
export NLA_EXTERNAL_GUARD_CONFIRMED=1
export NLA_MODELS_DIR=/workspace/models
export NLA_PYTHON=/root/venvs/axb44-7cef0d4-v1/bin/python
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export AXB44_MODEL={model_key}
export AXB44_SOURCE_RUN_ROOT={source_root}
export AXB44_SENSITIVITY_ROOT={run_root}
export AXB44_SENSITIVITY_PACKAGE={run_root}/package
cd {remote_source}/lab
bash experiments/axbench_lexicality20/run_lexical_sensitivity_model.sh
"""
            log(f"starting {model_key} canary and lexical-alpha delta")
            run_remote_job(host, port, model_key, experiment)
            retrieve(host, port, model_key, "complete")
            log(f"{model_key} complete archive verified locally")

        run_succeeded = True
    except Exception as exc:  # noqa: BLE001 - preserve partial data and stop
        log(f"controller failure: {type(exc).__name__}: {exc}")
        log("partial outputs remain on the persistent volume and are non-evidential")
    finally:
        heartbeat_stop.set()
        if stop_required:
            # Keep requesting and externally checking until RunPod reports a
            # final state.  The independent guard is terminated only after
            # that external verification.
            attempt = 0
            next_stop_request = 0.0
            while not stop_verified:
                now = time.monotonic()
                if now >= next_stop_request:
                    attempt += 1
                    try:
                        runpod_guard.stop_pod(args.pod_id, api_key)
                        log(f"pod stop requested attempt={attempt}")
                    except runpod_guard.GuardError as exc:
                        log(f"pod stop attempt {attempt} failed: {exc}")
                    next_stop_request = now + 30
                try:
                    state = str(runpod_guard.get_pod(args.pod_id, api_key).get("desiredStatus"))
                    if state in runpod_guard.FINAL_STATES:
                        stop_verified = True
                        log(f"pod externally verified stopped: {state}")
                except runpod_guard.GuardError as exc:
                    log(f"stop verification retry: {exc}")
                if stop_verified:
                    break
                time.sleep(10)
        if guard_process is not None and guard_process.poll() is None:
            if stop_verified:
                guard_process.terminate()
            else:
                log("stop not verified; leaving independent lease guard active")
        if guard_handle is not None:
            guard_handle.close()
        release_controller_lock(controller_lock)
        log(f"controller exit run_success={run_succeeded} stop_verified={stop_verified}")
    return 0 if run_succeeded and stop_verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
