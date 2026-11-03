"""A hidden AGENT VIEWPORT names the agent_viewport action that brings it back (MCP_ISSUES #53)."""

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

from maxmcp.max_client import MaxBridgeError  # noqa: E402

OLD = json.dumps({"code": "BAD_PARAM", "message": "AGENT VIEWPORT must remain visible for fresh Nitrous captures",
                  "retryable": False, "type": "NativeError"})


def _load(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.viewport", None)
        return importlib.import_module("maxmcp.tools.viewport")


def _client(error, status=None):
    client = mock.Mock()
    client.native_available = True

    def send(command, cmd_type="maxscript", **kwargs):
        if json.loads(command).get("action") == "status":
            if isinstance(status, BaseException):
                raise status
            return {"result": json.dumps(status or {})}
        raise error
    client.send_command.side_effect = send
    return client


def message(exc):
    return json.loads(exc.bridge_message)["message"]


class HiddenAgentViewportTests(unittest.TestCase):
    def test_capture_names_release_then_open_from_status(self):
        status = {"owner": "agent", "window_state": "hidden", "next_action": "release then open"}
        viewport = _load(_client(MaxBridgeError(OLD, {"error": OLD}), status))
        for kwargs in ({"source": "agent"}, {}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MaxBridgeError) as ctx:
                viewport.capture_viewport(**kwargs)
            text = message(ctx.exception)
            self.assertIn("hidden or was closed (for example after a Max restart)", text)
            self.assertIn('Run agent_viewport(action="release"), then agent_viewport(action="open"), then retry.',
                          text)
            self.assertEqual(json.loads(ctx.exception.bridge_message)["code"], "BAD_PARAM")
            self.assertIsInstance(ctx.exception.__cause__, MaxBridgeError)

    def test_set_viewport_names_open(self):
        status = {"owner": "agent", "window_state": "hidden", "next_action": "open"}
        viewport = _load(_client(MaxBridgeError(OLD, {}), status))
        with self.assertRaises(MaxBridgeError) as ctx:
            viewport.set_viewport(source="agent", view="top")
        self.assertIn('Run agent_viewport(action="open"), then retry.', message(ctx.exception))

    def test_status_failure_still_gives_the_safe_steps(self):
        viewport = _load(_client(MaxBridgeError(OLD, {}), RuntimeError("pipe")))
        with self.assertRaises(MaxBridgeError) as ctx:
            viewport.capture_viewport(source="agent")
        self.assertIn('agent_viewport(action="release"), then agent_viewport(action="open")', message(ctx.exception))

    def test_new_bridge_message_and_other_errors_pass_unchanged(self):
        new = json.dumps({"code": "BAD_PARAM", "message": "AGENT VIEWPORT must remain visible ...; Run "
                          "agent_viewport(action=\"open\") to show it again, then retry."})
        for error in (MaxBridgeError(new, {}), MaxBridgeError('{"code":"BAD_PARAM","message":"other"}', {}),
                      RuntimeError("boom")):
            with self.subTest(error=str(error)):
                client = _client(error)
                viewport = _load(client)
                with self.assertRaises(type(error)) as ctx:
                    viewport.capture_viewport(source="agent")
                self.assertIs(ctx.exception, error)
                self.assertEqual(client.send_command.call_count, 1)  # no status read


class NativeMessageSourceTests(unittest.TestCase):
    def test_bridge_message_names_the_action(self):
        src = (ROOT / "native" / "src" / "agent_viewport.cpp").read_text(encoding="utf-8")
        self.assertIn("it is hidden or was closed (for example after a Max restart). ", src)
        self.assertIn('Run agent_viewport(action=\\"open\\") to show it again, then retry.', src)
        self.assertIn('Run agent_viewport(action=\\"release\\"), then agent_viewport(action=\\"open\\"), then retry.',
                      src)


if __name__ == "__main__":
    unittest.main()
