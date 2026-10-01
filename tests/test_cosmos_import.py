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
MAIN_HWND, BROWSER_HWND = 101, 202
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
    if "renderers.current" in command and "cosmosAssetId" not in command:
        return "renderer"
    if "mcp_cosmosMeditBackup=#(" in command:
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
                                                 "editor_open": False}}
        self.global_client = FakeClient("global", self.events, self.flow)
        self.op_client = FakeClient("operation", self.events, self.flow)
        self.service = FakeService(self.events, [VRAY, OTHER])
        self.hung = []  # quick_hung_check answers, then False
        self.settle = settle_result(True)
        self.mains, self.browsers, self.hung_hwnds = [], [], set()
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
        responds = self.wm_null.pop(0) if self.wm_null else True
        return {"hwnd": hwnd, "exists": True, "hung": False, "responds": responds}

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
        self.run_import()
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
        result = self.run_import()
        self.assertTrue(result["safe_to_edit"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("still Scanline", result["warnings"][0])
        self.assertIn(cosmos._MEDIT_RESTORE, result["warnings"][0])
        self.assertIn("if true then", self.op_client.commands[-1][1])

    def test_restore_medit_renderer_false(self):
        self.flow["finalize"] = {"selection_restored": True, "medit": "not_requested", "editor_open": False}
        result = self.run_import(restore_medit_renderer=False)
        self.assertIn("if false then", self.op_client.commands[-1][1])
        self.assertIn("restore_medit_renderer is false", result["warnings"][0])

    def test_scanline_unavailable_skips_swap_with_warning(self):
        self.flow["prepare"] = prepared(scanline_available=False, swapped=False, backup_pending=False)
        result = self.run_import()
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
        result = self.run_import()
        self.assertEqual(result["state"], "settling")
        self.assertFalse(result["safe_to_edit"])
        self.assertTrue(result["detected"])
        self.assertIn("IMPORT_SETTLING", result["next"])
        self.assertIn("5-8 min", result["next"])
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
        result = self.run_import()
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

    def test_hung_browser_before_dispatch_refuses_import(self):
        self.browsers, self.hung_hwnds = [BROWSER_HWND], {BROWSER_HWND}
        with self.assertRaises(CosmosError) as ctx:
            self.run_import()
        self.assertEqual(ctx.exception.code, "IMPORT_SETTLING")
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("Chaos Cosmos Browser", str(ctx.exception))
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
        self.mains = [MAIN_HWND]
        self.flow["prepare"] = MaxNotRespondingAfterDispatch(
            "3ds Max (PID 4242) is not responding", {"process": {"state": "blocked"}, "request_sent": True})
        result = self.run_import()
        self.assertEqual(result["state"], "not_imported")
        self.assertFalse(result["safe_to_edit"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["health"]["code"], "MAX_NOT_RESPONDING")
        self.assertEqual(result["pending_restore"]["maxscript"], cosmos._MEDIT_RESTORE)
        self.assertNotIn(("dispatch",), self.events)
        self.assertEqual(max_client.settling_state(PID)["windows"], {MAIN_HWND: ""})

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
        result = self.run_import()
        self.assertTrue(result["safe_to_edit"])
        self.assertIn("left as is", result["warnings"][0])


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


if __name__ == "__main__":
    unittest.main()
