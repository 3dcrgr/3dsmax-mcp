"""Tests for resting_check / drop_to_surface (fork issue #17)."""

import base64
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import resting as rs  # noqa: E402

tool = importlib.import_module("maxmcp.tools.resting")

UPM = 0.1  # centimetre scene: 1 mm = 0.1 units
TOL = 0.5 * UPM


def b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def unit_line(idx, handle, name, min_z, *, gap, hits=10, samples=12, not_movable="", node_z=None, ancestors="",
              meshes=None, kind="node", stride=1, classes="Editable_Poly", contact=None, group=None):
    g = "1e+30" if gap is None else repr(gap)
    node_z = min_z if node_z is None else node_z
    contact = (hits if gap is not None and abs(gap) <= TOL else 0) if contact is None else contact
    grp_h, grp_n = (str(group[0]), b64(group[1])) if group else ("0", "")
    point = "" if gap is None else "1,2,%r" % min_z
    return "|".join(["I", str(idx), str(handle), b64(name), kind, grp_h, grp_n, repr(min_z), "100", str(stride),
                     str(samples), str(hits if gap is not None else 0), g, g, g, str(contact), point, not_movable,
                     repr(node_z), ancestors, classes, ",".join(str(h) for h in (meshes or [handle]))])


def gap_line(idx, support, name, gap, hits=10):
    return "|".join(["G", str(idx), str(support), b64(name), repr(gap), str(hits), "1,2,3"])


def report(*records, unchecked=0, stop="none", auto_key="false", measured=None, scatter=0):
    n_units = sum(1 for r in records if r.startswith("I|")) + unchecked
    head = "|".join(["U", repr(TOL), repr(20 * UPM), repr(50 * UPM), "#centimeters", repr(UPM), str(n_units),
                     str(measured if measured is not None else n_units - unchecked), "1234", "20000", "5", stop,
                     "0", auto_key, "7", str(scatter)])
    return "\n".join([head, *records, "END"]) + "\n"


def settings(**kw):
    args = dict(names=["Plate01"], supports=None, exclude_supports=None, group_mode="members", tolerance_mm=0.5,
                max_drop_mm=50, max_sink_mm=20, samples=64, max_item_verts=100000, max_support_faces=1000000,
                time_budget_s=20, request_timeout_s=120)
    args.update(kw)
    return rs.validate(*args.values())


