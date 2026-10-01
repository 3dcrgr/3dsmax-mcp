"""Native stack capture (maxmcp.diagnostics.stackdump) and capture_hang_diagnostics.

Heuristics use synthetic captures built from the frames of two real hangs
(diagnostics/hang_25016_allthreads.txt, hang_34300_allthreads.txt and
MCP_ISSUES #1). Live tests only touch child processes this test spawns; a
real 3ds Max is never touched.
"""
import contextlib
import ctypes
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from maxmcp import process_health, tool_response  # noqa: E402
from maxmcp.diagnostics import stackdump  # noqa: E402
from maxmcp.max_client import MaxClient  # noqa: E402

assert Path(stackdump.__file__).resolve().parents[2] == REPO_ROOT, stackdump.__file__

FAKE_WINDOW = Path(__file__).resolve().parent / "fake_window.py"
IS_WINDOWS = sys.platform == "win32"


def frames(*specs):
    """'module+0xoff' or 'module+0xoff Symbol+0xd' -> frame dicts."""
    out = []
    for spec in specs:
        location, _, symbol = spec.partition(" ")
        module, _, offset = location.rpartition("+")
        frame = {"module": module or None, "offset": int(offset, 16)}
        if symbol:
            frame["symbol"] = symbol
        out.append(frame)
    return out


def thread(tid, stack, is_main=False, windows=()):
    return {"tid": tid, "is_main": is_main, "state_hint": stackdump.classify_state(stack), "frames": stack,
            "windows": list(windows), "suspended_ms": 0.5}


def window(title, visible=True, hung=False, cls="Qt663QWindowIcon", hwnd=0x1000):
    return {"hwnd": hwnd, "title": title, "class": cls, "visible": visible, "owned": False, "hung": hung}


def capture(*threads, source="title"):
    return {"pid": 4242, "image": "3dsmax.exe", "captured_at": "2026-10-01T09:44:00+03:00", "mode": "selected",
            "depth": 48, "thread_count": 180, "main_thread": next((t["tid"] for t in threads if t["is_main"]), None),
            "main_thread_source": source, "threads": list(threads), "omitted": 180 - len(threads), "errors": []}


MAX_WINDOW = window("Untitled - Autodesk 3ds Max 2026", hung=True)
VRAY_POOL = frames("ntdll+0x160e44", "KERNELBASE+0x1be9f", "vray+0x1d94a98", "vray+0x1d83cbd", "vray+0x1d81da6",
                   "ucrtbase+0x2cd30", "KERNEL32+0x2cd87", "ntdll+0xacaec")
NETWORKING = frames("ntdll+0x160e44", "MSWSOCK+0x951a", "MSWSOCK+0x91f2", "WS2_32+0x119b1",
                    "chaos_networking_201+0xcd887", "chaos_networking_201+0xc0ffc", "ucrtbase+0x2cd30",
                    "KERNEL32+0x2cd87", "ntdll+0xacaec")
BRIDGE_PIPE = frames("ntdll+0x161914", "KERNELBASE+0x22773", "KERNELBASE+0x22641", "mcp_bridge_2026+0xa517",
                     "mcp_bridge_2026+0x9d97", "ucrtbase+0x2cd30", "KERNEL32+0x2cd87", "ntdll+0xacaec")
# Sixth hang (MCP_ISSUES #1): Material Editor asks V-Ray for a sample-slot render.
VRAY_MEDIT_MAIN = frames(
    "ntdll+0x160e44 NtWaitForSingleObject+0x14", "KERNELBASE+0x1be9f WaitForSingleObjectEx+0xaf",
    "vray+0x1e17ed6", "vray+0x1b36d80", "vray+0x1b28aeb", "vrender2026+0x2a4db7", "vrender2026+0x295034",
    "vray_rtmax2026+0x19afca", "3dsmax+0x25b320", "3dsmax+0x25b785", "3dsmax+0x1cc655", "core+0x26f5b4",
    "mtl+0x6953c", "mtl+0x697d2", "USER32+0x152c6", "USER32+0x15d9d")
# hang_34300 thread 14528: MatEditor.Close() from a bridge request, blocked in a USER32 syscall.
MXS_CLOSE_MAIN = frames(
    "win32u+0x2564", "3dsmax+0x21a20b", "MXSAgni+0xeb09d", "MAXScrpt+0xed8ae", "MAXScrpt+0x4b981",
    "MAXScrpt+0x64924", "MAXScrpt+0xed784", "MAXScrpt+0x4b981", "MAXScrpt+0xed86e", "MAXScrpt+0x4b981",
    "MAXScrpt+0x78682", "MAXScrpt+0xed784", "MAXScrpt+0x4b981", "MAXScrpt+0x59e8c", "MAXScrpt+0xed784",
    "MAXScrpt+0x4b981", "MAXScrpt+0xed86e", "MAXScrpt+0x4b981", "MAXScrpt+0xed86e", "MAXScrpt+0x4b981",
    "MAXScrpt+0x4b782", "MAXScrpt+0x4bc9a", "MAXScrpt+0x10fe97", "MAXScrpt+0x53b3f", "mcp_bridge_2026+0x18603",
    "mcp_bridge_2026+0x2127e", "mcp_bridge_2026+0x2a13e", "mcp_bridge_2026+0x2a491", "USER32+0x152c6",
    "USER32+0x15d9d")
