"""Atomically update the machine-readable queue status from stage markers."""
import argparse, datetime, json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--log-dir", required=True)
p.add_argument("--event", required=True)
args = p.parse_args()
log = Path(args.log_dir)
stages = [
    "qwen_baseline", "qwen_baseline_verify", "qwen_av_download", "qwen_ar_download",
    "qwen_nla_calibration_vectors", "qwen_nla_alpha_sweep", "qwen_nla",
    "qwen_nla_score", "qwen_nla_verify", "qwen_cache_cleanup", "gemma_baseline",
    "gemma_baseline_verify", "gemma_av_download", "gemma_ar_download",
    "gemma_nla_calibration_vectors", "gemma_nla_alpha_sweep", "gemma_nla",
    "gemma_nla_score", "gemma_nla_verify", "final_report"]
status = {}
for stage in stages:
    if (log / f"{stage}.done").exists():
        state = "completed"
    elif (log / f"{stage}.failed").exists():
        state = "failed"
    elif (log / f"{stage}.exit_code").exists() and (log / f"{stage}.exit_code").read_text().strip() != "0":
        state = "failed_or_retrying"
    else:
        state = "pending"
    status[stage] = state
parts = args.event.split()
if parts and parts[0] in ("START", "WAIT") and len(parts) > 1 and parts[1] in status:
    status[parts[1]] = "running"
payload = {"updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "last_event": args.event, "stages": status}
tmp = log.parent / "queue_status.json.tmp"
tmp.write_text(json.dumps(payload, indent=2) + "\n")
tmp.replace(log.parent / "queue_status.json")