class ImportTargetTests(unittest.TestCase):
    def test_imports_checkout(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(Path(rs.__file__).resolve(), repo / "maxmcp" / "helpers" / "resting.py")
        self.assertEqual(Path(tool.__file__).resolve(), repo / "maxmcp" / "tools" / "resting.py")


class ValidationTests(unittest.TestCase):
    def test_defaults(self):
        s = settings()
        self.assertEqual((s["names"], s["rigid"], s["tolerance_mm"], s["samples"]), (["Plate01"], False, 0.5, 64))

    def test_rejections(self):
        bad = [dict(group_mode="flat"), dict(tolerance_mm=0), dict(tolerance_mm=True), dict(max_drop_mm=0.5),
               dict(max_sink_mm=0), dict(samples=2), dict(samples=2000), dict(max_item_verts=10),
               dict(time_budget_s=46), dict(names=[""]), dict(supports=["A"], exclude_supports=["B"]),
               dict(tolerance_mm=float("nan"))]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                settings(**kw)

    def test_string_name_and_dedup(self):
        self.assertEqual(settings(names="A")["names"], ["A"])
        self.assertEqual(settings(names=["A", "A", "B"])["names"], ["A", "B"])


class ScriptTests(unittest.TestCase):
    def test_measure_script(self):
        s = settings(names=['Plate "01"', "C:\\x"], group_mode="rigid")
        script = rs.build_measure_script(s, sent_ms=123)
        self.assertNotIn("@@", script)
        self.assertIn('#("Plate \\"01\\"", "C:\\\\x")', script)
        self.assertIn("local rigid = true", script)
        self.assertIn("123L", script)
        self.assertIn("rm.intersectSegment (q + [0, 0, lift]) (q - [0, 0, maxDrop]) true", script)
        self.assertIn("snapshotAsMesh nd renderMesh:false", script)
        self.assertIn('units.decodeValue "1mm"', script)
        self.assertIn("(0.500000000 as float) * upm", script)
        self.assertEqual(script.count("("), script.count(")"))

    def test_scripts_are_safe(self):
        file_commands = ("resetmaxfile", "loadmaxfile", "fetchmaxfile", "quitmax", "checkforsave", "max reset file",
                         "max file new", "max file open", "max fetch", "filereset", "filefetch", "loadfromfile")
        s = settings()
        apply = rs.build_apply_script(s, [{"handle": 5, "dz": -1.2, "z_before": 3.0, "z_after": 1.8}],
                                      [{"unit": 1, "meshes": [5], "supports": [9], "low": -0.05, "high": 0.07}],
                                      label="MCP drop to surface", eps=0.001, time_budget_s=10, sent_ms=1)
        for script in (rs.build_measure_script(s), apply):
            low = script.lower()
            for word in ("doscommand", "shelllaunch", "deletefile", "python.execute", "createfile",
                         "hiddendoscommand", "intersectray $", "meshprojintersect"):
                self.assertNotIn(word, low)
            self.assertFalse([w for w in file_commands if w in low])  # quiet mode stays on (#12)
        self.assertIn("#(#(5, -1.200000000, 3.000000000, 1.800000000))", apply)
        self.assertIn("#(#(1, #(5), #(9), -0.050000000, 0.070000000))", apply)
        self.assertIn('theHold.Accept "MCP drop to surface"', apply)
        self.assertIn("theHold.Cancel()", apply)
        self.assertIn("in coordsys world move nodesM[k] [0, 0, moves[k][2]]", apply)
        self.assertNotIn("@@", apply)

    def test_apply_needs_moves(self):
        with self.assertRaises(ValueError):
            rs.build_apply_script(settings(), [], [], label="x", eps=0.1, time_budget_s=5)


class ParseTests(unittest.TestCase):
    def test_parse_full_report(self):
        raw = report(
            "D|1|11|%s|11" % b64("Placemat"),
            "C|1|12|%s||0.75|3" % b64("Placemat_ENMESH"),
            "P|90|%s|Editable_Mesh|500" % b64("Table"),
            gap_line(1, 90, "Table", 0.3),
            unit_line(1, 11, "Placemat", 0.75, gap=0.3, group=(3, "GRP_Set")),
            "Q|91|%s|coincident|1" % b64("Placemat001"),
            "K|40|%s|not_mesh" % b64("Light01"),
            "R|1|50|%s" % b64("Fork"),
            scatter=2)
        r = rs.parse_measure(raw)
        u = r["units"][1]
        self.assertEqual((u["name"], u["group"]["name"], u["min_gap"], u["meshes"]), ("Placemat", "GRP_Set", 0.3, [11]))
        self.assertEqual((r["units_name"], r["upm"], r["scatter_excluded"], r["pool"]), ("#centimeters", UPM, 2, 7))
        self.assertEqual(r["gaps"][0]["support_name"], "Table")
        self.assertEqual(r["companions"][0]["name"], "Placemat_ENMESH")
        self.assertEqual(r["supports_used"][0]["name"], "Table")
        self.assertEqual(r["supports_skipped"][0]["reason"], "coincident")
        self.assertEqual(r["skipped"][0]["reason"], "not_mesh")
        self.assertEqual(r["dependents"][0]["name"], "Fork")
        self.assertEqual(r["declared"][1]["name"], "Placemat")
        self.assertEqual(rs.classify(u, r["tolerance"]), "floating")

    def test_malformed_reports(self):
        for raw in ("", "nonsense\nEND\n", report("I|1|2") , report("Z|1"), report().replace("END\n", ""),
                    report("Q|1|%s|weird|0" % b64("x"))):
            with self.subTest(raw=raw[:30]), self.assertRaises(RuntimeError):
                rs.parse_measure(raw)
        with self.assertRaises(RuntimeError) as ctx:
            rs.parse_measure("__ERROR__|-- Node name must resolve uniquely: Plate")
        self.assertIn("resolve uniquely", str(ctx.exception))

    def test_classify(self):
        cases = [(None, 0, "no_support"), (-0.2, 10, "penetrating"), (0.2, 10, "floating"), (0.01, 10, "resting"),
                 (-0.04, 10, "resting")]
        for gap, hits, status in cases:
            self.assertEqual(rs.classify({"min_gap": gap, "hits": hits}, TOL), status)


def stack_report(placemat_gap=0.5, plate_gap=0.0, glass_gap=0.2, plate_movable="", extra=()):
    """Table (static) <- placemat (unit 1) <- plate (unit 2) <- glass (unit 3)."""
    return rs.parse_measure(report(
        gap_line(1, 90, "Table", placemat_gap), unit_line(1, 11, "Placemat", 0.75, gap=placemat_gap),
        gap_line(2, 11, "Placemat", plate_gap), unit_line(2, 12, "Plate", 0.85, gap=plate_gap, not_movable=plate_movable),
        gap_line(3, 12, "Plate", glass_gap), unit_line(3, 13, "Glass", 1.2, gap=glass_gap),
        *extra))


class PlanTests(unittest.TestCase):
    def test_stack_follows_dropped_support(self):
        r = stack_report()
        plan = rs.plan_drops(r, clearance=0.02)
        self.assertAlmostEqual(plan[1]["own"], 0.02 - 0.5)        # placemat drops onto the table
        self.assertAlmostEqual(plan[2]["own"], 0.02 - 0.5)        # plate rested on it, follows it down
        self.assertAlmostEqual(plan[3]["own"], (0.02 - 0.5) + (0.02 - 0.2))  # glass follows and closes its own gap
        self.assertAlmostEqual(plan[2]["gap_now"], 0.48)  # the placemat moved away beneath it

    def test_unmovable_support_stays(self):
        r = stack_report(placemat_gap=0.0, plate_gap=0.4, plate_movable="frozen")
        plan = rs.plan_drops(r, clearance=0.0)
        self.assertEqual((plan[1]["own"], plan[2]["own"], plan[2]["reason"]), (0.0, 0.0, "frozen"))
        self.assertAlmostEqual(plan[3]["own"], -0.2)  # glass plans against the plate where it is

    def test_penetrating_only_lifted_on_request(self):
        r = stack_report(placemat_gap=-0.3, plate_gap=0.0, glass_gap=0.0)
        self.assertEqual(rs.plan_drops(r, clearance=0.0)[1]["reason"], "penetrating_not_lifted")
        lifted = rs.plan_drops(r, clearance=0.0, lift_penetrating=True)
        self.assertAlmostEqual(lifted[1]["own"], 0.3)
        self.assertAlmostEqual(lifted[2]["own"], 0.3)  # the plate follows its support up

    def test_child_inherits_parent_move(self):
        r = rs.parse_measure(report(
            gap_line(1, 90, "Table", 0.6), unit_line(1, 11, "Tray", 0.75, gap=0.6),
            gap_line(2, 11, "Tray", 0.0), unit_line(2, 12, "Cup", 0.85, gap=0.0, ancestors="11,3"),
        ))
        plan = rs.plan_drops(r, clearance=0.0)
        self.assertAlmostEqual(plan[2]["inherited"], -0.6)
        self.assertAlmostEqual(plan[2]["own"], 0.0)  # moves with the tray, nothing of its own
        moves, verify, _ = rs.moves_and_checks(r, plan, clearance=0.0)
        self.assertEqual([m["handle"] for m in moves], [11])
        self.assertEqual(verify[0]["supports"], [90])

    def test_unchecked_and_no_support(self):
        r = rs.parse_measure(report(unit_line(1, 11, "Spoon", 0.9, gap=None), "D|2|12|%s|12" % b64("Knife"),
                                    "X|2|deadline", unchecked=1, stop="deadline"))
        plan = rs.plan_drops(r, clearance=0.0)
        self.assertEqual(plan[1]["reason"], "no_support")
        self.assertEqual(plan[1]["own"], 0.0)

    def test_companions_move_and_block(self):
        r = stack_report(extra=["C|1|20|%s||0.75|3" % b64("Placemat_ENMESH"),
                                "C|3|21|%s|frozen|1.2|3" % b64("Glass_ENMESH")])
        plan = rs.plan_drops(r, clearance=0.0)
        self.assertEqual(plan[3]["reason"], "companion_not_movable")
        moves, verify, companions = rs.moves_and_checks(r, plan, clearance=0.0)
        self.assertIn(20, [m["handle"] for m in moves])
        self.assertEqual(companions, [{"of": "Placemat", "name": "Placemat_ENMESH", "handle": 20}])
        twin = next(m for m in moves if m["handle"] == 20)
        self.assertAlmostEqual(twin["z_after"], 0.75 - 0.5)
        self.assertEqual(sorted(v["unit"] for v in verify), [1, 2])

    def test_twin_under_moved_head_is_not_moved_twice(self):
        r = stack_report(extra=["C|1|20|%s||0.75|11,3" % b64("Placemat_ENMESH")])
        moves, _, companions = rs.moves_and_checks(r, rs.plan_drops(r, clearance=0.0), clearance=0.0)
        self.assertNotIn(20, [m["handle"] for m in moves])
        self.assertEqual(companions, [])


class ApplyParseTests(unittest.TestCase):
    def test_outcomes(self):
        ok = rs.parse_apply("W|applied|900|8000\nV|1|0.01|10|1\nEND\n")
        self.assertEqual((ok["status"], ok["verify"][0]["ok"]), ("applied", True))
        rb = rs.parse_apply("W|rolled_back|900|8000\nV|1|0.4|10|0\nEND\n")
        self.assertEqual(rb["status"], "rolled_back")
        for raw, code in (("__BUSY__", "USER_BUSY"), ("__AUTOKEY__", "AUTO_KEY_ON"),
                          ("__DEADLINE__", "DEADLINE_BEFORE_APPLY"), ("__STALE__|Plate01", "SCENE_CHANGED"),
                          ("__GONE__|12", "NODE_GONE")):
            self.assertEqual(rs.parse_apply(raw)["code"], code)
        self.assertEqual(rs.parse_apply("__READBACK__|Plate01")["status"], "rolled_back")
        with self.assertRaises(RuntimeError):
            rs.parse_apply("W|maybe|1|1\nEND\n")


def mock_client(*results, timeout=120):
    client = MagicMock()
    client.timeout = timeout
    client.send_command.side_effect = [{"result": r} for r in results]
    return client


class ToolTests(unittest.TestCase):
    def run_check(self, raw, **kw):
        with patch.object(tool, "client", mock_client(raw)) as client:
            return tool.resting_check(**{"names": ["Placemat"], **kw}), client

    def test_resting_check_rows_and_warnings(self):
        raw = report(gap_line(1, 90, "Table", 0.3), unit_line(1, 11, "Placemat", 0.75, gap=0.3, stride=3),
                     unit_line(2, 12, "Spoon", 0.9, gap=None),
                     gap_line(3, 90, "Table", 0.0), unit_line(3, 13, "Plate", 0.85, gap=0.0),
                     "Q|91|%s|coincident|1" % b64("Placemat001"), "P|90|%s|VRayProxy|500" % b64("Table"),
                     scatter=4)
        out, client = self.run_check(raw)
        self.assertEqual(client.send_command.call_count, 1)
        self.assertEqual([r["name"] for r in out["nodes"]], ["Placemat", "Spoon"])  # resting hidden
        top = out["nodes"][0]
        self.assertEqual((top["status"], top["min_gap_mm"], top["support"]["name"], top["flags"]),
                         ("floating", 3.0, "Table", ["sampled"]))
        self.assertEqual(out["summary"]["resting"], 1)
        text = " ".join(out["warnings"])
        for needle in ("every n-th vertex", "Coincident duplicates", "4 scatter objects", "Nothing within max_drop_mm",
                       "viewport preview"):
            self.assertIn(needle, text)
        self.assertEqual((out["units"], out["tolerance_mm"], out["complete"]), ("#centimeters", 0.5, True))
        out, _ = self.run_check(raw, include_resting=True)
        self.assertEqual(len(out["nodes"]), 3)

    def test_partial_results(self):
        raw = report(unit_line(1, 11, "Placemat", 0.75, gap=0.0), "D|2|12|%s|12" % b64("Garland"),
                     "X|2|deadline", unchecked=1, stop="deadline")
        out, _ = self.run_check(raw)
        self.assertFalse(out["complete"])
        self.assertEqual(out["nodes"][0], {"name": "Garland", "handle": 12, "status": "unchecked", "reason": "deadline"})
        self.assertIn("time budget ran out", out["warnings"][0])

    def drop(self, measure_raw, *apply_raw, **kw):
        client = mock_client(measure_raw, *apply_raw)
        with patch.object(tool, "client", client):
            return tool.drop_to_surface(**{"names": ["GRP_Set"], **kw}), client

    def stack_raw(self, **kw):
        return report(gap_line(1, 90, "Table", 0.5), unit_line(1, 11, "Placemat", 0.75, gap=0.5),
                      gap_line(2, 11, "Placemat", 0.0), unit_line(2, 12, "Plate", 0.85, gap=0.0),
                      "R|2|50|%s" % b64("Fork"), **kw)

    def test_dry_run_plans_only(self):
        out, client = self.drop(self.stack_raw())
        self.assertEqual(client.send_command.call_count, 1)
        self.assertEqual((out["dry_run"], out["applied"], out["planned_moves"], out["stack_order"]),
                         (True, False, 2, ["Placemat", "Plate"]))
        plate = next(r for r in out["nodes"] if r["name"] == "Plate")
        self.assertEqual((plate["plan"]["dz_mm"], plate["plan"]["reason"]), (-4.8, "follows_support"))
        self.assertIn("Fork", " ".join(out["warnings"]))

    def test_apply_moves_and_verifies(self):
        out, client = self.drop(self.stack_raw(), "W|applied|900|8000\nV|1|0.02|10|1\nV|2|0.02|10|1\nEND\n",
                                dry_run=False)
        self.assertEqual(client.send_command.call_count, 2)
        script = client.send_command.call_args_list[1].args[0]
        self.assertIn("theHold.Begin()", script)
        self.assertTrue(out["applied"])
        self.assertEqual(out["undo_label"], "MCP drop to surface")
        self.assertEqual([m["name"] for m in out["move_log"]], ["Placemat", "Plate"])
        self.assertEqual(out["move_log"][0]["dz_mm"], -4.8)

    def test_apply_rolled_back(self):
        out, _ = self.drop(self.stack_raw(), "W|rolled_back|900|8000\nV|1|0.3|10|0\nV|2|0.02|10|1\nEND\n",
                           dry_run=False)
        self.assertFalse(out["applied"])
        self.assertTrue(out["rolled_back"])
        self.assertIn("Placemat", out["warnings"][-1])

    def test_apply_refusals(self):
        out, client = self.drop(self.stack_raw(auto_key="true"), dry_run=False)
        self.assertEqual((out["refused"]["code"], client.send_command.call_count), ("AUTO_KEY_ON", 1))
        raw = report(gap_line(1, 90, "Table", 0.5), unit_line(1, 11, "Placemat", 0.75, gap=0.5),
                     "D|2|12|%s|12" % b64("Plate"), "X|2|deadline", unchecked=1, stop="deadline")
        out, client = self.drop(raw, dry_run=False)
        self.assertEqual((out["refused"]["code"], client.send_command.call_count), ("INCOMPLETE", 1))
        out, _ = self.drop(self.stack_raw(), "__BUSY__", dry_run=False)
        self.assertEqual(out["refused"]["code"], "USER_BUSY")
        self.assertFalse(out["applied"])

    def test_nothing_to_move(self):
        raw = report(gap_line(1, 90, "Table", 0.0), unit_line(1, 11, "Placemat", 0.75, gap=0.0))
        out, client = self.drop(raw, dry_run=False)
        self.assertEqual((out["planned_moves"], out["applied"], client.send_command.call_count), (0, False, 1))

    def test_drop_params(self):
        for kw in ({"clearance_mm": 0.6}, {"clearance_mm": -1}, {"dry_run": "no"}, {"limit": 0}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.drop(self.stack_raw(), **kw)


class RegistrationTests(unittest.TestCase):
    def test_registration(self):
        from maxmcp import server
        from maxmcp.tool_discovery import TOOLSET_SPECS
        from scripts.gen_tool_registry import extract_tools
        from scripts.gen_tool_smoke import MUTATE_TOOLS, SKIP_DEFAULT
        self.assertIn("resting", server.CORE_TOOL_MODULES)
        self.assertIn("resting_check", server._READ_ONLY_TOOLS)
        self.assertIn("drop_to_surface", server._DESTRUCTIVE_TOOLS)
        self.assertNotIn("drop_to_surface", server._READ_ONLY_TOOLS)
        self.assertEqual(sum("resting" in s.modules for s in TOOLSET_SPECS), 1)
        self.assertTrue({"resting_check", "drop_to_surface"} <= SKIP_DEFAULT)
        self.assertIn("drop_to_surface", MUTATE_TOOLS)
        names = {t["name"] for t in extract_tools(Path(tool.__file__))}
        self.assertFalse(names & {"resting_check", "drop_to_surface"})


if __name__ == "__main__":
    unittest.main()
