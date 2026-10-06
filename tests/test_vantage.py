"""Tests for the Chaos Vantage tools: vantage_status and vantage_live_link."""

import base64
import importlib
import json
import os
import socket
import sys
import tempfile
import threading
import struct
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import vantage_client as vc  # noqa: E402
from maxmcp.helpers import vantage_image as vimg  # noqa: E402
from maxmcp.helpers.vantage_client import VantageError  # noqa: E402
from maxmcp.max_client import MaxNotRespondingAfterDispatch  # noqa: E402

vt = importlib.import_module("maxmcp.tools.vantage")

READY = {"running": True, "pids": [28660], "command_port": 20702, "link_port": 20703, "ports_from": "vantage.ini",
         "listening": True, "phase": "ready", "ready": True, "state": "RenderingInteractive",
         "live_link": "Online", "offline_task": "None", "linked": False, "version": "3.4.0"}
LOG = (True, 111, 10)


def bmp32(w, h, top_down=True, pixel=lambda x, y: (x * 10 % 256, y * 20 % 256, 200)):
    """A Vantage-shaped 32-bit BMP: 56-byte header, B,G,R,X pixels (pixel() gives R,G,B)."""
    hdr = bytearray(56)
    struct.pack_into("<2sI", hdr, 0, b"BM", w * h * 4 + 56)
    struct.pack_into("<I", hdr, 10, 56)
    struct.pack_into("<IiiHH", hdr, 14, 40, w, -h if top_down else h, 1, 32)
    px = bytearray()
    for row in range(h):
        y = row if top_down else h - 1 - row
        for x in range(w):
            r, g, b = pixel(x, y)
            px += bytes([b, g, r, 0x7F])
    return bytes(hdr) + bytes(px)


def image_reply(bmp):
    return json.dumps({"bitmap": base64.b64encode(bmp).decode() + "\x00", "headerSize": 56}, indent=4)


