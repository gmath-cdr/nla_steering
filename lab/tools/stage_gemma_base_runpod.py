"""Stage the exact authenticated Gemma base checkpoint on the AxB44 volume.

This is a lifecycle-guarded prerequisite, not an experiment.  It downloads
only the 13 base-model files named in the original Gemma AxB44 receipt at the
original pinned Hugging Face revision.  The NLA AV and AR are not downloaded.
After staging, the existing offline verifier must reproduce the old receipt.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

from run_lexical_alpha_sensitivity import (
    EXPECTED_NETWORK_VOLUME,
    acquire_controller_lock,
    endpoint,
    read_secret,
    release_controller_lock,
    stamp,
)


REPOSITORY = "google/gemma-3-12b-it"
REVISION = "96b6f1eccf38110c56df3a15bffe176da04bfd80"
SOURCE_COMPLETE_SHA256 = "d251d0d70d7abcdbb9bb3292296db30f0c59afd4c73044b14ed7a08f812d4912"
VERIFIED_FILES = 13
VERIFIED_BYTES = 24_414_134_098
ALLOW_PATTERNS = [
    "added_tokens.json",
    "config.json",
    "generation_config.json",
    "model-00001-of-00005.safetensors",
    "model-00002-of-00005.safetensors",
    "model-00003-of-00005.safetensors",
    "model-00004-of-00005.safetensors",
    "model-00005-of-00005.safetensors",
    "model.safetensors.index.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--secrets", type=Path, required=True)
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--pod-id", required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    secrets = args.secrets.resolve()
    ssh_key = args.ssh_key.resolve()
    if not ssh_key.is_file():
        raise RuntimeError("SSH private key is absent")
    sys.path.insert(0, str(repo / "lab" / "tools"))
    import runpod_guard  # type: ignore

    api_key = read_secret(secrets, "RUNPOD_API_KEY")
    hf_token = read_secret(secrets, "HUGGING_FACE")
    local = repo.parent / "axb44-gemma-base-stage"
    local.mkdir(parents=True, exist_ok=True)
    controller_log = local / "controller.log"
    guard_log = local / "guard.log"
    lease = local / "controller.lease"
    known_hosts = local / "known_hosts"
    verification_local = local / "gemma-base-verification.json"
    lock = acquire_controller_lock(local / "controller.lock")

    stop_required = False
    stop_verified = False
    guard_process: subprocess.Popen[bytes] | None = None
    guard_handle = None
    heartbeat_stop = threading.Event()
    succeeded = False

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
        if str(pod.get("desiredStatus")) == "RUNNING":
            raise RuntimeError("refusing to take custody of an already-running pod")

        stop_required = True
        while True:
            try:
                runpod_guard.start_pod(args.pod_id, api_key)
                log("pod start accepted for model staging")
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
                sys.executable, str(repo / "lab" / "tools" / "runpod_guard.py"),
                "guard", "--pod-id", args.pod_id,
                "--lease-file", str(lease), "--lease-minutes", "10",
                "--max-runtime-minutes", "180", "--poll-seconds", "15",
                "--stop-grace-minutes", "5",
            ],
            stdout=guard_handle,
            stderr=subprocess.STDOUT,
            env=guard_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=heartbeat, daemon=True).start()
        log("independent model-staging guard active")

        connected = None
        deadline = time.monotonic() + 20 * 60
        while time.monotonic() < deadline:
            try:
                pod = runpod_guard.get_pod(args.pod_id, api_key)
            except runpod_guard.GuardError:
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
        log("SSH ready for exact Gemma base staging")

        preflight = """
