"""Judge one authenticated lexical-alpha union in a single administration.

The original H-tuned and new lexical-tuned generations are deliberately
rejudged together.  This avoids combining API ratings made at different times.
The runner is atomic and resume-safe; an incomplete pass remains incomplete.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from experiments.axbench_lexicality20.axbench_judge_templates import (
    SOURCE_FILE_SHA256,
    SOURCE_PATH,
    TEMPLATES,
)
from experiments.axbench_lexicality20.judge import (
    MODEL,
    call_with_retry,
    harmonic,
    parse_rating_info,
)
from experiments.axbench_lexicality20.lexical_select_alpha import (
    ANALYSIS_ID,
    file_sha256,
)
from experiments.axbench_lexicality20.lexical_union import load_union
from experiments.axbench_lexicality20.protocol import canonical_sha256


SCHEMA = "axb44-lexical-alpha-judge/v1"
METRICS = ("concept", "instruction", "fluency")
AXBENCH_REVISION = "41c8332543e5a631f9a8c0a9df38799893ace758"


class UnionJudgeError(RuntimeError):
    """Raised when judge inputs, calls, or checkpoint identity drift."""


def validate_run_limits(workers: int, max_new_calls: int) -> None:
    """Keep the first resumable pass large enough for all canary metrics."""

    if not 1 <= workers <= 8 or max_new_calls < len(METRICS):
        raise UnionJudgeError(
            "workers must be 1..8 and max-new-calls must be at least 3"
        )


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def prompts(item: dict[str, Any]) -> dict[str, str]:
    return {
        "concept": TEMPLATES["concept"].format(
            concept=item["concept_label"], sentence=item["sentence"]
        ),
        "instruction": TEMPLATES["instruction"].format(
            instruction=item["instruction"], sentence=item["sentence"]
        ),
        "fluency": TEMPLATES["fluency"].format(sentence=item["sentence"]),
    }


def call_key(item: dict[str, Any], metric: str, prompt: str) -> str:
    return canonical_sha256(
        {
            "model": MODEL,
            "temperature": 0,
            "record_key": item["record_key"],
            "metric": metric,
            "prompt": prompt,
        }
    )


def load_items(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, str]]:
    roots = {
        "qwen": (args.qwen_source, args.qwen_sensitivity),
        "gemma": (args.gemma_source, args.gemma_sensitivity),
    }
    receipts: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    for model_key, (source_root, sensitivity_root) in roots.items():
        complete_sha256 = getattr(args, f"{model_key}_sensitivity_complete_sha256")
        rows, _ = load_union(
            model_key=model_key,
            source_root=source_root,
            sensitivity_root=sensitivity_root,
            sensitivity_complete_sha256=complete_sha256,
        )
        receipts[f"{model_key}_source_complete_sha256"] = file_sha256(
            source_root / "COMPLETE.json"
        )
        receipts[f"{model_key}_sensitivity_complete_sha256"] = file_sha256(
            sensitivity_root / "COMPLETE.json"
        )
        for row in rows:
            items.append({**row, "record_key": f"{model_key}:{row['cell_id']}"})
    items.sort(key=lambda item: item["record_key"])
    if len(items) != 2120 or len({item["record_key"] for item in items}) != 2120:
        raise UnionJudgeError("judge union must contain 2,120 unique records")
    return items, receipts


def input_sha256(items: list[dict[str, Any]]) -> str:
    return canonical_sha256(
        [
            {
                "record_key": item["record_key"],
                "source": item["source"],
                "result_sha256": item["result_sha256"],
                "record_index": item["record_index"],
                "concept_label": item["concept_label"],
                "instruction_sha256": hashlib.sha256(
                    item["instruction"].encode("utf-8")
                ).hexdigest(),
                "sentence_sha256": hashlib.sha256(
                    item["sentence"].encode("utf-8")
                ).hexdigest(),
            }
            for item in items
        ]
    )


def template_receipt() -> dict[str, Any]:
    """Bind every API call to the exact released AxBench judge wording."""

    return {
        "axbench_revision": AXBENCH_REVISION,
        "source_path": SOURCE_PATH,
        "source_file_sha256": SOURCE_FILE_SHA256,
        "metric_order": list(METRICS),
        "template_sha256": {
            metric: hashlib.sha256(TEMPLATES[metric].encode("utf-8")).hexdigest()
            for metric in METRICS
        },
    }


def expected_checkpoint(
    items: list[dict[str, Any]], receipts: dict[str, str]
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "analysis_id": ANALYSIS_ID,
        "evidential_status": "post_hoc_sensitivity_descriptive_judge",
        "judge_model": MODEL,
        "judge_temperature": 0,
        "transport": "openai_chat_completions",
        "record_count": len(items),
        "call_count": len(items) * 3,
        "union_input_sha256": input_sha256(items),
        "complete_receipts": receipts,
        "template_receipt": template_receipt(),
        "calls": {},
        "records": {},
        "canary": None,
        "batch_receipts": [],
        "errors": [],
        "complete": False,
    }


def validate_call(call: dict[str, Any], prompt: str) -> None:
    if call.get("returned_model") != MODEL:
        raise UnionJudgeError(f"judge returned {call.get('returned_model')!r}, expected {MODEL}")
    if not call.get("response_id") or not isinstance(call.get("usage"), dict):
        raise UnionJudgeError("judge response lacks response ID or usage")
    rating, status = parse_rating_info(str(call.get("completion", "")))
    if float(call.get("rating", -1)) != rating or call.get("parse_status") != status:
        raise UnionJudgeError("stored judge parse does not reproduce")
    if call.get("request_prompt_sha256") != hashlib.sha256(prompt.encode("utf-8")).hexdigest():
        raise UnionJudgeError("stored judge prompt hash does not reproduce")


def validate_canary_call(call: dict[str, Any], prompt: str) -> None:
    """Require the service-format canary to parse rather than fail to zero."""

    validate_call(call, prompt)
    if call.get("parse_status") != "parsed":
        raise UnionJudgeError("judge canary rating did not parse")


def derive_records(
    calls: dict[str, Any], items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Rebuild every scored record from authenticated calls and source rows."""

    records: dict[str, Any] = {}
    expected_keys: set[str] = set()
    for item in items:
        scores: dict[str, float] = {}
        keys: dict[str, str] = {}
        for metric, prompt in prompts(item).items():
            key = call_key(item, metric, prompt)
            expected_keys.add(key)
            call = calls.get(key)
            if not isinstance(call, dict):
                raise UnionJudgeError("judge finalization is missing a call")
            validate_call(call, prompt)
            scores[metric] = float(call["rating"])
            keys[metric] = key
        records[item["record_key"]] = {
            **item,
            "scores": scores,
            "axbench_harmonic": harmonic([scores[metric] for metric in METRICS]),
            "call_keys": keys,
        }
    if set(calls) != expected_keys or len(records) != 2120:
        raise UnionJudgeError("judge checkpoint contains missing or unexpected calls")
    return records