# hang_34300 thread 24948: owns the hidden "Chaos Cosmos Browser", waits on SleepConditionVariableSRW.
GALAXY_BROWSER = frames(
    "ntdll+0x164a14", "ntdll+0x1137b", "KERNELBASE+0x23ac8", "galaxyimporter2026+0x240ee2",
    "galaxyimporter2026+0x2c339e", "galaxyimporter2026+0x243b7b", "galaxyimporter2026+0x244a21",
    "galaxyimporter2026+0x23b4b1", "galaxyimporter2026+0x3f806", "galaxyimporter2026+0x5247c",
    "galaxyimporter2026+0x380d7", "ucrtbase+0x2cd30", "KERNEL32+0x2cd87", "ntdll+0xacaec")
GALAXY_WORKER = frames("ntdll+0x164a14", "ntdll+0x1137b", "KERNELBASE+0x23ac8", "galaxyimporter2026+0x240ee2",
                       "galaxyimporter2026+0x254b74", "galaxyimporter2026+0x2e3118", "KERNEL32+0x2cd87",
                       "ntdll+0xacaec")
QT_IDLE = frames("win32u+0xaee4 NtUserMsgWaitForMultipleObjectsEx+0x14", "Qt6Core+0x21c01f", "Qt6Core+0xf56d4",
                 "KERNEL32+0x2cd87", "ntdll+0xacaec")
BROWSER_WINDOW = window("Chaos Cosmos Browser", visible=False, hung=True, hwnd=0x2000)


def vray_medit_capture():
    return capture(thread(5000, VRAY_MEDIT_MAIN, True, [MAX_WINDOW]), thread(31476, VRAY_POOL),
                   thread(8700, VRAY_POOL), thread(26512, NETWORKING), thread(32720, BRIDGE_PIPE))


def cosmos_deadlock_capture(browser_hung=True):
    browser = dict(BROWSER_WINDOW, hung=browser_hung)
    return capture(thread(14528, MXS_CLOSE_MAIN, True, [MAX_WINDOW]),
                   thread(34860, QT_IDLE, windows=[window("QtIdle", visible=False, hwnd=0x3000)]),
                   thread(24948, GALAXY_BROWSER, windows=[browser]),
                   thread(31776, GALAXY_WORKER), thread(3796, GALAXY_WORKER), thread(29120, VRAY_POOL))


def kinds(summary):
    return [f["kind"] for f in summary["findings"]]


class ClassifyTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(stackdump.classify_state([]), "unknown")
        self.assertEqual(stackdump.classify_state(VRAY_POOL), "kernel_wait")
        self.assertEqual(stackdump.classify_state(GALAXY_BROWSER), "kernel_wait")
        self.assertEqual(stackdump.classify_state(MXS_CLOSE_MAIN), "user_call")
        self.assertEqual(stackdump.classify_state(QT_IDLE), "message_wait")  # idle pump, known by its symbol
        self.assertEqual(stackdump.classify_state(frames("win32u+0x1404", "USER32+0x1139a", "MAXScrpt+0x236b32")),
                         "user_call")  # same idle pump without symbols: cannot tell
        self.assertEqual(stackdump.classify_state(frames("MAXScrpt+0x4b981", "MAXScrpt+0xed784")), "running")
        self.assertEqual(stackdump.classify_state([{"module": None, "offset": 0x7ff612340000}]), "running")  # JIT

    def test_system_modules(self):
        for name in ("ntdll", "KERNELBASE", "win32u", "USER32", "ucrtbase", "MSVCP140", "VCRUNTIME140_1"):
            self.assertTrue(stackdump.is_system_module(name), name)
        for name in ("vray", "3dsmax", "MAXScrpt", "mtl", "Qt6Core", "galaxyimporter2026", None, ""):
            self.assertFalse(stackdump.is_system_module(name), name)

    def test_format_frame(self):
        self.assertEqual(stackdump.format_frame({"module": "ntdll", "offset": 0x160e44}), "ntdll+0x160e44")
        self.assertEqual(stackdump.format_frame({"module": "ntdll", "offset": 0x160e44, "symbol": "NtWait+0x14"}),
                         "ntdll+0x160e44 (NtWait+0x14)")
        self.assertEqual(stackdump.format_frame({"module": None, "offset": 0x1234}), "0x1234")