set -euo pipefail
gpu=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
[[ $gpu == *A100* ]]
available=$(df -Pk /workspace | awk 'NR==2 {print $4}')
[[ -n $available && $available -ge 31457280 ]]
[[ -d /workspace/nla-source/axb44-lexical-6027c02/.git ]]
test "$(git -C /workspace/nla-source/axb44-lexical-6027c02 rev-parse HEAD)" = 6027c0299db64401ef62d97c53e628c213bbb299
[[ -z $(git -C /workspace/nla-source/axb44-lexical-6027c02 status --porcelain) ]]
mkdir -p /workspace/nla-controller/axb44-gemma-base-stage /workspace/models
echo "PREFLIGHT_OK gpu=$gpu available_kb=$available"
"""
        with controller_log.open("ab") as output:
            result = subprocess.run(
                ssh_args(host, port) + ["bash -lc " + shlex.quote(preflight)],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if result.returncode:
            raise RuntimeError("remote staging preflight failed")

        # RunPod rebuilds /root on every stopped-pod restart.  Recreate the
        # exact historical AxB44 environment before downloading or verifying.
        runtime_setup = """
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
import huggingface_hub, numpy, torch, transformers, yaml
assert torch.__version__ == '2.8.0+cu128'
assert transformers.__version__ == '4.57.6'
assert huggingface_hub.__version__ == '0.36.2'
assert numpy.__version__ == '2.1.2'
assert yaml.__version__ == '6.0.3'
assert torch.cuda.is_available()
assert 'A100' in torch.cuda.get_device_name(0)
assert torch.cuda.get_device_capability(0) == (8, 0)
assert torch.version.cuda == '12.8'
PY
"""
        with controller_log.open("ab") as output:
            result = subprocess.run(
                ssh_args(host, port) + ["bash -lc " + shlex.quote(runtime_setup)],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if result.returncode:
            raise RuntimeError("historical staging environment could not be recreated")
        log("historical AxB44 environment recreated and verified")

        token_path = "/dev/shm/axb44-gemma-base-hf-token"
        upload_token = f"umask 077; cat > {token_path}; test -s {token_path}"
        result = subprocess.run(
            ssh_args(host, port) + ["bash -lc " + shlex.quote(upload_token)],
            input=(hf_token + "\n").encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("could not stage the temporary Hugging Face credential")
        hf_token = ""  # Drop the extra in-process copy as soon as it is staged.

        job_root = "/workspace/nla-controller/axb44-gemma-base-stage"
        job_script = f"{job_root}/run.sh"
        job_status = f"{job_root}/status"
        job_log = f"{job_root}/run.log"
        job_pid = f"{job_root}/pid"
        patterns_json = json.dumps(ALLOW_PATTERNS)
        remote_script = f"""#!/usr/bin/env bash
set -euo pipefail
token_file={token_path}
cleanup() {{ rm -f -- "$token_file"; }}
trap cleanup EXIT
export HF_TOKEN=$(cat "$token_file")
export HF_HOME=/workspace/hf
export HF_HUB_DISABLE_TELEMETRY=1
python=/root/venvs/axb44-7cef0d4-v1/bin/python
dest=/workspace/models/gemma-3-12b-it
if [[ -L "$dest" ]]; then echo 'refusing symlink model destination' >&2; exit 42; fi
mkdir -p "$dest"
$python - <<'PY'
import json
import os
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id={REPOSITORY!r},
    revision={REVISION!r},
    local_dir="/workspace/models/gemma-3-12b-it",
    allow_patterns=json.loads({patterns_json!r}),
    token=os.environ["HF_TOKEN"],
    max_workers=4,
)
PY
unset HF_TOKEN
cd /workspace/nla-source/axb44-lexical-6027c02/lab
$python -m experiments.axbench_lexicality20.lexical_verify_base \
  --model-key gemma \
  --source-run-root /workspace/nla-artifacts/axb44/gemma-run-02 \
  --models-root /workspace/models \
  --output {job_root}/base_verification.json
echo STAGING_COMPLETE
"""
        wrapper = f"""#!/usr/bin/env bash
set +e
(
{remote_script}
)
rc=$?
printf 'EXIT=%s\n' "$rc" > {job_status}.tmp
mv {job_status}.tmp {job_status}
exit "$rc"
"""
        encoded = base64.b64encode(wrapper.encode("utf-8")).decode("ascii")
        launch = f"""
