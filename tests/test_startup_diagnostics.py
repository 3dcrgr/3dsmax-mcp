import unittest
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from maxmcp import max_client, tool_response
from maxmcp.max_client import MaxClient, MaxBridgeUnavailableError


class StartupDiagnosticsTests(unittest.TestCase):
    def test_auto_refusal_reports_running_candidates_without_claiming_ready(self):
        client = MaxClient(transport="auto")
        with mock.patch.object(client, "_send_via_pipe", side_effect=ConnectionError("native pipe not found")), \
             mock.patch.object(client, "_send_via_tcp", side_effect=ConnectionRefusedError("TCP refused")), \
             mock.patch.object(max_client, "list_processes", return_value=[{"pid": 34436}]):
            with self.assertRaises(MaxBridgeUnavailableError) as caught:
                client.send_command("", cmd_type="ping")
        error = tool_response._error_from_exception(caught.exception)
        self.assertEqual(error["code"], "BRIDGE_DOWN")
        self.assertTrue(error["retryable"])
        self.assertEqual(error["details"]["running_max_pids"], [34436])
        self.assertEqual(error["details"]["attempted_transports"], ["namedpipe", "tcp"])
        self.assertFalse(error["details"]["request_sent"])
        self.assertIn("may still be starting", error["message"])
        self.assertIn("missing/stopped", error["message"])
        self.assertNotIn("Is the MCP TCP listener", error["message"])

    def test_no_max_reports_start_max(self):
        with mock.patch.object(max_client, "list_processes", return_value=[]):
            error = MaxBridgeUnavailableError("no native pipe", "refused", "localhost", 8765)
        self.assertIn("Start Max", str(error))
        self.assertEqual(error.details["running_max_pids"], [])

    def test_explicit_tcp_keeps_legacy_diagnosis(self):
        client = MaxClient(transport="tcp")
        with mock.patch.object(client, "_send_via_tcp", side_effect=ConnectionRefusedError("legacy listener")), \
             mock.patch.object(max_client, "list_processes") as processes:
            with self.assertRaisesRegex(ConnectionRefusedError, "legacy listener"):
                client.send_command("", cmd_type="ping")
            processes.assert_not_called()

    def test_tcp_disconnect_after_dispatch_is_not_reported_as_unsent_startup(self):
        client = MaxClient(transport="auto")
        with mock.patch.object(client, "_send_via_pipe", side_effect=ConnectionError("no pipe")), \
             mock.patch.object(client, "_send_via_tcp", side_effect=ConnectionResetError("reply lost")):
            with self.assertRaises(ConnectionResetError):
                client.send_command("mutation")


if __name__ == "__main__":
    unittest.main()
