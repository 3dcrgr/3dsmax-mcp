"""cosmos_import stall-safe flow, light polling, settling guard and search without Max.

Everything Max-side is mocked; window tests use child processes this test spawns.
"""
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from maxmcp import max_client, process_health, tool_response  # noqa: E402
from maxmcp.helpers import cosmos  # noqa: E402
from maxmcp.helpers.cosmos_client import CosmosError  # noqa: E402
from maxmcp.max_client import (MaxBusyError, MaxClient, MaxImportSettlingError,  # noqa: E402
                               MaxNotRespondingAfterDispatch)

assert Path(cosmos.__file__).resolve().parents[2] == REPO_ROOT, cosmos.__file__

PID = 4242
PIPE = rf"\\.\pipe\3dsmax-mcp-pid-{PID}"
ASSET_ID = "0e5c6d4a-1111-2222-3333-444455556666"
MAIN_HWND, BROWSER_HWND, NEW_BROWSER_HWND = 101, 202, 303
MAIN_TID, OTHER_TID = 11, 22
FAKE_WINDOW = Path(__file__).resolve().parent / "fake_window.py"
CLOSE_OR_OPEN = re.compile(r"mateditor\s*\.\s*(close|open)\s*\(", re.IGNORECASE)


class Clock:
    def __init__(self, events):
        self.now = 1000.0
        self.sleeps = []
        self.events = events

    def monotonic(self):
        return self.now

    perf_counter = monotonic
    time = monotonic

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def ref(handle, name="Plaster White", cls="VRayMtl"):
    return {"handle": str(handle), "name": name, "class": cls, "filename": ""}


class FakeClient:
    """Stands in for MaxClient; classifies every MAXScript it is sent."""

    def __init__(self, name, events, flow):
        self.name = name
        self.events = events
        self.flow = flow
        self.commands = []
        self.probes = []

    def send_command(self, command, cmd_type="maxscript", timeout=None, probe=False):
        kind = classify(command)
        self.commands.append((kind, command, timeout))
        self.probes.append(probe)
        self.events.append(("bridge", self.name, kind))
        handler = self.flow.get(kind)
        value = handler() if callable(handler) else handler
        if isinstance(value, list) and value and isinstance(value[0], BaseException):
            value = value.pop(0)
        if isinstance(value, BaseException):
            raise value
        return {"result": json.dumps(value)}

    def get_selected_max_instance(self):
        return {"target_pid": PID, "available": True}

    def select_max_instance(self, pid):
        return {"target_pid": pid, "available": True}

    def release_max_instance(self):
        self.events.append(("release", self.name))

    def resolve_target(self):
        return {"target_pid": PID, "available": True}


def classify(command):
    if "actionMan.executeAction" in command:
        return "browser"
    if "renderers.current" in command and "cosmosAssetId" not in command:
        return "renderer"
    if "clearSelection()" in command:
        return "prepare"
    if "maxOps.getNodeByHandle" in command:
        return "finalize"
    if "for c in material.classes do" in command:
        return "full"
    if "cosmosAssetId" in command:
        return "light"
    return "other"


class FakeService:
    base_url = "http://cosmos.test"

    def __init__(self, events, importers):
        self.events = events
        self._importers = importers
        self.importer_id = None
        self.timeout = None
        self.dispatch_error = None

    def importers(self):
        return list(self._importers)

    def asset(self, package_id):
        return {"id": ASSET_ID, "name": "Plaster White", "kind": "material", "revision": 3,
                "availability": 3, "size": 1024, "preview_path": None}

    def import_asset(self, asset_id, revision):
        self.events.append(("dispatch",))
        if self.dispatch_error:
            raise self.dispatch_error
        return {"status": "ok"}

    def search(self, query, limit=10, offset=0, tag_ids=(), downloaded=False):
        self.events.append(("search", self.importer_id))
        return {"items": []}


BEFORE = {"nodes": [], "materials": [ref(1, "Plaster White old")], "maps": []}
AFTER = {"nodes": [], "materials": [ref(1, "Plaster White old"), ref(99)], "maps": []}
VRAY = {"id": "imp-vray", "pid": PID, "renderer": "V-Ray", "name": "Max", "renderer_version": "7", "host": "3dsmax"}
CORONA = {"id": "imp-corona", "pid": PID, "renderer": "Corona", "name": "Max", "renderer_version": "12",
          "host": "3dsmax"}
OTHER = {"id": "imp-other", "pid": 7, "renderer": "V-Ray", "name": "Max", "renderer_version": "7", "host": "3dsmax"}


def prepared(**medit):
    state = {"class": "V_Ray_7", "instance": "V_Ray_7:V_Ray_7", "locked": True, "editor_open": False,
             "scanline_available": True, "swapped": True, "backup_pending": True, "error": ""}
    state.update(medit)
    return {"before": BEFORE, "selection": [5, 6], "medit": state}


def settle_result(quiet, hung_browser=False, cpu=0.1, responsive_streak=None):
    windows = [
        {"hwnd": MAIN_HWND, "exists": True, "hung": False, "responds": True, "title": "Autodesk 3ds Max",
         "visible": True, "role": "main"},
        {"hwnd": BROWSER_HWND, "exists": True, "hung": hung_browser, "responds": not hung_browser,
         "title": process_health.COSMOS_BROWSER_TITLE, "visible": False, "role": "titled"},
    ]
    windows_streak = (3 if quiet else 0) if responsive_streak is None else responsive_streak
    return {"quiet": quiet, "windows_quiet": windows_streak >= 3, "waited_s": 3.0 if quiet else 90.0, "checks": 3,
            "streak": 3 if quiet else 0, "responsive_streak": windows_streak,
            "required_checks": 3, "cpu_threshold": 0.5, "windows": windows,
            "hwnds": [MAIN_HWND, BROWSER_HWND], "main_window_found": True, "cpu_cores": cpu,
            "state": "responsive" if quiet else "not_responding"}


