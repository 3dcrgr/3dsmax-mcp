"""Tests for the Forest Pack tools (forest_items, forest_edit_items, forest_set_mode, forest_library,
forest_from_library, forest_validate) and the fixed scatter_forest_pack."""

import base64
import importlib
import math
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import forest as fh  # noqa: E402
from maxmcp.helpers import forest_library as flib  # noqa: E402

forest = importlib.import_module("maxmcp.tools.forest")
scattering = importlib.import_module("maxmcp.tools.scattering")

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN = ("resetmaxfile", "loadmaxfile", "quitmax", "exportdata", "savemaxfile", "holdmaxfile", "fetchmaxfile",
             "doscommand", "shelllaunch", "deletefile", "python.execute", "checkforsave")


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def ok(*lines):
    return {"result": "FPOK\n" + "\n".join(lines) + "\n"}


class FakeClient:
    """Returns canned responses in order and records the scripts."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.scripts = []
        self.kwargs = []

    def send_command(self, script, **kwargs):
        self.scripts.append(script)
        self.kwargs.append(kwargs)
        return self.responses.pop(0)


def _top_level_args(text):
    """Space-separated top-level MAXScript arguments (parentheses and strings kept together)."""
    args, depth, current, in_string = [], 0, "", False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            current += ch
            if ch == "\\":
                current += text[i + 1]
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            current += ch
        elif ch in "([":
            depth += 1
            current += ch
        elif ch in ")]":
            depth -= 1
            current += ch
        elif ch.isspace() and depth == 0:
            if current:
                args.append(current)
            current = ""
        else:
            current += ch
        i += 1
    if current:
        args.append(current)
    return args


def check_formats(test, script):
    """Every `format "..." args to:ss` call passes one argument per % placeholder."""
    calls = re.findall(r'format "((?:[^"\\]|\\.)*)"(.*?)to:ss', script)
    for fmt, rest in calls:
        with test.subTest(fmt=fmt):
            test.assertEqual(fmt.count("%"), len(_top_level_args(rest)), rest)


# MAXScript reserved keywords (a local named "at" was a parse error, live), plus predefined constants that
# shouldn't be shadowed.
MXS_RESERVED = frozenset("""about and animate as at attributes by case catch collect continue coordsys do else
exit fn for from function global if in local macroscript mapped max not of off on or parameters persistent plugin
rcmenu return rollout set struct then throw to tool try undo utility when where while with true false ok undefined
unsupplied dontcollect pi e""".split())


def _code_lines(script):
    """Script lines with -- comments removed (the scripts keep no "--" inside strings)."""
    return [line.split("--")[0] for line in script.splitlines()]


def _split_top_level(text):
    parts, depth, current, in_string = [], 0, "", False
    for ch in text:
        if in_string:
            in_string = ch != '"'
        elif ch == '"':
            in_string = True
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += ch
    return parts + [current]


def declared_names(script):
    """Names declared as locals, function parameters and loop variables."""
    names = []
    for code in _code_lines(script):
        for m in re.finditer(r"\blocal\s+(.*)", code):
            names += [n.group(1) for n in (re.match(r"\s*(\w+)", p) for p in _split_top_level(m.group(1))) if n]
        m = re.match(r"\s*fn\s+\w+((?:\s+\w+)*)\s*=", code)
        if m:
            names += m.group(1).split()
        names += re.findall(r"\bfor\s+(\w+)\s+(?:in|=)", code)
    return names


def deletes_outside_undo_off(script):
    """Node deletes that are not inside a `with undo off (...)` block."""
    found, stack, in_string = [], [], False
    text = "\n".join(_code_lines(script))
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            in_string = ch != '"'
        elif ch == '"':
            in_string = True
        elif text.startswith("with undo off (", i):
            stack.append(True)
            i += len("with undo off (")
            continue
        elif ch == "(":
            stack.append(False)
        elif ch == ")":
            stack.pop()
        elif text.startswith("delete ", i) and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "._")):
            if not any(stack):
                end = text.find("\n", i)
                found.append(text[i:end if end >= 0 else len(text)])
        i += 1
    return found


def check_live_pitfalls(test, script):
    """The MAXScript pitfalls met in the live test (docs/fork/forest-pack.md)."""
    for name in declared_names(script):
        test.assertNotIn(name.lower(), MXS_RESERVED, f"'{name}' is a MAXScript keyword or constant")
    for code in _code_lines(script):
        # a for-collect only straight into a local (or as a function's whole body), never inside another
        # expression: written into a call's arguments it failed with "Bad free thunk local member index"
        if re.search(r"\bcollect\b", code):
            test.assertRegex(code, r"^\s*(local \w+ = for |fn \w+[\w ]*= \(for )", code)
        # getHandleByAnim returns an IntegerPtr
        test.assertGreaterEqual(code.count("as integer64"), code.count("getHandleByAnim"), code)
    # a ray from z = 1e7 missed a plane at z = 0 (float precision)
    test.assertNotRegex(script, r"\bray\s*\[[^\]]*\d[eE][+-]?\d")


def check_script(test, script):
    low = script.lower()
    for word in FORBIDDEN:
        test.assertNotIn(word, low)
    test.assertNotIn("@@", script)
    test.assertEqual(script.count("("), script.count(")"))
    test.assertNotRegex(script, r"geomlist\s*=\s*#\(\s*3")
    test.assertNotIn("maxdensity", low)
    check_formats(test, script)
    check_live_pitfalls(test, script)


class ImportTargetTests(unittest.TestCase):
    def test_imports_checkout(self):
        self.assertEqual(Path(forest.__file__).resolve(), REPO / "maxmcp" / "tools" / "forest.py")
        self.assertEqual(Path(scattering.__file__).resolve(), REPO / "maxmcp" / "tools" / "scattering.py")
        self.assertEqual(Path(fh.__file__).resolve(), REPO / "maxmcp" / "helpers" / "forest.py")
        self.assertEqual(Path(flib.__file__).resolve(), REPO / "maxmcp" / "helpers" / "forest_library.py")


class RegistrationTests(unittest.TestCase):
    NEW = {"forest_items", "forest_edit_items", "forest_set_mode", "forest_library", "forest_from_library",
           "forest_validate"}

    def test_registration(self):
        from maxmcp import server
        from maxmcp.tool_discovery import TOOLSET_SPECS, _module_tool_names
        from scripts.gen_tool_catalog import MODULE_CATEGORY
        from scripts.gen_tool_smoke import MUTATE_TOOLS, SKIP_DEFAULT
        self.assertIn("forest", server.SPECIALTY_TOOL_MODULES)
        self.assertNotIn("forest", server.CORE_TOOL_MODULES)
        specs = [s for s in TOOLSET_SPECS if "forest" in s.modules]
        self.assertEqual(len(specs), 1)
        self.assertIn("scattering", specs[0].modules)
        self.assertEqual(sum("scattering" in s.modules for s in TOOLSET_SPECS), 1)
        self.assertTrue({"forest_items", "forest_library"} <= server._READ_ONLY_TOOLS)
        self.assertTrue({"forest_items", "forest_library"} <= server._IDEMPOTENT_TOOLS)
        self.assertTrue({"forest_edit_items", "forest_set_mode"} <= server._DESTRUCTIVE_TOOLS)
        self.assertFalse({"forest_edit_items", "forest_set_mode", "forest_from_library", "forest_validate"}
                         & server._READ_ONLY_TOOLS)
        self.assertTrue(self.NEW | {"scatter_forest_pack"} <= SKIP_DEFAULT)
        self.assertTrue({"scatter_forest_pack", "forest_edit_items", "forest_set_mode", "forest_from_library",
                         "forest_validate"} <= MUTATE_TOOLS)
        self.assertFalse({"forest_items", "forest_library"} & MUTATE_TOOLS)
        self.assertEqual(MODULE_CATEGORY["forest"], "Scattering")
        self.assertEqual(set(_module_tool_names(Path(forest.__file__))), self.NEW)
        self.assertEqual(_module_tool_names(Path(scattering.__file__)), ("scatter_forest_pack",))

    def test_docstrings(self):
        for fn in (forest.forest_items, forest.forest_edit_items, forest.forest_set_mode, forest.forest_library,
                   forest.forest_from_library, forest.forest_validate, scattering.scatter_forest_pack):
            first = " ".join(fn.__doc__.strip().split("\n\n")[0].split())
            self.assertLess(len(first), 300, fn.__name__)

    def test_plugin_descriptor(self):
        from maxmcp.tools import plugins
        self.assertTrue(self.NEW | {"scatter_forest_pack"} <= set(plugins.PLUGIN_OVERLAYS["forestpack"]["workflow_tools"]))


class HelperTests(unittest.TestCase):
    def test_fill_is_single_pass(self):
        self.assertEqual(fh.fill("a @@X@@ b", X="@@Y@@"), "a @@Y@@ b")
        with self.assertRaises(KeyError):
            fh.fill("@@MISSING@@")

    def test_lines(self):
        self.assertEqual(fh.lines(ok("A|1", "B|2")), ["A|1", "B|2"])
        with self.assertRaises(fh.ForestError) as ctx:
            fh.lines({"result": "FPERR|-- Runtime error: NOT_FOREST: 'Box001' is a Box, not a Forest Pack object"})
        self.assertEqual(ctx.exception.code, "NOT_FOREST")
        self.assertTrue(str(ctx.exception).startswith("NOT_FOREST: 'Box001'"))
        with self.assertRaises(fh.ForestError) as ctx:
            fh.lines({"result": "FPERR|-- Unknown property: \"trees\" in undefined"})
        self.assertEqual(ctx.exception.code, "FOREST_ERROR")
        with self.assertRaises(fh.ForestError) as ctx:
            fh.lines({"result": "FPERR|USER_BUSY: an undo operation is open"})
        self.assertTrue(ctx.exception.retryable)
        with self.assertRaises(fh.ForestError) as ctx:
            fh.lines({"result": "__MCP_MS_ERR__: -- Syntax error"})
        self.assertEqual(ctx.exception.code, "MAXSCRIPT_ERROR")
        with self.assertRaises(fh.ForestError) as ctx:
            fh.lines({"ok": False, "error": {"code": "MAX_BUSY", "message": "busy"}})
        self.assertEqual(ctx.exception.code, "MAX_BUSY")
        with self.assertRaises(fh.ForestError) as ctx:
            fh.lines({"result": "OK"})
        self.assertEqual(ctx.exception.code, "INVALID_READBACK")

    def test_density_math(self):
        # 10 m plane, 300 cm tiles: measured 16023 items
        self.assertAlmostEqual(fh.estimate_count(100.0, 3.0, 3.0), 15966.7, places=1)
        # 10 m plane, 1000 cm tiles: measured 1437
        self.assertAlmostEqual(fh.estimate_count(100.0, 10.0, 10.0), 1437.0)
        self.assertAlmostEqual(fh.units_for_items_per_m2(1437.0), 100.0)
        self.assertAlmostEqual(fh.units_for_items_per_m2(1437.0 / 9.0), 300.0)

    def test_library_script_is_balanced(self):
        self.assertEqual(fh.MXS_LIB.count("("), fh.MXS_LIB.count(")"))
        self.assertEqual(fh.COLLECT_FN.count("("), fh.COLLECT_FN.count(")"))
        self.assertIn("(scaleMatrix src.objectOffsetScale) * (src.objectOffsetRot as matrix3) * "
                      "(transMatrix src.objectOffsetPos)", fh.MXS_LIB)
        self.assertIn("theHold.Holding() or theHold.IsSuspended() or theHold.SuperLevel() != 0", fh.MXS_LIB)
        self.assertEqual(len(fh.AREA_LISTS), 30)
        for name in fh.AREA_LISTS:
            self.assertIn(f'"{name}"', fh.MXS_LIB)
        check_formats(self, fh.COLLECT_FN)

    def test_live_pitfall_checks(self):
        """The script checks catch each MAXScript failure met in the live test."""
        check_live_pitfalls(self, fh.MXS_LIB + fh.COLLECT_FN)
        bad = ["local an = #(), at = #()",  # keyword as a name: parse error
               "fn f nd to = (nd)",
               "for by in #(1) do print by",
               "local ok = true",
               "setProperty nd p (for k in keep where k <= v.count collect v[k])",  # Bad free thunk
               "fp.namelist = for g in geos collect g.name",
               "format \"%\" ((getHandleByAnim o) as string) to:ss",  # IntegerPtr
               "intersectRay s (ray [p.x, p.y, 1e7] [0, 0, -1])"]  # missed a plane at z = 0
        for script in bad:
            with self.subTest(script=script), self.assertRaises(AssertionError):
                check_live_pitfalls(self, script)
        self.assertEqual(deletes_outside_undo_off("with undo off (\n    (for o in a do delete o)\n)\ndelete b\n"
                                                  "t.delete 3\nundo on (delete c)"), ["delete b", "delete c)"])

    def test_name_list(self):
        self.assertEqual(fh.name_list("A", "x"), ["A"])
        self.assertEqual(fh.name_list("A, B", "x"), ["A", "B"])
        self.assertEqual(fh.name_list(["A", "A", "B"], "x"), ["A", "B"])
        for bad in ([], [""], [1], "a\x00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fh.name_list(bad, "x")
        self.assertEqual(fh.name_list(None, "x", allow_empty=True), [])


# --------------------------------------------------------------------------------------------
# forest_items
# --------------------------------------------------------------------------------------------

def items_report(mode=0, count=3, items=(), types=None, invalid=0):
    types = types or [(1, 2, "2", "1", "Box", "Box", "Box"), (2, 1, "2", "0", "Tea", "Tea", "")]
    lines = [f"H|{b64('Forest001')}|{mode}|{count}|{len(types)}|{invalid}|100"]
    for g, n, geom, src_ok, label, coname, src in types:
        lines.append(f"G|{g}|{n}|{geom}|{src_ok}|{b64(label)}|{b64(coname)}|{b64(src) if src else ''}")
    lines.extend(items)
    return ok(*lines)


class ForestItemsTests(unittest.TestCase):
    def run_items(self, response, **kw):
        fake = FakeClient(response)
        with patch.object(forest, "client", fake):
            out = forest.forest_items(kw.pop("name", "Forest001"), **kw)
        return out, fake.scripts[0]

    def test_script(self):
        out, script = self.run_items(items_report(), offset=2, limit=7, refresh_surfaces=True)
        check_script(self, script)
        self.assertIn(f'fpForest "{b64("Forest001")}"', script)
        self.assertIn("fpGuard nd", script)  # never rebuild a forest with geomlist 3
        self.assertIn("local i0 = 2", script)
        self.assertIn("(i0 + 7 - 1)", script)
        self.assertIn("if true do (\n    with undo off (", script)  # surfanim pulse outside the undo stack
        self.assertIn("nd.surfanim = true\n        t.update()\n        nd.surfanim = was", script)
        self.assertIn("inverse (fpOffTM src)", script)
        self.assertIn("if true do piv = piv * objTM", script)  # world space
        self.assertLess(script.index("nd.surfanim = true"), script.index("local n = t.count()"))
        self.assertIn("if true do line += \"|\" + (fpTM piv)", script)
        self.assertIn("if false do line += \"|\" + ((t.getSeed i) as string)", script)

    def test_object_space_and_fields(self):
        _, script = self.run_items(items_report(), space="object", fields=["seed", "item_name"])
        self.assertIn("if false do piv = piv * objTM", script)
        self.assertIn("if true do line += \"|\" + ((t.getSeed i) as string)", script)
        self.assertIn("if false do line += \"|\" + (fpP piv.row4)", script)

    def test_parse(self):
        tm = "1,0,0,0,1,0,0,0,1,10,20,30"
        response = items_report(count=3, items=[
            f"T|0|1|1|10,20,30|{tm}|20,50",
            f"T|1|2|0|1,2,3|{tm}|30,22",
        ])
        out, _ = self.run_items(response, offset=0, limit=2)
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["mode"], "procedural")
        self.assertEqual((out["returned"], out["next_offset"], out["truncated"]), (2, 2, True))
        self.assertEqual(out["items"][0], {"index": 0, "geom_id": 1, "position": [10.0, 20.0, 30.0],
                                           "transform": [1.0, 0, 0, 0, 1, 0, 0, 0, 1, 10, 20, 30],
                                           "size": [20.0, 50.0]})
        self.assertTrue(out["items"][1]["no_source"])
        self.assertEqual(out["item_types"][0]["name"], "Box")
        self.assertEqual(out["item_types"][1]["source"], None)
        self.assertTrue(out["item_types"][1]["source_missing"])
        self.assertEqual(out["item_types"][0]["geom_type"], 2)
        self.assertTrue(any("no valid source" in w for w in out["warnings"]))
        self.assertTrue(any("procedural" in w for w in out["warnings"]))

    def test_all_fields_order(self):
        response = items_report(mode=1, count=1, items=["T|0|1|1|1,2,3|1,0,0,0,1,0,0,0,1,1,2,3|45|1,1,2|20,50|777"])
        out, _ = self.run_items(response, fields=list(forest.ITEM_FIELDS))
        item = out["items"][0]
        self.assertEqual(item["rotation_z"], 45.0)
        self.assertEqual(item["scale"], [1.0, 1.0, 2.0])
        self.assertEqual(item["seed"], 777)
        self.assertEqual((item["item_name"], item["source"]), ("Box", "Box"))
        self.assertEqual(out["mode"], "custom_edit")
        self.assertIsNone(out["next_offset"])

    def test_params(self):
        for kw in ({"limit": 0}, {"limit": 5001}, {"offset": -1}, {"space": "local"}, {"fields": ["colour"]},
                   {"fields": []}, {"refresh_surfaces": "yes"}, {"name": ""}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.run_items(items_report(), **kw)

    def test_errors(self):
        with self.assertRaises(fh.ForestError) as ctx:
            self.run_items({"result": "FPERR|-- Runtime error: CRASH_RISK: geomlist[1] is 3"})
        self.assertEqual(ctx.exception.code, "CRASH_RISK")


# --------------------------------------------------------------------------------------------
# forest_edit_items / forest_set_mode
# --------------------------------------------------------------------------------------------

def edit_report(m0=0, mode=1, n0=10, n2=9, switched=1, changed=()):
    return ok(f"E|{b64('Forest001')}|{m0}|{mode}|{n0}|{n2}|{switched}", *changed)


class ForestEditTests(unittest.TestCase):
    def run_edit(self, edits, response=None, **kw):
        fake = FakeClient(response or edit_report())
        with patch.object(forest, "client", fake):
            out = forest.forest_edit_items("Forest001", edits, **kw)
        return out, fake.scripts[0]

    def test_plan_and_script(self):
        edits = [{"op": "move", "index": 5, "position": [40, 3010]},
                 {"op": "set_rotation", "index": 3, "rotation_z": 45},
                 {"op": "delete", "index": 12}, {"op": "delete", "index": 20}, {"op": "delete", "index": 1},
                 {"op": "create", "position": [1, 2, 3], "geom_id": 2},
                 {"op": "set_size", "index": 4, "width": 2, "height": 3},
                 {"op": "set_seed", "index": 6, "seed": 777},
                 {"op": "set_geom", "index": 7, "geom_id": 1}]
        out, script = self.run_edit(edits, allow_custom_edit=True)
        check_script(self, script)
        self.assertIn("local dels = #(20, 12, 1)", script)  # descending
        self.assertIn("#(#move, 5, [40.000000, 3010.000000, 0.000000], false)", script)
        self.assertIn(f"#(#rotation, 3, {math.radians(45):.9f})", script)  # degrees -> radians
        self.assertIn("#(#size, 4, 2.000000, 3.000000)", script)
        self.assertIn("#(#seed, 6, 777)", script)
        self.assertIn("#(#geom, 7, 1)", script)
        self.assertIn("#([1.000000, 2.000000, 3.000000], true, 2, -1.000000, -1.000000, undefined, undefined)",
                      script)
        self.assertIn("for g in #(1, 2) do", script)
        self.assertIn("if 20 >= n0 do throw", script)
        self.assertIn("if m0 == 0 and not true do throw \"CUSTOM_EDIT_REQUIRED", script)
        # build in mode 0 before switching, then edit, all in one undo step
        self.assertLess(script.index("t.update()\nlocal n0 = t.count()"), script.index("nd.mode = 1"))
        self.assertLess(script.index('undo "MCP Forest edit items" on'), script.index("nd.mode = 1"))
        self.assertEqual(script.count("undo \"MCP"), 1)
        self.assertLess(script.index("for op in mods where op[1] != #rotation do"), script.index("for d in dels do t.delete d"))
        # rotations after the other edits, as final angles (random part subtracted), before the deletes
        self.assertLess(script.index("for op in mods where op[1] != #rotation do"),
                        script.index("for op in mods where op[1] == #rotation do fpSetAngle t nd op[2] op[3]"))
        self.assertLess(script.index("fpSetAngle t nd op[2] op[3]"), script.index("for d in dels do t.delete d"))
        self.assertIn("t.setRotation i [0, 0, target - (degToRad randomPart)]", script)
        self.assertIn("if not (fpOnSurface nd c[1]) do append offSurface j", script)
        # the off-surface ray starts just above the surface's bounding box (from 1e7 it missed, live)
        self.assertIn("local z0 = s.max.z + 1.0 + 0.01 * (s.max.z - s.min.z)", script)
        self.assertIn("(intersectRay s (ray [p.x, p.y, z0] [0, 0, -1]))", script)
        # item state before the edits, read in custom edit mode, for the revert list
        self.assertLess(script.index("nd.mode = 1"), script.index('format "P|'))
        self.assertLess(script.index('format "P|'), script.index("for op in mods where op[1] != #rotation do"))
        self.assertIn("for i in #(1, 3, 4, 5, 6, 7, 12, 20) do", script)
        self.assertLess(script.index("for d in dels do t.delete d"), script.index("t.create (fpToObj"))
        self.assertIn("q * inv", script)  # world -> object: p * inverse(objectTransform)
        # readback: original index -> index after the deletes below it
        self.assertIn("#(#(5, 4), #(3, 2), #(4, 3), #(6, 5), #(7, 6))", script)
        self.assertEqual(out["applied"], {"move": 1, "set_rotation": 1, "delete": 3, "create": 1, "set_size": 1,
                                          "set_seed": 1, "set_geom": 1})

    def test_refusal_literal(self):
        _, script = self.run_edit([{"op": "delete", "index": 0}])
        self.assertIn("if m0 == 0 and not false do throw \"CUSTOM_EDIT_REQUIRED", script)

    def test_custom_edit_required(self):
        response = {"result": "FPERR|-- Runtime error: CUSTOM_EDIT_REQUIRED: the forest is procedural"}
        with self.assertRaises(fh.ForestError) as ctx:
            self.run_edit([{"op": "delete", "index": 0}], response=response)
        self.assertEqual(ctx.exception.code, "CUSTOM_EDIT_REQUIRED")

    def test_validation(self):
        bad = [[], [{"op": "spin", "index": 0}], [{"op": "move", "index": 0}],
               [{"op": "move", "index": 0, "position": [1, 2], "rotation": 5}],
               [{"op": "delete", "index": -1}], [{"op": "delete", "index": True}],
               [{"op": "delete", "index": 1}, {"op": "delete", "index": 1}],
               [{"op": "delete", "index": 1}, {"op": "set_seed", "index": 1, "seed": 3}],
               [{"op": "set_width", "index": 0, "width": 0}], [{"op": "set_width", "index": 0, "width": float("nan")}],
               [{"op": "move", "index": 0, "position": [1]}], [{"op": "create", "position": [1, 2], "index": 3}],
               [{"op": "set_geom", "index": 0, "geom_id": 0}], [{"op": "set_seed", "index": 0, "seed": 2**31}],
               ["move"], [{"op": "create", "position": [1, 2], "width": -1}]]
        for edits in bad:
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                self.run_edit(edits)
        with self.assertRaises(ValueError):
            self.run_edit([{"op": "delete", "index": 0}], allow_custom_edit="yes")

    def test_result(self):
        changed = [
            "C|5|4|1|1|39.5,3013.7,50|40,3010,0|20|50|30|123",
            "C|3|2|1|1|7,8,9|7,8,0|20|50|45|1",
            "C|new|9|2|1|1,2,3|1,2,0|25|60|0|5",
        ]
        edits = [{"op": "move", "index": 5, "position": [40, 3010]}, {"op": "set_rotation", "index": 3,
                                                                       "rotation_z": 45},
                 {"op": "delete", "index": 1}, {"op": "create", "position": [1, 2], "geom_id": 2}]
        out, _ = self.run_edit(edits, response=edit_report(n0=10, n2=10, changed=changed), allow_custom_edit=True)
        self.assertEqual((out["count_before"], out["count_after"], out["mode"]), (10, 10, "custom_edit"))
        self.assertTrue(out["switched_to_custom_edit"])
        self.assertEqual(out["created_indices"], [9])
        self.assertEqual(out["changed"][0]["was_index"], 5)
        self.assertEqual(out["changed"][0]["index"], 4)
        self.assertEqual(out["changed"][0]["position"], [39.5, 3013.7, 50.0])
        self.assertTrue(out["changed"][2]["created"])
        self.assertTrue(any("random translation" in w for w in out["warnings"]))
        self.assertFalse(any("expected" in w for w in out["warnings"]))

    def test_undo_and_revert(self):
        edits = [{"op": "move", "index": 5, "position": [40, 3010]},
                 {"op": "set_rotation", "index": 3, "rotation_z": 45},
                 {"op": "delete", "index": 1},
                 {"op": "create", "position": [1, 2], "geom_id": 2},
                 {"op": "set_seed", "index": 6, "seed": 777}]
        report = ["C|5|4|1|1|40,3010,50|40,3010,0|20|50|0|13",
                  "C|3|2|1|1|7,8,9|7,8,0|20|50|45|12",
                  "C|6|5|1|1|1,1,1|1,1,0|25|60|10|777",
                  "C|new|9|2|1|1,2,3|1,2,0|25|60|0|5",
                  "P|1|10,20,0|20|50|30|11|1",
                  "P|3|7,8,0|20|50|15|12|1",
                  "P|5|40,3000,0|20|50|0|13|2",
                  "P|6|1,1,0|25|60|90|14|1"]
        out, _ = self.run_edit(edits, response=edit_report(m0=1, mode=1, n0=10, n2=10, switched=0, changed=report))
        # Forest Pack doesn't record item edits in undo (live): only revert puts them back
        self.assertFalse(out["undoable"])
        self.assertTrue(any("undo can't revert them" in w for w in out["warnings"]))
        self.assertEqual(out["revert"], [
            {"op": "delete", "index": 9},
            {"op": "set_rotation", "index": 2, "rotation_z": 15.0},
            {"op": "move", "index": 4, "position": [40.0, 3000.0]},
            {"op": "set_seed", "index": 5, "seed": 14},
            {"op": "set_rotation", "index": 5, "rotation_z": 90.0},
            {"op": "create", "position": [10.0, 20.0], "geom_id": 1, "width": 20.0, "height": 50.0,
             "rotation_z": 30.0, "seed": 11},
        ])
        forest._edit_plan(out["revert"])  # a valid edit list for forest_edit_items
        # switching out of procedural is recorded in undo, and undoing it drops every edit
        out, _ = self.run_edit([{"op": "delete", "index": 0}], allow_custom_edit=True,
                               response=edit_report(m0=0, mode=1, n0=10, n2=9, switched=1))
        self.assertTrue(out["undoable"])
        self.assertTrue(out["switched_to_custom_edit"])
        self.assertFalse(any("undo can't revert them" in w for w in out["warnings"]))

    def test_move_not_applied_warning(self):
        changed = ["C|0|0|1|1|0,0,0|0,0,0|20|50|0|1"]
        out, _ = self.run_edit([{"op": "move", "index": 0, "position": [100, 0]}],
                               response=edit_report(m0=1, n0=5, n2=5, switched=0, changed=changed))
        self.assertTrue(any("differs from the requested" in w for w in out["warnings"]))


class ForestSetModeTests(unittest.TestCase):
    def run_mode(self, response, **kw):
        fake = FakeClient(response)
        with patch.object(forest, "client", fake):
            out = forest.forest_set_mode("Forest001", **kw)
        return out, fake.scripts[0]

    def test_custom_edit(self):
        out, script = self.run_mode(ok(f"M|{b64('Forest001')}|0|1|516|516|1"), mode="custom_edit")
        check_script(self, script)
        self.assertIn("local target = 1", script)
        self.assertLess(script.index("t.update()\nlocal n0"), script.index("nd.mode = 1"))
        self.assertEqual(out, {"name": "Forest001", "previous_mode": "procedural", "mode": "custom_edit",
                               "changed": True, "count_before": 516, "count": 516})

    def test_procedural_needs_confirm(self):
        _, script = self.run_mode(ok(f"M|{b64('Forest001')}|1|1|516|516|0"), mode="procedural")
        self.assertIn("local target = 0", script)
        self.assertIn("if not false do throw \"CONFIRM_DISCARD_REQUIRED", script)
        out, script = self.run_mode(ok(f"M|{b64('Forest001')}|1|0|516|627|1"), mode="procedural",
                                    confirm_discard=True)
        self.assertIn("if not true do throw", script)
        self.assertEqual(out["note"], "custom edits were discarded")
        with self.assertRaises(ValueError):
            self.run_mode(ok(), mode="frozen")


# --------------------------------------------------------------------------------------------
# forest_library / forest_from_library
# --------------------------------------------------------------------------------------------

PRESETS_XML = """<?xml version="1.0" encoding="utf-8"?>
<library version="100" masterScale="0.393700787402">
  <item class="maxobject">
    <uuid>7507a9b2-bdcb-4a99-a8a1-bfbf9e256abd</uuid>
    <name lang="english">Common grass 01 (detail)</name>
    <name lang="latin">Common grass 01 (detail)</name>
    <family>Grass</family>
    <description>A medium long common grass lawn preset.</description>
    <bitmap>06-common_grass_01.jpg</bitmap>
    <maxfile>objects\\_fp_common_grass_01.max</maxfile>
    <object>_fp_common_grass_01</object>
    <matlib>$(LIBROOTPATH)\\..\\matlib\\3d_$(RENDERER).mat</matlib>
    <available>LITE</available>
  </item>
  <item class="maxobject">
    <uuid>11111111-2222-3333-4444-555555555555</uuid>
    <name lang="english">Daisy field</name>
    <maxfile>objects\\_fp_daisy.max</maxfile>
    <object>_fp_daisy</object>
    <matlib>matlibs\\daisy_$(RENDERER).pmat</matlib>
  </item>
