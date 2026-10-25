from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import runpod_capacity  # noqa: E402


ROWS = [
    {
        "id": "US-MD-1",
        "gpuAvailability": [
            {
                "displayName": "A100 SXM",
                "gpuId": "NVIDIA A100-SXM4-80GB",
                "stockStatus": "Medium",
            }
        ],
    }
]


class RunpodCapacityTests(unittest.TestCase):
    def test_parse_filters_one_region_and_target_gpu(self) -> None:
        rows = runpod_capacity.parse_capacity(
            ROWS, data_center_id="US-MD-1",
            gpu_types=("NVIDIA A100-SXM4-80GB",),
        )
        self.assertEqual(rows[0]["stock_status"], "Medium")
        self.assertEqual(runpod_capacity.ready(rows), rows)

    @patch("runpod_capacity.resolve_runpodctl", return_value="runpodctl-test")
    @patch("runpod_capacity.subprocess.run")
    def test_query_uses_official_cli_and_key_only_in_environment(
        self, run, _resolve
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps(ROWS)
        rows = runpod_capacity.query_capacity(
            "private-key", data_center_id="US-MD-1",
            gpu_types=("NVIDIA A100-SXM4-80GB",),
        )
        args = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(
            args, ["runpodctl-test", "datacenter", "list", "-o", "json"]
        )
        self.assertNotIn("private-key", " ".join(args))
        self.assertEqual(kwargs["env"]["RUNPOD_API_KEY"], "private-key")
        self.assertEqual(rows[0]["stock_status"], "Medium")

    def test_unavailable_row_is_not_ready(self) -> None:
        self.assertEqual(runpod_capacity.ready([{"stock_status": "None"}]), [])

    def test_signal_is_atomic_and_contains_no_credential(self) -> None:
        rows = [{
            "id": "GPU-A", "display_name": "A", "stock_status": "High",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ready.json"
            runpod_capacity.write_signal(path, "US-MD-1", rows)
            text = path.read_text(encoding="utf-8")
            self.assertIn('"schema": "runpod-capacity-signal/v1"', text)
            self.assertNotIn("private-key", text)
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