def png_pixels(png):
    """(width, height, rows of RGB tuples) from an 8-bit RGB, unfiltered PNG."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", None, None
    while pos < len(png):
        length, kind = struct.unpack_from(">I4s", png, pos)
        data = png[pos + 8:pos + 8 + length]
        assert zlib.crc32(kind + data) & 0xFFFFFFFF == struct.unpack_from(">I", png, pos + 8 + length)[0]
        if kind == b"IHDR":
            w, h, depth, color = struct.unpack_from(">IIBB", data)
            assert (depth, color) == (8, 2)
        elif kind == b"IDAT":
            idat += data
        pos += 12 + length
    raw = zlib.decompress(idat)
    rows = []
    for y in range(h):
        line = raw[y * (1 + 3 * w):(y + 1) * (1 + 3 * w)]
        assert line[0] == 0
        rows.append([tuple(line[1 + 3 * x:4 + 3 * x]) for x in range(w)])
    return w, h, rows


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
        if json.loads(body).get("command") == "getImage":
            code, reply = self.behaviour.get("image", (200, image_reply(bmp32(3, 2))))
            if reply is None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(code, reply)
        code, reply = self.behaviour.get("command", (200, json.dumps({
            "state": "RenderingInteractive", "liveLinkState": "Connected", "offlineTaskState": "None",
            "offlineStartFrame": 0, "offlineCurrentFrame": 0, "lastProcessedTaskID": 3,
            "appVersion": "3.4.0", "somethingNew": 1})))
        self._send(code, reply)


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6  # Vantage listens on ::1 only


class _ServerTestCase(unittest.TestCase):
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


class HttpClientTests(_ServerTestCase):
    def test_ping_ready_and_initializing(self):
        self.assertEqual(vc.ping(self.port), {"listening": True, "responsive": True, "status": 200})
        _Handler.behaviour = {"ping": 503}
        self.assertEqual(vc.ping(self.port)["status"], 503)
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
        _Handler.behaviour = {"command": (404, "nope")}
        with self.assertRaises(VantageError) as ctx:
            vc.get_status(self.port)
        self.assertFalse(ctx.exception.retryable)
        _Handler.behaviour = {"command": (200, "not json")}
        with self.assertRaises(VantageError) as ctx:
            vc.get_status(self.port)
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), ("VANTAGE_BAD_REPLY", False))
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
        _Handler.behaviour = {"command": (200, "not json")}
        with patch.object(vc, "read_ports", return_value=ports), patch.object(vc, "list_processes", return_value=procs):
            out = vc.read_status()
        self.assertEqual((out["phase"], out["status_code"], out["status_retryable"]),
                         ("ready", "VANTAGE_BAD_REPLY", False))
        self.assertNotIn("live_link", out)


class GetImageHttpTests(_ServerTestCase):
    def test_request_and_reply(self):
        doc = vc.get_image(self.port, 3)
        self.assertEqual(json.loads(_Handler.requests[-1][3]), {"command": "getImage", "timeoutSeconds": 3})
        self.assertEqual(doc["headerSize"], 56)
        self.assertTrue(doc["bitmap"].endswith("\x00"))
        vc.get_image(self.port, 2, 64)
        self.assertEqual(json.loads(_Handler.requests[-1][3]),
                         {"command": "getImage", "timeoutSeconds": 2, "minimumSamples": 64})

    def test_error_statuses(self):
        cases = [((403, None), "VANTAGE_NO_HOST", False), ((408, None), "VANTAGE_TIMEOUT", True),
                 ((503, None), "VANTAGE_UNAVAILABLE", True),
                 ((400, json.dumps({"errorText": "unknown command 'getImage'"})), "VANTAGE_UNAVAILABLE", False),
                 ((200, json.dumps({"state": "x"})), "VANTAGE_BAD_REPLY", False)]
        for behaviour, code, retryable in cases:
            with self.subTest(code=code, status=behaviour[0]):
                _Handler.behaviour = {"image": behaviour}
                with self.assertRaises(VantageError) as ctx:
                    vc.get_image(self.port, 1)
                self.assertEqual((ctx.exception.code, ctx.exception.retryable), (code, retryable))
                if behaviour[0] == 400:
                    self.assertIn("unknown command", str(ctx.exception))


class VantageImageTests(unittest.TestCase):
    def test_decode_strips_nul_and_handles_null(self):
        bmp = bmp32(2, 2)
        self.assertEqual(vimg.decode_bitmap(json.loads(image_reply(bmp))), bmp)
        self.assertIsNone(vimg.decode_bitmap({"bitmap": None, "headerSize": 0}))
        with self.assertRaises(vimg.VantageImageError):
            vimg.decode_bitmap({"bitmap": "not base64!!"})

    def test_png_colours_and_row_order(self):
        for top_down in (True, False):
            with self.subTest(top_down=top_down):
                png, info = vimg.bmp_to_png(bmp32(5, 3, top_down), 56, 0)
                w, h, rows = png_pixels(png)
                self.assertEqual((w, h, info["factor"]), (5, 3, 1))
                self.assertEqual(rows[0][0], (0, 0, 200))
                self.assertEqual(rows[2][4], (40, 40, 200))  # R from x, G from y: top row first

    def test_downscale_keeps_every_nth_pixel(self):
        png, info = vimg.bmp_to_png(bmp32(7, 5), 56, 3)
        w, h, rows = png_pixels(png)
        self.assertEqual((w, h, info["source_width"], info["factor"]), (3, 2, 7, 3))
        self.assertEqual([p[0] for p in rows[1]], [0, 30, 60])
        self.assertEqual([r[0][1] for r in rows], [0, 60])

    def test_rejects_bad_bitmaps(self):
        good = bmp32(4, 4)
        for bad in (b"XX" + good[2:], good[:-5], good[:28] + struct.pack("<H", 16) + good[30:]):
            with self.assertRaises(vimg.VantageImageError):
                vimg.bmp_to_png(bad, 56, 0)


class UnresponsiveServerTests(unittest.TestCase):
    def test_accepts_but_never_answers(self):
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        listener.bind(("::1", 0))
        listener.listen(4)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        probe = vc.ping(port, timeout=0.3)
        self.assertEqual((probe["listening"], probe["responsive"]), (True, False))
        ports = {"command": port, "link": 20703, "source": "default"}
        with patch.object(vc, "read_ports", return_value=ports), \
                patch.object(vc, "list_processes", return_value=[{"pid": 7, "image": "vantage.exe"}]), \
                patch.object(vc, "PING_TIMEOUT", 0.3):
            out = vc.read_status()
        self.assertEqual(out["phase"], "unresponsive")


class ReadStatusOfflineTests(unittest.TestCase):
    def test_no_process_sends_nothing(self):
        with patch.object(vc, "list_processes", return_value=[]), patch.object(vc, "_request") as request:
            out = vc.read_status()
        request.assert_not_called()
        self.assertEqual((out["running"], out["phase"], out["ready"]), (False, "not_running", False))

    def test_process_without_listener_is_starting(self):
        with patch.object(vc, "list_processes", return_value=[{"pid": 7, "image": "vantage.exe"}]), \
                patch.object(vc, "_request", side_effect=vc.NotListening("TimeoutError: timed out")):
            out = vc.read_status()
        self.assertEqual((out["phase"], out["listening"]), ("starting", False))
        self.assertIn("timed out", out["ping_error"])


class PortsTests(unittest.TestCase):
    def _appdata(self, text, encoding="utf-8"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "Chaos" / "Vantage"
        path.mkdir(parents=True)
        (path / "vantage.ini").write_text(text, encoding=encoding)
        return tmp.name

    def read(self, text, encoding="utf-8"):
        with patch.dict(os.environ, {"APPDATA": self._appdata(text, encoding)}):
            return vc.read_ports()

    def test_reads_vantage_preferences(self):
        ports = self.read("[General]\nweird line without value\nrecent\\1\\path=C:/a:b\n\n[Preferences]\n"
                          "commandPort=21702\nliveLink2Port=21703\nstartLiveLinkServerWithApp=false\n")
        self.assertEqual((ports["command"], ports["link"], ports["source"], ports["link_server_with_app"]),
                         (21702, 21703, "vantage.ini", False))

    def test_bom_and_bad_lines_keep_good_sections(self):
        self.assertEqual(self.read("[Preferences]\ncommandPort=21702\n", "utf-8-sig")["command"], 21702)
        self.assertEqual(self.read("[Preferences]\ncommandPort=21702\n[X]\n=v\n")["command"], 21702)

    def test_defaults_when_missing_or_bad(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, {"APPDATA": tmp.name}):
            self.assertEqual(vc.read_ports(), {"command": 20702, "link": 20703, "source": "default"})
        ports = self.read("[Preferences]\ncommandPort=abc\nliveLink2Port=99999\n")
        self.assertEqual((ports["command"], ports["link"], ports["source"]), (20702, 20703, "default"))


# ── vantage_live_link ────────────────────────────────────────────────────────

AGENT_FREE = json.dumps({"owned": False, "available": False})


def max_client(*results, agent=AGENT_FREE, probes=("0", "1"), native=True):
    """A MagicMock bridge: agent_viewport status, probe reads, then the given MAXScript results in order."""
    client = MagicMock()
    client.native_available = native
    queue = list(results)
    probe_queue = list(probes)

    def send(command, cmd_type="maxscript", timeout=None, request_fields=None, *, probe=False):
        if cmd_type == "native:agent_viewport":
            return {"result": agent}
        if probe:
            return {"result": probe_queue.pop(0) if len(probe_queue) > 1 else probe_queue[0]}
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return {"result": item}

    client.send_command.side_effect = send
    client._dialog_control.return_value = {"dialogs": []}
    client.get_last_transport.return_value = {}
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
        self.log = LOG
        for name, value in (("_sleep", self.clock.sleep), ("_clock", self.clock.clock),
                            ("_chaos_log_state", lambda: self.log), ("_OWN", None)):
            patcher = patch.object(vt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, statuses, client, **kwargs):
        statuses = list(statuses)
        last = statuses[-1]
        kwargs.setdefault("action", "start")
        with patch.object(vt.vc, "read_status", side_effect=lambda timeout=None: statuses.pop(0) if statuses else last), \
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
        self.assertIn("toolbar button", out["note"])
        calls = client.send_command.call_args_list
        kinds = [("probe" if c.kwargs.get("probe") else c.kwargs.get("cmd_type")) for c in calls]
        # pre-check probe, agent viewport, one atomic start, post probe; nothing while polling
        self.assertEqual(kinds, ["probe", "native:agent_viewport", "maxscript", "probe"])
        self.assertEqual(calls[2].kwargs["request_fields"], {"quiet": True})
        sent = scripts(client)[0]
        self.assertIn('vrayGPUSetLiveLinkServers #("localhost:20703")', sent)
        self.assertIn("vrayGPUStartIPR liveLinkMode:true", sent)
        self.assertTrue(vt._OWN and vt._OWN["connected_seen"])
        self.assertIn('mcp_vantageLiveLink = "%s"' % vt._OWN["token"], sent)

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
        self.assertRefused("VANTAGE_STARTING", [status(state="Startup")], max_client())
        self.assertRefused("VANTAGE_UNRESPONSIVE", [{"running": True, "phase": "unresponsive", "ready": False}],
                           max_client())
        e = self.assertRefused("VANTAGE_BAD_REPLY", [status(status_error="x", status_code="VANTAGE_BAD_REPLY",
                                                            status_retryable=False)], max_client())
        self.assertFalse(e.retryable)
        e = self.assertRefused("VANTAGE_UNAVAILABLE", [{"running": True, "phase": "unexpected", "ready": False,
                                                         "ping_status": 404}], max_client())
        self.assertFalse(e.retryable)
        self.assertRefused("VANTAGE_UNAVAILABLE", [status(state="Shutdown")], max_client())

    def test_busy_vantage(self):
        client = max_client()
        self.assertRefused("VANTAGE_LINK_OFFLINE", [status(live_link="Offline")], client)
        self.assertRefused("VANTAGE_BUSY", [status(state="RenderingOffline")], client)
        self.assertRefused("VANTAGE_BUSY", [status(state="LoadingScene")], client)
        self.assertRefused("VANTAGE_BUSY", [status(offline_task="Queued")], client)
        client.send_command.assert_not_called()

    def test_open_dialog_blocks_before_anything_is_sent(self):
        client = max_client()
        dialogs = [{"title": "Save As", "main_thread": True}]
        with patch.object(vt, "open_main_dialogs", return_value=dialogs):
            e = self.assertRefused("BLOCKED_BY_DIALOG", [status()], client)
        self.assertEqual(e.details["dialogs"], dialogs)
        client.send_command.assert_not_called()

    def test_agent_viewport_preview_blocks_start(self):
        agent = json.dumps({"owner": "agent", "owned": True, "render": {
            "mode": "vray_vfb", "capture_target": "vray_vfb", "session_state": "stopped",
            "progressive_requested": True}})
        client = max_client(agent=agent)
        e = self.assertRefused("RENDER_BUSY", [status()], client)
        self.assertTrue(e.retryable)
        self.assertEqual(scripts(client), [])

    def test_agent_viewport_window_state(self):
        hidden = json.dumps({"owner": "agent", "owned": True, "window_state": "hidden", "capture_ready": False})
        self.assertRefused("RENDER_BUSY", [status()], max_client(agent=hidden))
        minimized = json.dumps({"owner": "agent", "owned": True, "window_state": "minimized"})
        out = self.run_tool([status(), status(live_link="Connected")], max_client('"started|"', agent=minimized))
        self.assertEqual(out["result"], "linked")
        shaded = json.dumps({"owner": "agent", "owned": True, "render": {
            "mode": "shaded", "capture_target": "agent", "session_state": "stopped", "progressive_requested": False}})
        out = self.run_tool([status(), status(live_link="Connected")], max_client('"started|"', agent=shaded))
        self.assertEqual(out["result"], "linked")

    def test_running_ipr_skips_agent_status(self):
        client = max_client('"ipr_running|1"', probes=("1",))
        self.assertRefused("RENDER_BUSY", [status()], client)
        self.assertNotIn("native:agent_viewport", [c.kwargs.get("cmd_type") for c in client.send_command.call_args_list])

    def test_max_side_refusals(self):
        cases = [("ipr_running|1", "RENDER_BUSY"), ("viewport_ipr|", "RENDER_BUSY"), ("holding|", "USER_BUSY"),
                 ("not_vray_gpu|V_Ray_7", "UNSUPPORTED"), ("dr_version|1", "UNSUPPORTED"),
                 ("no_live_link_api|", "UNSUPPORTED"), ("error|-- Unknown property", "LIVE_LINK_FAILED"),
                 ("__MCP_MS_ERR__: boom", "LIVE_LINK_FAILED")]
        for reply, code in cases:
            with self.subTest(reply=reply):
                e = self.assertRefused(code, [status()], max_client('"%s"' % reply if "|" in reply else reply))
                self.assertIn("hint", json.loads(e.bridge_message))
        self.assertIsNone(vt._OWN)

    def test_retry_while_own_link_still_connecting(self):
        out = self.run_tool([status()], max_client('"started|"'), wait_seconds=3)
        self.assertEqual(out["result"], "starting")
        token = vt._OWN["token"]
        client = max_client('"own_running|1"', probes=("1",))
        out = self.run_tool([status(), status(live_link="Connected")], client)
        self.assertEqual((out["result"], out["changed"]), ("linked", False))
        self.assertIn('== "%s" then "own_running|"' % token, scripts(client)[0])
        self.assertEqual(vt._OWN["token"], token)

    def test_timeout_reports_starting_without_stopping(self):
        client = max_client('"started|"')
        out = self.run_tool([status()], client, wait_seconds=3)
        self.assertEqual(out["result"], "starting")
        self.assertEqual(len(scripts(client)), 1)  # never stops on its own
        self.assertTrue(any("vantage_live_link(action='stop')" in w for w in out["warnings"]))
        self.assertLessEqual(self.clock.now, 3.5)

    def test_start_failed_when_no_ipr_runs(self):
        out = self.run_tool([status()], max_client('"started|"', probes=("0", "0")), wait_seconds=3)
        self.assertEqual(out["result"], "start_failed")
        self.assertIsNone(vt._OWN)

    def test_link_failed_when_vantage_goes_offline(self):
        out = self.run_tool([status(), status(live_link="Offline")], max_client('"started|"'))
        self.assertEqual(out["result"], "link_failed")

    def test_lost_reply_keeps_ownership(self):
        lost = MaxNotRespondingAfterDispatch("not responding", {"process": {"state": "blocked"}})
        e = self.assertRefused("REQUEST_OUTCOME_UNKNOWN", [status()], max_client(lost))
        self.assertFalse(e.retryable)
        self.assertIsNotNone(vt._OWN)

    def test_wait_zero_returns_requested(self):
        out = self.run_tool([status()], max_client('"started|"', probes=("0", "0")), wait_seconds=0)
        self.assertEqual(out["result"], "requested")  # an IPR launch may still be pending

    def test_bad_params(self):
        for kwargs in ({"action": "toggle"}, {"wait_seconds": 61}, {"wait_seconds": -1}, {"wait_seconds": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_tool([status()], max_client(), **kwargs)

    def test_open_dialog_after_start_is_reported(self):
        client = max_client('"started|"')
        dialogs = [{"title": "V-Ray message", "main_thread": True}]
        with patch.object(vt, "open_main_dialogs", side_effect=[[], dialogs]):
            out = self.run_tool([status(), status(live_link="Connected")], client)
        self.assertEqual(out["dialogs"], dialogs)
        self.assertTrue(any("max_dialogs" in w for w in out["warnings"]))


class StopTests(LiveLinkTestCase):
    def own(self, connected_seen=True):
        vt._OWN = {"token": "123-abc", "log": LOG, "connected_seen": connected_seen}

    def test_not_connected_and_not_ours_sends_nothing(self):
        client = max_client()
        out = self.run_tool([status(live_link="Online")], client, action="stop", force=True)
        self.assertEqual((out["result"], out["changed"]), ("not_linked", False))
        client.send_command.assert_not_called()

    def test_stops_own_link_and_waits_for_vantage(self):
        self.own()
        client = max_client('"stopped|1|0"')
        out = self.run_tool([status(live_link="Connected"), status(live_link="Connected"),
                             status(live_link="Online")], client, action="stop")
        self.assertEqual((out["result"], out["changed"], out["vray_ipr_after"]), ("stopped", True, 0))
        self.assertEqual(out["vantage"]["live_link"], "Online")
        script = scripts(client)[0]
        self.assertIn("vrayGPUStopIPR()", script)
        self.assertIn('"123-abc" != "" and mcp_vantageLiveLink == "123-abc"', script)
        self.assertIn("not ours and not false", script)
        self.assertIsNone(vt._OWN)

    def test_stops_own_link_that_never_connected(self):
        self.own(connected_seen=False)
        client = max_client('"stopped|1|0"')
        out = self.run_tool([status(live_link="Online")], client, action="stop")
        self.assertEqual(out["result"], "stopped")
        self.assertIn('mcp_vantageLiveLink == "123-abc"', scripts(client)[0])
        self.assertIn("not ours and not false", scripts(client)[0])  # force never applies without a link

    def test_dropped_link_is_not_stopped_without_connection(self):
        self.own(connected_seen=True)
        client = max_client()
        out = self.run_tool([status(live_link="Online")], client, action="stop")
        self.assertEqual(out["result"], "not_linked")
        client.send_command.assert_not_called()

    def test_toolbar_use_drops_ownership(self):
        self.own()
        self.log = (True, 999, 40)  # Chaos's toolbar rewrote its log since our start
        client = max_client('"not_owned|1"')
        self.assertRefused("LIVE_LINK_NOT_OWNED", [status(live_link="Connected")], client, action="stop")
        self.assertIn('"" != ""', scripts(client)[0])
        self.assertIsNone(vt._OWN)

    def test_not_owned_needs_force(self):
        e = self.assertRefused("LIVE_LINK_NOT_OWNED", [status(live_link="Connected")], max_client('"not_owned|1"'),
                               action="stop")
        self.assertIn("force=true", json.loads(e.bridge_message)["hint"])
        client = max_client('"stopped|1|0"')
        self.run_tool([status(live_link="Connected"), status(live_link="Online")], client, action="stop", force=True)
        self.assertIn("not ours and not true", scripts(client)[0])

    def test_no_ipr_in_this_max(self):
        out = self.run_tool([status(live_link="Connected")], max_client('"not_running|"'), action="stop")
        self.assertEqual(out["result"], "not_linked")
        self.assertIn("another 3ds Max", out["warnings"][0])

    def test_stop_refusals(self):
        self.assertRefused("UNSUPPORTED", [status(live_link="Connected")], max_client('"not_vray_gpu|V_Ray_7"'),
                           action="stop", force=True)
        self.assertRefused("LIVE_LINK_FAILED", [status(live_link="Connected")], max_client('"error|boom"'),
                           action="stop", force=True)
        self.assertRefused("UNSUPPORTED", [status(live_link="Connected")], max_client('"no_live_link_api|"'),
                           action="stop", force=True)

    def test_still_connected_after_stop_warns(self):
        self.own()
        out = self.run_tool([status(live_link="Connected")], max_client('"stopped|1|1"'), action="stop",
                            wait_seconds=2)
        self.assertIn("still reports a connected", out["warnings"][0])


class GetImageToolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("IMAGE_DIR", tmp.name), ("client", MagicMock())):
            patcher = patch.object(vt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def grab(self, st, doc=None, **kwargs):
        doc = doc if doc is not None else json.loads(image_reply(bmp32(40, 20)))
        with patch.object(vt.vc, "read_status", return_value=st), \
                patch.object(vt.vc, "get_image", return_value=doc) as get_image:
            return vt.vantage_get_image(**kwargs), get_image

    def test_saves_png_and_never_touches_max(self):
        out, get_image = self.grab(status(live_link="Connected"), max_width=16)
        self.assertEqual((out["type"], out["width"], out["height"], out["source_width"], out["factor"]),
                         ("image_file", 14, 7, 40, 3))
        with open(out["file"], "rb") as f:
            self.assertEqual(png_pixels(f.read())[:2], (14, 7))
        self.assertEqual(out["size_bytes"], os.path.getsize(out["file"]))
        get_image.assert_called_once_with(20702, 5, 0)
        self.assertNotIn("warnings", out)
        vt.client.send_command.assert_not_called()

    def test_unlinked_vantage_warns(self):
        out, _ = self.grab(status(live_link="Online"), max_width=0, minimum_samples=32, timeout_seconds=4)
        self.assertEqual((out["width"], out["minimum_samples"]), (40, 32))
        self.assertIn("isn't linked", out["warnings"][0])

    def test_refusals(self):
        with self.assertRaises(VantageError) as ctx:
            self.grab({"running": False, "phase": "not_running", "ready": False})
        self.assertEqual(ctx.exception.code, "VANTAGE_NOT_RUNNING")
        for st in (status(state="RenderingOffline"), status(state="LoadingScene")):
            with self.assertRaises(VantageError) as ctx:
                self.grab(st)
            self.assertEqual(ctx.exception.code, "VANTAGE_BUSY")
        with self.assertRaises(VantageError) as ctx:
            self.grab(status(), {"bitmap": None, "headerSize": 0})
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), ("VANTAGE_NO_FRAME", True))
        with self.assertRaises(VantageError) as ctx:
            self.grab(status(), {"bitmap": base64.b64encode(b"nope").decode(), "headerSize": 56})
        self.assertEqual(ctx.exception.code, "VANTAGE_BAD_REPLY")

    def test_bad_params(self):
        for kwargs in ({"max_width": -1}, {"minimum_samples": 16385}, {"timeout_seconds": 0},
                       {"timeout_seconds": 61}, {"max_width": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.grab(status(), **kwargs)


class ScriptSafetyTests(unittest.TestCase):
    SCRIPTS = staticmethod(lambda: (vt._start_script(20703, "1-a", ""), vt._start_script(20703, "1-b", "1-a"),
                                    vt._stop_script("", False), vt._stop_script("1-a", True)))

    def test_scripts_are_minimal(self):
        start = vt._start_script(20703, "1-a", "")
        for needle in ("1770671000", "1323107829", "vrayIsRenderingIPR()", "theHold.Holding()",
                       "vrayGetDRVersion() != 2", "global mcp_vantageLiveLink", "if ipr == 0 do mcp_vantageLiveLink"):
            self.assertIn(needle, start)
        for script in self.SCRIPTS():
            for banned in ("renderers.current =", "renderers.production =", "VRayVantage_", "ShellLaunch", "queryBox",
                           "messageBox", "sleep", "vrayExportVRScene", "openFile", "vrayStopIPR", "dotNet",
                           "rt_system_vrayLog_show", "distributed_rendering", "callbacks"):
                self.assertNotIn(banned, script)

    def test_scripts_keep_quiet_mode(self):
        from maxmcp.tools.execute import mentions_scene_file_command
        for script in self.SCRIPTS():
            self.assertFalse(mentions_scene_file_command(script))

    def test_stop_script_checks_renderer_and_rereads(self):
        script = vt._stop_script("1-a", False)
        self.assertIn("1770671000", script)
        self.assertIn('"|" + ((vrayIsRenderingIPR()) as string)', script)
        self.assertIn("if ipr == 0 then", script)


class RegistrationTests(unittest.TestCase):
    def test_python_only_tools_stay_out_of_native_registry(self):
        from scripts.gen_tool_registry import extract_tools
        names = {t["name"] for t in extract_tools(Path(vt.__file__))}
        self.assertNotIn("vantage_status", names)
        self.assertNotIn("vantage_live_link", names)
        self.assertNotIn("vantage_get_image", names)

    def test_registered_and_skipped_by_smoke(self):
        from maxmcp import server
        from maxmcp.tool_discovery import TOOLSET_SPECS
        from scripts.gen_tool_smoke import SKIP_DEFAULT
        self.assertIn("vantage", server.SPECIALTY_TOOL_MODULES)
        self.assertIn("vantage_status", server._READ_ONLY_TOOLS)
        self.assertEqual(sum("vantage" in spec.modules for spec in TOOLSET_SPECS), 1)
        self.assertTrue({"vantage_status", "vantage_live_link", "vantage_get_image"} <= SKIP_DEFAULT)
        self.assertIn("vantage_get_image", server._READ_ONLY_TOOLS)

    def test_progressive_profile_indexes_the_tools(self):
        from maxmcp import server
        from maxmcp.tool_discovery import ProgressiveToolCatalog
        catalog = ProgressiveToolCatalog(  # raises if a module isn't in exactly one toolset
            package="maxmcp", tools_dir=Path(vt.__file__).resolve().parent, hidden_mcp=MagicMock(),
            allowed_modules=server.CORE_TOOL_MODULES + server.SPECIALTY_TOOL_MODULES)
        self.assertEqual(set(catalog.module_tools.get("vantage", ())),
                         {"vantage_status", "vantage_live_link", "vantage_get_image"})

    def test_error_envelope_keeps_code_hint_and_details(self):
        from maxmcp.tool_response import envelope_exception
        exc = VantageError("Chaos Vantage isn't running.", "VANTAGE_NOT_RUNNING", False, "Start it.",
                           {"vantage": {"phase": "not_running"}})
        env = envelope_exception(exc, elapsed_ms=1.0, tool_name="vantage_live_link")
        self.assertEqual((env["error"]["code"], env["error"]["retryable"]), ("VANTAGE_NOT_RUNNING", False))
        self.assertEqual(env["hint"]["message"], "Start it.")
        self.assertEqual(env["error"]["details"], {"vantage": {"phase": "not_running"}})


if __name__ == "__main__":
    unittest.main()