def _canonical_key_hash(keys: set[str]) -> str:
    return canonical_sha256(sorted(keys))


def _archived_batch_call(
    row: dict[str, Any], prompt: str, batch_id: str
) -> dict[str, Any]:
    """Reconstruct one stored call from the raw OpenAI Batch response row."""

    response = row.get("response") or {}
    body = response.get("body") or {}
    if (
        response.get("status_code") != 200
        or row.get("error")
        or body.get("model") != MODEL
        or not isinstance(body.get("created"), (int, float))
    ):
        raise UnionJudgeError("archived successful Batch response is malformed")
    try:
        completion = str(body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise UnionJudgeError("archived Batch response lacks completion text") from exc
    rating, parse_status = parse_rating_info(completion)
    call = {
        "completion": completion,
        "rating": rating,
        "parse_status": parse_status,
        "returned_model": body.get("model"),
        "usage": body.get("usage", {}),
        "response_id": body.get("id"),
        "completed_utc": datetime.fromtimestamp(
            body["created"], timezone.utc
        ).isoformat(timespec="seconds"),
        "retry_count": 0,
        "transport": "batch",
        "batch_id": batch_id,
        "batch_request_id": response.get("request_id"),
        "request_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    validate_call(call, prompt)
    return call


def _read_archived_jsonl(
    checkpoint_path: Path,
    root_relative: str,
    entry: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Read one receipt-bound Batch file without allowing path traversal."""

    if entry is None:
        return []
    root_parent = checkpoint_path.resolve().parent
    root = (root_parent / root_relative).resolve()
    try:
        root.relative_to(root_parent)
    except ValueError as exc:
        raise UnionJudgeError("batch archive root escapes the checkpoint directory") from exc
    path = (root / str(entry.get("path", ""))).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise UnionJudgeError("batch archive file escapes its sealed root") from exc
    if not path.is_file():
        raise UnionJudgeError(f"sealed batch archive is missing: {path.name}")
    payload = path.read_bytes()
    if (
        len(payload) != int(entry.get("bytes", -1))
        or hashlib.sha256(payload).hexdigest() != entry.get("sha256")
    ):
        raise UnionJudgeError(f"sealed batch archive hash drifted: {path.name}")
    rows: list[dict[str, Any]] = []
    for line in payload.decode("utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise UnionJudgeError("batch archive JSONL row is not an object")
            rows.append(value)
    return rows


def validate_batch_receipt_archives(
    checkpoint: dict[str, Any],
    items: list[dict[str, Any]],
    checkpoint_path: Path,
) -> None:
    """Bind every Batch call/error to its immutable request and result files."""

    expected_prompts: dict[str, str] = {}
    for item in items:
        for metric, prompt in prompts(item).items():
            expected_prompts[call_key(item, metric, prompt)] = prompt

    calls = checkpoint["calls"]
    errors = checkpoint.get("errors", [])
    receipts = checkpoint.get("batch_receipts", [])
    seen_batches: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise UnionJudgeError("judge batch receipt is not an object")
        batch_id = str(receipt.get("batch_id", ""))
        if not batch_id or batch_id in seen_batches:
            raise UnionJudgeError("judge batch receipt ID is missing or duplicated")
        seen_batches.add(batch_id)
        root_relative = str(receipt.get("archive_root_relative_to_checkpoint", ""))
        archive_files = receipt.get("archive_files")
        if not root_relative or not isinstance(archive_files, dict):
            raise UnionJudgeError("judge batch receipt lacks its archive manifest")
        remote_counts = receipt.get("remote_request_counts")
        metadata = receipt.get("metadata")
        normal_terminal_counts = (
            isinstance(remote_counts, dict)
            and int(remote_counts.get("total", -1))
            == int(receipt.get("request_count", -1))
            and int(remote_counts.get("completed", -1))
            + int(remote_counts.get("failed", -1))
            == int(remote_counts.get("total", -1))
        )
        zero_yield_terminal = (
            isinstance(remote_counts, dict)
            and receipt.get("terminal_status") in {"failed", "expired", "cancelled"}
            and all(int(remote_counts.get(key, -1)) == 0 for key in ("total", "completed", "failed"))
            and receipt.get("output_file_id") is None
            and receipt.get("error_file_id") is None
        )
        if (
            receipt.get("endpoint") != "/v1/chat/completions"
            or receipt.get("completion_window") != "24h"
            or metadata
            != {
                "study": "axb44-lexical-alpha",
                "chunk": str(receipt.get("chunk_index")),
            }
            or receipt.get("terminal_status")
            not in {"completed", "failed", "expired", "cancelled"}
            or not (normal_terminal_counts or zero_yield_terminal)
        ):
            raise UnionJudgeError("judge batch remote identity does not reproduce")
        for role, file_id_key in (
            ("input", "input_file_id"),
            ("output", "output_file_id"),
            ("error", "error_file_id"),
        ):
            entry = archive_files.get(role)
            if (entry is None) != (receipt.get(file_id_key) is None):
                raise UnionJudgeError("batch archive and remote file IDs disagree")
            if entry is not None and entry.get("file_id") != receipt.get(file_id_key):
                raise UnionJudgeError("batch archive file identity drifted")
        input_rows = _read_archived_jsonl(
            checkpoint_path, root_relative, archive_files.get("input")
        )
        output_rows = _read_archived_jsonl(
            checkpoint_path, root_relative, archive_files.get("output")
        )
        error_rows = _read_archived_jsonl(
            checkpoint_path, root_relative, archive_files.get("error")
        )

        submitted: set[str] = set()
        for row in input_rows:
            key = str(row.get("custom_id", ""))
            if not key or key in submitted or key not in expected_prompts:
                raise UnionJudgeError("batch input has an unknown or duplicate call key")
            submitted.add(key)
            expected_body = {
                "model": MODEL,
                "temperature": 0,
                "messages": [{"role": "user", "content": expected_prompts[key]}],
            }
            if (
                row.get("method") != "POST"
                or row.get("url") != "/v1/chat/completions"
                or row.get("body") != expected_body
            ):
                raise UnionJudgeError("archived batch request body drifted")

        output_ids: set[str] = set()
        successful: set[str] = set()
        failed: set[str] = set()
        reconstructed_calls: dict[str, dict[str, Any]] = {}
        for row in output_rows:
            key = str(row.get("custom_id", ""))
            if not key or key not in submitted or key in output_ids:
                raise UnionJudgeError("batch output has an unknown or duplicate call key")
            output_ids.add(key)
            response = row.get("response") or {}
            if response.get("status_code") == 200 and not row.get("error"):
                successful.add(key)
                reconstructed_calls[key] = _archived_batch_call(
                    row, expected_prompts[key], batch_id
                )
            else:
                failed.add(key)
        error_ids: set[str] = set()
        for row in error_rows:
            key = str(row.get("custom_id", ""))
            if (
                not key
                or key not in submitted
                or key in output_ids
                or key in error_ids
            ):
                raise UnionJudgeError("batch error file has an unknown or duplicate call key")
            error_ids.add(key)
            failed.add(key)
        unreturned = submitted - output_ids - error_ids

        expected_counts = {
            "request_count": len(submitted),
            "observed_output_ids": len(output_ids),
            "observed_error_ids": len(error_ids),
            "successful_calls": len(successful),
            "failed_calls": len(failed),
            "unreturned_calls": len(unreturned),
        }
        if any(int(receipt.get(key, -1)) != value for key, value in expected_counts.items()):
            raise UnionJudgeError("judge batch receipt counts do not reproduce")
        if zero_yield_terminal:
            if successful or failed or len(unreturned) != len(submitted):
                raise UnionJudgeError("zero-yield terminal batch was not fully adjudicated")
        elif (
            len(successful) != int(remote_counts.get("completed", -1))
            or len(failed | unreturned) != int(remote_counts.get("failed", -1))
        ):
            raise UnionJudgeError("batch archives disagree with remote request counts")
        expected_hashes = {
            "custom_ids_sha256": _canonical_key_hash(submitted),
            "successful_custom_ids_sha256": _canonical_key_hash(successful),
            "failed_custom_ids_sha256": _canonical_key_hash(failed),
            "unreturned_custom_ids_sha256": _canonical_key_hash(unreturned),
        }
        if any(receipt.get(key) != value for key, value in expected_hashes.items()):
            raise UnionJudgeError("judge batch receipt key sets do not reproduce")

        stored_successes = {
            key
            for key, call in calls.items()
            if call.get("transport") == "batch" and call.get("batch_id") == batch_id
        }
        if stored_successes != successful:
            raise UnionJudgeError("batch calls do not match the archived successful keys")
        if any(calls[key] != reconstructed_calls[key] for key in successful):
            raise UnionJudgeError("stored Batch call does not reproduce from its archive")
        stored_failures = {
            str(row.get("key"))
            for row in errors
            if isinstance(row, dict) and row.get("batch_id") == batch_id
        }
        if stored_failures != failed | unreturned:
            raise UnionJudgeError("batch errors do not match archived failed keys")


def validate_complete_checkpoint(
    checkpoint: dict[str, Any],
    items: list[dict[str, Any]],
    receipts: dict[str, str],
    *,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the complete 2,120-row/6,360-call artifact against its inputs."""

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
            raise UnionJudgeError(f"complete judge checkpoint identity drift: {key}")
    if checkpoint.get("complete") is not True:
        raise UnionJudgeError("judge checkpoint is not complete")
    calls = checkpoint.get("calls")
    records = checkpoint.get("records")
    if not isinstance(calls, dict) or not isinstance(records, dict):
        raise UnionJudgeError("judge checkpoint calls/records are not mappings")
    derived = derive_records(calls, items)
    if records != derived:
        raise UnionJudgeError("stored judge records do not reproduce from calls and inputs")
    canary = checkpoint.get("canary")
    if (
        not isinstance(canary, dict)
        or canary.get("record_key") not in records
        or set(canary.get("call_keys", []))
        != set(records[canary["record_key"]]["call_keys"].values())
        or len(canary.get("call_keys", [])) != 3
    ):
        raise UnionJudgeError("complete judge checkpoint lacks a valid three-call canary")
    canary_keys = set(canary["call_keys"])
    canary_record = records[canary["record_key"]]
    canary_prompts = prompts(canary_record)
    for metric, key in canary_record["call_keys"].items():
        validate_canary_call(calls[key], canary_prompts[metric])
    parse_statuses = Counter(str(call.get("parse_status")) for call in calls.values())
    returned_models = Counter(str(call.get("returned_model")) for call in calls.values())
    transports = Counter(str(call.get("transport")) for call in calls.values())
    batch_call_keys = {
        key for key, call in calls.items() if call.get("transport") == "batch"
    }
    synchronous_call_keys = {
        key for key, call in calls.items() if call.get("transport") == "synchronous"
    }
    if batch_call_keys and synchronous_call_keys != canary_keys:
        raise UnionJudgeError("mixed transport does not isolate the three-call canary")
    batch_receipts = checkpoint.get("batch_receipts", [])
    if not isinstance(batch_receipts, list):
        raise UnionJudgeError("judge batch receipts are not a list")
    if transports.get("batch", 0):
        if checkpoint_path is None:
            raise UnionJudgeError(
                "complete mixed-transport validation requires the checkpoint path"
            )
        validate_batch_receipt_archives(
            checkpoint, items, checkpoint_path.resolve()
        )
        if transports != {"batch": 6357, "synchronous": 3}:
            raise UnionJudgeError("mixed judge transport does not preserve 3 + 6,357 calls")
        receipt_ids = [str(row.get("batch_id")) for row in batch_receipts]
        successful_receipt_ids = {
            str(row.get("batch_id"))
            for row in batch_receipts
            if int(row.get("successful_calls", 0)) > 0
        }
        call_batch_ids = {
            str(call.get("batch_id"))
            for call in calls.values()
            if call.get("transport") == "batch"
        }
        if (
            not batch_receipts
            or len(receipt_ids) != len(set(receipt_ids))
            or successful_receipt_ids != call_batch_ids
            or sum(int(row.get("successful_calls", -1)) for row in batch_receipts)
            != 6357
            or any(
                int(row.get("request_count", -1))
                != int(row.get("successful_calls", -1))
                + int(row.get("failed_calls", -1))
                + int(row.get("unreturned_calls", -1))
                or int(row.get("request_count", -1))
                != int(row.get("observed_output_ids", -1))
                + int(row.get("observed_error_ids", -1))
                + int(row.get("unreturned_calls", -1))
                or len(str(row.get("input_sha256", ""))) != 64
                or (
                    row.get("output_sha256") is not None
                    and len(str(row.get("output_sha256"))) != 64
                )
                or (
                    row.get("error_sha256") is not None
                    and len(str(row.get("error_sha256"))) != 64
                )
                or len(str(row.get("custom_ids_sha256", ""))) != 64
                or len(str(row.get("successful_custom_ids_sha256", ""))) != 64
                or len(str(row.get("failed_custom_ids_sha256", ""))) != 64
                or len(str(row.get("unreturned_custom_ids_sha256", ""))) != 64
                or len(str(row.get("tool_sha256", ""))) != 64
                for row in batch_receipts
            )
            or len({str(row.get("tool_sha256")) for row in batch_receipts}) != 1
        ):
            raise UnionJudgeError("judge batch receipts do not reproduce")
    elif batch_receipts:
        raise UnionJudgeError("batch receipts exist without batch judge calls")
    completed = sorted(
        str(call.get("completed_utc"))
        for call in calls.values()
        if call.get("completed_utc")
    )
    if len(completed) != len(calls):
        raise UnionJudgeError("one or more judge calls lack completion timestamps")
    expected_usage = {
        field: sum(int(call.get("usage", {}).get(field, 0)) for call in calls.values())
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    if checkpoint.get("usage") != expected_usage:
        raise UnionJudgeError("judge usage totals do not reproduce")
    return {
        "record_count": len(records),
        "call_count": len(calls),
        "parse_status_counts": dict(sorted(parse_statuses.items())),
        "returned_model_counts": dict(sorted(returned_models.items())),
        "transport_counts": dict(sorted(transports.items())),
        "retry_count_total": sum(int(call.get("retry_count", 0)) for call in calls.values()),
        "historical_error_count": len(checkpoint.get("errors", [])),
        "first_call_utc": completed[0] if completed else None,
        "last_call_utc": completed[-1] if completed else None,
        "union_input_sha256": checkpoint["union_input_sha256"],
        "complete_receipts": checkpoint["complete_receipts"],
        "template_receipt": checkpoint["template_receipt"],
    }


def finalize(checkpoint: dict[str, Any], items: list[dict[str, Any]]) -> None:
    calls = checkpoint["calls"]
    records = derive_records(calls, items)
    checkpoint["records"] = records
    checkpoint["complete"] = True
    checkpoint["completed_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    checkpoint["usage"] = {
        field: sum(int(call.get("usage", {}).get(field, 0)) for call in calls.values())
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def seal_or_validate(
    checkpoint: dict[str, Any],
    items: list[dict[str, Any]],
    receipts: dict[str, str],
    *,
    checkpoint_path: Path | None = None,
) -> bool:
    """Seal once, or validate an existing seal without changing one byte."""

    if checkpoint.get("complete") is True:
        validate_complete_checkpoint(
            checkpoint, items, receipts, checkpoint_path=checkpoint_path
        )
        return False
    finalize(checkpoint, items)
    validate_complete_checkpoint(
        checkpoint, items, receipts, checkpoint_path=checkpoint_path
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwen-source", type=Path, required=True)
    parser.add_argument("--qwen-sensitivity", type=Path, required=True)
    parser.add_argument("--qwen-sensitivity-complete-sha256", required=True)
    parser.add_argument("--gemma-source", type=Path, required=True)
    parser.add_argument("--gemma-sensitivity", type=Path, required=True)
    parser.add_argument("--gemma-sensitivity-complete-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--max-new-calls",
        type=int,
        default=6360,
        help="Maximum calls in this pass; rerun to resume the remaining calls.",
    )
    args = parser.parse_args()
    validate_run_limits(args.workers, args.max_new_calls)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise UnionJudgeError("OPENAI_API_KEY is not set")
    items, receipts = load_items(args)
    expected = expected_checkpoint(items, receipts)
    if args.output.exists():
        checkpoint = json.loads(args.output.read_text(encoding="utf-8"))
        for key, value in expected.items():
            if key not in {
                "calls",
                "records",
                "errors",
                "complete",
                "canary",
                "batch_receipts",
            } and checkpoint.get(key) != value:
                raise UnionJudgeError(f"existing judge checkpoint identity drift: {key}")
    else:
        checkpoint = expected

    call_map: dict[str, tuple[str, str]] = {}
    for item in items:
        for metric, prompt in prompts(item).items():
            key = call_key(item, metric, prompt)
            if key in call_map:
                raise UnionJudgeError("duplicate judge call key")
            call_map[key] = (item["record_key"], prompt)
    calls = checkpoint["calls"]
    for key, call in calls.items():
        if key not in call_map or not isinstance(call, dict):
            raise UnionJudgeError("cached judge checkpoint has an unexpected call")
        validate_call(call, call_map[key][1])
    pending = [key for key in sorted(call_map) if key not in calls]
    if not pending:
        if seal_or_validate(checkpoint, items, receipts):
            atomic_json(args.output, checkpoint)
        print(f"judge complete: {len(items)} records / {len(calls)} calls")
        return

    # A fresh administration starts with one complete three-call record.  This
    # verifies model identity and parsing before concurrency is released.
    remaining_budget = args.max_new_calls
    if not calls:
        first_record = items[0]["record_key"]
        canary_keys = [
            call_key(items[0], metric, prompt)
            for metric, prompt in prompts(items[0]).items()
        ]
        if len(canary_keys) != 3 or any(key not in pending for key in canary_keys):
            raise UnionJudgeError("could not construct a complete three-call canary")
        for key in canary_keys:
            _, prompt = call_map[key]
            _, response = call_with_retry(prompt, api_key, key)
            response["request_prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            response["transport"] = "synchronous"
            validate_canary_call(response, prompt)
            calls[key] = response
        checkpoint["canary"] = {
            "record_key": first_record,
            "call_keys": canary_keys,
            "returned_model": MODEL,
        }
        atomic_json(args.output, checkpoint)
        remaining_budget -= 3
        print(f"judge canary passed: {len(canary_keys)} calls from {MODEL}", flush=True)
    else:
        canary = checkpoint.get("canary")
        if not isinstance(canary, dict) or len(canary.get("call_keys", [])) != 3:
            raise UnionJudgeError("resumed judge checkpoint lacks a sealed canary")
        canary_record = canary.get("record_key")
        for key in canary["call_keys"]:
            if key not in calls or call_map.get(key, (None, None))[0] != canary_record:
                raise UnionJudgeError("resumed three-call canary identity drifted")
            validate_canary_call(calls[key], call_map[key][1])

    pending = [key for key in sorted(call_map) if key not in calls]
    selected = pending[:remaining_budget]

    print(
        f"judge calls total={len(call_map)} cached={len(calls)} "
        f"scheduled_this_pass={len(selected)}",
        flush=True,
    )
    completed_since_save = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(call_with_retry, call_map[key][1], api_key, key): key
            for key in selected
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                _, response = future.result()
            except Exception as exc:  # noqa: BLE001 - retain all completed calls
                checkpoint["errors"].append(
                    {
                        "key": key,
                        "error_type": type(exc).__name__,
                        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                )
                continue
            prompt = call_map[key][1]
            response["request_prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            response["transport"] = "synchronous"
            validate_call(response, prompt)
            calls[key] = response
            completed_since_save += 1
            if completed_since_save >= 20:
                atomic_json(args.output, checkpoint)
                completed_since_save = 0
    atomic_json(args.output, checkpoint)
    remaining = len(call_map) - len(calls)
    if remaining:
        print(f"judge pass incomplete: {remaining} calls remain; rerun to resume", flush=True)
        raise SystemExit(2)
    seal_or_validate(checkpoint, items, receipts)
    atomic_json(args.output, checkpoint)
    print(f"judge complete: {len(items)} records / {len(calls)} calls")


if __name__ == "__main__":
    main()