</library>
"""

GRASS_XML = """<?xml version="1.0"?>
<library version="100">
  <item class="maxobject">
    <uuid>83351937-f8a1-4627-8c20-150547ad9382</uuid>
    <name lang="english">Cane 01</name>
    <family/>
    <maxfile>objects\\_fp_grass.max</maxfile>
    <object>_fp_cane01</object>
    <matlib>$(LIBROOTPATH)\\..\\matlib\\3d_$(RENDERER).mat</matlib>
    <material>_fp_grass</material>
  </item>
  <item class="maxobject">
    <uuid>99999999-2222-3333-4444-555555555555</uuid>
    <name lang="english">Common grass 01 (detail)</name>
    <maxfile>objects\\_fp_other.max</maxfile>
    <object>_fp_other_grass</object>
  </item>
</library>
"""

TREES_XML = """<?xml version="1.0" encoding="utf-8"?>
<library version="100">
  <item class="tree_billboard">
    <uuid>c24fd971-a99b-4396-8e17-5895fc6afa6f</uuid>
    <name lang="english">Almond</name>
    <bitmap>prunus_dulcis.jpg</bitmap>
    <library>$(LIBROOTPATH)\\..\\matlib\\2d_std_$(RENDERER).mat</library>
    <material>prunus_dulcis</material>
  </item>