class SummarizeTests(unittest.TestCase):
    def test_vray_material_editor_render(self):
        summary = stackdump.summarize(vray_medit_capture())
        self.assertEqual(kinds(summary), ["medit_vray_render"])
        self.assertEqual(summary["findings"][0]["message"],
                         "Material Editor sample-slot render (V-Ray) is blocking the main thread")
        main = summary["main_thread"]
        self.assertEqual(main["tid"], 5000)
        self.assertEqual(main["top_modules"][:3], ["vray", "vrender2026", "vray_rtmax2026"])
        self.assertEqual(main["blocked_in"], "kernel wait in KERNELBASE+0x1be9f (WaitForSingleObjectEx+0xaf), "
                                             "called from vray+0x1e17ed6")
        self.assertEqual(len(main["frames_preview"]), 12)
        self.assertEqual(main["frames_preview"][2], "vray+0x1e17ed6")
        self.assertEqual(summary["hung_windows"][0]["title"], MAX_WINDOW["title"])
        self.assertTrue(summary["hung_windows"][0]["main_thread"])

    def test_vray_without_material_editor_is_not_medit(self):
        stack = [f for f in VRAY_MEDIT_MAIN if f["module"] != "mtl"]
        summary = stackdump.summarize(capture(thread(5000, stack, True)))
        self.assertEqual(kinds(summary), ["main_thread_modules"])
        self.assertIn("vray, vrender2026, vray_rtmax2026", summary["findings"][0]["message"])

    def test_galaxyimporter_cross_thread_deadlock(self):
        summary = stackdump.summarize(cosmos_deadlock_capture())
        self.assertEqual(kinds(summary), ["cross_thread_window_deadlock", "cosmos_importer", "mcp_bridge_call"])
        deadlock, cosmos, bridge = summary["findings"]
        self.assertIn("with 'Chaos Cosmos Browser' (thread 24948, module galaxyimporter2026)", deadlock["message"])
        self.assertEqual(deadlock["evidence"]["tid"], 14528)
        self.assertEqual(deadlock["evidence"]["partner"]["tid"], 24948)
        self.assertEqual(deadlock["evidence"]["partner"]["windows"], ["'Chaos Cosmos Browser' (hidden, hung)"])
        self.assertIn("galaxyimporter2026", cosmos["message"])
        self.assertIn("3 thread(s): 24948, 31776, 3796", cosmos["message"])
        self.assertIn("thread 24948 owns 'Chaos Cosmos Browser' (hidden, hung)", cosmos["message"])
        self.assertIn("bridge", bridge["message"])
        self.assertEqual(summary["main_thread"]["blocked_in"], "USER32 call in win32u+0x2564, called from 3dsmax+0x21a20b")
        titles = [(w["title"], w["visible"], w["tid"]) for w in summary["hung_windows"]]
        self.assertIn(("Chaos Cosmos Browser", False, 24948), titles)

    def test_cross_thread_needs_a_hung_partner_window(self):
        summary = stackdump.summarize(cosmos_deadlock_capture(browser_hung=False))
        self.assertNotIn("cross_thread_window_deadlock", kinds(summary))
        self.assertEqual(kinds(summary)[0], "maxscript")  # the next explanation for the main thread
        self.assertIn("cosmos_importer", kinds(summary))

    def test_plain_maxscript(self):
        stack = MXS_CLOSE_MAIN[3:]  # running inside MAXScrpt, called from the bridge
        summary = stackdump.summarize(capture(thread(14528, stack, True, [MAX_WINDOW]), thread(31476, VRAY_POOL)))
        self.assertEqual(kinds(summary), ["maxscript", "mcp_bridge_call"])
        self.assertEqual(summary["findings"][0]["message"], "A long-running MAXScript is executing on the main thread "
                                                            "(running)")
        self.assertEqual(summary["main_thread"]["blocked_in"], "running in MAXScrpt+0xed8ae")
        waiting = frames("ntdll+0x160e44", "KERNELBASE+0x1be9f", "MAXScrpt+0x10c181", "MAXScrpt+0x9e0a1",
                         "3dsmax+0x1000", "USER32+0x152c6")
        summary = stackdump.summarize(capture(thread(1, waiting, True)))
        self.assertEqual(kinds(summary), ["maxscript"])
        self.assertIn("(kernel_wait)", summary["findings"][0]["message"])

    def test_unknown_lists_top_non_system_modules(self):
        stack = frames("ntdll+0x160e44", "KERNELBASE+0x1be9f", "ForestPackPro+0x1234", "ForestPackPro+0x2000",
                       "3dsmax+0x1000", "core+0x500", "geom+0x10", "USER32+0x152c6")
        summary = stackdump.summarize(capture(thread(77, stack, True), thread(78, VRAY_POOL)))
        self.assertEqual(kinds(summary), ["main_thread_modules"])
        self.assertEqual(summary["findings"][0]["message"], "Main thread is in ForestPackPro, 3dsmax, core (kernel_wait)")
        self.assertEqual(summary["main_thread"]["top_modules"], ["ForestPackPro", "3dsmax", "core", "geom"])
        self.assertEqual(summary["hung_windows"], [])

    def test_main_thread_missing_or_unreadable(self):
        summary = stackdump.summarize(capture(thread(1, VRAY_POOL), thread(24948, GALAXY_BROWSER,
                                                                            windows=[BROWSER_WINDOW])))
        self.assertIsNone(summary["main_thread"])
        self.assertEqual(kinds(summary), ["main_thread_unknown", "cosmos_importer"])
        unreadable = thread(9, [], True)
        unreadable["error"] = "OpenThread failed: Access is denied. (Win32 error 5)"
        summary = stackdump.summarize(capture(unreadable))
        self.assertEqual(kinds(summary), ["main_thread_unreadable"])
        self.assertIn("Access is denied", summary["findings"][0]["message"])
        self.assertEqual(summary["main_thread"]["blocked_in"], unreadable["error"])

    def test_rules_are_data(self):
        rules = stackdump.RULES + ({"kind": "forest_pack", "thread": "any", "any_module": r"forestpack",
                                    "message": "{module} on {count} thread(s)"},)
        stack = frames("ntdll+0x160e44", "ForestPackPro+0x10", "3dsmax+0x1")
        summary = stackdump.summarize(capture(thread(1, VRAY_MEDIT_MAIN, True), thread(2, stack)), rules=rules)
        self.assertEqual(kinds(summary), ["medit_vray_render", "forest_pack"])
        self.assertEqual(summary["findings"][1]["message"], "ForestPackPro on 1 thread(s)")

    def test_missing_state_hint_is_derived(self):
        bare = {"tid": 14528, "is_main": True, "frames": MXS_CLOSE_MAIN, "windows": []}
        browser = {"tid": 24948, "frames": GALAXY_BROWSER, "windows": [BROWSER_WINDOW]}
        summary = stackdump.summarize({"threads": [bare, browser]})
        self.assertEqual(kinds(summary)[0], "cross_thread_window_deadlock")
        self.assertNotIn("state_hint", bare)  # the input is not modified

    def test_format_text_keeps_the_original_layout(self):
        cap = cosmos_deadlock_capture()
        text = stackdump.format_text(cap, stackdump.summarize(cap))
        lines = text.splitlines()
        start = lines.index("=== thread 14528 (30 frames) ===")
        self.assertTrue(lines[start + 1].startswith("   # main thread (title); user_call"))
        self.assertIn("   win32u+0x2564", lines)
        self.assertIn("   # window 0x2000 'Chaos Cosmos Browser' [Qt663QWindowIcon] hidden hung", lines)
        self.assertIn("=== summary ===", lines)
        self.assertTrue(any(line.startswith("# [cross_thread_window_deadlock] ") for line in lines))


