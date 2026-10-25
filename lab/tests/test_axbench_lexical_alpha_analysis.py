from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.axbench_lexicality20.lexical_analyze_sensitivity import (  # noqa: E402
    concept_bootstrap_interval,
    multilevel_difference_interval,
    paired_difference,
)
from experiments.axbench_lexicality20.lexical_judge_union import (  # noqa: E402
    MODEL,
    call_key,
    expected_checkpoint,
    finalize,
    prompts,
    seal_or_validate,
    template_receipt,
    validate_canary_call,
    validate_batch_receipt_archives,
    validate_complete_checkpoint,
    validate_run_limits,
)
from experiments.axbench_lexicality20.lexical_judge_union_batch import (  # noqa: E402
    BatchJudgeError,
    BATCH_CHUNK_REQUESTS,
    active_batch,
    archive_entry,
    call_map as batch_call_map,
    ensure_batch_created,
    expected_metadata,
    expected_state_identity,
    ingest,
    load_state,
    open_checkpoint,
    request_jsonl,
    select_batch_chunk,
    validate_remote_batch,
    validate_state_coherence,
)
from experiments.axbench_lexicality20.protocol import canonical_sha256  # noqa: E402


def panel_rows(value: float) -> list[dict[str, object]]:
    return [
        {
            "concept_id": concept,
            "prompt_id": f"c{concept}-p{prompt}",
            "generation_seed": seed,
            "strict_hit": value,
        }
        for concept in range(10)
        for prompt in range(4)
        for seed in range(2)
    ]


class LexicalAlphaAnalysisTests(unittest.TestCase):
    def test_concept_bootstrap_preserves_constant_mean(self) -> None:
        result = concept_bootstrap_interval(panel_rows(1.0), seed=4)
        self.assertEqual(result["ci95_low"], 1.0)
        self.assertEqual(result["ci95_high"], 1.0)
        self.assertEqual(result["bootstrap_repetitions"], 20_000)

    def test_multilevel_interval_preserves_paired_generation_seeds(self) -> None:
        rows = [
            {
                "concept_id": concept,
                "prompt_id": f"c{concept}-p{prompt}",
                "generation_seed": generation,
                "difference": 0.25,
            }
            for concept in range(10)
            for prompt in range(4)
            for generation in range(2)
        ]
        result = multilevel_difference_interval(rows, seed=5)
        self.assertEqual(result["mean_difference"], 0.25)
        self.assertEqual(result["ci95_low"], 0.25)
        self.assertEqual(result["ci95_high"], 0.25)
        self.assertIsNone(result["p_value"])

    def test_paired_difference_refuses_missing_cell(self) -> None:
        left = panel_rows(1.0)
        right = panel_rows(0.0)[:-1]
        with self.assertRaisesRegex(RuntimeError, "80/80"):
            paired_difference(left, right, seed=6)

    def test_paired_difference_uses_matching_prompt_seed_cells(self) -> None:
        result = paired_difference(panel_rows(1.0), panel_rows(0.0), seed=7)
        self.assertEqual(result["n_pairs"], 80)
        self.assertEqual(result["mean_difference"], 1.0)
        self.assertEqual(result["ci95_low"], 1.0)
        self.assertEqual(result["ci95_high"], 1.0)


class LexicalAlphaJudgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.items = [
            {
                "record_key": f"model:cell-{index:04d}",
                "cell_id": f"cell-{index:04d}",
                "source": "fixture",
                "result_sha256": hashlib.sha256(str(index).encode()).hexdigest(),
                "record_index": index,
                "model_key": "qwen" if index < 1096 else "gemma",
                "concept_id": index % 10,
                "concept_label": f"concept {index % 10}",
                "instruction": f"instruction {index}",
                "sentence": f"sentence {index}",
            }
            for index in range(2120)
        ]
        cls.receipts = {
            "qwen_source_complete_sha256": "1" * 64,
            "qwen_sensitivity_complete_sha256": "2" * 64,
            "gemma_source_complete_sha256": "3" * 64,
            "gemma_sensitivity_complete_sha256": "4" * 64,
        }
        cls.checkpoint = expected_checkpoint(cls.items, cls.receipts)
        for item in cls.items:
            for metric, prompt in prompts(item).items():
                key = call_key(item, metric, prompt)
                cls.checkpoint["calls"][key] = {
                    "completion": "Rating: [[2]]",
                    "rating": 2.0,
                    "parse_status": "parsed",
                    "returned_model": MODEL,
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                    "response_id": f"response-{key}",
                    "completed_utc": "2026-09-05T00:00:00+00:00",
                    "retry_count": 0,
                    "request_prompt_sha256": hashlib.sha256(
                        prompt.encode("utf-8")
                    ).hexdigest(),
                    "transport": "synchronous",
                }
        first_keys = [
            call_key(cls.items[0], metric, prompt)
            for metric, prompt in prompts(cls.items[0]).items()
        ]
        cls.checkpoint["canary"] = {
            "record_key": cls.items[0]["record_key"],
            "call_keys": first_keys,
            "returned_model": MODEL,
        }
        finalize(cls.checkpoint, cls.items)

    def canary_checkpoint(self) -> dict[str, object]:
        checkpoint = expected_checkpoint(self.items, self.receipts)
        first_keys = [
            call_key(self.items[0], metric, prompt)
            for metric, prompt in prompts(self.items[0]).items()
        ]
        for key in first_keys:
            checkpoint["calls"][key] = json.loads(
                json.dumps(self.checkpoint["calls"][key])
            )
        checkpoint["canary"] = {
            "record_key": self.items[0]["record_key"],
            "call_keys": first_keys,
            "returned_model": MODEL,
        }
        return checkpoint

    def test_complete_judge_is_bound_to_all_rows_and_calls(self) -> None:
        result = validate_complete_checkpoint(
            self.checkpoint, self.items, self.receipts
        )
        self.assertEqual(result["record_count"], 2120)
        self.assertEqual(result["call_count"], 6360)
        self.assertEqual(result["parse_status_counts"], {"parsed": 6360})

    def test_complete_judge_rejects_same_key_with_changed_sentence(self) -> None:
        tampered = list(self.items)
        tampered[0] = {**tampered[0], "sentence": "different generated text"}
        with self.assertRaisesRegex(RuntimeError, "identity drift"):
            validate_complete_checkpoint(self.checkpoint, tampered, self.receipts)

    def test_complete_judge_rejects_missing_stored_record(self) -> None:
        key = self.items[-1]["record_key"]
        value = self.checkpoint["records"].pop(key)
        try:
            with self.assertRaisesRegex(RuntimeError, "do not reproduce"):
                validate_complete_checkpoint(
                    self.checkpoint, self.items, self.receipts
                )
        finally:
            self.checkpoint["records"][key] = value

    def test_template_receipt_seals_all_three_metrics(self) -> None:
        receipt = template_receipt()
        self.assertEqual(receipt["metric_order"], ["concept", "instruction", "fluency"])
        self.assertEqual(set(receipt["template_sha256"]), set(receipt["metric_order"]))
        self.assertTrue(all(len(value) == 64 for value in receipt["template_sha256"].values()))

    def test_call_budget_cannot_release_an_incomplete_canary(self) -> None:
        for maximum in (1, 2):
            with self.assertRaisesRegex(RuntimeError, "at least 3"):
                validate_run_limits(4, maximum)
        validate_run_limits(4, 3)

    def test_canary_rejects_fail_to_zero_parse(self) -> None:
        prompt = "canary prompt"
        call = {
            "completion": "No machine-readable score was returned.",
            "rating": 0.0,
            "parse_status": "missing_rating_to_zero",
            "returned_model": MODEL,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "response_id": "response-canary",
            "request_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        with self.assertRaisesRegex(RuntimeError, "did not parse"):
            validate_canary_call(call, prompt)

    def test_complete_checkpoint_validation_is_byte_stable(self) -> None:
        before = json.dumps(self.checkpoint, ensure_ascii=False, sort_keys=True)
        changed = seal_or_validate(self.checkpoint, self.items, self.receipts)
        after = json.dumps(self.checkpoint, ensure_ascii=False, sort_keys=True)
        self.assertFalse(changed)
        self.assertEqual(after, before)

    def test_complete_validator_rejects_unparsed_canary(self) -> None:
        checkpoint = json.loads(json.dumps(self.checkpoint))
        key = checkpoint["canary"]["call_keys"][0]
        checkpoint["calls"][key]["completion"] = "No rating was returned."
        checkpoint["calls"][key]["rating"] = 0.0
        checkpoint["calls"][key]["parse_status"] = "missing_rating_to_zero"
        finalize(checkpoint, self.items)
        with self.assertRaisesRegex(RuntimeError, "canary rating did not parse"):
            validate_complete_checkpoint(checkpoint, self.items, self.receipts)

    def test_complete_validator_binds_canary_to_sync_transport(self) -> None:
        checkpoint = json.loads(json.dumps(self.checkpoint))
        key = checkpoint["canary"]["call_keys"][0]
        checkpoint["calls"][key]["transport"] = "batch"
        checkpoint["calls"][key]["batch_id"] = "batch-1"
        with self.assertRaisesRegex(RuntimeError, "isolate the three-call canary"):
            validate_complete_checkpoint(checkpoint, self.items, self.receipts)

    def test_batch_jsonl_preserves_exact_call_keys_model_and_prompts(self) -> None:
        calls_by_key = batch_call_map(self.items)
        keys = sorted(calls_by_key)[:3]
        payload = request_jsonl(keys, calls_by_key)
        rows = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
        self.assertEqual([row["custom_id"] for row in rows], keys)
        for row in rows:
            self.assertEqual(row["method"], "POST")
            self.assertEqual(row["url"], "/v1/chat/completions")
            self.assertEqual(row["body"]["model"], MODEL)
            self.assertEqual(row["body"]["temperature"], 0)
            self.assertEqual(
                row["body"]["messages"],
                [{"role": "user", "content": calls_by_key[row["custom_id"]][1]}],
            )

    def test_batch_chunking_is_deterministic_and_below_account_limit(self) -> None:
        pending = [f"call-{index:05d}" for index in range(6357)]
        selected = select_batch_chunk(pending)
        self.assertEqual(BATCH_CHUNK_REQUESTS, 2500)
        self.assertEqual(selected, pending[:2500])

    def test_batch_preflight_rejects_extra_synchronous_cached_call(self) -> None:
        checkpoint = self.canary_checkpoint()
        calls_by_key = batch_call_map(self.items)
        extra = next(key for key in sorted(calls_by_key) if key not in checkpoint["calls"])
        checkpoint["calls"][extra] = json.loads(
            json.dumps(self.checkpoint["calls"][extra])
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "judge.json"
            output.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaisesRegex(BatchJudgeError, "exactly the three"):
                open_checkpoint(
                    output, self.items, self.receipts, calls_by_key
                )

    def test_remote_batch_identity_and_terminal_counts_are_checked(self) -> None:
        row = {
            "chunk_index": 0,
            "batch_id": "batch-1",
            "input_file_id": "file-1",
            "request_count": 12,
        }
        remote = {
            "id": "batch-1",
            "input_file_id": "file-1",
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
            "metadata": expected_metadata(row),
            "status": "completed",
            "request_counts": {"total": 12, "completed": 11, "failed": 1},
        }
        validate_remote_batch(remote, row, terminal=True)
        drifted = {**remote, "metadata": {"study": "other", "chunk": "0"}}
        with self.assertRaisesRegex(BatchJudgeError, "identity drifted"):
            validate_remote_batch(drifted, row, terminal=True)
        zero_yield = {
            **remote,
            "status": "cancelled",
            "request_counts": {"total": 0, "completed": 0, "failed": 0},
            "output_file_id": None,
            "error_file_id": None,
        }
        validate_remote_batch(zero_yield, row, terminal=True)

    def test_terminal_batch_remains_active_until_ingested(self) -> None:
        row = {"status": "expired", "ingested": False}
        self.assertIs(active_batch({"batches": [row]}), row)
        row["ingested"] = True
        self.assertIsNone(active_batch({"batches": [row]}))

    def test_missing_state_refuses_existing_batch_provenance(self) -> None:
        checkpoint = self.canary_checkpoint()
        extra = next(
            key for key in sorted(self.checkpoint["calls"]) if key not in checkpoint["calls"]
        )
        checkpoint["calls"][extra] = {
            **json.loads(json.dumps(self.checkpoint["calls"][extra])),
            "transport": "batch",
            "batch_id": "batch-1",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(BatchJudgeError, "state is missing"):
                load_state(
                    root / "missing-state.json",
                    checkpoint,
                    root / "judge.json",
                    root / "batch-files",
                )

    def test_state_reconciles_checkpoint_first_ingestion_commit(self) -> None:
        state = {
            "batches": [
                {
                    "chunk_index": 0,
                    "batch_id": "batch-1",
                    "ingested": False,
                }
            ]
        }
        checkpoint = {"batch_receipts": [{"batch_id": "batch-1"}]}
        self.assertTrue(validate_state_coherence(state, checkpoint))
        self.assertTrue(state["batches"][0]["ingested"])

    def test_ambiguous_create_recovers_matching_remote_batch(self) -> None:
        row = {
            "chunk_index": 0,
            "batch_id": None,
            "input_file_id": "file-1",
            "request_count": 3,
            "create_attempted_utc": "2026-09-05T00:00:00+00:00",
        }
        state = {"batches": [row]}
        remote = {
            "id": "batch-1",
            "input_file_id": "file-1",
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
            "metadata": expected_metadata(row),
            "status": "in_progress",
            "request_counts": {"total": 3, "completed": 1, "failed": 0},
        }
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(state=Path(directory) / "state.json")
            with patch(
                "experiments.axbench_lexicality20.lexical_judge_union_batch."
                "_recent_batch_for_input",
                return_value=remote,
            ):
                recovered = ensure_batch_created(args, "unused", state, row)
        self.assertEqual(recovered["batch_id"], "batch-1")
        self.assertTrue(recovered["create_recovered"])

    def test_zero_yield_terminal_batch_is_archived_before_retry(self) -> None:
        checkpoint = self.canary_checkpoint()
        calls_by_key = batch_call_map(self.items)
        pending = sorted(set(calls_by_key) - set(checkpoint["calls"]))[:3]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "judge.json"
            state_path = root / "batch-state.json"
            artifacts = root / "batch-files"
            artifacts.mkdir()
            output.write_text(json.dumps(checkpoint), encoding="utf-8")
            input_path = artifacts / "chunk-00-input.jsonl"
            input_payload = request_jsonl(pending, calls_by_key)
            input_path.write_bytes(input_payload)
            row = {
                "chunk_index": 0,
                "batch_id": "batch-1",
                "input_file_id": "file-input",
                "request_count": 3,
                "custom_ids_sha256": canonical_sha256(pending),
                "input_path": str(input_path.resolve()),
                "input_bytes": len(input_payload),
                "input_sha256": hashlib.sha256(input_payload).hexdigest(),
                "tool_sha256": "a" * 64,
                "submitted_utc": "2026-09-05T00:00:00+00:00",
                "status": "cancelled",
                "ingested": False,
                "endpoint": "/v1/chat/completions",
                "completion_window": "24h",
                "metadata": expected_metadata({"chunk_index": 0}),
            }
            state = {
                **expected_state_identity(checkpoint, output, artifacts),
                "batches": [row],
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            remote = {
                "id": "batch-1",
                "input_file_id": "file-input",
                "endpoint": "/v1/chat/completions",
                "completion_window": "24h",
                "metadata": expected_metadata(row),
                "status": "cancelled",
                "request_counts": {"total": 0, "completed": 0, "failed": 0},
                "output_file_id": None,
                "error_file_id": None,
                "errors": {"message": "cancelled before validation"},
            }
            args = SimpleNamespace(
                output=output,
                state=state_path,
                artifacts=artifacts,
            )
            with patch(
                "experiments.axbench_lexicality20.lexical_judge_union_batch."
                "load_items",
                return_value=(self.items, self.receipts),
            ), patch(
                "experiments.axbench_lexicality20.lexical_judge_union_batch."
                "api_request",
                return_value=remote,
            ):
                remaining = ingest(args, "unused")
            self.assertEqual(remaining, 6357)
            persisted = json.loads(output.read_text(encoding="utf-8"))
            receipt = persisted["batch_receipts"][0]
            self.assertEqual(receipt["unreturned_calls"], 3)
            self.assertEqual(receipt["successful_calls"], 0)
            state_after = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIsNone(active_batch(state_after))

    def test_all_error_batch_archive_validates_and_tampering_fails(self) -> None:
        item = self.items[0]
        expected_prompts = prompts(item)
        keys = [
            call_key(item, metric, prompt)
            for metric, prompt in expected_prompts.items()
        ]
        input_payload = request_jsonl(
            keys,
            {
                call_key(item, metric, prompt): (item["record_key"], prompt)
                for metric, prompt in expected_prompts.items()
            },
        )
        error_payload = (
            "\n".join(json.dumps({"custom_id": key}) for key in keys) + "\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "batch"
            artifacts.mkdir()
            input_path = artifacts / "chunk-00-input.jsonl"
            error_path = artifacts / "chunk-00-errors.jsonl"
            input_path.write_bytes(input_payload)
            error_path.write_bytes(error_payload)
            checkpoint_path = root / "judge.json"
            receipt = {
                "chunk_index": 0,
                "batch_id": "batch-1",
                "input_file_id": "file-input",
                "output_file_id": None,
                "error_file_id": "file-errors",
                "endpoint": "/v1/chat/completions",
                "completion_window": "24h",
                "metadata": expected_metadata({"chunk_index": 0}),
                "terminal_status": "failed",
                "remote_request_counts": {
                    "total": 3,
                    "completed": 0,
                    "failed": 3,
                },
                "request_count": 3,
                "observed_output_ids": 0,
                "observed_error_ids": 3,
                "successful_calls": 0,
                "failed_calls": 3,
                "unreturned_calls": 0,
                "custom_ids_sha256": canonical_sha256(sorted(keys)),
                "successful_custom_ids_sha256": canonical_sha256([]),
                "failed_custom_ids_sha256": canonical_sha256(sorted(keys)),
                "unreturned_custom_ids_sha256": canonical_sha256([]),
                "archive_root_relative_to_checkpoint": "batch",
                "archive_files": {
                    "input": archive_entry(input_path, "file-input"),
                    "output": None,
                    "error": archive_entry(error_path, "file-errors"),
                },
            }
            checkpoint = {
                "calls": {},
                "errors": [
                    {
                        "batch_id": "batch-1",
                        "key": key,
                        "error_type": "batch_request_error",
                    }
                    for key in keys
                ],
                "batch_receipts": [receipt],
            }
            validate_batch_receipt_archives(
                checkpoint, [item], checkpoint_path
            )
            error_path.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash drifted"):
                validate_batch_receipt_archives(
                    checkpoint, [item], checkpoint_path
                )

    def test_complete_mixed_transport_checkpoint_replays_archives(self) -> None:
        checkpoint = json.loads(json.dumps(self.checkpoint))
        canary_keys = set(checkpoint["canary"]["call_keys"])
        calls_by_key = batch_call_map(self.items)
        batch_keys = sorted(set(calls_by_key) - canary_keys)
        for key, call in checkpoint["calls"].items():
            if key not in canary_keys:
                call["transport"] = "batch"
                call["batch_id"] = "batch-1"
                call["batch_request_id"] = f"request-{key}"
        input_payload = request_jsonl(batch_keys, calls_by_key)
        output_payload = (
            "\n".join(
                json.dumps(
                    {
                        "custom_id": key,
                        "response": {
                            "status_code": 200,
                            "request_id": checkpoint["calls"][key][
                                "batch_request_id"
                            ],
                            "body": {
                                "id": checkpoint["calls"][key]["response_id"],
                                "model": MODEL,
                                "created": 1788566400,
                                "choices": [
                                    {
                                        "message": {
                                            "content": checkpoint["calls"][key][
                                                "completion"
                                            ]
                                        }
                                    }
                                ],
                                "usage": checkpoint["calls"][key]["usage"],
                            },
                        },
                    },
                    separators=(",", ":"),
                )
                for key in batch_keys
            )
            + "\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "batch"
            artifacts.mkdir()
            input_path = artifacts / "chunk-00-input.jsonl"
            output_path = artifacts / "chunk-00-output.jsonl"
            input_path.write_bytes(input_payload)
            output_path.write_bytes(output_payload)
            checkpoint["batch_receipts"] = [
                {
                    "chunk_index": 0,
                    "batch_id": "batch-1",
                    "input_file_id": "file-input",
                    "output_file_id": "file-output",
                    "error_file_id": None,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": "24h",
                    "metadata": expected_metadata({"chunk_index": 0}),
                    "terminal_status": "completed",
                    "remote_request_counts": {
                        "total": len(batch_keys),
                        "completed": len(batch_keys),
                        "failed": 0,
                    },
                    "request_count": len(batch_keys),
                    "observed_output_ids": len(batch_keys),
                    "observed_error_ids": 0,
                    "successful_calls": len(batch_keys),
                    "failed_calls": 0,
                    "unreturned_calls": 0,
                    "custom_ids_sha256": canonical_sha256(batch_keys),
                    "successful_custom_ids_sha256": canonical_sha256(batch_keys),
                    "failed_custom_ids_sha256": canonical_sha256([]),
                    "unreturned_custom_ids_sha256": canonical_sha256([]),
                    "input_bytes": len(input_payload),
                    "input_sha256": hashlib.sha256(input_payload).hexdigest(),
                    "output_bytes": len(output_payload),
                    "output_sha256": hashlib.sha256(output_payload).hexdigest(),
                    "error_bytes": 0,
                    "error_sha256": None,
                    "archive_root_relative_to_checkpoint": "batch",
                    "archive_files": {
                        "input": archive_entry(input_path, "file-input"),
                        "output": archive_entry(output_path, "file-output"),
                        "error": None,
                    },
                    "submitted_utc": "2026-09-05T00:00:00+00:00",
                    "ingested_utc": "2026-09-05T00:01:00+00:00",
                    "tool_sha256": "a" * 64,
                }
            ]
            result = validate_complete_checkpoint(
                checkpoint,
                self.items,
                self.receipts,
                checkpoint_path=root / "judge.json",
            )
        self.assertEqual(
            result["transport_counts"], {"batch": 6357, "synchronous": 3}
        )


if __name__ == "__main__":
    unittest.main()
