"""Batch transport for the authenticated lexical-alpha judge union.

Run ``lexical_judge_union`` with ``--max-new-calls 3`` first.  That performs
the required synchronous, fully parsed three-metric canary.  This adapter then
submits only the remaining exact call keys to OpenAI's Batch API, archives the
raw request and response files, and merges responses into the same sealed
checkpoint.  It never changes prompts, scores, model identity, or row keys.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
from urllib.request import Request, urlopen
import uuid

from experiments.axbench_lexicality20.judge import parse_rating_info
from experiments.axbench_lexicality20.lexical_judge_union import (
    MODEL,
    UnionJudgeError,
    atomic_json,
    call_key,
    expected_checkpoint,
    load_items,
    prompts,
    seal_or_validate,
    validate_batch_receipt_archives,
    validate_call,
    validate_canary_call,
)
from experiments.axbench_lexicality20.lexical_select_alpha import file_sha256
from experiments.axbench_lexicality20.protocol import canonical_sha256


API_ROOT = "https://api.openai.com/v1"
TERMINAL_STATES = {"completed", "failed", "expired", "cancelled"}
MAX_REQUESTS = 50_000
MAX_INPUT_BYTES = 200 * 1024 * 1024
BATCH_CHUNK_REQUESTS = 2_500
STATE_SCHEMA = "axb44-lexical-alpha-judge-batch/v1"


class BatchJudgeError(UnionJudgeError):
    """Raised when batch transport or its archived receipts drift."""


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as stream:
        stream.write(payload)
        temporary = Path(stream.name)
    temporary.replace(path)


def archive_entry(path: Path | None, file_id: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = path.read_bytes()
    return {
        "path": path.name,
        "file_id": file_id,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def api_request(
    path: str,
    api_key: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    raw: bool = False,
) -> Any:
    request_headers = {"Authorization": f"Bearer {api_key}"}
    request_headers.update(headers or {})
    request = Request(
        API_ROOT + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    with urlopen(request, timeout=600) as response:  # noqa: S310 - fixed HTTPS root
        payload = response.read()
    return payload if raw else json.loads(payload.decode("utf-8"))


def upload_batch_file(api_key: str, path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    boundary = uuid.uuid4().hex
    body = io.BytesIO()

    def write_text(value: str) -> None:
        body.write(value.encode("utf-8"))

    write_text(
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="purpose"\r\n\r\n'
        "batch\r\n"
    )
    write_text(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        "Content-Type: application/jsonl\r\n\r\n"
    )
    body.write(payload)
    write_text(f"\r\n--{boundary}--\r\n")
    result = api_request(
        "/files",
        api_key,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    if not isinstance(result, dict) or not result.get("id"):
        raise BatchJudgeError("Batch API did not return an uploaded file ID")
    return result


def checkpoint_identity(checkpoint: dict[str, Any]) -> str:
    """Hash immutable judge identity fields, excluding calls and receipts."""

    fields = {
        key: checkpoint[key]
        for key in (
            "schema",
            "analysis_id",
            "judge_model",
            "judge_temperature",
            "transport",
            "record_count",
            "call_count",
            "union_input_sha256",
            "complete_receipts",
            "template_receipt",
        )
    }
    return canonical_sha256(fields)


def has_batch_provenance(checkpoint: dict[str, Any]) -> bool:
    return bool(checkpoint.get("batch_receipts")) or any(
        isinstance(call, dict) and call.get("transport") == "batch"
        for call in checkpoint.get("calls", {}).values()
    )


def expected_state_identity(
    checkpoint: dict[str, Any], output: Path, artifacts: Path
) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "union_input_sha256": checkpoint["union_input_sha256"],
        "checkpoint_identity_sha256": checkpoint_identity(checkpoint),
        "batch_tool_sha256": file_sha256(Path(__file__).resolve()),
        "model": MODEL,
        "template_receipt": checkpoint["template_receipt"],
        "complete_receipts": checkpoint["complete_receipts"],
        "checkpoint_path": str(output.resolve()),
        "artifacts_path": str(artifacts.resolve()),
    }


def load_state(
    path: Path,
    checkpoint: dict[str, Any],
    output: Path,
    artifacts: Path,
) -> dict[str, Any]:
    expected = expected_state_identity(checkpoint, output, artifacts)
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if any(state.get(key) != value for key, value in expected.items()):
            raise BatchJudgeError("batch state identity drift")
        if not isinstance(state.get("batches"), list):
            raise BatchJudgeError("batch state batches are not a list")
        return state
    if has_batch_provenance(checkpoint):
        raise BatchJudgeError(
            "batch state is missing after Batch calls were recorded; refusing resubmission"
        )
    if artifacts.exists() and any(artifacts.iterdir()):
        raise BatchJudgeError(
            "batch artifact directory is nonempty without a sealed state file"
        )
    return {**expected, "batches": []}


def active_batch(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return the one batch that has not yet been archived and adjudicated."""

    candidates = [
        row
        for row in state.get("batches", [])
        if not row.get("ingested")
    ]
    if len(candidates) > 1:
        raise BatchJudgeError("more than one batch is active or awaiting ingestion")
    return candidates[0] if candidates else None


