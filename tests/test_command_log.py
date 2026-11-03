"""Issue #50 (MCP side): rolling on-disk log of the commands sent to 3ds Max."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import command_log  # noqa: E402
from maxmcp.max_client import MaxBusy, current_tool  # noqa: E402
from test_hang_diagnosis import PID, REPLY, FakeKernel, _ClientCase  # noqa: E402


class _LogCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name, "MCP_COMMAND_LOG": "1"})
        env.start()
        self.addCleanup(env.stop)
        estimate = mock.patch.object(command_log, "_lines_estimate", None)
        estimate.start()
        self.addCleanup(estimate.stop)

    def lines(self):
        path = Path(self.tmp.name) / "3dsmax-mcp" / "diagnostics" / "recent-commands.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class CommandLogTests(_LogCase):
    def test_reuses_the_hang_diagnostics_folder(self):
        self.assertEqual(command_log.log_path(),
                         Path(self.tmp.name) / "3dsmax-mcp" / "diagnostics" / "recent-commands.jsonl")

    def test_sent_line_fields(self):
        request = json.dumps({"command": "x" * 500, "type": "maxscript", "requestId": "R1"})
        with current_tool("execute_maxscript"):
            command_log.record_sent(request, "R1", "maxscript", 4242, "namedpipe")
        (entry,) = self.lines()
        self.assertEqual(entry["event"], "sent")
        self.assertEqual(entry["request_id"], "R1")
        self.assertEqual(entry["max_pid"], 4242)
        self.assertEqual(entry["server_pid"], os.getpid())
        self.assertEqual(entry["tool"], "execute_maxscript")
        self.assertEqual(entry["cmd_type"], "maxscript")
        self.assertEqual(entry["chars"], 500)
        self.assertEqual(len(entry["command"]), command_log.PREVIEW_CHARS)
        self.assertTrue(entry["t"])

    def test_preview_is_one_line_and_redacts_secrets(self):
        text = command_log.preview('a = 1\n\tpassword = "hunter2"; api_key:abc123 token="t0k"\nb = 2')
        self.assertNotIn("\n", text)
        for secret in ("hunter2", "abc123", "t0k"):
            self.assertNotIn(secret, text)
        self.assertIn("<redacted>", text)
        self.assertIn("a = 1", text)

    def test_done_line_and_disable(self):
        command_log.record_done("R1", "maxscript", 0.0, ok=False, code="MAX_BUSY")
        (entry,) = self.lines()
        self.assertEqual((entry["event"], entry["ok"], entry["code"]), ("done", False, "MAX_BUSY"))
        with mock.patch.dict(os.environ, {"MCP_COMMAND_LOG": "0"}):
            command_log.record_sent("{}", "R2", "maxscript", 1, "namedpipe")
        self.assertEqual(len(self.lines()), 1)

    def test_size_cap_keeps_the_newest(self):
        with mock.patch.object(command_log, "KEEP_LINES", 10):
            for i in range(35):
                command_log.record_done(f"R{i}", "maxscript", 0.0, ok=True)
        ids = [e["request_id"] for e in self.lines()]
        self.assertLessEqual(len(ids), 20)
        self.assertEqual(ids[-1], "R34")
        self.assertEqual(ids, [f"R{i}" for i in range(35 - len(ids), 35)])

    def test_write_errors_never_raise(self):
        with mock.patch.object(Path, "open", side_effect=PermissionError("locked")):
            command_log.record_sent("{}", "R1", "maxscript", 1, "namedpipe")
            command_log.record_done("R1", "maxscript", 0.0, ok=True)


class ClientLogTests(_LogCase, _ClientCase):
    def test_sent_and_done_lines_for_a_call(self):
        start = self.clock.now
        self.use_kernel(FakeKernel(self.clock, [(start + 0.5, REPLY)]))
        client = self.pinned_client()
        with current_tool("execute_maxscript"):
            client.send_command('delete $Box001', timeout=5.0)
        sent, done = self.lines()
        self.assertEqual(sent["event"], "sent")
        self.assertEqual(sent["max_pid"], PID)
        self.assertEqual(sent["tool"], "execute_maxscript")
        self.assertEqual(sent["command"], "delete $Box001")
        self.assertEqual(done["event"], "done")
        self.assertEqual(done["request_id"], sent["request_id"])
        self.assertTrue(done["ok"])

    def test_abandoned_call_logs_its_error_code(self):
        self.use_kernel(FakeKernel(self.clock))
        self.states = ["busy"]
        client = self.pinned_client()
        with self.assertRaises(MaxBusy):
            client.send_command("render()", timeout=1.0)
        sent, done = self.lines()
        self.assertEqual(done["request_id"], sent["request_id"])
        self.assertEqual(done["code"], "MAX_BUSY")
        self.assertFalse(done["ok"])

    def test_refused_call_writes_nothing_and_probes_are_not_logged(self):
        self.use_kernel(FakeKernel(self.clock))
        self.states = ["busy"]
        client = self.pinned_client()
        with self.assertRaises(MaxBusy):
            client.send_command("render()", timeout=1.0)
        count = len(self.lines())
        with self.assertRaises(Exception):
            client.send_command("1+1", timeout=1.0)  # refused: our abandoned render() still runs (#48)
        self.assertEqual(len(self.lines()), count)
        with mock.patch.object(command_log, "record_sent") as record:
            try:
                client.send_command("", cmd_type="ping", timeout=0.5, probe=True)
            except Exception:
                pass
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