class MainThreadTests(unittest.TestCase):
    def win(self, tid, title, visible=True, area=100, owned=False):
        return {"hwnd": tid * 10, "tid": tid, "title": title, "class": "Qt", "visible": visible, "owned": owned,
                "hung": False, "area": area}

    def test_titled_window_beats_a_larger_one(self):
        windows = [self.win(1, "Render Frame Window", area=10_000), self.win(2, "scene.max - Autodesk 3ds Max 2026")]
        self.assertEqual(stackdump.identify_main_thread(windows, first_thread=9), (2, "title"))

    def test_unowned_titled_window_preferred(self):
        windows = [self.win(3, "Autodesk 3ds Max dialog", area=5_000, owned=True),
                   self.win(4, "Untitled - Autodesk 3ds Max 2026", area=1_000)]
        self.assertEqual(stackdump.identify_main_thread(windows), (4, "title"))

    def test_hidden_titled_window_is_ignored(self):
        windows = [self.win(5, "Autodesk 3ds Max 2026", visible=False), self.win(6, "Small", area=4),
                   self.win(7, "Large", area=400), self.win(8, "Chaos Cosmos Browser", visible=False, area=10**6)]
        self.assertEqual(stackdump.identify_main_thread(windows), (7, "largest_window"))

    def test_fallbacks(self):
        self.assertEqual(stackdump.identify_main_thread([self.win(5, "x", visible=False)], first_thread=11),
                         (11, "first_thread"))
        self.assertEqual(stackdump.identify_main_thread([]), (None, None))


