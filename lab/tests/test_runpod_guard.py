from __future__ import annotations

import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import runpod_guard  # noqa: E402


class RunpodGuardTests(unittest.TestCase):
    def test_load_key_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.txt"
            path.write_text(
                "RUNPOD_API_KEY = test-key\nOTHER=x\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(runpod_guard.load_api_key(path), "test-key")

    def test_environment_key_takes_precedence(self) -> None:
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "env-key"}, clear=True):
            self.assertEqual(runpod_guard.load_api_key(None), "env-key")

    def test_heartbeat_and_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease"
            runpod_guard.heartbeat(path)
            self.assertTrue(path.is_file())
            self.assertLess(runpod_guard.lease_age_minutes(path), 0.1)

    def test_missing_lease_is_expired(self) -> None:
        self.assertEqual(
            runpod_guard.lease_age_minutes(Path("definitely-absent.lease")),
            float("inf"),
        )

    def test_guard_retries_status_failure_until_final_state(self) -> None:
        args = Namespace(
            secrets_file=None,
            pod_id="pod-id",
            lease_file=Path("definitely-absent.lease"),
            lease_minutes=10,
            max_runtime_minutes=360,
            poll_seconds=0.01,
            stop_grace_minutes=5,
        )
        with (
            patch("runpod_guard.load_api_key", return_value="private-key"),
            patch(
                "runpod_guard.get_pod",
                side_effect=[
                    runpod_guard.GuardError("transient control-plane failure"),
                    {"desiredStatus": "EXITED"},
                ],
            ),
            patch("runpod_guard.stop_pod") as stop,
            patch("runpod_guard.time.sleep"),
        ):
            self.assertEqual(runpod_guard.run_guard(args), 0)
        stop.assert_called_once_with("pod-id", "private-key")

    @patch("runpod_guard.urllib.request.urlopen")
    def test_stop_uses_header_not_url(self, urlopen) -> None:
        response = MagicMock()
        response.read.return_value = b"{}"
        urlopen.return_value.__enter__.return_value = response

        runpod_guard.stop_pod("pod/id", "private-key")

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://rest.runpod.io/v1/pods/pod%2Fid/stop",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn("private-key", request.full_url)

    @patch("runpod_guard.urllib.request.urlopen")
    def test_start_uses_header_not_url(self, urlopen) -> None:
        response = MagicMock()
        response.read.return_value = b"{}"
        urlopen.return_value.__enter__.return_value = response

        runpod_guard.start_pod("pod/id", "private-key")

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://rest.runpod.io/v1/pods/pod%2Fid/start",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn("private-key", request.full_url)


if __name__ == "__main__":
    unittest.main()