</library>
"""


class LibraryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fp_lib_")
        self.root = os.path.join(self.tmp, "Forest Pack Pro", "lib")
        files = {
            ("Presets", "Lawns", "index.xml"): PRESETS_XML,
            ("Presets", "Lawns", "objects", "_fp_common_grass_01.max"): "x",
            ("Presets", "Lawns", "objects", "_fp_daisy.max"): "x",
            ("Presets", "Lawns", "objects", "index.xml"): "<library><item><name>skipped</name></item></library>",
            ("Presets", "Lawns", "thumbnails", "06-common_grass_01.jpg"): "x",
            ("Presets", "Lawns", "matlibs", "daisy_vray.pmat"): "x",
            ("3D", "Flowers and grass", "index.xml"): GRASS_XML,
            ("3D", "Flowers and grass", "objects", "_fp_grass.max"): "x",
            ("2D", "Trees", "index.xml"): TREES_XML,
        }
        for parts, text in files.items():
            path = os.path.join(self.root, *parts)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh_:
                fh_.write(text)
        matlib = os.path.join(self.tmp, "Forest Pack Pro", "matlib")
        os.makedirs(matlib)
        for name in ("3d_vray.mat", "3d_corona.mat", "2d_std_vray.mat"):
            open(os.path.join(matlib, name), "w").close()
        self.matlib_dir = matlib
        roots = [{"label": "Forest Library", "path": self.root, "source": "default"}]
        self.patches = [patch.object(flib, "library_roots", return_value=roots)]
        for p in self.patches:
            p.start()
        flib._scan_cache.clear()
        flib._parse_cache.clear()
        flib._renderer_cache.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        flib._scan_cache.clear()
        flib._parse_cache.clear()
        flib._renderer_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)


class ForestLibraryTests(LibraryFixture):
    def test_search_presets(self):
        out = forest.forest_library("grass", "presets", 10)
        self.assertEqual(out["total"], 1)
        item = out["items"][0]
        self.assertEqual((item["name"], item["kind"], item["library"], item["category"]),
                         ("Common grass 01 (detail)", "presets", "Presets", "Lawns"))
        self.assertEqual(item["object"], "_fp_common_grass_01")
        self.assertTrue(item["maxfile_exists"])
        self.assertEqual(os.path.normcase(item["maxfile"]), os.path.normcase(
            os.path.join(self.root, "Presets", "Lawns", "objects", "_fp_common_grass_01.max")))
        self.assertEqual(os.path.normcase(item["matlib"]),
                         os.path.normcase(os.path.join(self.matlib_dir, "3d_$(RENDERER).mat")))
        self.assertEqual(item["matlib_renderers"], ["corona", "vray"])
        self.assertTrue(item["thumbnail"].endswith("06-common_grass_01.jpg"))
        self.assertFalse(any(k.startswith("_") for k in item))
        self.assertEqual(out["roots"][0]["index_files"], 3)  # objects/ is skipped
        self.assertEqual(out["roots"][0]["items"], 5)

    def test_kinds_and_query(self):
        self.assertEqual([i["name"] for i in forest.forest_library("", "3d", 50)["items"]],
                         ["Cane 01", "Common grass 01 (detail)"])
        two_d = forest.forest_library("", "2d", 50)["items"]
        self.assertEqual(two_d[0]["name"], "Almond")
        self.assertEqual(two_d[0]["matlib_renderers"], ["vray"])
        self.assertEqual(forest.forest_library("grass common", "all", 50)["total"], 2)
        self.assertEqual(forest.forest_library("cane", "all", 1)["returned"], 1)
        for kw in ({"kind": "trees"}, {"limit": 0}, {"limit": 501}, {"query": 5}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                forest.forest_library(**kw)

    def test_find_item(self):
        self.assertEqual(flib.find_item("7507A9B2-bdcb-4a99-a8a1-bfbf9e256abd")["object"], "_fp_common_grass_01")
        self.assertEqual(flib.find_item("_fp_cane01")["name"], "Cane 01")
        with self.assertRaises(fh.ForestError) as ctx:
            flib.find_item("Common grass 01 (detail)")
        self.assertEqual(ctx.exception.code, "AMBIGUOUS")
        with self.assertRaises(fh.ForestError) as ctx:
            flib.find_item("Cane 1")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")
        self.assertIn("Cane 01", str(ctx.exception))

    def test_rejects_dtd(self):
        path = os.path.join(self.root, "Presets", "Lawns", "index.xml")
        with open(path, "w", encoding="utf-8") as fh_:
            fh_.write('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]><library/>')
        flib._parse_cache.clear()
        out = forest.forest_library("", "all", 50)
        self.assertTrue(any("DTD" in w for w in out["warnings"]))

    def test_scan_root_limits(self):
        files, error = flib.scan_root(os.path.join(self.tmp, "missing"))
        self.assertEqual(files, [])
        self.assertTrue(error.startswith("not readable"))
        flib._scan_cache.clear()
        files, error = flib.scan_root(self.root, budget_s=0.0)  # the deadline has passed at once
        self.assertTrue(error.startswith("partial"))
        self.assertEqual(flib.scan_root(self.root)[1], error)  # cached
        flib._scan_cache.clear()
        files, error = flib.scan_root(self.root)
        self.assertIsNone(error)
        self.assertEqual(len(files), 3)

    def test_resolve_path(self):
        self.assertEqual(flib.resolve_path(r"$(LIBROOTPATH)\..\matlib\3d_$(RENDERER).mat", r"C:\lib\Presets\Lawns",
                                           r"C:\lib", "vray"), r"C:\matlib\3d_vray.mat")
        self.assertEqual(flib.resolve_path(r"matlibs\gravel_$(RENDERER).mat", r"C:\lib\Presets\Gravel", r"C:\lib",
                                           "corona"), r"C:\lib\Presets\Gravel\matlibs\gravel_corona.mat")
        self.assertIsNone(flib.resolve_path("", "C:\\", "C:\\"))


def library_report(count=23473, kind="preset", extra=()):
    lines = [f"F|{b64('Lawn')}|{kind}|{count}|203.2|25400|0|100",
             f"G|1|2|100|{b64('_fp_wildgrass_09')}|{b64('_fp_wildgrass_09')}|n",
             f"A|1|3|1|0|{b64('Surface Area')}",
             f"A|2|0|1|1|{b64('Hole')}",
             f"S|{b64('Ground')}",
             f"M|{b64('Lawn')}|{b64('Forest_Pro')}",
             f"M|{b64('_fp_wildgrass_09')}|{b64('Editable_mesh')}",
             f"P|{b64('_fp_wildgrass_09')}",
             f"D|{b64('area2m')}",
             f"Z|{b64('_fp_wildgrass_09')}"]
    return ok(*(lines + list(extra)))


class ForestFromLibraryTests(LibraryFixture):
    def run_place(self, response, **kw):
        fake = FakeClient(response)
        args = {"item": "_fp_common_grass_01", "surfaces": ["Ground"], "allow_undo_flush": True}
        args.update(kw)
        with patch.object(forest, "client", fake):
            out = forest.forest_from_library(**args)
        return out, fake

    def test_needs_undo_flush_consent(self):
        fake = FakeClient(library_report())
        with patch.object(forest, "client", fake), self.assertRaises(ValueError) as ctx:
            forest.forest_from_library(item="_fp_common_grass_01", surfaces=["Ground"])
        self.assertIn("clears 3ds Max's undo history", str(ctx.exception))
        self.assertEqual(fake.scripts, [])  # refused before anything reaches Max
        for kw in ({"allow_undo_flush": "yes"}, {"allow_undo_flush": 1}):
            fake = FakeClient(library_report())
            with self.subTest(kw=kw), patch.object(forest, "client", fake), self.assertRaises(ValueError):
                forest.forest_from_library(item="_fp_common_grass_01", surfaces=["Ground"], **kw)
            self.assertEqual(fake.scripts, [])
        # item and file checks come first: a bad item fails with its own error, not the consent one
        with self.assertRaises(fh.ForestError) as ctx:
            forest.forest_from_library(item="Almond", surfaces=["Ground"])
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_ITEM")
        out, _ = self.run_place(library_report())
        self.assertTrue(out["undo_history_cleared"])
        self.assertFalse(out["undoable"])
        self.assertNotIn("undo", out)
        # the nodes to delete by hand: the forest, then its merged sources
        self.assertEqual(out["remove_with"], ["Lawn", "_fp_wildgrass_09"])

    def test_3d_remove_with_lists_the_new_forest(self):
        report = ok(f"F|{b64('Forest_cane')}|3d|220|300|300|0|100",
                    f"G|1|2|100|{b64('_fp_cane01')}|{b64('_fp_cane01')}|n",
                    f"M|{b64('_fp_cane01')}|{b64('Editable_mesh')}")
        out, _ = self.run_place(report, item="_fp_cane01")
        self.assertEqual(out["merged_nodes"], [{"name": "_fp_cane01", "class": "Editable_mesh"}])
        self.assertEqual(out["remove_with"], ["Forest_cane", "_fp_cane01"])
        self.assertFalse(out["undoable"])

    def test_preset_script(self):
        out, fake = self.run_place(library_report(), exclude_splines=["Hole"], name="Lawn", matlib_renderer="vray")
        script = fake.scripts[0]
        check_script(self, script)
        self.assertEqual(fake.kwargs[0], {"timeout": forest.LIBRARY_TIMEOUT_S})
        maxfile = os.path.join(self.root, "Presets", "Lawns", "objects", "_fp_common_grass_01.max")
        self.assertIn(f'fpDec "{b64(os.path.normpath(maxfile))}"', script)
        self.assertIn(f'fpDec "{b64("_fp_common_grass_01")}"', script)
        self.assertIn(b64(os.path.join(self.matlib_dir, "3d_vray.mat")), script)
        # phase 1 probe outside the undo stack, deleted before phase 2
        probe = script.index("with undo off (\n    mergeMAXFile libfile #(objname)")
        phase2 = script.index("mergeMAXFile libfile (#(objname) + deps)")
        self.assertLess(probe, script.index("for o in probeNodes where isValidNode o do delete o"))
        self.assertLess(script.index("for o in probeNodes where isValidNode o do delete o"), phase2)
        # Library merges clear the undo history and aren't recorded (live), so nothing goes in an undo step:
        # phase 2 merges the object and its sources in one call, then retargets, swaps materials and hides,
        # all with undo off. No copies, renames or deleted originals (an undo record that referred to
        # originals deleted with undo off crashed Max on undo).
        self.assertNotRegex(script, r'\bundo\s+(on|true|")')
        for gone in ("copy o", "_mcp_merged", "copies", "merged["):
            self.assertNotIn(gone, script)
        self.assertEqual(deletes_outside_undo_off(forest._FROM_LIBRARY_BODY), [])
        phase2_try = script.index("try (\n    with undo off (\n        mergeMAXFile libfile (#(objname) + deps) ")
        catch_at = script.index(") catch (\n    -- fpNewNodes again")
        self.assertLess(phase2_try, phase2)
        for step in ("newNodes = fpNewNodes mark2", "f.surflist = surfs", "loadTempMaterialLibrary mp",
                     "hide c", "f.trees.update()\n    )\n", 'format "FPOK\\n" to:ss',
                     'for x in notes do format "W|%\\n" x to:ss'):
            with self.subTest(step=step):
                self.assertLess(phase2, script.index(step, phase2))
                self.assertLess(script.index(step, phase2), catch_at)
        self.assertLess(script.index("local mark2 = fpMark()"), phase2_try)
        # on an error: delete everything created since the merge (a 3D item's new forest included), rethrow
        self.assertIn(") catch (\n    -- fpNewNodes again: it also finds a 3D item's new forest and nodes merged "
                      "before the error\n    leftovers = fpNewNodes mark2\n    with undo off (for o in leftovers "
                      "where isValidNode o do delete o)\n    throw()\n)\nss as string", script)
        self.assertIn("if f == undefined and src == undefined do throw (\"MERGE_FAILED:", script)
        self.assertEqual(script.count("mergeMAXFile"), 2)
        self.assertEqual(script.count("#useSceneMtlDups"), 2)  # never replaces the scene's materials
        self.assertNotIn("#useMergedMtlDups", script)
        self.assertIn("cn[i] as string", script)  # dependencies via conamelist
        self.assertIn("fpKeepAreas f (if k3 > 0 then #(k3) else #())", script)
        self.assertIn("act[k3] = (incs.count == 0)", script)
        self.assertIn("for s in incs do fpAppendArea f s 0 0 true", script)
        self.assertIn("for s in excs do fpAppendArea f s 0 1 true", script)
        self.assertIn(f'#("{b64("Hole")}")', script)
        self.assertIn("loadTempMaterialLibrary mp", script)
        self.assertIn("if true do (\n            for c in (f.cobjlist as array)", script)  # hide merged sources
        self.assertEqual(out["count"], 23473)
        self.assertEqual(out["kind"], "preset")
        self.assertEqual(out["sources_merged"], ["_fp_wildgrass_09"])
        self.assertEqual(out["dropped_preset_areas"], ["area2m"])
        self.assertEqual(out["hidden_sources"], ["_fp_wildgrass_09"])
        self.assertEqual(out["areas"][1], {"index": 2, "name": "Hole", "type": "spline", "active": True,
                                           "exclude": True})
        self.assertEqual(out["items"][0]["source"], "_fp_wildgrass_09")
        self.assertTrue(out["items"][0]["source_linked"])
        self.assertEqual(out["materials"]["matlib"], os.path.join(self.matlib_dir, "3d_vray.mat"))
        self.assertEqual(out["library_item"]["uuid"], "7507a9b2-bdcb-4a99-a8a1-bfbf9e256abd")

    def test_3d_item(self):
        report = library_report(kind="3d", count=220, extra=[f"X|swapped|{b64('_fp_grass')}"])
        out, fake = self.run_place(report, item="_fp_cane01", items_per_m2=1437.0 / 9.0, matlib_renderer="corona",
                                   include_splines=["Rect"])
        script = fake.scripts[0]
        self.assertIn('units.decodeValue "300.0000cm"', script)
        self.assertIn(f'fpDec "{b64("_fp_grass")}"', script)
        self.assertEqual(out["materials"]["swapped"], ["_fp_grass"])
        self.assertAlmostEqual(out["estimated_items_per_m2"], 159.67, places=2)

    def test_3d_without_matlib_warns(self):
        out, _ = self.run_place(library_report(kind="3d", count=5), item="_fp_cane01")
        self.assertTrue(any("matlib_renderer" in w for w in out["warnings"]))

    def test_zero_items(self):
        with self.assertRaises(fh.ForestError) as ctx:
            self.run_place(library_report(count=0))
        self.assertEqual(ctx.exception.code, "ZERO_ITEMS")
        self.assertEqual(ctx.exception.details["name"], "Lawn")

    def test_refusals(self):
        cases = [({"item": "Almond"}, "UNSUPPORTED_ITEM"), ({"item": "nothing like it"}, "NOT_FOUND"),
                 ({"item": "_fp_daisy", "matlib_renderer": "vray"}, ValueError),  # .pmat
                 ({"matlib_renderer": "arnold"}, "FILE_NOT_FOUND"),
                 ({"matlib_renderer": "v ray"}, ValueError),
                 ({"surfaces": []}, ValueError),
                 ({"include_splines": ["A"], "exclude_splines": ["A"]}, ValueError),
                 ({"include_splines": ["Ground"]}, ValueError),
                 ({"items_per_m2": 0}, ValueError),
                 ({"hide_sources": "no"}, ValueError)]
        for kw, expected in cases:
            with self.subTest(kw=kw):
                if expected is ValueError:
                    with self.assertRaises(ValueError):
                        self.run_place(library_report(), **kw)
                else:
                    with self.assertRaises(fh.ForestError) as ctx:
                        self.run_place(library_report(), **kw)
                    self.assertEqual(ctx.exception.code, expected)

    def test_missing_maxfile(self):
        os.remove(os.path.join(self.root, "3D", "Flowers and grass", "objects", "_fp_grass.max"))
        flib._parse_cache.clear()
        with self.assertRaises(fh.ForestError) as ctx:
            self.run_place(library_report(), item="_fp_cane01")
        self.assertEqual(ctx.exception.code, "FILE_NOT_FOUND")


# --------------------------------------------------------------------------------------------
# forest_validate
# --------------------------------------------------------------------------------------------

LISTS_OK = {"namelist": 2, "cobjlist": 2, "geomlist": 2, "problist": 2, "widthlist": 2, "heightlist": 2,
            "conamelist": 2}


def collect_lines(lengths=None, items=None, areas=None, surfaces=None, count="12933|1|0", mode=0, units=100.0,
                  ar_len=None):
    lengths = dict(LISTS_OK, **(lengths or {}))
    out = [f"N|{b64('Forest001')}|{mode}|{units}|{units}|1|{b64('dense.bmp')}|50|0|0|0|0|2|0|100|100"]
    for name in fh.ITEM_LISTS:
        out.append(f"L|{name}|{lengths.get(name, 0)}")
    areas = areas if areas is not None else [(1, "3", "1", "0", "u", "", "Surface Area")]
    for name in fh.AREA_LISTS:
        out.append(f"R|{name}|{(ar_len or {}).get(name, len(areas))}")
    items = items if items is not None else [
        (1, "2", "50", "20", "50", "n", "Box", "1", "Box", "Box", "20", "50"),
        (2, "2", "50", "30", "22", "n", "Tea", "1", "Tea", "Tea", "30", "22")]
    for it in items:
        i, geom, prob, w, h, cobj, cname, sg, label, coname, sw, sh = it
        out.append("|".join(["I", str(i), geom, prob, w, h, cobj, b64(cname), sg,
                             "-" if label is None else b64(label), "-" if coname is None else b64(coname), sw, sh]))
    for a in areas:
        i, t, act, inc, node, node_name, name = a
        out.append("|".join(["A", str(i), t, act, inc, node, b64(node_name), b64(name)]))
    surfaces = surfaces if surfaces is not None else [(1, "n", "Ground", "90000")]  # 300 x 300 cm
    for s in surfaces:
        out.append("|".join(["S", str(s[0]), s[1], b64(s[2]), s[3]]))
    out.append("C|" + count)
    return out


def codes(issues):
    return [i["code"] for i in issues]


class ForestValidateTests(unittest.TestCase):
    def state(self, **kw):
        return fh.parse_state(collect_lines(**kw))

    def test_healthy(self):
        state = self.state()
        self.assertEqual(state["name"], "Forest001")
        self.assertEqual(state["count"], 12933)
        analysis = fh.analyze(state)
        self.assertEqual(analysis["errors"], [])
        self.assertEqual(analysis["warnings"], [])
        self.assertAlmostEqual(analysis["estimated_count"], 12933.0, places=0)  # 1437 * 9 m2 / 1 m2

    def test_errors(self):
        state = self.state(
            lengths={"geomlist": 1, "problist": 3},
            items=[(1, "3", "0", "20", "0", "u", "", "0", "Box", "Box", "", ""),
                   (2, "-", "50", "30", "22", "n", "Tea2", "1", "Tea", "Tea", "30", "22"),
                   (3, "-", "50", "-", "-", "-", "", "0", "-", "-", "", "")],
            areas=[(1, "3", "1", "0", "u", "", "Surface Area"), (2, "0", "1", "0", "u", "", "area2m")],
            surfaces=[], count="-1|0|1", ar_len={"aridlist": 1})
        analysis = fh.analyze(state)
        found = codes(analysis["errors"])
        for code in ("ITEM_LIST_SHORT", "CRASH_RISK", "PROB_NONPOSITIVE", "SIZE_NONPOSITIVE", "AREA_LIST_LENGTH",
                     "AREA_NODE_MISSING", "SURFACE_LIST_EMPTY", "NOT_REBUILT"):
            self.assertIn(code, found)
        self.assertNotIn("SOURCE_MISSING", found)  # geom 3 is reported as CRASH_RISK only
        self.assertIn("ITEM_LIST_LONG", codes(analysis["warnings"]))
        self.assertIn("CONAME_MISMATCH", codes(analysis["warnings"]))

    def test_inactive_and_units(self):
        analysis = fh.analyze(self.state(areas=[(1, "3", "0", "0", "u", "", "Surface Area")], count="0|1|0"))
        self.assertIn("NO_ACTIVE_AREA", codes(analysis["errors"]))
        self.assertIn("NO_ITEMS", codes(analysis["errors"]))
        self.assertIsNone(analysis["estimated_count"])
        analysis = fh.analyze(self.state(units=10000.0, surfaces=[(1, "n", "Small", "5000")]))
        self.assertIn("UNITS_TOO_LARGE", codes(analysis["warnings"]))
        analysis = fh.analyze(self.state(mode=1, areas=[(1, "0", "1", "0", "n", "Rect", "Rect")]))
        self.assertIn("CUSTOM_EDIT_MODE", codes(analysis["warnings"]))
        self.assertIn("SURFACE_AREA_MISSING", codes(analysis["warnings"]))

    def test_fix_plan(self):
        state = self.state(
            lengths={"namelist": 3, "cobjlist": 2, "geomlist": 1, "problist": 3, "widthlist": 4, "heightlist": 2,
                     "conamelist": 3},
            items=[(1, "2", "50", "20", "50", "u", "", "0", "Box", "Box", "", ""),
                   (2, "-", "50", "30", "22", "n", "Tea", "1", "Tea", "Tea", "30", "22"),
                   (3, "-", "50", "40", "-", "-", "", "0", "Cone", "Cone", "", ""),
                   (4, "-", "-", "50", "-", "-", "", "0", "-", "-", "", "")],
            ar_len={"aridlist": 1, "artypelist": 1}, areas=[(1, "3", "1", "0", "u", "", "Surface Area"),
                                                           (2, "0", "0", "0", "u", "", "x")])
        plan = fh.fix_plan(state)
        by_list = {step["list"]: step for step in plan}
        self.assertEqual(plan[0]["list"], "cobjlist")  # relink before geomlist reads it
        self.assertEqual(by_list["cobjlist"]["relink"], [1])
        self.assertEqual((by_list["geomlist"]["from"], by_list["geomlist"]["to"]), (1, 3))
        self.assertEqual(by_list["heightlist"]["fallback"], 50.0)
        self.assertNotIn("widthlist", by_list)  # longer lists are never truncated
        self.assertNotIn("problist", by_list)
        self.assertEqual(by_list["aridlist"]["action"], "pad_area")
        self.assertEqual(by_list["aridlist"]["to"], 2)
        self.assertNotIn("arnodelist", by_list)
        ops = forest._fix_ops(plan)
        self.assertIn('#("cobjlist", #(1))', ops)
        self.assertIn('#("heightlist", #size, 50.000000)', ops)
        self.assertIn('#("aridlist", undefined, 2)', ops)
        self.assertIn('#("artypelist", 0, 2)', ops)
        self.assertNotIn(", 3)", ops.replace("#(1)", ""))

    def test_tool_without_fix(self):
        fake = FakeClient(ok(*collect_lines()))
        with patch.object(forest, "client", fake):
            out = forest.forest_validate("Forest001")
        script = fake.scripts[0]
        check_script(self, script)
        self.assertIn("fn fpCollect nd ss doBuild", script)
        self.assertIn("fpCollect nd ss true", script)
        self.assertNotIn("setProperty nd p v", script)
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 12933)
        self.assertEqual(out["areas"], [{"index": 1, "name": "Surface Area", "type": "surface", "active": True,
                                         "exclude": False}])
        self.assertEqual(len(fake.scripts), 1)

    def test_tool_fix(self):
        before = collect_lines(lengths={"geomlist": 1, "problist": 1},
                               items=[(1, "2", "50", "20", "50", "n", "Box", "1", "Box", "Box", "20", "50"),
                                      (2, "-", "-", "30", "22", "n", "Tea", "1", "Tea", "Tea", "30", "22")])
        after = ["X|geomlist|1|2|0", "X|problist|1|2|0"] + collect_lines()
        fake = FakeClient(ok(*before), ok(*after))
        with patch.object(forest, "client", fake):
            out = forest.forest_validate("Forest001", fix=True)
        script = fake.scripts[1]
        check_script(self, script)
        self.assertIn('undo "MCP Forest validate fix" on', script)
        self.assertIn('for x in #(#("geomlist", 1), #("problist", 1)) do', script)
        self.assertIn("if nd.namelist.count != n do throw \"STALE", script)
        self.assertIn("then 2 else 0", script)  # padded geometry type: 2 with a source, else 0; never 3
        self.assertIn('for op in #(#("geomlist"), #("problist")) do', script)
        self.assertTrue(out["ok"])
        self.assertEqual(out["fixed"], [{"list": "geomlist", "from": 1, "to": 2},
                                        {"list": "problist", "from": 1, "to": 2}])
        self.assertIn("ITEM_LIST_SHORT", codes(out["errors_before"]))

    def test_tool_fix_nothing(self):
        fake = FakeClient(ok(*collect_lines()))
        with patch.object(forest, "client", fake):
            out = forest.forest_validate("Forest001", fix=True)
        self.assertEqual((out["fixed"], len(fake.scripts)), ([], 1))

    def test_dialog_blocking_the_check_before_a_fix_is_retryable(self):
        import json
        from maxmcp.helpers.precheck import PrecheckBlocked
        from maxmcp.max_client import DialogBlocked
        client = MagicMock()
        client.send_command.side_effect = DialogBlocked("r1", "maxscript", [{"title": "Missing External Files"}])
        with patch.object(forest, "client", client):
            with self.assertRaises(PrecheckBlocked) as ctx:
                forest.forest_validate("Forest001", fix=True)
        self.assertEqual(client.send_command.call_count, 1)  # never the fix
        payload = json.loads(ctx.exception.bridge_message)
        self.assertEqual((payload["code"], payload["retryable"]), ("BLOCKED_BY_DIALOG", True))
        self.assertIn("Nothing was fixed", payload["message"])
        self.assertIn("Then repeat forest_validate", payload["hint"])
        self.assertFalse(payload["details"]["changed"])

    def test_dialog_blocking_a_plain_check_stays_dialog_blocked(self):
        from maxmcp.max_client import DialogBlocked
        client = MagicMock()
        client.send_command.side_effect = DialogBlocked("r1", "maxscript", [{"title": "Missing External Files"}])
        with patch.object(forest, "client", client):
            with self.assertRaises(DialogBlocked):  # a read-only call: max_dialogs reports its result
                forest.forest_validate("Forest001")


# --------------------------------------------------------------------------------------------
# scatter_forest_pack
# --------------------------------------------------------------------------------------------

def scatter_report(count=16023, area="1000000", geometry=(("Box", "20", "50"),)):
    lines = [f"F|{b64('ForestScatter')}|{count}|{area}|100|300|300"]
    lines += [f"G|{b64(n)}|{w}|{h}" for n, w, h in geometry]
    return ok(*lines)


class ScatterTests(unittest.TestCase):
    def run_scatter(self, response=None, **kw):
        fake = FakeClient(response or scatter_report())
        args = {"surfaces": ["Plane"], "geometry": ["Box"]}
        args.update(kw)
        with patch.object(scattering, "client", fake):
            out = scattering.scatter_forest_pack(**args)
        return out, fake.scripts[0]

    def test_script(self):
        out, script = self.run_scatter()
        check_script(self, script)
        self.assertIn("fp.pf_aractivelist = #(true)", script)
        # lists collected into locals before the undo step, never inline (live: "Bad free thunk")
        self.assertLess(script.index("local geoNames = for g in geos collect g.name"),
                        script.index('undo "MCP Forest scatter" on'))
        self.assertLess(script.index("local geoTypes = for g in geos collect 2"),
                        script.index('undo "MCP Forest scatter" on'))
        self.assertIn("fp.namelist = geoNames\n    fp.conamelist = geoNames\n    fp.geomlist = geoTypes", script)
        self.assertIn('fp.units_x = units.decodeValue "300.0000cm"', script)
        self.assertIn("fp.direction = 0", script)
        self.assertIn("fp.trees.update()\nlocal n = fp.trees.count()", script)
        self.assertIn('undo "MCP Forest scatter" on', script)
        self.assertNotIn("fp.arnodelist", script)  # the minimal recipe: surflist + an active Surface Area
        self.assertNotIn("fp.artypelist", script)
        self.assertIn("fp.zrotmin = -180", script)
        self.assertIn("fp.scalexmin = 85", script)
        self.assertEqual(out["count"], 16023)
        self.assertEqual(out["estimated_count"], 15967)  # 1437 * 100 m2 / 9 m2
        self.assertEqual(out["surface_area_m2"], 100.0)
        self.assertEqual(out["geometry"], [{"name": "Box", "width": 20.0, "height": 50.0}])
        self.assertEqual(out["warnings"], [])

    def test_density_and_facing(self):
        _, script = self.run_scatter(density=200, facing_mode=1)
        self.assertIn(f'units.decodeValue "{300 / math.sqrt(2):.4f}cm"', script)
        self.assertIn("fp.direction = 100", script)
        out, script = self.run_scatter(items_per_m2=1437.0, facing_mode="up")
        self.assertIn('fp.units_x = units.decodeValue "100.0000cm"', script)
        self.assertIn("fp.direction = 100", script)
        self.assertEqual(out["facing"], "up")
        _, script = self.run_scatter(facing_mode="normal", density_units_x_cm=500, density_units_y_cm=400)
        self.assertIn("fp.direction = 0", script)
        self.assertIn('fp.units_y = units.decodeValue "400.0000cm"', script)

    def test_zero_items(self):
        with self.assertRaises(fh.ForestError) as ctx:
            self.run_scatter(scatter_report(count=0, area="100"))
        self.assertEqual(ctx.exception.code, "ZERO_ITEMS")
        self.assertEqual(ctx.exception.details["count"], 0)

    def test_validation(self):
        bad = [{"surfaces": []}, {"geometry": []}, {"geometry": ["Plane"]}, {"probabilities": [1, 2]},
               {"probabilities": [0]}, {"density": 0}, {"density": True}, {"scale_min": 120, "scale_max": 80},
               {"z_rotation_min": 10, "z_rotation_max": -10}, {"facing_mode": 2}, {"facing_mode": "down"},
               {"facing_mode": True}, {"viewport_mode": 9}, {"items_per_m2": 0}, {"icon_size_cm": 0},
               {"name": ""}, {"seed": 2**40}]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.run_scatter(**kw)

    def test_names_are_encoded(self):
        _, script = self.run_scatter(surfaces=['Plane "A"'], geometry=["C:\\x"], name="Lawn\n2")
        self.assertIn(b64('Plane "A"'), script)
        self.assertIn(b64("C:\\x"), script)
        self.assertIn(b64("Lawn\n2"), script)
        self.assertNotIn('Plane "A"', script)


if __name__ == "__main__":
    unittest.main()
