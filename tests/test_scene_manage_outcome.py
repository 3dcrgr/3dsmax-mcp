"""manage_scene reset/fetch report whether they really happened (MCP_ISSUES #46)."""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.scene_manage", None)
        return importlib.import_module("maxmcp.tools.scene_manage")


def _client(result, native=False):
    client = mock.Mock()
    client.native_available = native
    client.send_command.return_value = {"result": result}
    return client


def reply(resets, opens, before=("C:/s/a.max", 12, "true"), after=("C:/s/a.max", 12, "true"), err=""):
    return '"%d|%d|%s|%d|%s|%s|%d|%s|%s"' % ((resets, opens) + before + after + (err,))


class FallbackOutcomeTests(unittest.TestCase):
    def run_action(self, action, result):
        client = _client(result)
        module = _load(client)
        out = module.manage_scene(action)
        return client, json.loads(out)

    def test_reset_declined_reports_not_reset(self):
        client, out = self.run_action("reset", reply(0, 0))
        self.assertIs(out["reset"], False)
        self.assertEqual(out["status"], "not_reset")
        self.assertIn("was not reset", out["message"])
        self.assertEqual(out["warnings"], [out["message"]])
        self.assertEqual(out["before"], {"file": "C:/s/a.max", "objects": 12, "save_required": True})
        self.assertNotIn("Scene reset to empty", json.dumps(out))
        script = client.send_command.call_args.args[0]
        self.assertIn("resetMaxFile()", script)
        self.assertIn("callbacks.addScript #systemPostReset", script)
        self.assertIn("callbacks.removeScripts id:#mcpManageScene46", script)
        self.assertEqual(client.send_command.call_args.kwargs["request_fields"], {"quiet": False})

    def test_reset_that_ran(self):
        # A reset also fires #filePostOpen for its start-up scene: the reset count decides.
        _, out = self.run_action("reset", reply(1, 1, after=("", 0, "false")))
        self.assertIs(out["reset"], True)
        self.assertEqual(out["message"], "Scene reset to empty")
        self.assertNotIn("warnings", out)
        self.assertEqual(out["after"], {"file": "", "objects": 0, "save_required": False})

    def test_fetch_declined_and_done(self):
        client, out = self.run_action("fetch", reply(0, 0))
        self.assertIs(out["fetched"], False)
        self.assertEqual(out["status"], "not_fetched")
        self.assertIn("fetchMaxFile quiet:false", client.send_command.call_args.args[0])
        _, out = self.run_action("fetch", reply(0, 1))
        self.assertIs(out["fetched"], True)
        self.assertEqual(out["message"], "Fetched (restored) held state")

    def test_maxscript_error_is_reported(self):
        _, out = self.run_action("reset", reply(0, 0, err="-- Runtime error: x|y"))
        self.assertEqual(out["maxscript_error"], "-- Runtime error: x|y")
        self.assertTrue(any("MAXScript reported" in w for w in out["warnings"]))

    def test_unreadable_reply_is_returned_as_is(self):
        client = _client("__MCP_MS_ERR__: boom")
        self.assertEqual(_load(client).manage_scene("reset"), "__MCP_MS_ERR__: boom")

    def test_untitled_scene_reports_empty_file(self):
        client = _client(reply(0, 0))
        _load(client).manage_scene("reset")
        self.assertIn('if maxFileName == "" then ""', client.send_command.call_args.args[0])

    def test_native_route_returns_handler_json(self):
        native = json.dumps({"action": "reset", "reset": False, "status": "not_reset"})
        client = _client(native, native=True)
        self.assertEqual(_load(client).manage_scene("RESET"), native)
        self.assertEqual(client.send_command.call_args.kwargs["cmd_type"], "native:manage_scene")


class NativeHandlerSourceTests(unittest.TestCase):
    """The bridge can't run here: check the handler decides from Max's notifications."""

    def setUp(self):
        self.src = (ROOT / "native" / "src" / "handlers" / "scene_manage_handlers.cpp").read_text(encoding="utf-8")

    def test_reset_and_fetch_watch_notifications(self):
        self.assertIn("NOTIFY_SYSTEM_POST_RESET", self.src)
        self.assertIn("NOTIFY_FILE_POST_OPEN", self.src)
        self.assertIn("reset = watch.resets > 0;", self.src)
        self.assertIn("fetched = watch.opens > 0 && watch.resets == 0;", self.src)
        self.assertIn('"not_reset"', self.src)
        self.assertIn('"not_fetched"', self.src)
        self.assertIn("UnRegisterNotification(OnReset", self.src)

    def test_success_message_only_when_done(self):
        self.assertNotIn('return "Scene reset to empty";', self.src)
        self.assertIn("if (reset) NativeHandlers::ResetSceneDeltaSessions();", self.src)


if __name__ == "__main__":
    unittest.main()
