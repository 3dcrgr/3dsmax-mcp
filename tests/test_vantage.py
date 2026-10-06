"""Tests for the Chaos Vantage tools: vantage_status and vantage_live_link."""

import importlib
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import vantage_client as vc  # noqa: E402
from maxmcp.helpers.vantage_client import VantageError  # noqa: E402

vt = importlib.import_module("maxmcp.tools.vantage")

READY = {"running": True, "pids": [28660], "command_port": 20702, "link_port": 20703, "ports_from": "vantage.ini",
         "listening": True, "phase": "ready", "ready": True, "state": "RenderingInteractive",
         "live_link": "Online", "offline_task": "None", "linked": False, "version": "3.4.0"}


def status(**changes):
    out = dict(READY)
    out.update(changes)
    out["linked"] = out.get("live_link") == "Connected"
    return out


class ImportTargetTests(unittest.TestCase):
    def test_imports_checkout(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(Path(vc.__file__).resolve(), repo / "maxmcp" / "helpers" / "vantage_client.py")
        self.assertEqual(Path(vt.__file__).resolve(), repo / "maxmcp" / "tools" / "vantage.py")


# ── HTTP client against a real local server ──────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    behaviour: dict = {}
    requests: list = []

    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.requests.append(("GET", self.path, None, None))
        code = self.behaviour.get("ping", 200)
        self._send(code, "Hello" if code == 200 else "Initializing", "text/plain")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        self.requests.append(("POST", self.path, self.headers.get("Content-Type"), body))
        code, reply = self.behaviour.get("command", (200, json.dumps({
            "state": "RenderingInteractive", "liveLinkState": "Connected", "offlineTaskState": "None",
            "offlineStartFrame": 0, "offlineCurrentFrame": 0, "lastProcessedTaskID": 3,
            "appVersion": "3.4.0", "somethingNew": 1})))
        self._send(code, reply)


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6  # Vantage listens on ::1 only


class HttpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _V6Server(("::1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.behaviour = {}
        _Handler.requests = []

    def test_ping_ready_and_initializing(self):
        self.assertEqual(vc.ping(self.port), {"listening": True, "status": 200})
        _Handler.behaviour = {"ping": 503}
        self.assertEqual(vc.ping(self.port), {"listening": True, "status": 503})
        self.assertEqual(_Handler.requests[0][:2], ("GET", "/ping"))

    def test_get_status_sends_chaos_request(self):
        data = vc.get_status(self.port)
        self.assertEqual(data["liveLinkState"], "Connected")
        method, path, ctype, body = _Handler.requests[0]
        self.assertEqual((method, path, ctype), ("POST", "/command", "application/json"))
        self.assertEqual(json.loads(body), {"command": "getStatus"})

    def test_get_status_errors(self):
        _Handler.behaviour = {"command": (500, "boom")}
        with self.assertRaises(VantageError) as ctx:
            vc.get_status(self.port)
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), ("VANTAGE_UNAVAILABLE", True))
        _Handler.behaviour = {"command": (200, "not json")}
        with self.assertRaises(VantageError) as ctx:
            vc.get_status(self.port)
        self.assertEqual(ctx.exception.code, "VANTAGE_BAD_REPLY")
        _Handler.behaviour = {"command": (200, "[1, 2]")}
        with self.assertRaises(VantageError):
            vc.get_status(self.port)

    def test_read_status_maps_fields(self):
        ports = {"command": self.port, "link": 20703, "source": "vantage.ini"}
        with patch.object(vc, "read_ports", return_value=ports), \
                patch.object(vc, "list_processes", return_value=[{"pid": 7, "image": "vantage.exe"}]):
            out = vc.read_status()
        self.assertEqual(out["phase"], "ready")
        self.assertTrue(out["ready"] and out["linked"])
        self.assertEqual((out["state"], out["live_link"], out["offline_task"], out["last_task_id"], out["version"]),
                         ("RenderingInteractive", "Connected", "None", 3, "3.4.0"))
        self.assertEqual(out["other_fields"], {"somethingNew": 1})
        self.assertEqual(out["pids"], [7])

    def test_read_status_initializing_and_errors(self):
        ports = {"command": self.port, "link": 20703, "source": "default"}
        procs = [{"pid": 7, "image": "vantage.exe"}, {"pid": 8, "image": "vantage.exe"}]
        _Handler.behaviour = {"ping": 503}
        with patch.object(vc, "read_ports", return_value=ports), patch.object(vc, "list_processes", return_value=procs):
            out = vc.read_status()
        self.assertEqual((out["phase"], out["ready"]), ("initializing", False))
        self.assertIn("2 vantage.exe processes", out["warnings"][0])
        _Handler.behaviour = {"command": (500, "boom")}
        with patch.object(vc, "read_ports", return_value=ports), patch.object(vc, "list_processes", return_value=procs):
            out = vc.read_status()
        self.assertEqual((out["phase"], out["status_code"]), ("ready", "VANTAGE_UNAVAILABLE"))
        self.assertNotIn("live_link", out)


class ReadStatusOfflineTests(unittest.TestCase):
    def test_no_process_sends_nothing(self):
        with patch.object(vc, "list_processes", return_value=[]), patch.object(vc, "_request") as request:
            out = vc.read_status()
        request.assert_not_called()
        self.assertEqual((out["running"], out["phase"], out["ready"]), (False, "not_running", False))

    def test_process_without_listener_is_starting(self):
        with patch.object(vc, "list_processes", return_value=[{"pid": 7, "image": "vantage.exe"}]), \
                patch.object(vc, "_request", side_effect=ConnectionRefusedError()):
            out = vc.read_status()
        self.assertEqual((out["phase"], out["listening"]), ("starting", False))


class PortsTests(unittest.TestCase):
    def _ini(self, text):
        folder = tempfile.mkdtemp()
        path = Path(folder) / "Chaos" / "Vantage"
        path.mkdir(parents=True)
        (path / "vantage.ini").write_text(text, encoding="utf-8")
        return folder

    def test_reads_vantage_preferences(self):
        folder = self._ini("[General]\nweird line without value\nrecent\\1\\path=C:/a:b\n\n[Preferences]\n"
                           "commandPort=21702\nliveLink2Port=21703\nstartLiveLinkServerWithApp=false\n")
        with patch.dict(os.environ, {"APPDATA": folder}):
            ports = vc.read_ports()
        self.assertEqual((ports["command"], ports["link"], ports["source"], ports["link_server_with_app"]),
                         (21702, 21703, "vantage.ini", False))

    def test_defaults_when_missing_or_bad(self):
        with patch.dict(os.environ, {"APPDATA": tempfile.mkdtemp()}):
            self.assertEqual(vc.read_ports(), {"command": 20702, "link": 20703, "source": "default"})
        folder = self._ini("[Preferences]\ncommandPort=abc\nliveLink2Port=99999\n")
        with patch.dict(os.environ, {"APPDATA": folder}):
            ports = vc.read_ports()
        self.assertEqual((ports["command"], ports["link"], ports["source"]), (20702, 20703, "default"))


# ── vantage_live_link ────────────────────────────────────────────────────────

AGENT_FREE = json.dumps({"owned": False, "available": False})


def max_client(*results, agent=AGENT_FREE, native=True):
    """A MagicMock bridge: agent_viewport status, then the given MAXScript results in order."""
    client = MagicMock()
    client.native_available = native
    queue = list(results)

    def send(command, cmd_type="maxscript", timeout=None, request_fields=None, *, probe=False):
        if cmd_type == "native:agent_viewport":
            return {"result": agent}
        if probe:
            return {"result": "1"}
        return {"result": queue.pop(0)}

    client.send_command.side_effect = send
    client._dialog_control.return_value = {"dialogs": []}
    return client


def scripts(client):
    return [c.args[0] for c in client.send_command.call_args_list
            if c.kwargs.get("cmd_type", "maxscript") == "maxscript" and not c.kwargs.get("probe")]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now


class LiveLinkTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        for name, value in (("_sleep", self.clock.sleep), ("_clock", self.clock.clock)):
            patcher = patch.object(vt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, statuses, client, **kwargs):
        statuses = list(statuses)
        last = statuses[-1]
        with patch.object(vt.vc, "read_status", side_effect=lambda: statuses.pop(0) if statuses else last), \
                patch.object(vt, "client", client):
            return vt.vantage_live_link(**kwargs)

    def assertRefused(self, code, statuses, client, **kwargs):
        with self.assertRaises(VantageError) as ctx:
            self.run_tool(statuses, client, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception


class StartTests(LiveLinkTestCase):
    def test_start_links_and_polls_vantage_only(self):
        client = max_client('"started|"')
        out = self.run_tool([status(), status(), status(live_link="Connected")], client)
        self.assertEqual((out["result"], out["changed"], out["link_server"]), ("linked", True, "localhost:20703"))
        self.assertEqual(out["vray_ipr"], 1)
        sent = scripts(client)
        self.assertEqual(len(sent), 1)  # one atomic check-and-start, no bridge calls while polling
        start_call = [c for c in client.send_command.call_args_list if not c.kwargs.get("probe")
                      and c.kwargs.get("cmd_type") == "maxscript"][0]
        self.assertEqual(start_call.kwargs["request_fields"], {"quiet": True})
        self.assertIn('vrayGPUSetLiveLinkServers #("localhost:20703")', sent[0])
        self.assertIn("vrayGPUStartIPR liveLinkMode:true", sent[0])

    def test_start_uses_vantage_link_port(self):
        client = max_client('"started|"')
        out = self.run_tool([status(link_port=21703), status(live_link="Connected")], client)
        self.assertIn('#("localhost:21703")', scripts(client)[0])
        self.assertEqual(out["link_server"], "localhost:21703")

    def test_already_linked_sends_nothing_to_max(self):
        client = max_client()
        out = self.run_tool([status(live_link="Connected")], client)
        self.assertEqual((out["result"], out["changed"]), ("already_linked", False))
        client.send_command.assert_not_called()

    def test_vantage_not_ready(self):
        e = self.assertRefused("VANTAGE_NOT_RUNNING", [{"running": False, "phase": "not_running", "ready": False}],
                               max_client())
        self.assertIn("doesn't launch", json.loads(e.bridge_message)["hint"])
        self.assertRefused("VANTAGE_STARTING", [{"running": True, "phase": "initializing", "ready": False}],
                           max_client())
        self.assertRefused("VANTAGE_UNAVAILABLE", [status(status_error="x", status_code="VANTAGE_UNAVAILABLE")],
                           max_client())

    def test_link_server_offline_and_offline_render(self):
        client = max_client()
        self.assertRefused("VANTAGE_LINK_OFFLINE", [status(live_link="Offline")], client)
        self.assertRefused("VANTAGE_BUSY", [status(state="RenderingOffline")], client)
        self.assertRefused("VANTAGE_BUSY", [status(offline_task="Queued")], client)
        client.send_command.assert_not_called()

    def test_agent_viewport_preview_blocks_start(self):
        agent = json.dumps({"owner": "agent", "owned": True, "render": {
            "mode": "vray_vfb", "capture_target": "vray_vfb", "session_state": "stopped",
            "progressive_requested": True}})
        client = max_client(agent=agent)
        e = self.assertRefused("RENDER_BUSY", [status()], client)
        self.assertTrue(e.retryable)
        self.assertEqual(scripts(client), [])

    def test_agent_viewport_shaded_is_fine(self):
        agent = json.dumps({"owner": "agent", "owned": True, "render": {
            "mode": "shaded", "capture_target": "agent", "session_state": "stopped",
            "progressive_requested": False}})
        out = self.run_tool([status(), status(live_link="Connected")], max_client('"started|"', agent=agent))
        self.assertEqual(out["result"], "linked")

    def test_max_side_refusals(self):
        cases = [("ipr_running|1", "RENDER_BUSY"), ("viewport_ipr|", "RENDER_BUSY"), ("holding|", "USER_BUSY"),
                 ("not_vray_gpu|V_Ray_7", "UNSUPPORTED"), ("dr_version|1", "UNSUPPORTED"),
                 ("no_live_link_api|", "UNSUPPORTED"), ("error|-- Unknown property", "LIVE_LINK_FAILED"),
                 ("__MCP_MS_ERR__: boom", "LIVE_LINK_FAILED")]
        for reply, code in cases:
            with self.subTest(reply=reply):
                e = self.assertRefused(code, [status()], max_client('"%s"' % reply if "|" in reply else reply))
                self.assertIn("hint", json.loads(e.bridge_message))

    def test_timeout_reports_starting_without_stopping(self):
        client = max_client('"started|"')
        out = self.run_tool([status()], client, wait_seconds=3)
        self.assertEqual(out["result"], "starting")
        self.assertEqual(len(scripts(client)), 1)  # never stops on its own
        self.assertTrue(any("didn't report a connected link" in w for w in out["warnings"]))
        self.assertLessEqual(self.clock.now, 3.5)

    def test_link_failed_when_vantage_goes_offline(self):
        out = self.run_tool([status(), status(live_link="Offline")], max_client('"started|"'))
        self.assertEqual(out["result"], "link_failed")

    def test_wait_zero_returns_requested(self):
        out = self.run_tool([status()], max_client('"started|"'), wait_seconds=0)
        self.assertEqual(out["result"], "requested")

    def test_bad_params(self):
        for kwargs in ({"action": "toggle"}, {"wait_seconds": 61}, {"wait_seconds": -1}, {"wait_seconds": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_tool([status()], max_client(), **kwargs)

    def test_open_dialog_is_reported(self):
        client = max_client('"started|"')
        dialogs = [{"title": "V-Ray message", "main_thread": True}]
        with patch.object(vt, "_open_main_dialogs", return_value=dialogs):
            out = self.run_tool([status(), status(live_link="Connected")], client)
        self.assertEqual(out["dialogs"], dialogs)
        self.assertTrue(any("max_dialogs" in w for w in out["warnings"]))


class StopTests(LiveLinkTestCase):
    def test_not_connected_sends_nothing(self):
        client = max_client()
        out = self.run_tool([status(live_link="Online")], client, action="stop")
        self.assertEqual((out["result"], out["changed"]), ("not_linked", False))
        client.send_command.assert_not_called()

    def test_stops_own_link_and_waits_for_vantage(self):
        client = max_client('"stopped|1"')
        out = self.run_tool([status(live_link="Connected"), status(live_link="Connected"),
                             status(live_link="Online")], client, action="stop")
        self.assertEqual((out["result"], out["changed"]), ("stopped", True))
        self.assertEqual(out["vantage"]["live_link"], "Online")
        script = scripts(client)[0]
        self.assertIn("vrayGPUStopIPR()", script)
        self.assertIn("not ours and not false", script)

    def test_not_owned_needs_force(self):
        e = self.assertRefused("LIVE_LINK_NOT_OWNED", [status(live_link="Connected")], max_client('"not_owned|1"'),
                               action="stop")
        self.assertIn("force=true", json.loads(e.bridge_message)["hint"])
        client = max_client('"stopped|1"')
        self.run_tool([status(live_link="Connected"), status(live_link="Online")], client, action="stop", force=True)
        self.assertIn("not ours and not true", scripts(client)[0])

    def test_no_ipr_in_this_max(self):
        out = self.run_tool([status(live_link="Connected")], max_client('"not_running|"'), action="stop")
        self.assertEqual(out["result"], "not_linked")
        self.assertIn("another 3ds Max", out["warnings"][0])


class ScriptSafetyTests(unittest.TestCase):
    def test_start_script_is_minimal(self):
        script = vt._start_script(20703)
        for needle in ("1770671000", "1323107829", "vrayIsRenderingIPR()", "theHold.Holding()",
                       "vrayGetDRVersion() != 2", "global mcp_vantageLiveLink"):
            self.assertIn(needle, script)
        for banned in ("renderers.current =", "renderers.production =", "VRayVantage_", "ShellLaunch", "queryBox",
                       "messageBox", "sleep", "vrayExportVRScene", "openFile", "vrayStopIPR", "dotNet",
                       "rt_system_vrayLog_show", "distributed_rendering"):
            self.assertNotIn(banned, script)

    def test_scripts_keep_quiet_mode(self):
        from maxmcp.tools.execute import mentions_scene_file_command
        for script in (vt._start_script(20703), vt._stop_script(False), vt._stop_script(True)):
            self.assertFalse(mentions_scene_file_command(script))

    def test_stop_script_stops_only_gpu_ipr(self):
        script = vt._stop_script(False)
        self.assertIn("vrayGPUStopIPR()", script)
        self.assertNotIn("vrayStopIPR", script)
        self.assertIn("if ipr == 0 then", script)


class RegistrationTests(unittest.TestCase):
    def test_python_only_tools_stay_out_of_native_registry(self):
        from scripts.gen_tool_registry import extract_tools
        names = {t["name"] for t in extract_tools(Path(vt.__file__))}
        self.assertNotIn("vantage_status", names)
        self.assertNotIn("vantage_live_link", names)

    def test_registered_and_skipped_by_smoke(self):
        from maxmcp import server
        from maxmcp.tool_discovery import TOOLSET_SPECS
        from scripts.gen_tool_smoke import SKIP_DEFAULT
        self.assertIn("vantage", server.SPECIALTY_TOOL_MODULES)
        self.assertIn("vantage_status", server._READ_ONLY_TOOLS)
        self.assertEqual(sum("vantage" in spec.modules for spec in TOOLSET_SPECS), 1)
        self.assertTrue({"vantage_status", "vantage_live_link"} <= SKIP_DEFAULT)

    def test_progressive_profile_indexes_the_tools(self):
        from maxmcp import server
        from maxmcp.tool_discovery import ProgressiveToolCatalog
        catalog = ProgressiveToolCatalog(  # raises if a module isn't in exactly one toolset
            package="maxmcp", tools_dir=Path(vt.__file__).resolve().parent, hidden_mcp=MagicMock(),
            allowed_modules=server.CORE_TOOL_MODULES + server.SPECIALTY_TOOL_MODULES)
        names = set(catalog.module_tools.get("vantage", ()))
        self.assertEqual(names, {"vantage_status", "vantage_live_link"})


if __name__ == "__main__":
    unittest.main()
