import unittest
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from maxmcp import max_client, tool_response
from maxmcp.max_client import MaxClient, MaxBridgeUnavailableError


class StartupDiagnosticsTests(unittest.TestCase):
    def test_auto_connect_timeout_has_native_context_and_no_send(self):
        client = MaxClient(transport="auto")
        sock = mock.Mock()
        sock.connect.side_effect = max_client.socket.timeout()
        with mock.patch.object(client, "_send_via_pipe", side_effect=ConnectionError("no native pipe")), \
             mock.patch.object(max_client.socket, "socket", return_value=sock), \
             mock.patch.object(max_client, "list_processes", return_value=[{"pid": 23620}]):
            with self.assertRaises(MaxBridgeUnavailableError) as caught:
                client.send_command("mutation", timeout=2)
        error = tool_response._error_from_exception(caught.exception)
        self.assertEqual(error["code"], "BRIDGE_DOWN")
        self.assertEqual(error["details"]["tcp_failure"], "connect_timeout")
        self.assertEqual(error["details"]["running_max_pids"], [23620])
        self.assertFalse(error["details"]["request_sent"])
        self.assertNotIn("Is the MCP TCP listener", error["message"])
        sock.sendall.assert_not_called()
        sock.close.assert_called_once()

    def test_receive_timeout_after_send_is_not_unsent_startup_failure(self):
        client = MaxClient(transport="auto")
        sock = mock.Mock()
        sock.recv.side_effect = max_client.socket.timeout()
        with mock.patch.object(client, "_send_via_pipe", side_effect=ConnectionError("no native pipe")), \
             mock.patch.object(max_client.socket, "socket", return_value=sock):
            with self.assertRaises(TimeoutError) as caught:
                client.send_command("mutation", timeout=2)
        self.assertNotIsInstance(caught.exception, max_client.MaxTcpConnectTimeoutError)
        self.assertFalse(hasattr(caught.exception, "details"))
        sock.sendall.assert_called_once()
        sock.close.assert_called_once()

    def test_explicit_tcp_connect_timeout_keeps_tcp_advice(self):
        client = MaxClient(transport="tcp")
        sock = mock.Mock()
        sock.connect.side_effect = max_client.socket.timeout()
        with mock.patch.object(max_client.socket, "socket", return_value=sock):
            with self.assertRaisesRegex(max_client.MaxTcpConnectTimeoutError, "TCP listener"):
                client.send_command("", cmd_type="ping", timeout=2)

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