class _FlowCase(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.clock = Clock(self.events)
        self.light_results = [BEFORE, AFTER]
        self.flow = {"renderer": {"renderer": "V_Ray_7"}, "prepare": prepared(), "light": self._light,
                     "full": AFTER, "finalize": {"selection_restored": True, "medit": "restored",
                                                 "editor_open": False}, "browser": self._open_browser}
        self.global_client = FakeClient("global", self.events, self.flow)
        self.op_client = FakeClient("operation", self.events, self.flow)
        self.service = FakeService(self.events, [VRAY, OTHER])
        self.hung = []  # quick_hung_check answers, then False
        self.settle = settle_result(True)
        # Default: a responsive Cosmos browser on Max's main thread (no browser action needed).
        self.mains, self.browsers, self.hung_hwnds = [MAIN_HWND], [BROWSER_HWND], set()
        self.threads = {MAIN_HWND: MAIN_TID, BROWSER_HWND: MAIN_TID}
        self.titles = {MAIN_HWND: "Untitled - Autodesk 3ds Max 2026"}
        self.browser_action = {"found": True, "table": "V-Ray", "description": "Chaos Cosmos browser",
                               "executed": True, "error": ""}
        self.browser_appears = MAIN_TID  # thread of the window the action creates (None: no window)
        self.hung_after_action = set()  # hwnds that stop answering once the action ran
        self.wm_null = []  # window_responsive "responds" answers, then True
        max_client._settling.clear()
        self.addCleanup(max_client._settling.clear)
        for target, attr, value in (
            (cosmos, "time", self.clock),
            (cosmos, "MaxClient", lambda: self.op_client),
            (cosmos, "Cosmos", lambda timeout=30: self.service),
            (process_health, "quick_hung_check", self._quick_hung),
            (process_health, "cpu_cores", mock.Mock(return_value=0.1)),
            (process_health, "wait_responsive", self._wait_responsive),
            (process_health, "main_windows", lambda pid: list(self.mains)),
            (process_health, "find_windows", lambda pid, title: list(self.browsers)),
            (process_health, "window_hung", lambda hwnd: hwnd in self.hung_hwnds),
            (process_health, "window_responsive", self._window_responsive),
            (process_health, "window_thread", lambda hwnd: self.threads.get(hwnd)),
            (process_health, "_window_title", lambda hwnd: self.titles.get(hwnd, "")),
            (max_client, "process_start_time", mock.Mock(return_value=1)),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _light(self):
        return self.light_results.pop(0) if len(self.light_results) > 1 else self.light_results[0]

    def _quick_hung(self, pid):
        self.events.append(("os_check",))
        return self.hung.pop(0) if self.hung else False

    def _window_responsive(self, hwnd, timeout_ms=500):
        self.events.append(("wm_null", hwnd, timeout_ms))
        hung = hwnd in self.hung_hwnds
        responds = (self.wm_null.pop(0) if self.wm_null else True) and not hung
        return {"hwnd": hwnd, "exists": True, "hung": hung, "responds": responds,
                "visible": hwnd != BROWSER_HWND, "title": self.titles.get(hwnd, "")}

    def _open_browser(self):
        if self.browser_action.get("found") and self.browser_appears:
            self.browsers.append(NEW_BROWSER_HWND)
            self.threads[NEW_BROWSER_HWND] = self.browser_appears
        self.hung_hwnds |= self.hung_after_action
        return self.browser_action

    def _wait_responsive(self, pid, max_seconds, **kwargs):
        self.events.append(("settle_start", max_seconds, kwargs.get("titles")))
        result = self.settle(pid, max_seconds, **kwargs) if callable(self.settle) else self.settle
        self.events.append(("settle_end",))
        return result

    def run_import(self, **kwargs):
        return cosmos.import_asset(self.global_client, ASSET_ID, 0, "current", **kwargs)

    def bridge_kinds(self, client=None):
        return [e[2] for e in self.events if e[0] == "bridge" and (client is None or e[1] == client)]

    def index(self, event):
        return self.events.index(event)


class ImportOrderTests(_FlowCase):
    def test_swap_before_dispatch_and_no_bridge_during_settle(self):
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertTrue(result["safe_to_edit"])
        self.assertEqual(self.bridge_kinds("global"), ["renderer"])
        kinds = self.bridge_kinds("operation")
        self.assertEqual(kinds[0], "prepare")  # ONE call before dispatch: snapshot + medit swap
        self.assertEqual(kinds[-2:], ["full", "finalize"])
        dispatch = self.index(("dispatch",))
        before_dispatch = [e for e in self.events[:dispatch] if e[0] == "bridge" and e[1] == "operation"]
        self.assertEqual([e[2] for e in before_dispatch], ["prepare"])
        start = next(i for i, e in enumerate(self.events) if e[0] == "settle_start")
        end = self.index(("settle_end",))
        self.assertGreater(start, dispatch)
        self.assertFalse([e for e in self.events[start:end] if e[0] == "bridge"])
        self.assertEqual(self.events[start][2], (process_health.COSMOS_BROWSER_TITLE,))
        created = [m for m in result["materials"] if m["created"]]
        self.assertEqual([m["handle"] for m in created], ["99"])
        self.assertNotIn("warnings", result)
        self.assertEqual(self.events[-1], ("release", "operation"))

    def test_prepare_script_records_and_swaps_medit_renderer(self):
        self.run_import(swap_medit_renderer=True)
        script = self.op_client.commands[0][1]
        self.assertLess(script.index("local snap="), script.index("renderers.medit_locked=false"))
        for needle in ("renderers.medit_locked", "MatEditor.isOpen()", "SME.isOpen()", "Default_Scanline_Renderer",
                       "if scanline!=undefined do", "sl=scanline()", "renderers.medit=sl", "clearSelection()",
                       'throw "USER_BUSY"', "mcp_cosmosMeditBackup[3]==renderers.medit"):
            self.assertIn(needle, script)
        self.assertNotIn("__", script.replace("__KEY__", ""))  # every placeholder replaced
        self.assertLess(script.index("mcp_cosmosMeditBackup=#(renderers.medit, locked, sl)"),
                        script.index("renderers.medit_locked=false"))
        self.assertLess(script.index("renderers.medit_locked=false"), script.index("renderers.medit=sl"))

    def test_no_material_editor_close_or_open_anywhere(self):
        self.run_import()
        sent = [c[1] for c in self.op_client.commands + self.global_client.commands]
        constants = [v for k, v in vars(cosmos).items() if k.isupper() and isinstance(v, str)]
        for script in sent + constants + [cosmos._MEDIT_RESTORE, cosmos._PREPARE, cosmos._FINALIZE]:
            self.assertIsNone(CLOSE_OR_OPEN.search(script), script[:200])
            self.assertNotIn("mateditor.close", script.lower())

    def test_restore_guarded_by_material_editor_state(self):
        restore = cosmos._MEDIT_RESTORE
        self.assertLess(restore.index("MatEditor.isOpen()"), restore.index("renderers.medit=b[1]"))
        self.assertIn('then "editor_open"', restore)
        script = cosmos._finalize_script([5, 6], True)
        self.assertIn("for h in #(5,6)", script)
        self.assertLess(script.index("select (for h"), script.index("renderers.medit=b[1]"))
        self.assertIn("if true then", script)
        self.assertIn("if false then", cosmos._finalize_script([], False))

    def test_editor_open_leaves_scanline_with_exact_restore_script(self):
        self.flow["finalize"] = {"selection_restored": True, "medit": "editor_open", "editor_open": True}
        result = self.run_import(swap_medit_renderer=True)
        self.assertTrue(result["safe_to_edit"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("still Scanline", result["warnings"][0])
        self.assertIn(cosmos._MEDIT_RESTORE, result["warnings"][0])
        self.assertIn("if true then", self.op_client.commands[-1][1])

    def test_restore_medit_renderer_false(self):
        self.flow["finalize"] = {"selection_restored": True, "medit": "not_requested", "editor_open": False}
        result = self.run_import(restore_medit_renderer=False, swap_medit_renderer=True)
        self.assertIn("if false then", self.op_client.commands[-1][1])
        self.assertIn("restore_medit_renderer is false", result["warnings"][0])

    def test_scanline_unavailable_skips_swap_with_warning(self):
        self.flow["prepare"] = prepared(scanline_available=False, swapped=False, backup_pending=False)
        result = self.run_import(swap_medit_renderer=True)
        self.assertEqual(result["state"], "imported")
        self.assertIn("Default_Scanline_Renderer is unavailable", result["warnings"][0])
        self.assertIn("if false then", self.op_client.commands[-1][1])  # nothing to restore

    def test_settle_seconds_validated(self):
        for bad in (-1, 301, 1.5, True, "90"):
            with self.assertRaises(ValueError):
                self.run_import(settle_seconds=bad)
        self.assertFalse(self.events)
        self.assertTrue(cosmos._import_lock.acquire(blocking=False))
        cosmos._import_lock.release()

    def test_settle_seconds_passed_to_os_wait(self):
        self.run_import(settle_seconds=42)
        start = next(e for e in self.events if e[0] == "settle_start")
        self.assertEqual(start[1], 42)

    def test_settle_seconds_zero_still_waits_the_floor(self):
        self.run_import(settle_seconds=0)
        start = next(e for e in self.events if e[0] == "settle_start")
        self.assertEqual(start[1], cosmos._SETTLE_FLOOR_S)

    def test_dispatch_failure_is_structured_and_restores(self):
        self.service.dispatch_error = RuntimeError("Cosmos import returned status 2")
        self.flow["full"] = BEFORE
        result = self.run_import()
        self.assertEqual(result["state"], "import_unknown")
        self.assertIn("status 2", result["message"])
        self.assertEqual(self.bridge_kinds("operation"), ["prepare", "full", "finalize"])


class LightPollTests(_FlowCase):
    def test_polls_back_off_and_skip_while_hung(self):
        self.mains = []  # IsHungAppWindow gate only (no WM_NULL to a main window)
        self.hung = [True, True, False, False, False, False]
        self.light_results = [BEFORE, BEFORE, BEFORE, AFTER]
        result = cosmos._wait_import(self.op_client, cosmos._download(self.service, ASSET_ID, 0), BEFORE,
                                     "V-Ray", PID)
        self.assertTrue(result["detected"])
        self.assertEqual(result["skipped_hung"], 2)
        self.assertEqual(result["polls"], 4)
        self.assertEqual(self.clock.sleeps, [0.5, 0.75, 1.125, 1.688, 2.531, 3.0])
        # Every poll is preceded by an OS check; hung checks send nothing.
        sequence = [e[0] if e[0] != "bridge" else e[2] for e in self.events]
        self.assertEqual(sequence, ["os_check", "os_check", "os_check", "light", "os_check", "light",
                                    "os_check", "light", "os_check", "light"])
        light = [c for c in self.op_client.commands if c[0] == "light"]
        self.assertTrue(all(c[2] == cosmos._POLL_TIMEOUT_S for c in light))
        self.assertEqual(self.op_client.probes, [True] * 4)  # read-only: dropped at the deadline
        self.assertNotIn("for c in material.classes do", light[0][1])
        self.assertIn("for c in #() do", light[0][1])
        # Past ~4 s, every other poll also scans the renderer's own material classes.
        self.assertIn('pattern:"VRay*"', light[1][1])
        self.assertIn('"Multimaterial"', light[1][1])
        self.assertIn("for c in #() do", light[2][1])
        self.assertIn('pattern:"VRay*"', light[3][1])
        self.assertIn("if false do", light[0][1])  # maps only for HDRI

    def test_gives_up_after_timeout_while_hung(self):
        self.hung = [True] * 100
        result = cosmos._wait_import(self.op_client, cosmos._download(self.service, ASSET_ID, 0), BEFORE,
                                     "Corona", PID, timeout=10)
        self.assertFalse(result["detected"])
        self.assertEqual(result["polls"], 0)
        self.assertFalse(self.op_client.commands)

    def test_corona_and_hdri_light_scripts(self):
        hdri = {"id": ASSET_ID, "name": "Sky Dome", "kind": "hdri"}
        script = cosmos._snapshot_script(hdri, light=True, renderer="Corona", scan_classes=True)
        self.assertIn("if true do", script)
        self.assertIn("for c in #() do", script)  # material classes only for materials
        mat = {"id": ASSET_ID, "name": "Oak Floor", "kind": "material"}
        self.assertIn('pattern:"Corona*"', cosmos._snapshot_script(mat, True, "Corona", True))
        full = cosmos._snapshot_script(mat)
        self.assertIn("for c in material.classes do", full)
        self.assertIn("if true do", full)


class SettlingTests(_FlowCase):
    def test_not_quiet_returns_unsafe_and_guards_the_pid(self):
        self.settle = settle_result(False, hung_browser=True)
        result = self.run_import(swap_medit_renderer=True)
        self.assertEqual(result["state"], "settling")
        self.assertFalse(result["safe_to_edit"])
        self.assertTrue(result["detected"])
        self.assertIn("IMPORT_SETTLING", result["next"])
        self.assertIn("5-8 min", result["next"])
        self.assertIn("~10 min", result["next"])
        self.assertIn("deadlock", result["next"])
        self.assertIn("'Chaos Cosmos Browser' (hidden) hung", result["next"])
        self.assertIn("renderers.medit=b[1]", result["pending_restore"]["maxscript"])
        self.assertEqual(result["pending_restore"]["selection"], [5, 6])
        self.assertEqual([m["handle"] for m in result["materials"] if m["created"]], ["99"])
        # No bridge call after the settle phase: no confirm snapshot, no restore.
        end = self.index(("settle_end",))
        self.assertFalse([e for e in self.events[end:] if e[0] == "bridge"])
        state = max_client.settling_state(PID)
        self.assertEqual(state["windows"], {MAIN_HWND: "", BROWSER_HWND: process_health.COSMOS_BROWSER_TITLE})
        self.assertAlmostEqual(state["until"] - state["since"], cosmos._SETTLING_GUARD_S, places=3)

    def test_cpu_busy_but_windows_responsive_still_finishes(self):
        self.settle = settle_result(False, cpu=2.5, responsive_streak=5)
        result = self.run_import()
        self.assertTrue(result["safe_to_edit"])
        self.assertEqual(result["state"], "imported")
        self.assertIn("CPU was still busy", result["warnings"][0])
        self.assertIsNone(max_client.settling_state(PID))

    def test_not_responding_during_poll_becomes_structured_result(self):
        error = MaxNotRespondingAfterDispatch(
            "3ds Max (PID 4242) is not responding: main window hung", {"process": {"state": "blocked"},
                                                                       "request_sent": True})
        self.light_results = [error]
        self.settle = settle_result(False, hung_browser=True)
        result = self.run_import()
        self.assertEqual(result["state"], "settling")
        self.assertFalse(result["safe_to_edit"])
        self.assertEqual(result["health"]["code"], "MAX_NOT_RESPONDING")
        self.assertEqual(result["health"]["process"], {"state": "blocked"})
        self.assertIn("not responding", result["message"])
        self.assertIsNotNone(max_client.settling_state(PID))

    def test_not_responding_then_recovered_confirms_import(self):
        self.light_results = [MaxNotRespondingAfterDispatch("not responding", {"process": {"state": "blocked"}})]
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertTrue(result["safe_to_edit"])
        self.assertIn("not responding", result["message"])
        self.assertEqual(self.bridge_kinds("operation")[-2:], ["full", "finalize"])


class ReviewFixTests(_FlowCase):
    """Stall cases after dispatch, the in-progress guard and pre-dispatch checks."""

    def test_finalize_not_responding_after_good_snapshot_is_settling(self):
        self.mains, self.browsers = [MAIN_HWND], [BROWSER_HWND]
        self.flow["finalize"] = [MaxNotRespondingAfterDispatch(
            "3ds Max (PID 4242) is not responding", {"process": {"state": "blocked"}, "request_sent": True})]
        result = self.run_import(swap_medit_renderer=True)
        self.assertEqual(result["state"], "settling")
        self.assertFalse(result["safe_to_edit"])
        self.assertEqual(result["health"]["code"], "MAX_NOT_RESPONDING")
        self.assertIn("may already have run", result["next"])
        self.assertIn("renderers.medit=b[1]", result["pending_restore"]["maxscript"])
        self.assertEqual([m["handle"] for m in result["materials"] if m["created"]], ["99"])  # from the snapshot
        self.assertTrue(result["detected"])
        state = max_client.settling_state(PID)
        self.assertIsNone(state["owner"])  # converted to the hung-window guard
        self.assertEqual(state["windows"], {MAIN_HWND: "", BROWSER_HWND: process_health.COSMOS_BROWSER_TITLE})

    def test_confirm_snapshot_not_responding_is_settling(self):
        self.flow["full"] = [MaxBusyError("busy", {"request_sent": True})]
        result = self.run_import()
        self.assertEqual(result["state"], "settling")
        self.assertFalse(result["safe_to_edit"])
        self.assertIn("while confirming the import", result["next"])
        self.assertEqual(self.bridge_kinds("operation")[-1], "full")  # no restore sent afterwards

    def test_other_clients_refused_while_import_runs(self):
        seen = {}

        def settle(pid, max_seconds, **kwargs):
            state = max_client.settling_state(PID)
            seen["owner"] = state["owner"]
            other = MaxClient(transport="pipe", pipe_name=PIPE)
            opened = mock.Mock(side_effect=ConnectionError("sentinel"))
            with mock.patch.object(other, "_ensure_pipe_handle", opened):
                with self.assertRaises(MaxImportSettlingError) as ctx:
                    other.send_command("selection.count", cmd_type="ping")
            seen["message"] = str(ctx.exception)
            seen["opened"] = opened.called
            seen["in_progress"] = ctx.exception.details["settling"]["in_progress"]
            return settle_result(True)

        self.settle = settle
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                          if k not in ("MCP_MAX_PIPE", "MCP_MAX_PID")}, clear=True):
            result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertIs(seen["owner"], self.op_client)
        self.assertFalse(seen["opened"])
        self.assertTrue(seen["in_progress"])
        self.assertIn("busy with a Cosmos import", seen["message"])
        self.assertIsNone(max_client.settling_state(PID))  # released once the import returned

    def test_guard_released_when_import_raises(self):
        self.flow["prepare"] = RuntimeError("MAXScript error: USER_BUSY")
        with self.assertRaises(RuntimeError):
            self.run_import()
        self.assertIsNone(max_client.settling_state(PID))
        self.assertNotIn(("dispatch",), self.events)

    def test_hung_main_window_before_dispatch_refuses_import(self):
        self.hung = [True]
        with self.assertRaises(CosmosError) as ctx:
            self.run_import()
        self.assertEqual(ctx.exception.code, "IMPORT_SETTLING")
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("main window hung", str(ctx.exception))
        self.assertIn("~10 min", str(ctx.exception))
        self.assertFalse(self.bridge_kinds("operation"))
        self.assertNotIn(("dispatch",), self.events)
        self.assertIsNone(max_client.settling_state(PID))
        self.assertTrue(cosmos._import_lock.acquire(blocking=False))
        cosmos._import_lock.release()

    def test_pre_dispatch_state_recorded(self):
        self.browsers = [BROWSER_HWND]
        result = self.run_import()
        self.assertEqual(result["import_timing"]["pre_dispatch"],
                         {"main_hung": False, "browser_windows": 1, "browser_hung": 0})

    def test_prepare_lost_returns_structured_result_without_dispatch(self):
        self.flow["prepare"] = MaxNotRespondingAfterDispatch(
            "3ds Max (PID 4242) is not responding", {"process": {"state": "blocked"}, "request_sent": True})
        result = self.run_import(swap_medit_renderer=True)
        self.assertEqual(result["state"], "not_imported")
        self.assertFalse(result["safe_to_edit"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["health"]["code"], "MAX_NOT_RESPONDING")
        self.assertEqual(result["pending_restore"]["maxscript"], cosmos._MEDIT_RESTORE)
        self.assertNotIn(("dispatch",), self.events)
        self.assertEqual(max_client.settling_state(PID)["windows"],
                         {MAIN_HWND: "", BROWSER_HWND: process_health.COSMOS_BROWSER_TITLE})

    def test_prepare_not_sent_is_reraised(self):
        self.flow["prepare"] = MaxBusyError("busy", {"request_sent": False})
        with self.assertRaises(MaxBusyError):
            self.run_import()
        self.assertIsNone(max_client.settling_state(PID))

    def test_poll_skipped_while_main_window_misses_wm_null(self):
        self.mains = [MAIN_HWND]
        self.wm_null = [False, False]
        result = cosmos._wait_import(self.op_client, cosmos._download(self.service, ASSET_ID, 0), BEFORE,
                                     "V-Ray", PID)
        self.assertTrue(result["detected"])
        self.assertEqual(result["skipped_hung"], 2)
        self.assertIn(("wm_null", MAIN_HWND, cosmos._POLL_GATE_MS), self.events)

    def test_dropped_poll_ends_detection(self):
        self.light_results = [MaxBusyError("dropped", {"request_sent": True})]
        result = cosmos._wait_import(self.op_client, cosmos._download(self.service, ASSET_ID, 0), BEFORE,
                                     "V-Ray", PID)
        self.assertEqual(result["polls"], 1)
        self.assertIsInstance(result["error"], MaxBusyError)
        self.assertEqual(self.bridge_kinds("operation"), ["light"])

    def test_settle_uses_floor_min_span_and_cpu_grace(self):
        self.run_import()
        start = next(e for e in self.events if e[0] == "settle_start")
        self.assertEqual(start[1], cosmos.SETTLE_SECONDS_DEFAULT)
        kwargs = {}
        original = self._wait_responsive

        def capture(pid, max_seconds, **kw):
            kwargs.update(kw)
            return original(pid, max_seconds, **kw)
        with mock.patch.object(process_health, "wait_responsive", capture):
            self.run_import()
        self.assertEqual(kwargs["min_span_s"], cosmos._SETTLE_MIN_SPAN_S)
        self.assertEqual(kwargs["cpu_grace_s"], cosmos._SETTLE_CPU_GRACE_S)
        self.assertGreaterEqual(cosmos._SETTLE_MIN_SPAN_S, 5.0)  # IsHungAppWindow latency
        self.assertGreater(cosmos._SETTLE_FLOOR_S, cosmos._SETTLE_MIN_SPAN_S)

    def test_stale_backup_warns_without_overwriting(self):
        self.flow["finalize"] = {"selection_restored": True, "medit": "stale", "editor_open": False}
        result = self.run_import(swap_medit_renderer=True)
        self.assertTrue(result["safe_to_edit"])
        self.assertIn("left as is", result["warnings"][0])


def slotted(handle, name, slot=0, cls="VRayMtl"):
    return {**ref(handle, name, cls), "slot": slot}


# Pre-dispatch snapshot with the recorded handle set (issue #9): 1-3 existed before dispatch.
KNOWN_BEFORE = {"nodes": [], "materials": [], "maps": [], "known": {"materials": [3, 1, 2, 2], "maps": None}}


class HandleDiffTests(_FlowCase):
    """Issue #9: materials found by handle diff whatever their name."""

    def use(self, after, before=KNOWN_BEFORE):
        self.flow["prepare"] = {**prepared(), "before": before}
        self.light_results = [after]
        self.flow["full"] = after

    def test_differently_named_material_detected_by_handle(self):
        after = {"nodes": [], "materials": [slotted(77, "Steel_Polished #0", 13)], "maps": []}
        self.use(after)
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertTrue(result["import_timing"]["detected"])
        self.assertEqual(result["asset_name"], "Plaster White")
        self.assertEqual(result["material_name"], "Steel_Polished #0")
        self.assertEqual(result["primary_material"], {"handle": "77", "name": "Steel_Polished #0",
                                                      "class": "VRayMtl", "medit_slot": 13})
        self.assertIn("named 'Steel_Polished #0', not 'Plaster White'", result["note"])
        self.assertEqual([(m["handle"], m["created"], m["medit_slot"], m["sub_material"])
                          for m in result["materials"]], [("77", True, 13, False)])
        self.assertEqual(result["primary_reason"], "only_new")
        self.assertNotIn("slot", result["materials"][0])
        # The light polls and the confirming snapshot get the sorted recorded handles back.
        light = [c[1] for c in self.op_client.commands if c[0] == "light"]
        full = [c[1] for c in self.op_client.commands if c[0] == "full"]
        for script in light + full:
            self.assertIn("local knownMats=#(1L,2L,3L)", script)
            self.assertIn("local diffMats=true", script)
            self.assertIn("local record=false", script)
        self.assertIn("local diffMaps=false", full[0])  # no map handles recorded for a material

    def test_matching_name_unchanged_and_no_note(self):
        after = {"nodes": [], "materials": [slotted(77, "Plaster_White", 2)], "maps": []}
        self.use(after)
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertEqual(result["material_name"], "Plaster_White")
        self.assertNotIn("note", result)
        self.assertNotIn("warnings", result)

    def test_legacy_snapshot_without_known_still_name_based(self):
        result = self.run_import()  # BEFORE has no "known": the name-only path
        self.assertEqual(result["state"], "imported")
        self.assertEqual(result["primary_material"]["handle"], "99")
        self.assertIsNone(result["primary_material"]["medit_slot"])
        self.assertNotIn("note", result)
        full = [c[1] for c in self.op_client.commands if c[0] == "full"][0]
        self.assertIn("local diffMats=false", full)
        self.assertIn("local knownMats=#()", full)

    def test_several_new_materials_all_listed_name_match_is_primary(self):
        after = {"nodes": [], "materials": [slotted(80, "Wrapper", 13), slotted(81, "Plaster White"),
                                            slotted(82, "Other", 5, "Multimaterial")], "maps": []}
        self.use(after)
        result = self.run_import()
        self.assertEqual([m["handle"] for m in result["materials"] if m["created"]], ["80", "81", "82"])
        self.assertEqual(result["primary_material"]["handle"], "81")
        self.assertEqual(result["primary_reason"], "name")
        self.assertNotIn("note", result)  # a name match needs no guess note

    def test_matching_name_package_with_sub_materials(self):
        # A matching-name blend whose coats are named otherwise: coats are listed (flagged), no note.
        after = {"nodes": [], "materials": [slotted(80, "Plaster White", 13, "VRayBlendMtl"),
                                            {**slotted(81, "Coat A"), "sub": True},
                                            {**slotted(82, "Coat B"), "sub": True}], "maps": []}
        self.use(after)
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertEqual([(m["handle"], m["created"], m["sub_material"]) for m in result["materials"]],
                         [("80", True, False), ("81", True, True), ("82", True, True)])
        self.assertNotIn("sub", result["materials"][1])
        self.assertEqual(result["material_name"], "Plaster White")
        self.assertNotIn("note", result)

    def test_without_name_match_top_level_beats_earlier_sub_material(self):
        # Slate mode (no slots): the first class-scan hit is a coat, the wrapper is still primary.
        after = {"nodes": [], "materials": [{**slotted(81, "Coat"), "sub": True},
                                            slotted(80, "Steel_Polished #0", 0, "VRayBlendMtl")], "maps": []}
        self.use(after)
        result = self.run_import()
        self.assertEqual(result["primary_material"]["handle"], "80")
        self.assertEqual(result["primary_reason"], "top_level")
        self.assertIn("named 'Steel_Polished #0'", result["note"])
        self.assertNotIn("top-level materials appeared", result["note"])  # only one top-level

    def test_without_name_match_medit_slot_is_primary(self):
        after = {"nodes": [], "materials": [slotted(80, "Inner"), slotted(82, "Steel_Polished #0", 13)],
                 "maps": []}
        self.use(after)
        result = self.run_import()
        self.assertEqual(result["primary_material"]["handle"], "82")
        self.assertEqual(result["primary_reason"], "medit_slot")
        self.assertIn("named 'Steel_Polished #0'", result["note"])
        self.assertIn("2 new top-level materials appeared since the import started", result["note"])

    def test_material_seen_during_import_beats_later_hand_made_one(self):
        # The poll saw 82 during the import; 70 (slot 1) appeared only during settle (made by hand).
        self.flow["prepare"] = {**prepared(), "before": KNOWN_BEFORE}
        self.light_results = [{"nodes": [], "materials": [slotted(82, "Steel_Polished #0", 13)], "maps": []}]
        self.flow["full"] = {"nodes": [], "materials": [slotted(70, "Material #25", 1),
                                                        slotted(82, "Steel_Polished #0", 13)], "maps": []}
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertEqual(result["primary_material"]["handle"], "82")
        self.assertEqual(result["primary_reason"], "detected_during_import")
        self.assertIn("made by hand", result["note"])
        self.assertEqual([m["handle"] for m in result["materials"] if m["created"]], ["70", "82"])

    def test_snapshot_isolates_handle_check_and_flags_sub_materials(self):
        mat = {"id": ASSET_ID, "name": "Steel Blurry", "kind": "material"}
        script = cosmos._snapshot_script(mat, known={"materials": [5], "maps": None})
        # A throwing handle check reads as "not new" and cannot abort the name pass.
        self.assertIn("fn isNew value known diff = (diff and (try(not (isKnown (handleOf value) known))catch(false)))",
                      script)
        self.assertIn("if record do remember m knownMats", script)
        self.assertIn("isSub:((findItem subs m)>0)", script)
        self.assertEqual(cosmos._mxs_handles({3, 2147483648, 1}), "#(1L,3L,2147483648L)")
        self.assertEqual(cosmos._mxs_handles(()), "#()")

    def test_handles_known_before_dispatch_never_created(self):
        # Handle 2 was recorded but not name-matched; handle 1 was reported before.
        before = {**KNOWN_BEFORE, "materials": [ref(1, "Plaster White old")]}
        after = {"nodes": [], "materials": [ref(1, "Plaster White old"), slotted(2, "Default", 1)], "maps": []}
        self.use(after, before)
        result = self.run_import()
        self.assertEqual(result["state"], "imported_unverified")
        self.assertFalse(any(m["created"] for m in result["materials"]))
        self.assertNotIn("primary_material", result)
        self.assertEqual(result["asset_name"], "Plaster White")
        self.light_results = [after]
        timing = cosmos._wait_import(self.op_client, cosmos._download(self.service, ASSET_ID, 0), before,
                                     "V-Ray", PID, timeout=5)
        self.assertFalse(timing["detected"])

    def test_settling_reports_poll_candidates_by_handle(self):
        self.use({"nodes": [], "materials": [slotted(77, "Steel_Polished #0", 13)], "maps": []})
        self.settle = settle_result(False, hung_browser=True)
        result = self.run_import()
        self.assertEqual(result["state"], "settling")
        self.assertTrue(result["detected"])
        self.assertEqual(result["primary_material"]["name"], "Steel_Polished #0")
        self.assertIn("note", result)

    def test_prepare_records_handles_and_polls_stay_light(self):
        mat = {"id": ASSET_ID, "name": "Steel Blurry", "kind": "material"}
        script = cosmos._prepare_script(mat, swap=False)
        self.assertIn("local record=true", script)
        self.assertIn("local recordMaps=false", script)
        self.assertIn("local diffMats=false", script)
        self.assertNotIn("__", script.replace("__KEY__", ""))
        known = {"materials": [9, 3000000000, "bad"], "maps": [4]}
        light = cosmos._snapshot_script(mat, light=True, renderer="V-Ray", known=known)
        self.assertIn("local knownMats=#(9L,3000000000L)", light)
        self.assertIn("local knownMaps=#()", light)  # a light poll diffs only the asset's own kind
        self.assertIn("for c in #() do", light)  # still no class scan on this poll
        model = {"id": ASSET_ID, "name": "Oak Chair", "kind": "model"}
        light = cosmos._snapshot_script(model, light=True, renderer="V-Ray", known=known)
        self.assertIn("local diffMats=false", light)
        self.assertIn("local knownMats=#()", light)
        hdri = {"id": ASSET_ID, "name": "Sky Dome", "kind": "hdri"}
        self.assertIn("local recordMaps=true", cosmos._snapshot_script(hdri, record=True))
        light = cosmos._snapshot_script(hdri, light=True, renderer="V-Ray", known=known)
        self.assertIn("local knownMaps=#(4L)", light)
        self.assertIn("local diffMats=false", light)

    def test_hdri_new_map_detected_by_handle(self):
        before = {"nodes": [], "materials": [], "maps": [], "known": {"materials": [], "maps": [4, 5]}}
        probe = {"nodes": [], "materials": [], "maps": [ref(5, "Old", "Bitmaptexture"), ref(6, "env_4k", "VRayHDRI")]}
        self.light_results = [probe]
        hdri = {**cosmos._download(self.service, ASSET_ID, 0), "kind": "hdri"}
        timing = cosmos._wait_import(self.op_client, hdri, before, "V-Ray", PID)
        self.assertTrue(timing["detected"])
        response = cosmos._resources(before, probe)
        cosmos._primary(response, hdri)
        self.assertEqual([m["handle"] for m in response["maps"] if m["created"]], ["6"])
        self.assertEqual(response["primary_map"]["name"], "env_4k")
        self.assertNotIn("medit_slot", response["maps"][0])


def slot_prep(active=13, keep=True, free_slot=4, switched=True, **extra):
    return {"active": active, "material": {"handle": "2", "name": "Steel_Blurry"}, "keep": keep,
            "free_slot": free_slot, "switched": switched, "mode": "basic", "stale": False, "error": "", **extra}


def slot_fin(original=13, switched_to=4, displaced=None, occupant=None, fresh=False, moved_to=0, restored=False,
             kept_alive=False, active_restored=True, error=""):
    return {"original": original, "switched_to": switched_to, "displaced": displaced, "occupant": occupant,
            "imported_occupant": fresh, "moved_to": moved_to, "restored": restored, "kept_alive": kept_alive,
            "active_restored": active_restored, "error": error}


USER_MAT = {"handle": "2", "name": "Steel_Blurry"}
IMPORTED = {"handle": "77", "name": "Steel_Polished #0"}


class ActiveSlotTests(_FlowCase):
    """Issue #10: the importer writes into the ACTIVE Compact Material Editor slot."""

    def use(self, prep, fin, imported_slot):
        self.flow["prepare"] = {**prepared(), "before": KNOWN_BEFORE, "medit_slot": prep}
        after = {"nodes": [], "materials": [slotted(2, "Steel_Blurry", 0 if imported_slot == 13 else 13),
                                            slotted(77, "Steel_Polished #0", imported_slot)], "maps": []}
        self.light_results = [after]
        self.flow["full"] = after
        self.flow["finalize"] = {"selection_restored": True, "medit": "restored", "editor_open": True,
                                 "medit_slot": fin}

    def slots(self, result):
        return {m["handle"]: m["medit_slot"] for m in result["materials"]}

    def test_used_active_slot_switched_before_dispatch_and_restored_after(self):
        self.use(slot_prep(), slot_fin(), imported_slot=4)
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertNotIn("warnings", result)
        self.assertEqual(result["medit_active_slot"], {
            "original": 13, "material": USER_MAT, "kept": True, "switched_to": 4, "mode": "basic",
            "active_restored": True, "state": "kept"})
        self.assertEqual(result["medit_slot"], 4)
        self.assertEqual(self.slots(result), {"2": 13, "77": 4})
        self.assertNotIn("displaced_material", result)
        self.assertNotIn("medit_slot_restored", result)
        # The switch rides in the one prepare call; the restore in the one finalize call after quiet.
        kinds = self.bridge_kinds("operation")
        self.assertEqual(kinds[0], "prepare")
        self.assertEqual(kinds[-2:], ["full", "finalize"])
        self.assertLessEqual(set(kinds), {"prepare", "light", "full", "finalize"})
        dispatch, end = self.index(("dispatch",)), self.index(("settle_end",))
        start = next(i for i, e in enumerate(self.events) if e[0] == "settle_start")
        self.assertFalse([e for e in self.events[start:end] if e[0] == "bridge"])
        prepare = self.op_client.commands[0][1]
        self.assertLess(self.events.index(("bridge", "operation", "prepare")), dispatch)
        self.assertLess(prepare.index("local snap="), prepare.index("activeMeditSlot=slotFree"))
        self.assertLess(prepare.index("activeMeditSlot=slotFree"), prepare.index("clearSelection()"))
        finalize = self.op_client.commands[-1][1]
        self.assertGreater(self.events.index(("bridge", "operation", "finalize")), end)
        self.assertIn("activeMeditSlot=a", finalize)
        self.assertIn(">(3L)", finalize)  # newest pre-dispatch material handle from KNOWN_BEFORE

    def test_free_slot_appeared_after_prepare_displaced_material_restored(self):
        # No free slot before dispatch, but one was freed meanwhile (e.g. by hand); otherwise the
        # no-free-slot case ends in the warning below.
        fin = slot_fin(switched_to=0, displaced=USER_MAT, occupant=IMPORTED, fresh=True, moved_to=5, restored=True,
                       active_restored=None)
        self.use(slot_prep(free_slot=0, switched=False), fin, imported_slot=13)
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertNotIn("warnings", result)
        self.assertEqual(result["displaced_material"], {**USER_MAT, "slot": 13})
        self.assertTrue(result["medit_slot_restored"])
        self.assertEqual(result["medit_active_slot"]["state"], "restored")
        self.assertIsNone(result["medit_active_slot"]["switched_to"])
        self.assertEqual(result["medit_slot"], 5)
        self.assertEqual(result["primary_material"]["medit_slot"], 5)
        self.assertEqual(self.slots(result), {"2": 13, "77": 5})

    def test_no_free_slot_displaced_material_not_restorable_is_warned(self):
        fin = slot_fin(switched_to=0, displaced=USER_MAT, occupant=IMPORTED, fresh=True, kept_alive=True,
                       active_restored=None)
        self.use(slot_prep(free_slot=0, switched=False), fin, imported_slot=13)
        result = self.run_import()
        self.assertTrue(result["safe_to_edit"])
        self.assertFalse(result["medit_slot_restored"])
        self.assertEqual(result["medit_active_slot"]["state"], "displaced")
        self.assertEqual(result["medit_slot"], 13)
        [warning] = result["warnings"]
        for needle in ("'Steel_Blurry'", "slot 13", "'Steel_Polished #0'", "no free (unused default) slot",
                       "mcp_cosmosMeditDisplaced", "meditMaterials[13] = getAnimByHandle 2L"):
            self.assertIn(needle, warning)

    def test_switched_but_displaced_anyway_uses_the_free_slot(self):
        fin = slot_fin(displaced=USER_MAT, occupant=IMPORTED, fresh=True, moved_to=4, restored=True)
        self.use(slot_prep(), fin, imported_slot=13)
        result = self.run_import()
        self.assertNotIn("warnings", result)
        self.assertTrue(result["medit_slot_restored"])
        self.assertEqual(self.slots(result), {"2": 13, "77": 4})
        self.assertTrue(result["medit_active_slot"]["active_restored"])

    def test_active_slot_not_restored_is_warned(self):
        self.use(slot_prep(), slot_fin(active_restored=False, error="boom"), imported_slot=4)
        result = self.run_import()
        [warning] = result["warnings"]
        self.assertIn("activeMeditSlot = 13", warning)
        self.assertIn("boom", warning)

    def test_active_slot_already_free_no_switch(self):
        self.use(slot_prep(keep=False, free_slot=0, switched=False), None, imported_slot=13)
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertNotIn("warnings", result)
        self.assertEqual(result["medit_active_slot"]["state"], "not_needed")
        self.assertIsNone(result["medit_active_slot"]["switched_to"])
        self.assertNotIn("displaced_material", result)
        self.assertEqual(result["medit_slot"], 13)

    def test_settling_keeps_slot_restore_pending(self):
        self.use(slot_prep(), slot_fin(), imported_slot=4)
        self.settle = settle_result(False, hung_browser=True)
        result = self.run_import()
        self.assertEqual(result["state"], "settling")
        self.assertEqual(result["medit_active_slot"]["state"], "pending")
        self.assertIn("Selection and Material Editor slot not restored yet", " ".join(result["warnings"]))
        self.assertIn("activeMeditSlot=a", result["pending_restore"]["maxscript"])
        end = self.index(("settle_end",))
        self.assertFalse([e for e in self.events[end:] if e[0] == "bridge"])

    def test_stale_record_and_prepare_error_warned(self):
        self.use(slot_prep(keep=False, free_slot=0, switched=False, stale=True, error="no medit"), None, 13)
        result = self.run_import()
        text = " ".join(result["warnings"])
        self.assertIn("never finalized", text)
        self.assertIn("no medit", text)

    def test_finalize_slot_error_warns_with_active_slot_script(self):
        self.use(slot_prep(), {"error": "bad"}, imported_slot=4)
        result = self.run_import()
        self.assertEqual(result["medit_active_slot"]["state"], "error")
        self.assertIn("activeMeditSlot = 13", result["warnings"][0])

    def test_prepare_lost_warns_with_active_slot_undo(self):
        self.flow["prepare"] = MaxNotRespondingAfterDispatch(
            "3ds Max (PID 4242) is not responding", {"process": {"state": "blocked"}, "request_sent": True})
        result = self.run_import()
        self.assertIn(cosmos._SLOT_ACTIVE_RESTORE, result["warnings"][0])
        self.assertNotIn(("dispatch",), self.events)

    def test_finalize_in_undo_transaction_keeps_slot_restore_pending(self):
        self.use(slot_prep(), {"busy": True}, imported_slot=4)
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertEqual(result["medit_active_slot"]["state"], "pending")
        self.assertIn("activeMeditSlot=a", result["pending_restore"]["maxscript"])
        [warning] = result["warnings"]
        self.assertIn("undo transaction", warning)
        self.assertIn("pending_restore.maxscript", warning)
        self.assertNotIn("displaced_material", result)

    def test_stale_finalize_record_leaves_slots_as_they_are(self):
        self.use(slot_prep(), {"stale": True, "original": 13, "material": USER_MAT}, imported_slot=4)
        result = self.run_import()
        self.assertEqual(result["medit_active_slot"]["state"], "stale")
        [warning] = result["warnings"]
        self.assertIn("scene changed", warning)
        self.assertIn("'Steel_Blurry' is kept alive", warning)
        self.assertNotIn("getAnimByHandle", warning)
        self.assertNotIn("displaced_material", result)
        self.assertNotIn("pending_restore", result)

    def test_displaced_without_handle_has_no_broken_snippet(self):
        fin = slot_fin(switched_to=0, displaced={"handle": "", "name": ""}, occupant=IMPORTED, fresh=True,
                       kept_alive=True, active_restored=None)
        self.use(slot_prep(free_slot=0, switched=False), fin, imported_slot=13)
        [warning] = self.run_import()["warnings"]
        self.assertNotIn("getAnimByHandle", warning)
        self.assertIn("meditMaterials[13] = <its entry in mcp_cosmosMeditDisplaced>", warning)

    def test_slot_scripts_shape(self):
        asset = {"id": ASSET_ID, "name": "Plaster White", "kind": "material"}
        prepare, finalize = cosmos._prepare_script(asset, False), cosmos._finalize_script([5], False)
        for script in (prepare, finalize, cosmos._SLOT_ACTIVE_RESTORE):
            self.assertIsNone(CLOSE_OR_OPEN.search(script))
            self.assertNotIn("mateditor.close", script.lower())
            self.assertNotIn("__", script.replace("__KEY__", ""))
            self.assertEqual(script.count("("), script.count(")"))
        for block in (cosmos._SLOT_FNS, cosmos._SLOT_PREPARE, cosmos._SLOT_RESTORE):
            self.assertNotIn("SME.", block)  # activeMeditSlot only; Slate is left alone
        # Every slot change is inside try/catch; neither block can throw out of its script.
        self.assertTrue(cosmos._SLOT_PREPARE.startswith("try("))
        self.assertTrue(cosmos._SLOT_PREPARE.endswith(")catch(slotErr=getCurrentException())"))
        restore = cosmos._SLOT_RESTORE
        self.assertLess(restore.index("try("), restore.index("meditMaterials[movedTo]=cur"))
        self.assertIn(")catch(out=", restore)
        self.assertLess(restore.index("if f>0 do try("), restore.index("activeMeditSlot=a"))
        self.assertIn("catch(ok=false)", cosmos._SLOT_FNS)
        self.assertIn('pattern:"?? - Default"', cosmos._SLOT_FNS)
        self.assertIn("refs.dependentNodes m", cosmos._SLOT_FNS)
        # The switch is recorded before it is made, so finalize can undo a partial switch.
        self.assertLess(prepare.index("mcp_cosmosMeditSlot[3]=slotFree"), prepare.index("activeMeditSlot=slotFree"))
        self.assertLess(finalize.index("select (for h"), finalize.index("meditMaterials[a]=m"))
        self.assertIn("displaced and (false)", finalize)  # nothing recorded: no occupant counts as the import
        self.assertNotIn(">(-1L)", finalize)
        self.assertIn("displaced and (try(((getHandleByAnim cur) as integer64)>(3L))catch(false))",
                      cosmos._finalize_script([], False, 3))
        # Free means pristine: every property equals a new instance of its class.
        self.assertIn("local d=(classof m)()", cosmos._SLOT_FNS)
        self.assertIn("for p in (getPropNames m) while ok do", cosmos._SLOT_FNS)
        # Finalize leaves slots alone during an undo transaction or after a scene change.
        self.assertIn("mcp_cosmosMeditSlot=#(slotA, slotMat, 0, maxFilePath+maxFileName)", prepare)
        self.assertLess(restore.index("theHold.Holding()"), restore.index("meditMaterials[movedTo]=cur"))
        self.assertLess(restore.index("isDeleted b[2]"), restore.index("meditMaterials[movedTo]=cur"))
        self.assertLess(restore.index("b[4]!=(maxFilePath+maxFileName)"), restore.index("local a=b[1]"))
        self.assertEqual(cosmos._newest_known(KNOWN_BEFORE), 3)
        self.assertEqual(cosmos._newest_known(BEFORE), -1)
        self.assertIn(">(3000000000L)", cosmos._finalize_script([], False, 3000000000))


class BrowserEnsureTests(_FlowCase):
    """Main-thread Cosmos browser before _PREPARE: the decision table."""

    def test_main_thread_browser_needs_no_bridge_call(self):
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertNotIn("browser", self.bridge_kinds())
        self.assertEqual(self.bridge_kinds("operation")[0], "prepare")
        browser = result["cosmos_browser"]
        self.assertEqual((browser["ensured"], browser["opened"], browser["main_thread"]), (True, False, MAIN_TID))
        self.assertEqual(browser["windows"], [{"thread": MAIN_TID, "main_thread": True, "visible": False,
                                               "hung": False}])
        self.assertIsNone(browser["warning"])
        self.assertEqual(result["medit_renderer"]["swap"], "off")

    def test_hung_non_main_browser_refused_without_action_or_dispatch(self):
        self.threads[BROWSER_HWND], self.hung_hwnds = OTHER_TID, {BROWSER_HWND}
        result = self.run_import()
        self.assertEqual(result["state"], "browser_hung")
        self.assertEqual(result["code"], "IMPORT_SETTLING")
        self.assertFalse(result["safe_to_edit"])
        self.assertFalse(result["dispatched"])
        self.assertTrue(result["retryable"])
        self.assertIn("(hidden) on a separate thread %d hung" % OTHER_TID, result["evidence"])
        self.assertIn("~10 min", result["next"])
        self.assertIn("deadlock", result["next"])
        self.assertEqual(result["cosmos_browser"]["windows"][0]["main_thread"], False)
        self.assertIn("nothing was sent", result["next"])
        self.assertFalse(self.bridge_kinds("operation"))  # no browser action, no prepare
        self.assertNotIn(("dispatch",), self.events)
        guard = max_client.settling_state(PID)  # other clients refused while it stays hung
        self.assertEqual(guard["windows"], {BROWSER_HWND: process_health.COSMOS_BROWSER_TITLE, MAIN_HWND: ""})
        self.assertIsNone(guard["owner"])
        self.assertTrue(cosmos._import_lock.acquire(blocking=False))
        cosmos._import_lock.release()

    def test_wm_null_failure_alone_counts_as_hung(self):
        self.threads[BROWSER_HWND] = OTHER_TID
        self.wm_null = [False]  # IsHungAppWindow not yet true (~5 s lag)
        result = self.run_import()
        self.assertEqual(result["state"], "browser_hung")
        self.assertFalse(self.bridge_kinds("operation"))

    def test_no_browser_runs_action_then_waits_before_prepare(self):
        self.browsers = []
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertEqual(self.bridge_kinds("operation")[:2], ["browser", "prepare"])
        browser_call = self.index(("bridge", "operation", "browser"))
        checks = [e for e in self.events[browser_call:self.index(("bridge", "operation", "prepare"))]
                  if e[0] == "wm_null" and e[1] == NEW_BROWSER_HWND]
        self.assertGreaterEqual(len(checks), cosmos._BROWSER_CHECKS)  # OS-level wait, no bridge traffic
        self.assertLess(self.index(("bridge", "operation", "prepare")), self.index(("dispatch",)))
        browser = result["cosmos_browser"]
        self.assertEqual((browser["ensured"], browser["opened"]), (True, True))
        self.assertEqual(browser["action"]["table"], "V-Ray")
        self.assertEqual(result["medit_renderer"]["swap"], "off")
        self.assertNotIn("warnings", result)
        prepare = next(c[1] for c in self.op_client.commands if c[0] == "prepare")
        self.assertNotIn("renderers.medit=", prepare)

    def test_action_not_found_falls_back_to_swap_with_warning(self):
        self.browsers = []
        self.browser_action = {"found": False, "table": "", "description": "", "executed": False, "error": ""}
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        browser = result["cosmos_browser"]
        self.assertEqual((browser["ensured"], browser["opened"]), (False, False))
        self.assertIn("no Cosmos browser action found", result["warnings"][0])
        self.assertIn("Scanline", result["warnings"][0])
        self.assertEqual(browser["warning"], result["warnings"][0])
        self.assertEqual(result["medit_renderer"]["swap"], "fallback")
        prepare = next(c[1] for c in self.op_client.commands if c[0] == "prepare")
        self.assertIn("renderers.medit=sl", prepare)
        self.assertIn("if true then", self.op_client.commands[-1][1])  # restored afterwards

    def test_window_on_other_thread_is_not_ensured(self):
        self.browsers = []
        self.browser_appears = OTHER_TID
        started = self.clock.now
        result = self.run_import()
        self.assertFalse(result["cosmos_browser"]["ensured"])
        self.assertTrue(result["cosmos_browser"]["opened"])
        self.assertIn("within 15 s", result["warnings"][0])
        self.assertEqual(result["medit_renderer"]["swap"], "fallback")
        self.assertGreaterEqual(self.clock.now - started, cosmos._BROWSER_WAIT_S)

    def test_only_responsive_non_main_browser_runs_action_and_warns(self):
        self.threads[BROWSER_HWND] = OTHER_TID
        result = self.run_import()
        self.assertEqual(self.bridge_kinds("operation")[:2], ["browser", "prepare"])
        self.assertTrue(result["cosmos_browser"]["ensured"])
        self.assertIn("separate (non-main) thread", result["warnings"][0])
        self.assertEqual(result["medit_renderer"]["swap"], "off")

    def test_browser_call_lost_returns_structured_result_without_dispatch(self):
        self.browsers = []
        self.flow["browser"] = MaxNotRespondingAfterDispatch(
            "3ds Max (PID 4242) is not responding", {"process": {"state": "blocked"}, "request_sent": True})
        result = self.run_import()
        self.assertEqual(result["state"], "not_imported")
        self.assertFalse(result["safe_to_edit"])
        self.assertIn("browser action may have run", result["warnings"][0])
        self.assertNotIn("pending_restore", result)
        self.assertFalse(result["cosmos_browser"]["ensured"])
        self.assertEqual(self.bridge_kinds("operation"), ["browser"])
        self.assertNotIn(("dispatch",), self.events)

    def test_swap_default_off_leaves_medit_untouched(self):
        self.flow["prepare"] = prepared(swapped=False, backup_pending=False)
        self.flow["finalize"] = {"selection_restored": True, "medit": "not_requested", "editor_open": False}
        result = self.run_import()
        prepare, finalize = self.op_client.commands[0][1], self.op_client.commands[-1][1]
        for needle in ("renderers.medit=", "renderers.medit_locked=", "mcp_cosmosMeditBackup=", "scanline()"):
            self.assertNotIn(needle, prepare)
        self.assertIn("if false then", finalize)  # no restore
        self.assertNotIn("warnings", result)
        self.assertNotIn("pending_restore", result)

    def test_prepare_lost_without_swap_has_no_medit_restore(self):
        self.flow["prepare"] = MaxNotRespondingAfterDispatch(
            "not responding", {"process": {"state": "blocked"}, "request_sent": True})
        result = self.run_import()
        self.assertNotIn("pending_restore", result)
        self.assertNotIn("Scanline", result["warnings"][0])
        self.assertTrue(result["cosmos_browser"]["ensured"])


    def test_busy_main_thread_browser_waits_without_action_or_refusal(self):
        self.wm_null = [False]  # main-thread browser misses one WM_NULL: Max's main thread is busy
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertEqual(result["import_timing"]["pre_dispatch"]["browser_hung"], 0)
        self.assertNotIn("browser", self.bridge_kinds())
        self.assertTrue(result["cosmos_browser"]["ensured"])
        self.assertEqual(result["medit_renderer"]["swap"], "off")
        self.assertNotIn("warnings", result)

    def test_browser_hung_after_action_refused_before_prepare(self):
        self.threads[BROWSER_HWND] = OTHER_TID  # responsive non-main browser, so the action runs
        self.browser_appears = None
        self.hung_after_action = {BROWSER_HWND}
        result = self.run_import()
        self.assertEqual(result["state"], "browser_hung")
        self.assertFalse(result["dispatched"])
        self.assertFalse(result["safe_to_edit"])
        self.assertIn("browser action was sent", result["next"])
        self.assertEqual(self.bridge_kinds("operation"), ["browser"])  # no prepare
        self.assertNotIn(("dispatch",), self.events)
        guard = max_client.settling_state(PID)
        self.assertEqual(guard["windows"], {BROWSER_HWND: process_health.COSMOS_BROWSER_TITLE, MAIN_HWND: ""})
        self.assertIsNone(guard["owner"])

    def test_new_browser_hung_on_other_thread_refused_before_prepare(self):
        self.browsers = []
        self.browser_appears = OTHER_TID
        self.hung_after_action = {NEW_BROWSER_HWND}
        result = self.run_import()
        self.assertEqual(result["state"], "browser_hung")
        self.assertEqual(self.bridge_kinds("operation"), ["browser"])
        self.assertNotIn(("dispatch",), self.events)
        self.assertIn(NEW_BROWSER_HWND, max_client.settling_state(PID)["windows"])

    def test_main_window_busy_after_action_refused_before_prepare(self):
        self.browsers = []
        self.hung = [False] + [True] * 100  # pre-dispatch check fine, then hung through the gate
        result = self.run_import()
        self.assertEqual(result["state"], "not_imported")
        self.assertEqual(result["code"], "IMPORT_SETTLING")
        self.assertFalse(result["safe_to_edit"])
        self.assertIn("main window does not respond", result["evidence"])
        self.assertIn("~10 min", result["next"])
        self.assertEqual(self.bridge_kinds("operation"), ["browser"])
        self.assertNotIn(("dispatch",), self.events)
        self.assertEqual(max_client.settling_state(PID)["windows"], {MAIN_HWND: ""})

    def test_main_window_briefly_busy_at_gate_then_imports(self):
        self.browsers = []
        self.hung = [False, True, True]
        result = self.run_import()
        self.assertEqual(result["state"], "imported")
        self.assertEqual(self.bridge_kinds("operation")[:2], ["browser", "prepare"])

    def test_fresh_check_before_action_refuses_hung_browser(self):
        hung = {"hwnd": BROWSER_HWND, "thread": OTHER_TID, "main_thread": False, "visible": False, "hung": True}
        states = [{"main_threads": [MAIN_TID], "windows": []}, {"main_threads": [MAIN_TID], "windows": [hung]}]
        with mock.patch.object(cosmos, "_browser_state", side_effect=lambda pid: states.pop(0)):
            result = self.run_import()
        self.assertEqual(result["state"], "browser_hung")
        self.assertIn("nothing was sent", result["next"])
        self.assertFalse(self.bridge_kinds("operation"))  # no browser action, no prepare
        self.assertNotIn(("dispatch",), self.events)
        self.assertIn(BROWSER_HWND, max_client.settling_state(PID)["windows"])

    def test_found_but_not_executed_still_waits(self):
        self.browsers = []
        self.browser_action = dict(self.browser_action, executed=False)
        result = self.run_import()
        self.assertTrue(result["cosmos_browser"]["ensured"])
        self.assertFalse(result["cosmos_browser"]["opened"])
        self.assertEqual(result["medit_renderer"]["swap"], "off")

    def test_found_but_not_executed_without_window_falls_back(self):
        self.browsers = []
        self.browser_action = dict(self.browser_action, executed=False)
        self.browser_appears = None
        result = self.run_import()
        self.assertIn("'Chaos Cosmos browser' in table 'V-Ray' reported not executed", result["warnings"][0])
        self.assertEqual(result["medit_renderer"]["swap"], "fallback")

    def test_transport_error_on_browser_call_propagates(self):
        self.browsers = []
        self.flow["browser"] = TimeoutError("3ds Max did not respond within 30s")
        with self.assertRaises(TimeoutError):
            self.run_import()
        self.assertNotIn("prepare", self.bridge_kinds())
        self.assertNotIn(("dispatch",), self.events)
        self.assertIsNone(max_client.settling_state(PID))

    def test_main_and_non_main_browser_warns_without_action(self):
        self.browsers = [BROWSER_HWND, NEW_BROWSER_HWND]
        self.threads[NEW_BROWSER_HWND] = OTHER_TID
        result = self.run_import()
        self.assertNotIn("browser", self.bridge_kinds())
        self.assertTrue(result["cosmos_browser"]["ensured"])
        self.assertIn("separate (non-main) thread", result["warnings"][0])
        self.assertEqual(result["medit_renderer"]["swap"], "off")

    def test_leftover_backup_warns_without_restoring(self):
        self.flow["prepare"] = prepared(swapped=False, backup_pending=False, leftover_backup=True)
        result = self.run_import()
        self.assertIn("earlier import left", result["warnings"][0])
        self.assertIn(cosmos._MEDIT_RESTORE, result["warnings"][0])
        self.assertIn("if false then", self.op_client.commands[-1][1])


class BrowserActionScriptTests(unittest.TestCase):
    def test_searches_by_description_without_fixed_indices(self):
        for renderer, table in (("V-Ray", '"*v-ray*"'), ("Corona", '"*corona*"')):
            script = cosmos._open_browser_script(renderer)
            self.assertIn(table, script)
            self.assertIn('pattern:"*cosmos browser*"', script)
            self.assertIn("actionMan.executeAction tid (aid as string)", script)
            # getActionTable/getActionItem take 1-based <index> arguments.
            self.assertIn("for i=1 to actionMan.numActionTables while not found", script)
            self.assertIn("for j=1 to (try(t.numActionItems)catch(0)) while not found", script)
            self.assertIsNone(re.search(r"for [ij]=0 ", script))
            self.assertIsNone(re.search(r"getAction(Table|Item)\s+\d", script))
            self.assertIsNone(re.search(r"executeAction\s+-?\d", script))
            self.assertIsNone(CLOSE_OR_OPEN.search(script))
            self.assertNotIn("SME.", script)
            self.assertNotIn("__", script)
            self.assertEqual(script.count("("), script.count(")"))
            self.assertIn("catch(err=getCurrentException())", script)
        self.assertNotIn("v-ray", cosmos._open_browser_script("Corona"))

    def test_prepare_swap_block_only_when_requested(self):
        asset = {"id": ASSET_ID, "name": "Plaster White", "kind": "material"}
        on, off = cosmos._prepare_script(asset, True), cosmos._prepare_script(asset, False)
        self.assertIn(cosmos._MEDIT_SWAP, on)
        self.assertNotIn("renderers.medit=", off)
        self.assertIn(cosmos._MEDIT_LEFTOVER, off)  # read-only check for an earlier swap's backup
        self.assertNotIn(cosmos._MEDIT_LEFTOVER, on)
        self.assertNotIn("__", off.replace("__KEY__", ""))
        for script in (on, off):
            self.assertEqual(script.count("("), script.count(")"))


class HangWordingTests(unittest.TestCase):
    def test_bounded_advice_with_deadlock_caveat(self):
        advice = max_client.HANG_ADVICE
        for needle in ("5-8 min", "never did", "~10 min", "get_bridge_status", "deadlock", "end the process"):
            self.assertIn(needle, advice)
        self.assertIn("possibly from another MCP client", max_client.BLOCKED_CAUSE)
        self.assertIn(advice, cosmos._WAIT_ADVICE)
        for module in (max_client, cosmos):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("on their own", source)
            self.assertNotIn("(deadlock or stalled I/O)", source)
            self.assertNotIn("Do not end Max", source)


class MaxScriptShapeTests(unittest.TestCase):
    def test_slate_counts_as_open_everywhere(self):
        self.assertIn("SME.isOpen()", cosmos._MEDIT_RESTORE)
        self.assertIn("SME.isOpen()", cosmos._finalize_script([1], True))
        asset = {"id": ASSET_ID, "name": "Plaster White", "kind": "material"}
        self.assertIn("SME.isOpen()", cosmos._prepare_script(asset))
        self.assertNotIn("__EDITOR_OPEN__", cosmos._finalize_script([1], True) + cosmos._prepare_script(asset))

    def test_restore_checks_identity_first_and_relocks_without_assigning(self):
        restore = cosmos._MEDIT_RESTORE
        self.assertLess(restore.index("b[3]==renderers.medit"), restore.index("MatEditor.isOpen()"))
        self.assertIn('"stale"', restore)
        self.assertIn("if b[2]==true then renderers.medit_locked=true else (if b[1]!=undefined do "
                      "renderers.medit=b[1]", restore)
        self.assertEqual(restore.count("("), restore.count(")"))


class WaitResponsiveSpanTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock([])
        self.cpu = iter([float(i) for i in range(0, 400, 2)])
        for attr, value in (
            ("_IS_WINDOWS", True), ("time", self.clock),
            ("main_windows", lambda pid: [MAIN_HWND]),
            ("find_windows", lambda pid, title: []),
            ("window_responsive", lambda hwnd, timeout_ms=500: {"hwnd": hwnd, "exists": True, "hung": False,
                                                                 "responds": True, "title": "t"}),
            ("_open_process", lambda pid: ("h", 0)),
            ("_exit_status", lambda handle: (True, None)),
            ("_cpu_seconds", lambda handle: next(self.cpu)),
            ("_kernel32", mock.Mock()),
        ):
            p = mock.patch.object(process_health, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_quiet_needs_the_minimum_span(self):
        self.cpu = iter([0.0] * 100)
        result = process_health.wait_responsive(PID, 30, checks=3, interval=1.0, min_span_s=6.0)
        self.assertTrue(result["quiet"])
        self.assertGreaterEqual(self.clock.now - 1000.0, 6.0)
        self.assertEqual(result["checks"], 7)

    def test_busy_cpu_stops_after_grace_once_windows_answer(self):
        result = process_health.wait_responsive(PID, 90, checks=3, interval=1.0, cpu_threshold=0.5,
                                                min_span_s=6.0, cpu_grace_s=10.0)
        self.assertFalse(result["quiet"])
        self.assertTrue(result["windows_quiet"])
        self.assertLess(self.clock.now - 1000.0, 20.0)  # not the full 90 s


class GuardTests(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in ("MCP_MAX_PIPE", "MCP_MAX_PID")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock([])
        self.hung = {MAIN_HWND: False, BROWSER_HWND: True}
        for attr, value in (("time", self.clock), ("process_start_time", mock.Mock(return_value=1)),
                            ("window_hung", lambda hwnd: self.hung.get(hwnd)),
                            ("quick_hung_check", mock.Mock(return_value=None))):
            p = mock.patch.object(max_client, attr, value)
            p.start()
            self.addCleanup(p.stop)
        max_client._settling.clear()
        self.addCleanup(max_client._settling.clear)
        self.client = MaxClient(transport="pipe", pipe_name=PIPE)
        self.opened = mock.Mock(side_effect=ConnectionError("sentinel: would connect"))
        p = mock.patch.object(self.client, "_ensure_pipe_handle", self.opened)
        p.start()
        self.addCleanup(p.stop)
        max_client.mark_settling(PID, {MAIN_HWND: "", BROWSER_HWND: process_health.COSMOS_BROWSER_TITLE},
                                 "a Cosmos import", "evidence", 900)

    def test_refuses_without_sending_then_clears_when_window_recovers(self):
        for cmd_type in ("maxscript", "ping"):
            with self.assertRaises(MaxImportSettlingError) as ctx:
                self.client.send_command("selection.count", cmd_type=cmd_type)
            self.assertFalse(self.opened.called)
        exc = ctx.exception
        self.assertEqual(exc.code, "IMPORT_SETTLING")
        self.assertTrue(exc.retryable)
        self.assertFalse(exc.details["request_sent"])
        self.assertIn("'Chaos Cosmos Browser' not responding", str(exc))
        self.assertIn("5-8 min", str(exc))
        self.assertIn("~10 min", str(exc))
        self.assertNotIn("Do not end Max", str(exc))
        envelope = tool_response._error_from_exception(exc)
        self.assertEqual(envelope["code"], "IMPORT_SETTLING")
        self.assertTrue(envelope["retryable"])
        self.hung[BROWSER_HWND] = False
        with self.assertRaisesRegex(ConnectionError, "sentinel"):
            self.client.send_command("selection.count")
        self.assertTrue(self.opened.called)
        self.assertIsNone(max_client.settling_state(PID))

    def test_owner_exempt_others_refused_while_in_progress(self):
        max_client._settling.clear()
        owner = MaxClient(transport="pipe", pipe_name=PIPE)
        entry = max_client.mark_settling(PID, {}, "a Cosmos import", None, 900, owner=owner)
        self.hung = {}
        owner._check_settling(PID, PIPE)  # exempt: no raise
        with self.assertRaises(MaxImportSettlingError) as ctx:
            self.client.send_command("selection.count")
        self.assertFalse(self.opened.called)
        self.assertIn("expires_in_s", ctx.exception.details["settling"])
        max_client.release_settling(PID, {"other": "entry"})  # not ours: kept
        self.assertIsNotNone(max_client.settling_state(PID))
        max_client.release_settling(PID, entry)
        with self.assertRaisesRegex(ConnectionError, "sentinel"):
            self.client.send_command("selection.count")

    def test_destroyed_windows_clear_the_guard(self):
        self.hung = {}
        with self.assertRaisesRegex(ConnectionError, "sentinel"):
            self.client.send_command("selection.count")
        self.assertIsNone(max_client.settling_state(PID))

    def test_guard_hard_expires(self):
        with self.assertRaises(MaxImportSettlingError):
            self.client.send_command("selection.count")
        self.clock.now += 901
        with self.assertRaisesRegex(ConnectionError, "sentinel"):
            self.client.send_command("selection.count")
        self.assertIsNone(max_client.settling_state(PID))

    def test_new_process_with_same_pid_clears_the_guard(self):
        max_client.process_start_time.return_value = 2
        with self.assertRaisesRegex(ConnectionError, "sentinel"):
            self.client.send_command("selection.count")

    def test_other_pid_and_control_channel_unaffected(self):
        other = MaxClient(transport="pipe", pipe_name=r"\\.\pipe\3dsmax-mcp-pid-77")
        with mock.patch.object(other, "_ensure_pipe_handle", self.opened):
            with self.assertRaisesRegex(ConnectionError, "sentinel"):
                other.send_command("selection.count")
        control = MaxClient(transport="pipe", pipe_name=PIPE)
        control._control_channel = True
        with mock.patch.object(control, "_ensure_pipe_handle", self.opened):
            with self.assertRaisesRegex(ConnectionError, "sentinel"):
                control.send_command("", cmd_type="native:capture_screen")


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.service = FakeService(self.events, [VRAY, OTHER])
        p = mock.patch.object(cosmos, "Cosmos", lambda timeout=30: self.service)
        p.start()
        self.addCleanup(p.stop)
        self.client = FakeClient("global", self.events, {"renderer": {"renderer": "Corona"}})

    def test_one_matching_importer_needs_no_bridge_call(self):
        result = cosmos.search(self.client, "oak", "material", False, 10, 0, "current")
        self.assertEqual(result["renderer"], "V-Ray")
        self.assertEqual(result["max_pid"], PID)
        self.assertEqual(result["renderer_source"], "only_importer")
        self.assertEqual(self.events, [("search", "imp-vray")])
        self.assertFalse(self.client.commands)

    def test_explicit_renderer_picks_among_several_without_max(self):
        self.service._importers = [VRAY, CORONA]
        result = cosmos.search(self.client, "", "all", False, 10, 0, "corona")
        self.assertEqual(result["renderer"], "Corona")
        self.assertEqual(result["renderer_source"], "explicit")
        self.assertFalse(self.client.commands)

    def test_ambiguous_current_falls_back_to_asking_max(self):
        self.service._importers = [VRAY, CORONA]
        result = cosmos.search(self.client, "", "all", False, 10, 0, "current")
        self.assertEqual(result["renderer"], "Corona")
        self.assertEqual(result["renderer_source"], "scene")
        self.assertEqual([c[0] for c in self.client.commands], ["renderer"])

    def test_unavailable_target_falls_back(self):
        self.client.resolve_target = lambda: {"target_pid": None, "available": False}
        self.client.flow["renderer"] = {"renderer": "V_Ray_7"}
        cosmos.search(self.client, "", "all", False, 10, 0, "current")
        self.assertEqual([c[0] for c in self.client.commands], ["renderer"])


class ResolveTargetTests(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in ("MCP_MAX_PIPE", "MCP_MAX_PID")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_does_not_bind_lock_or_send(self):
        client = MaxClient(transport="pipe", pipe_name=PIPE)
        client._pipe_lock = mock.Mock(wraps=client._pipe_lock)
        with mock.patch.object(client, "_probe_pipe_available", return_value=True), \
                mock.patch.object(client, "send_command", side_effect=AssertionError("sent")):
            target = client.resolve_target()
        self.assertEqual(target["target_pid"], PID)
        self.assertTrue(target["available"])
        self.assertIsNone(client._bound_target)
        self.assertIsNone(getattr(client._local, "route_candidate", None))
        self.assertFalse(client._pipe_lock.acquire.called)

    def test_default_target_when_nothing_selected(self):
        client = MaxClient(transport="pipe")
        default = {"target_pid": 9, "target_pipe": r"\\.\pipe\3dsmax-mcp-pid-9", "target_source": "default",
                   "pinned": True}
        with mock.patch.object(client, "_default_target", return_value=default), \
                mock.patch.object(client, "_probe_pipe_available", return_value=False):
            target = client.resolve_target()
        self.assertEqual(target["target_pid"], 9)
        self.assertFalse(target["available"])
        self.assertIsNone(client._bound_target)


class WaitResponsiveUnitTests(unittest.TestCase):
    """wait_responsive logic with fake windows (no real windows, no Max)."""

    def setUp(self):
        self.clock = Clock([])
        self.responsive = {MAIN_HWND: True, BROWSER_HWND: True}
        self.cpu = iter([])
        for attr, value in (
            ("_IS_WINDOWS", True), ("time", self.clock),
            ("main_windows", lambda pid: [MAIN_HWND]),
            ("find_windows", lambda pid, title: [BROWSER_HWND]),
            ("window_responsive", self._state),
            ("_open_process", lambda pid: ("h", 0)),
            ("_exit_status", lambda handle: (True, None)),
            ("_cpu_seconds", lambda handle: next(self.cpu)),
            ("_kernel32", mock.Mock()),
        ):
            p = mock.patch.object(process_health, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _state(self, hwnd, timeout_ms=500):
        ok = self.responsive[hwnd]
        return {"hwnd": hwnd, "exists": True, "hung": not ok, "responds": ok, "title": "t", "visible": False}

    def test_quiet_after_consecutive_checks(self):
        self.cpu = iter([0.0, 0.1, 0.2, 0.3, 0.4])
        result = process_health.wait_responsive(PID, 30, checks=3, interval=1.0, cpu_threshold=0.5)
        self.assertTrue(result["quiet"])
        self.assertEqual(result["checks"], 3)
        self.assertEqual(self.clock.sleeps, [1.0, 1.0])
        self.assertEqual(result["hwnds"], [MAIN_HWND, BROWSER_HWND])

    def test_hung_browser_never_quiet_and_bounded(self):
        self.responsive[BROWSER_HWND] = False
        self.cpu = iter([0.0] * 100)
        result = process_health.wait_responsive(PID, 5, checks=3, interval=1.0)
        self.assertFalse(result["quiet"])
        self.assertEqual(result["responsive_streak"], 0)
        self.assertLessEqual(self.clock.now - 1000.0, 5.0)
        self.assertIn("'t' (hidden) hung", process_health.describe_windows(result))

    def test_busy_cpu_blocks_quiet_but_counts_window_streak(self):
        self.cpu = iter([float(i) for i in range(0, 100, 2)])  # 2 cores
        result = process_health.wait_responsive(PID, 4, checks=3, interval=1.0, cpu_threshold=0.5)
        self.assertFalse(result["quiet"])
        self.assertGreaterEqual(result["responsive_streak"], 3)
        self.assertEqual(result["streak"], 0)

    def test_zero_seconds_checks_once(self):
        self.cpu = iter([0.0] * 10)
        result = process_health.wait_responsive(PID, 0, checks=3)
        self.assertEqual(result["checks"], 1)
        self.assertFalse(result["quiet"])
        self.assertFalse(self.clock.sleeps)


@unittest.skipUnless(sys.platform == "win32", "Windows window probes")
class WindowFinderTests(unittest.TestCase):
    """Real windows in child processes this test spawns (never a real Max)."""

    def spawn(self, title, *extra):
        child = subprocess.Popen([sys.executable, str(FAKE_WINDOW), "--title", title, *extra],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True)
        self.addCleanup(self._kill, child)
        line = child.stdout.readline().split()
        self.assertEqual(line[:1], ["ready"], "fake window did not start")
        return child, int(line[1]), int(line[2])

    @staticmethod
    def _kill(child):
        if child.poll() is None:
            child.kill()
        child.wait(10)
        child.stdout.close()

    def test_finds_hidden_window_by_title_and_reports_responsive(self):
        title = "MCP Test Cosmos Browser %d" % os.getpid()
        child, hwnd, _ = self.spawn(title, "--hidden", "--mode", "pump")
        self.assertEqual(process_health.find_windows(child.pid, title), [hwnd])
        self.assertEqual(process_health.find_windows(child.pid, title.upper()), [hwnd])
        self.assertEqual(process_health.find_windows(child.pid, "something else"), [])
        self.assertEqual(process_health.find_windows(os.getpid(), title), [])
        self.assertNotIn(hwnd, process_health.main_windows(child.pid))  # hidden: not a main window
        state = process_health.window_responsive(hwnd, 500)
        self.assertEqual((state["exists"], state["visible"], state["hung"], state["responds"]),
                         (True, False, False, True))
        self.assertEqual(state["title"], title)
        self.assertIs(process_health.window_hung(hwnd), False)

    def test_hidden_hung_window_is_reported_hung(self):
        title = "MCP Test Hung Browser %d" % os.getpid()
        child, hwnd, main = self.spawn(title, "--hidden", "--mode", "hang", "--main")
        self.assertEqual(process_health.find_windows(child.pid, title), [hwnd])
        self.assertIn(main, process_health.main_windows(child.pid))
        time.sleep(1.0)  # past the child's initial 0.5 s of pumping
        started = time.monotonic()
        quick = process_health.window_responsive(hwnd, 300)
        self.assertFalse(quick["responds"])  # WM_NULL catches it before IsHungAppWindow does
        while process_health.window_hung(hwnd) is not True and time.monotonic() - started < 15:
            time.sleep(0.25)
        self.assertIs(process_health.window_hung(hwnd), True)
        state = process_health.window_responsive(hwnd, 500)
        self.assertEqual((state["hung"], state["responds"]), (True, False))
        self.assertIs(process_health.window_hung(main), False)
        settle = process_health.wait_responsive(child.pid, 1.5, titles=(title,), checks=2, interval=0.5,
                                                timeout_ms=200)
        self.assertFalse(settle["quiet"])
        roles = {s["hwnd"]: (s["role"], s["hung"], s["responds"]) for s in settle["windows"]}
        self.assertEqual(roles[hwnd], ("titled", True, False))
        self.assertEqual(roles[main], ("main", False, True))
        self.assertIn("(hidden) hung", process_health.describe_windows(settle))
        child.kill()
        child.wait(10)
        deadline = time.monotonic() + 5
        while process_health.window_hung(hwnd) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNone(process_health.window_hung(hwnd))  # destroyed with its process

    def test_wait_responsive_quiet_on_pumping_child(self):
        title = "MCP Test Quiet Browser %d" % os.getpid()
        child, hwnd, main = self.spawn(title, "--hidden", "--mode", "pump", "--main")
        settle = process_health.wait_responsive(child.pid, 10, titles=(title,), checks=2, interval=0.3,
                                                timeout_ms=500)
        self.assertTrue(settle["quiet"], settle)
        self.assertEqual(sorted(settle["hwnds"]), sorted([hwnd, main]))

    def test_thread_windows_main_thread_browser(self):
        title = "MCP Test Main Browser %d" % os.getpid()
        child, hwnd, main = self.spawn(title, "--on-main", "--mode", "pump")
        state = process_health.thread_windows(child.pid, title, 500)
        tid = process_health.window_thread(main)
        self.assertTrue(tid)
        self.assertEqual(process_health.window_thread(hwnd), tid)
        self.assertEqual(state["main_threads"], [tid])
        self.assertEqual(state["windows"], [{"hwnd": hwnd, "thread": tid, "main_thread": True, "visible": True,
                                             "hung": False}])

    def test_thread_windows_visible_browser_on_other_thread(self):
        title = "MCP Test Other Browser %d" % os.getpid()
        child, hwnd, main = self.spawn(title, "--main", "--mode", "pump")
        self.assertIn(hwnd, process_health.main_windows(child.pid))  # visible+unowned, yet not the main thread
        state = process_health.thread_windows(child.pid, title, 500)
        self.assertEqual(state["main_threads"], [process_health.window_thread(main)])
        self.assertNotEqual(process_health.window_thread(hwnd), process_health.window_thread(main))
        self.assertEqual([(w["main_thread"], w["visible"], w["hung"]) for w in state["windows"]],
                         [(False, True, False)])

    def test_thread_windows_hidden_hung_browser_on_other_thread(self):
        title = "MCP Test Hung Other Browser %d" % os.getpid()
        child, hwnd, main = self.spawn(title, "--hidden", "--main", "--mode", "hang")
        time.sleep(1.0)  # past the child's initial 0.5 s of pumping; WM_NULL catches it before IsHungAppWindow
        state = process_health.thread_windows(child.pid, title, 300)
        self.assertEqual([(w["hwnd"], w["main_thread"], w["visible"], w["hung"]) for w in state["windows"]],
                         [(hwnd, False, False, True)])
        self.assertEqual(process_health.thread_windows(os.getpid(), title), {"main_threads": [], "windows": []})
        self.assertIsNone(process_health.window_thread(0))


if __name__ == "__main__":
    unittest.main()