class SelectedPidTests(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in ("MCP_MAX_PIPE", "MCP_MAX_PID")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_memory_only_and_lock_free(self):
        client = MaxClient()
        self.assertEqual(client.selected_pid_nowait(), {"pid": None, "source": None, "pipe": None})
        client._bound_target = MaxClient._target(r"\\.\pipe\3dsmax-mcp-pid-111", "default")
        client._pinned_pipe_name = None
        self.assertTrue(client._pipe_lock.acquire(timeout=1))  # a stuck request holds the lock
        try:
            with mock.patch.object(client, "_probe_pipe_available", side_effect=AssertionError("probed")):
                self.assertEqual(client.selected_pid_nowait()["pid"], 111)
                client._inflight = {"target_pid": 222, "target_pipe": r"\\.\pipe\3dsmax-mcp-pid-222"}
                self.assertEqual(client.selected_pid_nowait(), {"pid": 222, "source": "inflight",
                                                                "pipe": r"\\.\pipe\3dsmax-mcp-pid-222"})
        finally:
            client._pipe_lock.release()

    def test_pinned_and_environment(self):
        os.environ["MCP_MAX_PID"] = "333"
        client = MaxClient()
        self.assertEqual(client.selected_pid_nowait()["pid"], 333)
        self.assertEqual(client.selected_pid_nowait()["source"], "environment")
        client._pinned_pipe_name = r"\\.\pipe\3dsmax-mcp-pid-444"
        self.assertEqual(client.selected_pid_nowait(), {"pid": 444, "source": "explicit",
                                                        "pipe": r"\\.\pipe\3dsmax-mcp-pid-444"})


def load_tool(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.diagnostics", None)
        return importlib.import_module("maxmcp.tools.diagnostics")


def max_process(*pids):
    return [{"pid": pid, "image": "3dsmax.exe"} for pid in pids]


class ToolPidResolutionTests(unittest.TestCase):
    def setUp(self):
        # spec: any client call other than the memory-only lookup fails the test (no bridge call)
        self.client = mock.Mock(spec=["selected_pid_nowait"])
        self.client.selected_pid_nowait.return_value = {"pid": None, "source": None, "pipe": None}
        self.tool = load_tool(self.client)

    def resolve(self, pid=None, running=()):
        with mock.patch.object(self.tool.process_health, "list_processes", return_value=max_process(*running)):
            return self.tool.resolve_pid(pid)

    def test_one_running_max(self):
        self.assertEqual(self.resolve(running=(25016,)), {"pid": 25016, "source": "only_running"})

    def test_no_max(self):
        result = self.resolve()
        self.assertEqual(result["code"], "NOT_FOUND")
        self.assertIn("No 3ds Max", result["error"])
        envelope = tool_response.envelope_result(result, elapsed_ms=1.0)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "NOT_FOUND")

    def test_two_running_is_ambiguous(self):
        result = self.resolve(running=(25016, 34300))
        self.assertEqual(result["code"], "AMBIGUOUS")
        self.assertIn("25016, 34300", result["error"])
        self.assertEqual(result["details"]["max_pids"], [25016, 34300])
        self.assertEqual(tool_response.envelope_result(result, elapsed_ms=1.0)["error"]["code"], "AMBIGUOUS")

    def test_selected_instance_wins(self):
        self.client.selected_pid_nowait.return_value = {"pid": 34300, "source": "inflight", "pipe": "p"}
        self.assertEqual(self.resolve(running=(25016, 34300)), {"pid": 34300, "source": "inflight"})

    def test_selected_instance_not_running(self):
        self.client.selected_pid_nowait.return_value = {"pid": 11984, "source": "default", "pipe": "p"}
        result = self.resolve(running=(25016,))
        self.assertEqual(result["code"], "NOT_FOUND")
        self.assertIn("PID 11984", result["error"])

    def test_explicit_pid(self):
        self.assertEqual(self.resolve(25016, running=(25016, 34300)), {"pid": 25016, "source": "explicit"})
        self.assertEqual(self.resolve(os.getpid(), running=(25016,))["code"], "NOT_FOUND")  # not a 3ds Max
        self.assertEqual(self.resolve(True, running=(1,))["code"], "BAD_PARAM")
        self.assertEqual(self.resolve(-5, running=(25016,))["code"], "BAD_PARAM")
        self.client.selected_pid_nowait.assert_not_called()


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock(spec=["selected_pid_nowait"])
        self.client.selected_pid_nowait.return_value = {"pid": None, "source": None, "pipe": None}
        self.tool = load_tool(self.client)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name}),
                   mock.patch.object(self.tool.process_health, "list_processes", return_value=max_process(4242)),
                   mock.patch.object(self.tool.process_health, "diagnose_process",
                                     return_value={"pid": 4242, "alive": True, "state": "blocked",
                                                   "window_hung": True, "main_window_found": True,
                                                   "cpu_seconds_per_second": 0.01, "cpu_sample_s": 1.0})]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, cap, **kwargs):
        with mock.patch.object(self.tool.stackdump, "capture_stacks", return_value=cap) as captured:
            result = self.tool.capture_hang_diagnostics(**kwargs)
        return result, captured

    def test_compact_result_and_saved_files(self):
        result, captured = self.run_tool(cosmos_deadlock_capture())
        captured.assert_called_once_with(4242, all_threads=False, depth=48)
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(result["target_source"], "only_running")
        self.assertEqual(result["process"]["state"], "blocked")
        self.assertEqual(kinds(result), ["cross_thread_window_deadlock", "cosmos_importer", "mcp_bridge_call"])
        self.assertEqual(result["threads"]["captured"], 6)
        self.assertEqual(result["threads"]["total"], 180)
        self.assertEqual(result["threads"]["max_suspended_ms"], 0.5)
        self.assertEqual(len(result["main_thread"]["frames_preview"]), 12)  # previews only, no full stacks
        self.assertNotIn("windows", result["threads"])
        self.assertLess(len(json.dumps(result)), 8000)
        text, data = Path(result["saved"]["text"]), Path(result["saved"]["json"])
        self.assertEqual(text.parent, Path(self.tmp.name) / "3dsmax-mcp" / "diagnostics")
        self.assertRegex(text.name, r"^hang-4242-\d{8}-\d{6}\.txt$")
        self.assertIn("=== thread 24948 (14 frames) ===", text.read_text(encoding="utf-8"))
        saved = json.loads(data.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["capture"]["threads"][0]["frames"]), 30)
        self.assertEqual(saved["process"]["state"], "blocked")
        envelope = tool_response.envelope_result(result, elapsed_ms=1.0)
        self.assertTrue(envelope["ok"], envelope)
        second, _ = self.run_tool(cosmos_deadlock_capture())  # same second: a new file, not an overwrite
        self.assertNotEqual(second["saved"]["text"], result["saved"]["text"])

    def test_saved_captures_are_pruned_to_the_newest(self):
        folder = Path(self.tmp.name) / "3dsmax-mcp" / "diagnostics"
        folder.mkdir(parents=True)
        old = []
        for i in range(5):
            for ext in ("txt", "json"):
                path = folder / f"hang-4242-2026010{i}-120000.{ext}"
                path.write_text("old", encoding="utf-8")
                os.utime(path, (1_000_000 + i, 1_000_000 + i))
                old.append(path)
        other = folder / "notes.txt"
        other.write_text("keep", encoding="utf-8")
        with mock.patch.object(self.tool, "MAX_SAVED_CAPTURES", 3):
            result, _ = self.run_tool(vray_medit_capture())
        names = sorted(p.name for p in folder.iterdir())
        self.assertIn(Path(result["saved"]["text"]).name, names)
        self.assertIn(Path(result["saved"]["json"]).name, names)
        self.assertIn("notes.txt", names)  # only its own captures are pruned
        # The new capture plus the two newest old ones survive, as pairs.
        self.assertEqual(sorted(p.name for p in old if p.exists()),
                         sorted(p.name for p in old[-4:]))
        self.assertEqual(len(names), 3 * 2 + 1)

    def test_no_save(self):
        result, captured = self.run_tool(vray_medit_capture(), all_threads=True, depth=12, save=False)
        captured.assert_called_once_with(4242, all_threads=True, depth=12)
        self.assertNotIn("saved", result)
        self.assertEqual(kinds(result), ["medit_vray_render"])
        self.assertFalse((Path(self.tmp.name) / "3dsmax-mcp").exists())

    def test_capture_failure_is_an_error(self):
        failed = capture()
        failed["errors"] = [{"stage": "OpenProcess", "win32_error": 5, "message": "OpenProcess failed: Access is denied."}]
        result, _ = self.run_tool(failed)
        self.assertEqual(result["code"], "DIAGNOSTICS_FAILED")
        self.assertIn("Access is denied", result["error"])
        self.assertEqual(result["details"]["process"]["state"], "blocked")

    def test_resolution_error_skips_capture(self):
        self.tool.process_health.list_processes.return_value = max_process(1, 2)
        result, captured = self.run_tool(vray_medit_capture())
        self.assertEqual(result["code"], "AMBIGUOUS")
        captured.assert_not_called()
        self.tool.process_health.diagnose_process.assert_not_called()


