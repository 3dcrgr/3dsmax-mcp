"""agent_viewport reclaim of a restored AGENT VIEWPORT (MCP_ISSUES #11)."""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.max_client import MaxBridgeError  # noqa: E402


def _load_viewport(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.viewport", None)
        return importlib.import_module("maxmcp.tools.viewport")


def _client(*results):
    client = mock.Mock()
    client.native_available = True
    client.send_command.side_effect = [{"result": json.dumps(r)} for r in results]
    return client


RECLAIMED = {"owner": "agent", "owned": True, "available": True, "capture_ready": True,
             "window_state": "visible", "label": "AGENT VIEWPORT", "floating_id": 3,
             "view_id": 7, "view_token": "abc", "reclaimed": True}
UNOWNED_STALE = {"owned": False, "available": False, "capture_ready": False,
                 "window_state": "unavailable", "reclaimable": True, "reclaim_floating_id": 3,
                 "reclaim_window_title": "Floating Viewport - 3", "next_action": "open"}


class ReclaimActionTests(unittest.TestCase):
    def test_reclaim_is_forwarded_and_result_returned(self):
        client = _client(RECLAIMED)
        viewport = _load_viewport(client)
        result = viewport.agent_viewport(action="reclaim")
        sent = json.loads(client.send_command.call_args.args[0])
        self.assertEqual(sent["action"], "reclaim")
        self.assertNotIn("start_minimized", sent)
        self.assertEqual(client.send_command.call_args.kwargs["cmd_type"], "native:agent_viewport")
        self.assertIs(result["reclaimed"], True)
        self.assertEqual(result["floating_id"], 3)

    def test_open_result_carries_reclaimed_flag(self):
        client = _client(dict(RECLAIMED, reclaimed=False))
        viewport = _load_viewport(client)
        result = viewport.agent_viewport(action="open")
        self.assertEqual(json.loads(client.send_command.call_args.args[0])["action"], "open")
        self.assertIs(result["reclaimed"], False)

    def test_start_minimized_stays_open_only(self):
        viewport = _load_viewport(_client())
        with self.assertRaises(ValueError):
            viewport.agent_viewport(action="reclaim", start_minimized=True)

    def test_unowned_reclaimable_status_keeps_active_view(self):
        # Status reports a stale panel but never takes it: source=auto stays on the user's view.
        viewport = _load_viewport(_client(UNOWNED_STALE))
        self.assertIsNone(viewport._agent_context())

    def test_unverified_panel_error_names_the_window(self):
        message = ("All floating viewports are in use; no user viewport was taken over. Floating Viewport 3 "
                   "(\"Floating Viewport - 3\") is open without the AGENT VIEWPORT tag. If one of these is a "
                   "stale AGENT VIEWPORT restored from a saved layout or Hold/Fetch, close that floating "
                   "viewport (its window close button, or post WM_CLOSE to that window) and run "
                   "agent_viewport open again.")
        response = {"code": "BAD_PARAM", "message": message, "hint": {
            "floating_viewports": [{"floating_id": 3, "window_title": "Floating Viewport - 3",
                                    "panel_name": "", "visible": True, "minimized": False,
                                    "agent_tag": False, "restored_with_scene": False,
                                    "agent_layout": True}],
            "workaround": "Close the stale floating viewport window (WM_CLOSE), then agent_viewport open"}}
        client = mock.Mock()
        client.send_command.side_effect = MaxBridgeError(message, response)
        viewport = _load_viewport(client)
        with self.assertRaises(MaxBridgeError) as caught:
            viewport.agent_viewport(action="open")
        self.assertIn("WM_CLOSE", str(caught.exception))
        self.assertIn("Floating Viewport - 3", str(caught.exception))
        hint = caught.exception.bridge_response["hint"]
        self.assertEqual(hint["floating_viewports"][0]["floating_id"], 3)
        self.assertFalse(hint["floating_viewports"][0]["agent_tag"])

    def test_reshown_tagged_panel_is_reported_not_taken(self):
        # A tagged slot the user closed and showed again is the user's viewport.
        message = ("No AGENT VIEWPORT to reclaim: no floating viewport is the tagged AGENT VIEWPORT restored "
                   "with the scene. Floating Viewport 3 (\"Floating Viewport - 3\") is open with the AGENT "
                   "VIEWPORT tag, but it is not the window restored with the scene (it was closed or shown "
                   "again since), so it is treated as yours.")
        response = {"code": "BAD_PARAM", "message": message, "hint": {"floating_viewports": [
            {"floating_id": 3, "window_title": "Floating Viewport - 3", "panel_name": "", "visible": True,
             "minimized": False, "agent_tag": True, "restored_with_scene": False, "agent_layout": True}]}}
        client = mock.Mock()
        client.send_command.side_effect = MaxBridgeError(message, response)
        viewport = _load_viewport(client)
        with self.assertRaises(MaxBridgeError) as caught:
            viewport.agent_viewport(action="reclaim")
        self.assertEqual(json.loads(client.send_command.call_args.args[0])["action"], "reclaim")
        slot = caught.exception.bridge_response["hint"]["floating_viewports"][0]
        self.assertTrue(slot["agent_tag"])
        self.assertFalse(slot["restored_with_scene"])

    def test_replaced_owned_window_points_capture_at_open(self):
        # Hold/Fetch recreated the owned window: status says open (not release), which keeps the tag.
        stale_owned = dict(UNOWNED_STALE, owned=True, owner="agent", label="AGENT VIEWPORT",
                           floating_id=3, view_id=-1)
        viewport = _load_viewport(_client(stale_owned))
        with self.assertRaises(RuntimeError) as caught:
            viewport._agent_context()
        self.assertIn("use agent_viewport open before capture", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
