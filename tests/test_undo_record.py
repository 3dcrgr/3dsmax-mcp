"""Issue #47: execute_maxscript runs the script inside one labelled undo record."""

import importlib
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.max_client import MaxClient  # noqa: E402
from maxmcp.tool_response import make_structured_tool  # noqa: E402

LABEL = "MCP execute_maxscript"
PREFIX = 'undo "MCP execute_maxscript" on ('


def _load_execute(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.execute", None)
        return importlib.import_module("maxmcp.tools.execute")


class _Case(unittest.TestCase):
    def run_script(self, code, result="ok", **kwargs):
        client = mock.Mock()
        response = {"result": result}

        def send(script, **kw):
            if script == "getSaveRequired()":
                return {"result": "false"}
            return response

        client.send_command.side_effect = send
        execute = _load_execute(client)
        out = execute.execute_maxscript(code=code, **kwargs)
        call = client.send_command.call_args_list[-1]
        return call.args[0], call.kwargs, response, out


class UndoWrapTests(_Case):
    def test_default_wraps_in_one_labelled_record(self):
        sent, kwargs, response, out = self.run_script('b = box name:"A"\ndelete b\n"done"', result="done")
        self.assertTrue(sent.startswith(PREFIX + 'b = box name:"A"\n'))
        self.assertTrue(sent.endswith('"done"\n)'))
        self.assertEqual(out, "done")
        self.assertEqual(response["meta"]["undoRecord"], LABEL)
        self.assertNotIn("undoSkipped", response["meta"])
        self.assertNotIn("mcpWarnings", response["meta"])

    def test_wrapping_keeps_the_scripts_line_numbers(self):
        script = "a = 1\nb = 2\nc = = 3\nd = 4"
        sent, _, _, _ = self.run_script(script)
        # Line N of the script is line N of what is sent: the prefix shares line 1.
        self.assertEqual(sent.split("\n")[:4], [PREFIX + "a = 1", "b = 2", "c = = 3", "d = 4"])
        self.assertEqual(sent.split("\n")[4], ")")

    def test_trailing_comment_cannot_swallow_the_close(self):
        sent, _, _, _ = self.run_script("42 -- trailing comment")
        self.assertEqual(sent, PREFIX + "42 -- trailing comment\n)")

    def test_undo_false_sends_the_script_unchanged(self):
        sent, _, response, _ = self.run_script("polyop.setVert $Big #{1..100000} pts", undo=False)
        self.assertEqual(sent, "polyop.setVert $Big #{1..100000} pts")
        self.assertIsNone(response["meta"]["undoRecord"])
        self.assertEqual(response["meta"]["undoSkipped"], "undo_false")

    def test_scripts_that_drive_undo_themselves_run_as_sent(self):
        for script in ("theHold.Begin(); box(); theHold.Accept \"x\"", "clearUndoBuffer()", "max undo",
                       "max  Redo", "THEHOLD.Holding()"):
            sent, _, response, _ = self.run_script(script)
            self.assertEqual(sent, script, script)
            self.assertEqual(response["meta"]["undoSkipped"], "own_undo_handling", script)

    def test_scene_file_scripts_run_as_sent(self):
        sent, _, response, _ = self.run_script("resetMaxFile #noPrompt", allow_discard=True)
        self.assertEqual(sent, "resetMaxFile #noPrompt")
        self.assertEqual(response["meta"]["undoSkipped"], "file_command")

    def test_undo_off_delete_warns(self):
        _, _, response, _ = self.run_script('with undo off (delete $Box001)')
        self.assertIn("Ctrl+Z", response["meta"]["mcpWarnings"][0])
        _, _, response, _ = self.run_script('delete $Box001', undo=False)
        self.assertIn("delete_objects", response["meta"]["mcpWarnings"][0])

    def test_wrapped_delete_does_not_warn(self):
        _, _, response, _ = self.run_script('delete $Box001')
        self.assertNotIn("mcpWarnings", response["meta"])
        _, _, response, _ = self.run_script('with undo off ($Box001.pos = [0,0,0])')
        self.assertNotIn("mcpWarnings", response["meta"])

    def test_error_sentinel_still_reported(self):
        _, _, _, out = self.run_script("undefined.foo", result="__MCP_MS_ERR__:-- Unknown property")
        payload = json.loads(out)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"], "-- Unknown property")

    def test_execute_python_is_not_wrapped_twice(self):
        client = mock.Mock()
        client.send_command.return_value = {"result": "{}"}
        execute = _load_execute(client)
        execute.execute_python(code="result = 1")
        sent = client.send_command.call_args.args[0]
        self.assertFalse(sent.startswith(PREFIX))
        self.assertIn('undo "MCP execute_python" on', sent)
        self.assertNotIn("undoRecord", client.send_command.return_value.get("meta", {}))


class EnvelopeTests(unittest.TestCase):
    def envelope(self, code, **kwargs):
        real = MaxClient(transport="pipe")
        client = mock.Mock()

        def send(script, **kw):
            response = {"result": "ok", "requestId": "r1", "meta": {"transport": "namedpipe"}}
            real._local.last_response = response
            return response

        client.send_command.side_effect = send
        execute = _load_execute(client)
        tool = make_structured_tool(execute.execute_maxscript, before_call=real.clear_last_response,
                                    transport_provider=real.get_last_transport)
        with mock.patch.dict(os.environ, {"MCP_TRIPBACK_MODE": os.environ.get("MCP_TRIPBACK_MODE_TEST", "full")}):
            return tool(code=code, **kwargs)

    def test_transport_names_the_undo_record(self):
        envelope = self.envelope("box()")
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["transport"]["undo_record"], LABEL)
        self.assertNotIn("undo_skipped", envelope["transport"])

    def test_meta_warnings_reach_the_envelope(self):
        envelope = self.envelope("delete $Box001", undo=False)
        self.assertEqual(envelope["transport"]["undo_skipped"], "undo_false")
        self.assertTrue(any("Ctrl+Z" in w for w in envelope["warnings"]))

    def test_meta_warnings_reach_a_minimal_envelope(self):
        with mock.patch.dict(os.environ, {"MCP_TRIPBACK_MODE_TEST": "minimal"}):
            envelope = self.envelope("delete $Box001", undo=False)
        self.assertNotIn("transport", envelope)
        self.assertTrue(any("Ctrl+Z" in w for w in envelope["warnings"]))


if __name__ == "__main__":
    unittest.main()
