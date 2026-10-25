from __future__ import annotations

from subprocess import CompletedProcess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import auto_stop  # noqa: E402


class AutoStopTests(unittest.TestCase):
    @patch("auto_stop.subprocess.run")
    def test_gpu_query_returns_maximum(self, run) -> None:
        run.return_value = CompletedProcess([], 0, stdout="3\n71\n", stderr="")
        self.assertEqual(auto_stop.gpu_util_percent(), 71)

    @patch("auto_stop.subprocess.run")
    def test_gpu_command_failure_is_unknown_not_idle(self, run) -> None:
        run.return_value = CompletedProcess([], 1, stdout="", stderr="driver error")
        self.assertIsNone(auto_stop.gpu_util_percent())

    @patch("auto_stop.subprocess.run")
    def test_malformed_gpu_output_is_unknown(self, run) -> None:
        run.return_value = CompletedProcess([], 0, stdout="N/A\n", stderr="")
        self.assertIsNone(auto_stop.gpu_util_percent())

    @patch("auto_stop.subprocess.run", side_effect=TimeoutError)
    def test_gpu_exception_is_unknown(self, _run) -> None:
        self.assertIsNone(auto_stop.gpu_util_percent())

    @patch.dict("auto_stop.os.environ", {"RUNPOD_API_KEY": "secret-value"}, clear=True)
    @patch("auto_stop.urllib.request.urlopen")
    def test_stop_uses_rest_without_exposing_key(self, urlopen) -> None:
        response = MagicMock()
        response.status = 200
        urlopen.return_value.__enter__.return_value = response

        accepted, detail = auto_stop.request_pod_stop("pod-id")

        self.assertTrue(accepted)
        self.assertEqual(detail, "REST HTTP 200")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://rest.runpod.io/v1/pods/pod-id/stop")
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn("secret-value", request.full_url)

    @patch.dict("auto_stop.os.environ", {}, clear=True)
    @patch("auto_stop.shutil.which", return_value="/usr/bin/runpodctl")
    @patch("auto_stop.subprocess.run")
    def test_stop_cli_fallback_uses_current_argument_order(self, run, _which) -> None:
        run.return_value = CompletedProcess([], 0, stdout="", stderr="")

        accepted, detail = auto_stop.request_pod_stop("pod-id")

        self.assertTrue(accepted)
        self.assertEqual(detail, "runpodctl accepted stop (current syntax)")
        self.assertEqual(run.call_args.args[0],
                         ["runpodctl", "pod", "stop", "pod-id"])

    @patch.dict("auto_stop.os.environ", {"RUNPOD_API_KEY": "pod-key"}, clear=True)
    @patch("auto_stop.shutil.which", return_value="/usr/bin/runpodctl")
    @patch("auto_stop.subprocess.run")
    @patch("auto_stop.urllib.request.urlopen")
    def test_rest_forbidden_falls_back_to_cli(
        self, urlopen, run, _which
    ) -> None:
        urlopen.side_effect = auto_stop.urllib.error.HTTPError(
            "https://rest.runpod.io", 403, "forbidden", {}, None
        )
        run.return_value = CompletedProcess([], 0, stdout="", stderr="")

        accepted, detail = auto_stop.request_pod_stop("pod-id")

        self.assertTrue(accepted)
        self.assertEqual(detail, "runpodctl accepted stop (current syntax)")
        self.assertEqual(run.call_args.args[0],
                         ["runpodctl", "pod", "stop", "pod-id"])


if __name__ == "__main__":
    unittest.main()