class RegistrationTests(unittest.TestCase):
    def test_server_and_progressive_catalog(self):
        from maxmcp import server
        from maxmcp.tool_discovery import ProgressiveToolCatalog

        self.assertIn("diagnostics", server.CORE_TOOL_MODULES)
        # It pauses Max threads and writes files: not advertised as read-only (no auto-approval).
        self.assertEqual(server._tool_annotations("capture_hang_diagnostics"), {})
        catalog = ProgressiveToolCatalog(package="maxmcp", tools_dir=REPO_ROOT / "maxmcp" / "tools",
                                         hidden_mcp=mock.Mock(),
                                         allowed_modules=server.CORE_TOOL_MODULES + server.SPECIALTY_TOOL_MODULES)
        self.assertEqual(catalog.module_tools["diagnostics"], ("capture_hang_diagnostics",))
        connection = next(spec for spec in catalog.toolsets if spec.name == "connection")
        self.assertIn("diagnostics", connection.modules)


class HintTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"MCP_TRIPBACK_MODE": "minimal"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def hint(self, exc):
        return tool_response.envelope_exception(exc, elapsed_ms=1.0, tool_name="assign_material").get("hint")

    def test_hung_and_settling_errors_suggest_the_capture(self):
        from maxmcp.max_client import MaxImportSettlingError, MaxNotRespondingError
        for exc in (MaxNotRespondingError("3ds Max (PID 7) is not responding: main window not found, 0.01 CPU-s/s; "
                                          "the bridge cancelled an earlier request"),
                    MaxNotRespondingError("3ds Max (PID 7) is still not responding (main window hung)"),
                    MaxImportSettlingError("3ds Max (PID 7) is still settling after a Cosmos import: "
                                           "'Chaos Cosmos Browser' not responding")):
            hint = self.hint(exc)
            self.assertEqual(hint["suggested_tools"][0], "capture_hang_diagnostics", exc)
            self.assertIn("Wait up to ~10 min", hint["message"])
            self.assertNotIn("Do not end it", hint["message"])

    def test_busy_is_not_a_hang(self):
        from maxmcp.max_client import MaxBusyError
        hint = self.hint(MaxBusyError("3ds Max (PID 7) is still busy with another request: waited 10 s"))
        self.assertNotIn("capture_hang_diagnostics", json.dumps(hint))


class GuardTests(unittest.TestCase):
    def test_not_windows(self):
        with mock.patch.object(stackdump, "_IS_WINDOWS", False):
            result = stackdump.capture_stacks(1234)
        self.assertEqual(result["threads"], [])
        self.assertEqual(result["errors"][0]["stage"], "platform")

    def test_bad_args_never_raise(self):
        for pid in (0, -1, True, "12", 2**33):
            result = stackdump.capture_stacks(pid)
            self.assertEqual(result["threads"], [], pid)
            self.assertTrue(result["errors"], pid)
        result = stackdump.capture_stacks(4242, tids=["x"], depth="deep")
        self.assertEqual((result["threads"], result["depth"]), ([], stackdump.DEFAULT_DEPTH))
        self.assertIn("tids", result["errors"][0]["message"])

    @unittest.skipUnless(IS_WINDOWS, "Windows")
    def test_capture_runs_off_the_calling_thread(self):
        # Signals (Ctrl+C) only reach the main thread: the suspensions happen elsewhere.
        seen = []

        def fake_capture(api, pid, wanted, all_threads, depth, result):
            seen.append(threading.current_thread())

        with mock.patch.object(stackdump, "_load_api", return_value=object()), \
                mock.patch.object(stackdump, "_capture", side_effect=fake_capture):
            stackdump.capture_stacks(4)
        self.assertEqual(len(seen), 1)
        self.assertIsNot(seen[0], threading.current_thread())
        self.assertEqual(seen[0].name, "maxmcp-stackdump")

    def test_symbols_are_loaded_before_any_thread_is_suspended(self):
        self.assertFalse(stackdump._SYM_OPTIONS & 0x4, "SYMOPT_DEFERRED_LOADS loads modules mid-walk")
        self.assertEqual(stackdump._SYM_SEARCH_PATH, "")  # not None, which means the CWD

    @unittest.skipUnless(IS_WINDOWS, "Windows")
    def test_refuses_own_process(self):
        result = stackdump.capture_stacks(os.getpid())
        self.assertEqual(result["threads"], [])
        self.assertIn("calling process", result["errors"][0]["message"])