set -euo pipefail
mkdir -p {job_root}
exec 9> {job_root}/launch.lock
flock -n 9 || {{ echo CONTENDED; exit 44; }}
rm -f {job_status}
printf %s {shlex.quote(encoded)} | base64 -d > {job_script}.tmp
mv {job_script}.tmp {job_script}
chmod 700 {job_script}
nohup bash {job_script} 9>&- > {job_log} 2>&1 < /dev/null &
echo $! > {job_pid}
echo STARTED
"""
        result = subprocess.run(
            ssh_args(host, port) + ["bash -lc " + shlex.quote(launch)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode or result.stdout.strip().splitlines()[-1:] != ["STARTED"]:
            raise RuntimeError("remote Gemma staging job did not start")
        log("exact pinned Gemma base download started")

        poll = f"""
set -euo pipefail
if [[ -f {job_status} ]]; then
  cat {job_status}
elif [[ -f {job_pid} ]] && kill -0 "$(cat {job_pid})" 2>/dev/null; then
  files=$(find /workspace/models/gemma-3-12b-it -maxdepth 1 -type f | wc -l)
  bytes=$(du -sk /workspace/models/gemma-3-12b-it | awk '{{print $1}}')
  echo "RUNNING files=$files kb=$bytes"
else
  echo LOST
fi
"""
        deadline = time.monotonic() + 2.5 * 60 * 60
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
            if result is None or result.returncode or not result.stdout.strip():
                if now - last_report >= 60:
                    log("Gemma staging status unavailable; detached download remains guarded")
                    last_report = now
                time.sleep(20)
                continue
            state = result.stdout.strip().splitlines()[-1]
            if state == "EXIT=0":
                break
            if state.startswith("EXIT="):
                raise RuntimeError(f"Gemma staging failed: {state}")
            if state == "LOST":
                raise RuntimeError("Gemma staging process disappeared")
            if now - last_report >= 120:
                log(f"Gemma staging {state}")
                last_report = now
            time.sleep(20)
        else:
            raise RuntimeError("Gemma staging exceeded 150 minutes")

        remote_verification = f"{job_root}/base_verification.json"
        result = subprocess.run(
            [
                "scp", "-i", str(ssh_key), "-P", str(port),
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={known_hosts}",
                f"root@{host}:{remote_verification}", str(verification_local),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("could not retrieve Gemma base verification")
        verification = json.loads(verification_local.read_text(encoding="utf-8"))
        if (
            verification.get("status") != "verified"
            or verification.get("model_key") != "gemma"
            or verification.get("source_complete_sha256") != SOURCE_COMPLETE_SHA256
            or verification.get("source_revision") != REVISION
            or int(verification.get("verified_files", -1)) != VERIFIED_FILES
            or int(verification.get("verified_bytes", -1)) != VERIFIED_BYTES
            or verification.get("network_model_downloads") != 0
        ):
            raise RuntimeError("Gemma base verification identity/count mismatch")
        succeeded = True
        log("Gemma base staged and authenticated against the original AxB44 receipt")
    except Exception as exc:  # noqa: BLE001 - always enter verified shutdown
        log(f"staging controller failure: {type(exc).__name__}: {exc}")
    finally:
        heartbeat_stop.set()
        if stop_required:
            attempt = 0
            next_request = 0.0
            while not stop_verified:
                now = time.monotonic()
                if now >= next_request:
                    attempt += 1
                    try:
                        runpod_guard.stop_pod(args.pod_id, api_key)
                        log(f"pod stop requested attempt={attempt}")
                    except runpod_guard.GuardError as exc:
                        log(f"pod stop attempt {attempt} failed: {exc}")
                    next_request = now + 30
                try:
                    state = str(runpod_guard.get_pod(args.pod_id, api_key).get("desiredStatus"))
                    if state in runpod_guard.FINAL_STATES:
                        stop_verified = True
                        log(f"pod externally verified stopped: {state}")
                except runpod_guard.GuardError as exc:
                    log(f"stop verification retry: {exc}")
                if not stop_verified:
                    time.sleep(10)
        if guard_process is not None and guard_process.poll() is None and stop_verified:
            guard_process.terminate()
        if guard_handle is not None:
            guard_handle.close()
        release_controller_lock(lock)
        log(f"staging controller exit success={succeeded} stop_verified={stop_verified}")
    return 0 if succeeded and stop_verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