def is_zero_yield_terminal(row: dict[str, Any]) -> bool:
    counts = row.get("request_counts") or {}
    return (
        row.get("status") in {"failed", "expired", "cancelled"}
        and all(
            int(counts.get(key, -1)) == 0
            for key in ("total", "completed", "failed")
        )
        and not row.get("output_file_id")
        and not row.get("error_file_id")
    )


def call_map(items: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    output: dict[str, tuple[str, str]] = {}
    for item in items:
        for metric, prompt in prompts(item).items():
            key = call_key(item, metric, prompt)
            if key in output:
                raise BatchJudgeError("duplicate judge call key")
            output[key] = (item["record_key"], prompt)
    if len(output) != 6360:
        raise BatchJudgeError("judge call map does not contain 6,360 keys")
    return output


def open_checkpoint(
    output: Path,
    items: list[dict[str, Any]],
    receipts: dict[str, str],
    calls_by_key: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    if not output.is_file():
        raise BatchJudgeError(
            "judge checkpoint is absent; run the synchronous three-call canary first"
        )
    checkpoint = json.loads(output.read_text(encoding="utf-8"))
    expected = expected_checkpoint(items, receipts)
    mutable = {
        "calls",
        "records",
        "errors",
        "complete",
        "canary",
        "batch_receipts",
    }
    for key, value in expected.items():
        if key not in mutable and checkpoint.get(key) != value:
            raise BatchJudgeError(f"judge checkpoint identity drift: {key}")
    calls = checkpoint.get("calls")
    if not isinstance(calls, dict):
        raise BatchJudgeError("judge checkpoint calls are not a mapping")
    for key, call in calls.items():
        if key not in calls_by_key or not isinstance(call, dict):
            raise BatchJudgeError("judge checkpoint has an unexpected cached call")
        validate_call(call, calls_by_key[key][1])
    canary = checkpoint.get("canary")
    if not isinstance(canary, dict) or len(canary.get("call_keys", [])) != 3:
        raise BatchJudgeError("judge checkpoint lacks the complete synchronous canary")
    for key in canary["call_keys"]:
        if (
            key not in calls
            or calls_by_key.get(key, (None, None))[0] != canary.get("record_key")
        ):
            raise BatchJudgeError("synchronous canary identity drift")
        validate_canary_call(calls[key], calls_by_key[key][1])
    canary_keys = set(canary["call_keys"])
    synchronous_keys = {
        key for key, call in calls.items() if call.get("transport") == "synchronous"
    }
    if synchronous_keys != canary_keys:
        raise BatchJudgeError(
            "mixed transport must contain exactly the three synchronous canary calls"
        )
    if any(
        call.get("transport") != "batch"
        for key, call in calls.items()
        if key not in canary_keys
    ):
        raise BatchJudgeError("every non-canary cached call must come from Batch")
    receipts_by_id: dict[str, dict[str, Any]] = {}
    for receipt in checkpoint.get("batch_receipts", []):
        batch_id = str(receipt.get("batch_id", ""))
        if not batch_id or batch_id in receipts_by_id:
            raise BatchJudgeError("cached batch receipt ID is missing or duplicated")
        receipts_by_id[batch_id] = receipt
    batch_calls_by_id: dict[str, set[str]] = {}
    for key, call in calls.items():
        if call.get("transport") != "batch":
            continue
        batch_id = str(call.get("batch_id", ""))
        if not batch_id:
            raise BatchJudgeError("cached Batch call lacks its batch ID")
        batch_calls_by_id.setdefault(batch_id, set()).add(key)
    if set(batch_calls_by_id) - set(receipts_by_id):
        raise BatchJudgeError("cached Batch calls lack a sealed receipt")
    for batch_id, receipt in receipts_by_id.items():
        keys = batch_calls_by_id.get(batch_id, set())
        if (
            len(keys) != int(receipt.get("successful_calls", -1))
            or canonical_sha256(sorted(keys))
            != receipt.get("successful_custom_ids_sha256")
        ):
            raise BatchJudgeError("cached Batch calls disagree with their receipt")
    return checkpoint


def validate_state_coherence(
    state: dict[str, Any], checkpoint: dict[str, Any]
) -> bool:
    """Prevent lost state, duplicated chunks, or detached receipt histories."""

    batches = state.get("batches", [])
    chunk_indexes = [int(row.get("chunk_index", -1)) for row in batches]
    if chunk_indexes != list(range(len(batches))):
        raise BatchJudgeError("batch chunk indexes are not contiguous and ordered")
    batch_ids = [str(row.get("batch_id")) for row in batches if row.get("batch_id")]
    if len(batch_ids) != len(set(batch_ids)):
        raise BatchJudgeError("batch state contains duplicate batch IDs")
    ingested_ids = {
        str(row.get("batch_id"))
        for row in batches
        if row.get("ingested") and row.get("batch_id")
    }
    receipt_ids = {
        str(row.get("batch_id")) for row in checkpoint.get("batch_receipts", [])
    }
    state_ids = {
        str(row.get("batch_id")) for row in batches if row.get("batch_id")
    }
    if not ingested_ids.issubset(receipt_ids) or not receipt_ids.issubset(state_ids):
        raise BatchJudgeError("batch state and checkpoint receipt history diverged")
    if any(row.get("ingested") and not row.get("batch_id") for row in batches):
        raise BatchJudgeError("an ingested batch state row lacks a batch ID")
    changed = False
    for row in batches:
        if row.get("batch_id") and str(row["batch_id"]) in receipt_ids and not row.get("ingested"):
            # The checkpoint is written before state at ingestion.  If a process
            # dies between those two atomic writes, the sealed receipt is the
            # authoritative commit record and safely completes the state row.
            row["ingested"] = True
            changed = True
    return changed


def request_jsonl(
    keys: list[str], calls_by_key: dict[str, tuple[str, str]]
) -> bytes:
    lines = []
    for key in keys:
        prompt = calls_by_key[key][1]
        lines.append(
            json.dumps(
                {
                    "custom_id": key,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": MODEL,
                        "temperature": 0,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    if len(keys) > MAX_REQUESTS or len(payload) > MAX_INPUT_BYTES:
        raise BatchJudgeError("batch request exceeds the documented file limits")
    return payload


def select_batch_chunk(pending: list[str]) -> list[str]:
    """Use a fixed prefix so retries cannot change transport allocation."""

    return pending[:BATCH_CHUNK_REQUESTS]


def _recent_batch_for_input(api_key: str, input_file_id: str) -> dict[str, Any] | None:
    payload = api_request("/batches?limit=100", api_key)
    matches = [
        row
        for row in payload.get("data", [])
        if row.get("input_file_id") == input_file_id
    ]
    if len(matches) > 1:
        raise BatchJudgeError("multiple Batch API jobs use the same input file")
    return matches[0] if matches else None


def expected_metadata(row: dict[str, Any]) -> dict[str, str]:
    return {
        "study": "axb44-lexical-alpha",
        "chunk": str(row["chunk_index"]),
    }


def validate_remote_batch(
    batch: dict[str, Any], row: dict[str, Any], *, terminal: bool = False
) -> None:
    """Authenticate the remote job against the locally sealed submission."""

    if not isinstance(batch, dict) or not batch.get("id"):
        raise BatchJudgeError("Batch API returned an invalid batch object")
    if row.get("batch_id") and batch.get("id") != row["batch_id"]:
        raise BatchJudgeError("remote batch ID drifted")
    if (
        batch.get("input_file_id") != row.get("input_file_id")
        or batch.get("endpoint") != "/v1/chat/completions"
        or batch.get("completion_window") != "24h"
        or batch.get("metadata") != expected_metadata(row)
    ):
        raise BatchJudgeError("remote batch identity drifted")
    status = str(batch.get("status", ""))
    counts = batch.get("request_counts")
    if not isinstance(counts, dict):
        raise BatchJudgeError("remote batch lacks request counts")
    total = int(counts.get("total", -1))
    completed = int(counts.get("completed", -1))
    failed = int(counts.get("failed", -1))
    if total not in {0, int(row["request_count"])}:
        raise BatchJudgeError("remote batch request total drifted")
    if min(completed, failed) < 0 or completed + failed > max(total, 0):
        raise BatchJudgeError("remote batch request counts are inconsistent")
    if terminal or status in TERMINAL_STATES:
        normal_terminal = (
            total == int(row["request_count"]) and completed + failed == total
        )
        zero_yield_terminal = (
            status in {"failed", "expired", "cancelled"}
            and total == completed == failed == 0
            and not batch.get("output_file_id")
            and not batch.get("error_file_id")
        )
        if not (normal_terminal or zero_yield_terminal):
            raise BatchJudgeError("terminal batch request counts do not reconcile")


def ensure_batch_created(
    args: argparse.Namespace,
    api_key: str,
    state: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any]:
    """Recover an ambiguous create request before ever attempting another."""

    if row.get("batch_id"):
        return row
    if row.get("create_attempted_utc"):
        for _ in range(6):
            recovered = _recent_batch_for_input(api_key, row["input_file_id"])
            if recovered is not None:
                validate_remote_batch(recovered, row)
                row["batch_id"] = recovered["id"]
                row["status"] = str(recovered.get("status"))
                row["create_recovered"] = True
                atomic_json(args.state, state)
                return row
            time.sleep(10)
        raise BatchJudgeError(
            "prior batch creation remains ambiguous; refusing a duplicate submission"
        )
    row["create_attempted_utc"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    atomic_json(args.state, state)
    batch = api_request(
        "/batches",
        api_key,
        data=json.dumps(
            {
                "input_file_id": row["input_file_id"],
                "endpoint": "/v1/chat/completions",
                "completion_window": "24h",
                "metadata": expected_metadata(row),
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    validate_remote_batch(batch, row)
    row["batch_id"] = batch["id"]
    row["status"] = str(batch.get("status"))
    row["create_recovered"] = False
    atomic_json(args.state, state)
    return row


def submit(args: argparse.Namespace, api_key: str) -> bool:
    items, receipts = load_items(args)
    calls_by_key = call_map(items)
    checkpoint = open_checkpoint(args.output, items, receipts, calls_by_key)
    state = load_state(args.state, checkpoint, args.output, args.artifacts)
    reconciled = validate_state_coherence(state, checkpoint)
    if checkpoint.get("batch_receipts"):
        validate_batch_receipt_archives(checkpoint, items, args.output)
    if reconciled:
        atomic_json(args.state, state)
    open_row = active_batch(state)
    if open_row is not None and not open_row.get("batch_id"):
        open_row = ensure_batch_created(args, api_key, state, open_row)
        print(
            f"batch submit: recovered {open_row['batch_id']} "
            f"with {open_row['request_count']} requests",
            flush=True,
        )
        return True
    if open_row is not None:
        raise BatchJudgeError("an existing batch must be polled and ingested first")
    pending = sorted(set(calls_by_key) - set(checkpoint["calls"]))
    if not pending:
        if seal_or_validate(
            checkpoint, items, receipts, checkpoint_path=args.output
        ):
            atomic_json(args.output, checkpoint)
        print("batch submit: no calls pending", flush=True)
        return False

    chunk_index = len(state["batches"])
    chunk_keys = select_batch_chunk(pending)
    request_path = args.artifacts / f"chunk-{chunk_index:02d}-input.jsonl"
    if request_path.exists():
        raise BatchJudgeError("refusing to overwrite an untracked batch input archive")
    payload = request_jsonl(chunk_keys, calls_by_key)
    atomic_bytes(request_path, payload)
    uploaded = upload_batch_file(api_key, request_path)
    status = uploaded.get("status")
    for _ in range(24):
        if status == "processed":
            break
        if status == "error":
            raise BatchJudgeError("uploaded batch file reached error status")
        time.sleep(5)
        status = api_request(f"/files/{uploaded['id']}", api_key).get("status")
    else:
        raise BatchJudgeError(f"uploaded batch file was not processed: {status}")

    receipt = {
        "chunk_index": chunk_index,
        "batch_id": None,
        "input_file_id": uploaded["id"],
        "input_file_status": status,
        "request_count": len(chunk_keys),
        "custom_ids_sha256": canonical_sha256(chunk_keys),
        "input_path": str(request_path.resolve()),
        "input_bytes": len(payload),
        "input_sha256": hashlib.sha256(payload).hexdigest(),
        "tool_sha256": file_sha256(Path(__file__).resolve()),
        "submitted_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "uploaded",
        "ingested": False,
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
        "metadata": expected_metadata({"chunk_index": chunk_index}),
    }
    state["batches"].append(receipt)
    atomic_json(args.state, state)
    ensure_batch_created(args, api_key, state, receipt)
    print(
        f"batch submit: {receipt['batch_id']} with {len(chunk_keys)} requests",
        flush=True,
    )
    return True


def poll(args: argparse.Namespace, api_key: str, *, quiet: bool = False) -> dict[str, Any] | None:
    items, receipts = load_items(args)
    calls_by_key = call_map(items)
    checkpoint = open_checkpoint(args.output, items, receipts, calls_by_key)
    state = load_state(args.state, checkpoint, args.output, args.artifacts)
    reconciled = validate_state_coherence(state, checkpoint)
    if checkpoint.get("batch_receipts"):
        validate_batch_receipt_archives(checkpoint, items, args.output)
    if reconciled:
        atomic_json(args.state, state)
    row = active_batch(state)
    if row is None:
        if not quiet:
            print("batch poll: no active batch", flush=True)
        return None
    row = ensure_batch_created(args, api_key, state, row)
    batch = api_request(f"/batches/{row['batch_id']}", api_key)
    validate_remote_batch(
        batch, row, terminal=str(batch.get("status")) in TERMINAL_STATES
    )
    row["status"] = str(batch.get("status"))
    row["request_counts"] = batch.get("request_counts", {})
    row["output_file_id"] = batch.get("output_file_id")
    row["error_file_id"] = batch.get("error_file_id")
    row["batch_errors"] = batch.get("errors")
    row["last_polled_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    atomic_json(args.state, state)
    if not quiet:
        counts = row["request_counts"]
        print(
            f"batch poll: {row['batch_id']} {row['status']} "
            f"completed={counts.get('completed')} failed={counts.get('failed')} "
            f"total={counts.get('total')}",
            flush=True,
        )
    return row


def _batch_timestamp(body: dict[str, Any]) -> str:
    created = body.get("created")
    if isinstance(created, (int, float)):
        return datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="seconds")
    raise BatchJudgeError("batch response lacks a numeric creation timestamp")


def ingest(args: argparse.Namespace, api_key: str) -> int:
    items, receipts = load_items(args)
    calls_by_key = call_map(items)
    checkpoint = open_checkpoint(args.output, items, receipts, calls_by_key)
    state = load_state(args.state, checkpoint, args.output, args.artifacts)
    reconciled = validate_state_coherence(state, checkpoint)
    if checkpoint.get("batch_receipts"):
        validate_batch_receipt_archives(checkpoint, items, args.output)
    if reconciled:
        atomic_json(args.state, state)
    row = active_batch(state)
    if row is None or row.get("status") not in TERMINAL_STATES:
        raise BatchJudgeError("no terminal batch is awaiting ingestion")
    remote = api_request(f"/batches/{row['batch_id']}", api_key)
    validate_remote_batch(remote, row, terminal=True)
    row["status"] = str(remote.get("status"))
    row["request_counts"] = remote.get("request_counts", {})
    row["output_file_id"] = remote.get("output_file_id")
    row["error_file_id"] = remote.get("error_file_id")
    row["batch_errors"] = remote.get("errors")
    atomic_json(args.state, state)
    input_payload = Path(row["input_path"]).read_bytes()
    submitted_keys = set(
        json.loads(line)["custom_id"]
        for line in input_payload.decode("utf-8").splitlines()
        if line.strip()
    )
    if (
        len(submitted_keys) != int(row["request_count"])
        or canonical_sha256(sorted(submitted_keys)) != row["custom_ids_sha256"]
        or len(input_payload) != int(row["input_bytes"])
        or hashlib.sha256(input_payload).hexdigest() != row["input_sha256"]
        or input_payload != request_jsonl(sorted(submitted_keys), calls_by_key)
    ):
        raise BatchJudgeError("archived batch input does not reproduce exactly")

    raw_output = b""
    output_path: Path | None = None
    if row.get("output_file_id"):
        raw_output = api_request(
            f"/files/{row['output_file_id']}/content", api_key, raw=True
        )
        output_path = args.artifacts / f"chunk-{row['chunk_index']:02d}-output.jsonl"
        atomic_bytes(output_path, raw_output)
    raw_error = b""
    error_path: Path | None = None
    if row.get("error_file_id"):
        raw_error = api_request(
            f"/files/{row['error_file_id']}/content", api_key, raw=True
        )
        error_path = args.artifacts / f"chunk-{row['chunk_index']:02d}-errors.jsonl"
        atomic_bytes(error_path, raw_error)

    seen: set[str] = set()
    successful_keys: set[str] = set()
    failed_keys: set[str] = set()
    merged = 0
    calls = checkpoint["calls"]
    for line in raw_output.decode("utf-8").splitlines():
        if not line.strip():
            continue
        result = json.loads(line)
        key = str(result.get("custom_id"))
        if key not in submitted_keys or key in seen:
            raise BatchJudgeError("batch output contains an unknown or duplicate custom ID")
        seen.add(key)
        response = result.get("response") or {}
        if response.get("status_code") != 200 or result.get("error"):
            failed_keys.add(key)
            continue
        successful_keys.add(key)
        body = response.get("body") or {}
        if body.get("model") != MODEL:
            raise BatchJudgeError(
                f"batch returned {body.get('model')!r}, expected {MODEL}"
            )
        try:
            completion = str(body["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise BatchJudgeError("batch response lacks chat completion content") from exc
        rating, parse_status = parse_rating_info(completion)
        prompt = calls_by_key[key][1]
        call = {
            "completion": completion,
            "rating": rating,
            "parse_status": parse_status,
            "returned_model": body.get("model"),
            "usage": body.get("usage", {}),
            "response_id": body.get("id"),
            "completed_utc": _batch_timestamp(body),
            "retry_count": 0,
            "transport": "batch",
            "batch_id": row["batch_id"],
            "batch_request_id": response.get("request_id"),
            "request_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        validate_call(call, prompt)
        if key in calls and calls[key] != call:
            raise BatchJudgeError("batch attempted to overwrite a cached call")
        if key not in calls:
            calls[key] = call
            merged += 1

    error_ids: set[str] = set()
    for line in raw_error.decode("utf-8").splitlines():
        if not line.strip():
            continue
        result = json.loads(line)
        key = str(result.get("custom_id"))
        if key not in submitted_keys or key in error_ids or key in seen:
            raise BatchJudgeError(
                "batch error file contains an unknown or duplicate custom ID"
            )
        error_ids.add(key)
        failed_keys.add(key)
    unreturned_keys = submitted_keys - seen - error_ids
    counts = row["request_counts"]
    zero_yield_terminal = is_zero_yield_terminal(row)
    counts_match_archives = (
        len(successful_keys) == int(counts.get("completed", -1))
        and len(failed_keys | unreturned_keys) == int(counts.get("failed", -1))
    )
    if zero_yield_terminal:
        if successful_keys or failed_keys or unreturned_keys != submitted_keys:
            raise BatchJudgeError("zero-yield terminal batch was not fully adjudicated")
    elif not counts_match_archives:
        raise BatchJudgeError(
            "archived terminal files do not reconcile with remote request counts"
        )
    if row["status"] == "completed" and unreturned_keys:
        raise BatchJudgeError("completed batch omitted one or more request IDs")
    existing_errors = {
        (entry.get("batch_id"), entry.get("key"), entry.get("error_type"))
        for entry in checkpoint["errors"]
        if isinstance(entry, dict)
    }
    for key in sorted(failed_keys):
        identity = (row["batch_id"], key, "batch_request_error")
        if identity not in existing_errors:
            checkpoint["errors"].append(
                {
                    "key": key,
                    "transport": "batch",
                    "batch_id": row["batch_id"],
                    "error_type": "batch_request_error",
                    "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )

    for key in sorted(unreturned_keys):
        identity = (row["batch_id"], key, "batch_unreturned")
        if identity not in existing_errors:
            checkpoint["errors"].append(
                {
                    "key": key,
                    "transport": "batch",
                    "batch_id": row["batch_id"],
                    "error_type": "batch_unreturned",
                    "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )

    row["output_path"] = str(output_path.resolve()) if output_path else None
    row["output_bytes"] = len(raw_output)
    row["output_sha256"] = (
        hashlib.sha256(raw_output).hexdigest() if output_path else None
    )
    row["error_path"] = str(error_path.resolve()) if error_path else None
    row["error_bytes"] = len(raw_error)
    row["error_sha256"] = hashlib.sha256(raw_error).hexdigest() if raw_error else None
    row["observed_output_ids"] = len(seen)
    row["observed_error_ids"] = len(error_ids)
    row["merged_calls"] = merged
    row["successful_calls"] = len(successful_keys)
    row["failed_calls"] = len(failed_keys)
    row["unreturned_calls"] = len(unreturned_keys)
    row["ingested_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row["ingested"] = True
    sealed_receipt = {
        "chunk_index": row["chunk_index"],
        "batch_id": row["batch_id"],
        "input_file_id": row["input_file_id"],
        "output_file_id": row.get("output_file_id"),
        "error_file_id": row.get("error_file_id"),
        "endpoint": row["endpoint"],
        "completion_window": row["completion_window"],
        "metadata": row["metadata"],
        "terminal_status": row["status"],
        "remote_request_counts": row["request_counts"],
        "remote_batch_errors": row.get("batch_errors"),
        "request_count": len(submitted_keys),
        "observed_output_ids": len(seen),
        "observed_error_ids": len(error_ids),
        "successful_calls": len(successful_keys),
        "failed_calls": len(failed_keys),
        "unreturned_calls": len(unreturned_keys),
        "custom_ids_sha256": canonical_sha256(sorted(submitted_keys)),
        "successful_custom_ids_sha256": canonical_sha256(
            sorted(successful_keys)
        ),
        "failed_custom_ids_sha256": canonical_sha256(sorted(failed_keys)),
        "unreturned_custom_ids_sha256": canonical_sha256(
            sorted(unreturned_keys)
        ),
        "input_bytes": len(input_payload),
        "input_sha256": hashlib.sha256(input_payload).hexdigest(),
        "output_bytes": len(raw_output),
        "output_sha256": (
            hashlib.sha256(raw_output).hexdigest() if output_path else None
        ),
        "error_bytes": len(raw_error),
        "error_sha256": (
            hashlib.sha256(raw_error).hexdigest() if error_path else None
        ),
        "archive_root_relative_to_checkpoint": args.artifacts.relative_to(
            args.output.parent
        ).as_posix(),
        "archive_files": {
            "input": archive_entry(Path(row["input_path"]), row["input_file_id"]),
            "output": archive_entry(output_path, row.get("output_file_id")),
            "error": archive_entry(error_path, row.get("error_file_id")),
        },
        "submitted_utc": row["submitted_utc"],
        "ingested_utc": row["ingested_utc"],
        "tool_sha256": row["tool_sha256"],
    }
    prior_receipts = [
        value
        for value in checkpoint["batch_receipts"]
        if value.get("batch_id") == row["batch_id"]
    ]
    if len(prior_receipts) > 1 or (
        prior_receipts and prior_receipts[0] != sealed_receipt
    ):
        raise BatchJudgeError("sealed batch receipt changed during re-ingestion")
    if not prior_receipts:
        checkpoint["batch_receipts"].append(sealed_receipt)
    atomic_json(args.output, checkpoint)
    atomic_json(args.state, state)
    remaining = len(set(calls_by_key) - set(calls))
    if remaining == 0:
        seal_or_validate(
            checkpoint, items, receipts, checkpoint_path=args.output
        )
        atomic_json(args.output, checkpoint)
    print(
        f"batch ingest: merged={merged} failed={len(failed_keys)} remaining={remaining}",
        flush=True,
    )
    return remaining


def drive(args: argparse.Namespace, api_key: str) -> None:
    for _ in range(8):
        if not args.state.exists() or active_batch(
            json.loads(args.state.read_text(encoding="utf-8"))
        ) is None:
            if not submit(args, api_key):
                return
        while True:
            row = poll(args, api_key)
            if row is None or row.get("status") in TERMINAL_STATES:
                break
            time.sleep(args.poll_seconds)
        if row is None:
            return
        zero_yield_terminal = is_zero_yield_terminal(row)
        remaining = ingest(args, api_key)
        if remaining == 0:
            return
        if zero_yield_terminal:
            raise BatchJudgeError(
                "zero-yield terminal batch was archived; inspect it before retrying"
            )
    raise BatchJudgeError("batch retry cap reached with judge calls still pending")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwen-source", type=Path, required=True)
    parser.add_argument("--qwen-sensitivity", type=Path, required=True)
    parser.add_argument("--qwen-sensitivity-complete-sha256", required=True)
    parser.add_argument("--gemma-source", type=Path, required=True)
    parser.add_argument("--gemma-sensitivity", type=Path, required=True)
    parser.add_argument("--gemma-sensitivity-complete-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=120)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--submit", action="store_true")
    mode.add_argument("--poll", action="store_true")
    mode.add_argument("--ingest", action="store_true")
    mode.add_argument("--drive", action="store_true")
    args = parser.parse_args()
    if args.poll_seconds < 15:
        raise BatchJudgeError("poll-seconds must be at least 15")
    args.output = args.output.resolve()
    args.state = args.state.resolve()
    args.artifacts = args.artifacts.resolve()
    try:
        relative_artifacts = args.artifacts.relative_to(args.output.parent)
    except ValueError as exc:
        raise BatchJudgeError(
            "batch artifacts must be inside the judge checkpoint directory"
        ) from exc
    if str(relative_artifacts) in {"", "."}:
        raise BatchJudgeError("batch artifacts require a dedicated subdirectory")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise BatchJudgeError("OPENAI_API_KEY is not set")
    if args.submit:
        submit(args, api_key)
    elif args.poll:
        poll(args, api_key)
    elif args.ingest:
        ingest(args, api_key)
    else:
        drive(args, api_key)


if __name__ == "__main__":
    main()