@unittest.skipUnless(IS_WINDOWS, "Windows stack capture")
class LiveCaptureTests(unittest.TestCase):
    """Real dbghelp walks of child processes this test spawns (never a real Max)."""

    def spawn(self, *args, **kwargs):
        # CREATE_NO_WINDOW: no console window, so a windowless child really has no window.
        child = subprocess.Popen([sys.executable, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
                                 **kwargs)
        self.addCleanup(self._kill, child)
        return child

    @staticmethod
    def _kill(child):
        if child.poll() is None:
            child.kill()
        child.wait(10)
        for stream in (child.stdin, child.stdout):
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def readline(child, timeout=10.0):
        box = []
        reader = threading.Thread(target=lambda: box.append(child.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout)
        return box[0] if box else None

    def assert_none_suspended(self, pid):
        """SuspendThread returns the previous count: 0 means capture resumed every thread."""
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenThread.restype = ctypes.c_void_p
        k32.SuspendThread.argtypes = k32.ResumeThread.argtypes = k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.SuspendThread.restype = k32.ResumeThread.restype = ctypes.c_uint32
        api = stackdump._load_api()
        for tid in stackdump._thread_ids(api, pid):
            handle = k32.OpenThread(0x0002, False, tid)
            if not handle:
                continue
            try:
                previous = k32.SuspendThread(handle)
                k32.ResumeThread(handle)
                self.assertEqual(previous, 0, f"thread {tid} was left suspended")
            finally:
                k32.CloseHandle(handle)

    def test_sleeping_child(self):
        child = self.spawn("-c", "import sys, time; print('ready', flush=True); time.sleep(30)")
        self.assertEqual((self.readline(child) or "").strip(), "ready")
        result = stackdump.capture_stacks(child.pid, depth=32)
        self.assertTrue(all(e.get("tid") for e in result["errors"]), result["errors"])  # idle workers may exit
        self.assertEqual(result["image"].lower(), Path(sys.executable).name.lower())
        self.assertGreaterEqual(len(result["threads"]), 1)
        self.assertEqual(result["mode"], "all")
        main = next(t for t in result["threads"] if t["is_main"])
        self.assertNotIn("error", main)
        self.assertEqual(result["main_thread_source"], "first_thread")  # no window: the oldest thread
        modules = [f["module"] for f in main["frames"]]
        self.assertEqual(modules[0], "ntdll")
        self.assertIn("ntdll", [m.lower() for m in modules])
        self.assertTrue(main["frames"][0].get("symbol", "").startswith(("Nt", "Zw")), main["frames"][0])
        self.assertEqual(main["state_hint"], "kernel_wait")
        self.assertTrue(any(m.lower().startswith("python") for m in modules if m), modules)
        self.assertTrue(all(t["suspended_ms"] < 1000 for t in result["threads"]))
        if stackdump._load_api().NtCreateThreadStateChange is not None:  # Windows 11 / Server 2022+
            self.assertEqual(list(result["pause_methods"]), ["state_change"])
        self.assertIsNone(child.poll())
        self.assert_none_suspended(child.pid)
        summary = stackdump.summarize(result)
        self.assertEqual(summary["main_thread"]["tid"], main["tid"])

    def test_child_stays_responsive(self):
        child = self.spawn("-c", "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line); sys.stdout.flush()")
        child.stdin.write("hello\n")
        child.stdin.flush()
        self.assertEqual(self.readline(child), "hello\n")
        for _ in range(3):
            result = stackdump.capture_stacks(child.pid, all_threads=True, depth=64)
            self.assertTrue(result["threads"])
        self.assertIsNone(child.poll())
        child.stdin.write("ping\n")
        child.stdin.flush()
        self.assertEqual(self.readline(child, 5.0), "ping\n")  # its main thread runs again

    def test_default_selection_keeps_window_threads(self):
        title = "MCP Test Capture Browser %d" % os.getpid()
        child = self.spawn(str(FAKE_WINDOW), "--title", title, "--hidden", "--mode", "pump", "--main")
        line = (self.readline(child) or "").split()
        self.assertEqual(line[:1], ["ready"])
        browser_hwnd, main_hwnd = int(line[1]), int(line[2])
        tid_of = {w["hwnd"]: w["tid"] for w in process_health.list_windows(child.pid)}
        result = stackdump.capture_stacks(child.pid, all_threads=False, depth=16)
        self.assertEqual(result["mode"], "selected")
        self.assertEqual(result["main_thread"], tid_of[main_hwnd])
        self.assertEqual(result["main_thread_source"], "largest_window")
        by_tid = {t["tid"]: t for t in result["threads"]}
        self.assertIn(tid_of[browser_hwnd], by_tid)
        browser = [w for w in by_tid[tid_of[browser_hwnd]]["windows"] if w["hwnd"] == browser_hwnd]
        self.assertEqual([(w["title"], w["visible"]) for w in browser], [(title, False)])
        self.assertNotIn("IME", [w["class"] for t in result["threads"] for w in t["windows"]])
        self.assertEqual(len(result["threads"]) + result["omitted"], result["thread_count"])
        for kept in result["threads"]:  # idle worker threads (ntdll only) are left out
            self.assertTrue(kept["is_main"] or kept["windows"]
                            or not stackdump.is_system_module(kept["frames"][0]["module"]), kept)
        self.assertTrue(by_tid[result["main_thread"]]["is_main"])
        self.assertIsNone(child.poll())

    def test_mocked_window_enumeration_picks_titled_thread(self):
        child = self.spawn("-c", "import sys, time; print('ready', flush=True); time.sleep(30)")
        self.assertEqual((self.readline(child) or "").strip(), "ready")
        tids = stackdump._thread_ids(stackdump._load_api(), child.pid)
        chosen = tids[-1]
        fake = [{"hwnd": 0x10, "tid": chosen, "title": "Untitled - Autodesk 3ds Max 2026", "class": "Qt",
                 "visible": True, "owned": False, "hung": True, "area": 100},
                {"hwnd": 0x20, "tid": tids[0], "title": "Bigger", "class": "Qt", "visible": True, "owned": False,
                 "hung": False, "area": 10_000}]
        with mock.patch.object(stackdump.process_health, "list_windows", return_value=fake):
            result = stackdump.capture_stacks(child.pid, tids=[chosen, 999_999_999], depth=8)
        self.assertEqual((result["main_thread"], result["main_thread_source"]), (chosen, "title"))
        self.assertEqual([t["tid"] for t in result["threads"]], [chosen])
        self.assertTrue(result["threads"][0]["is_main"])
        self.assertEqual(result["threads"][0]["windows"][0]["title"], "Untitled - Autodesk 3ds Max 2026")
        self.assertIn("does not belong", result["errors"][0]["message"])
        self.assertEqual(stackdump.summarize(result)["hung_windows"][0]["main_thread"], True)

    def test_killed_capture_never_leaves_a_thread_suspended(self):
        # A server killed (TerminateProcess) in the middle of a walk: the kernel
        # reverts the state-change suspension, so the target thread runs again.
        api = stackdump._load_api()
        if api.NtCreateThreadStateChange is None:
            self.skipTest("thread state-change objects need Windows 11 / Server 2022")
        child = self.spawn("-c", "import sys, time; print('ready', flush=True); time.sleep(30)")
        self.assertEqual((self.readline(child) or "").strip(), "ready")
        tid = stackdump._oldest_thread(api, stackdump._thread_ids(api, child.pid))
        holder_code = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from maxmcp.diagnostics import stackdump\n"
            "api = stackdump._load_api()\n"
            "def stuck(*args):\n"
            "    print('walking', flush=True)\n"
            "    time.sleep(60)\n"
            "    return False\n"
            "api.StackWalk64 = stuck\n"
            "stackdump.capture_stacks(int(sys.argv[1]), tids=[int(sys.argv[2])], depth=4)\n")
        holder = self.spawn("-c", holder_code, str(child.pid), str(tid))
        self.assertEqual((self.readline(holder) or "").strip(), "walking")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenThread.restype = ctypes.c_void_p
        k32.SuspendThread.argtypes = k32.ResumeThread.argtypes = k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.SuspendThread.restype = k32.ResumeThread.restype = ctypes.c_uint32
        handle = k32.OpenThread(0x0002, False, tid)
        self.assertTrue(handle)
        try:
            previous = k32.SuspendThread(handle)
            k32.ResumeThread(handle)
            self.assertEqual(previous, 1, "the holder should have the thread suspended")
            holder.kill()  # TerminateProcess: no atexit, no release_all
            holder.wait(10)
            deadline = time.monotonic() + 5
            while True:
                previous = k32.SuspendThread(handle)
                k32.ResumeThread(handle)
                if previous == 0 or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            self.assertEqual(previous, 0, "the killed capture left the thread suspended")
        finally:
            k32.CloseHandle(handle)
        self.assertIsNone(child.poll())

    def test_exited_process_is_an_error_entry(self):
        child = self.spawn("-c", "pass")
        child.wait(10)
        result = stackdump.capture_stacks(child.pid)
        self.assertEqual(result["threads"], [])
        self.assertTrue(result["errors"])

    def test_cli_text_and_json(self):
        child = self.spawn("-c", "import sys, time; print('ready', flush=True); time.sleep(30)")
        self.assertEqual((self.readline(child) or "").strip(), "ready")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = stackdump.main([str(child.pid), "--depth", "8"])
        self.assertEqual(code, 0)
        text = out.getvalue().splitlines()
        self.assertTrue(text[0].startswith(f"# pid {child.pid} "))
        self.assertTrue(any(line.startswith("=== thread ") and line.endswith(" frames) ===") for line in text))
        self.assertTrue(any(line.startswith("   ntdll+0x") for line in text))
        self.assertIn("=== summary ===", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stackdump.main([str(child.pid), "--all", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["capture"]["mode"], "all")
        self.assertIn("findings", payload["summary"])
        self.assertIsNone(child.poll())

    def test_list_processes_finds_child(self):
        child = self.spawn("-c", "import time; time.sleep(30)")
        time.sleep(0.2)
        found = process_health.list_processes((Path(sys.executable).name,))
        self.assertIn(child.pid, [p["pid"] for p in found])
        self.assertEqual(process_health.list_processes(("no-such-image.exe",)), [])


if __name__ == "__main__":
    unittest.main()
