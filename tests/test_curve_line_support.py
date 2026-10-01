"""Line objects count as editable spline bases in curve read/edit MAXScript."""

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout
# over an installed maxmcp.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import curve_runtime as cr  # noqa: E402
from maxmcp.tools import curve_edit as ce  # noqa: E402
from maxmcp.tools import curve_model as cm  # noqa: E402
from maxmcp.tools import splines as sp  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TOKEN = "-".join(["AB"] * 32)
PREDICATE = "fn cvIsSpline b = (isKindOf b SplineShape or isKindOf b line)"
BARE_GATE = re.compile(r"isKindOf\s+\(?\s*[\w.]+\s*\)?\s+SplineShape", re.IGNORECASE)


class _Stop(Exception):
    pass


def _capture(scripts):
    def run(script, *args, **kwargs):
        scripts.append(script)
        raise _Stop()
    return run


def _strip_strings(script):
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', script)


class PredicateTests(unittest.TestCase):
    def test_imports_checkout(self):
        self.assertEqual(Path(cr.__file__).resolve(), REPO / "maxmcp" / "helpers" / "curve_runtime.py")

    def test_predicate_accepts_splineshape_and_line_only(self):
        self.assertEqual(cr.SPLINE_BASE_FUNCTION.strip(), PREDICATE)

    def test_curve_functions_define_predicate_before_cvbase(self):
        text = cr.CURVE_FUNCTIONS
        self.assertTrue(text.startswith(cr.SPLINE_BASE_FUNCTION))
        self.assertIn("if not cvIsSpline obj.baseobject do throw "
                      '"Editable spline base required; no automatic conversion"', text)
        self.assertLess(text.index("fn cvIsSpline"), text.index("fn cvBase"))

    def test_owned_sources_have_no_bare_splineshape_gate(self):
        for rel in ("maxmcp/helpers/curve_runtime.py", "maxmcp/tools/curve_model.py",
                    "maxmcp/tools/curve_edit.py", "maxmcp/tools/splines.py"):
            source = (REPO / rel).read_text(encoding="utf-8").replace(PREDICATE, "")
            self.assertEqual(BARE_GATE.findall(source), [], rel)


class GeneratedScriptTests(unittest.TestCase):
    def _assert_uses_predicate(self, script, use):
        self.assertIn(PREDICATE, script)
        self.assertEqual(BARE_GATE.findall(script.replace(PREDICATE, "")), [])
        self.assertLess(script.index(PREDICATE), script.index(use))
        bare = _strip_strings(script)
        self.assertEqual(bare.count("("), bare.count(")"))

    def test_read_curve(self):
        scripts = []
        with patch.object(cr, "run", _capture(scripts)), self.assertRaises(_Stop):
            cr.read_curve("Line001")
        self._assert_uses_predicate(scripts[0], "local shape = cvBase obj")

    def test_inspect_curve(self):
        scripts = []
        with patch.object(cr, "run", _capture(scripts)), self.assertRaises(_Stop):
            ce.inspect_curve(name="Line001")
        self._assert_uses_predicate(scripts[0], "cvBase obj")

    def test_edit_curve(self):
        scripts = []
        with patch.object(ce, "run", _capture(scripts)), self.assertRaises(_Stop):
            ce.edit_curve(TOKEN, [{"op": "set", "spline": 1, "knot": 1, "pos": [1, 2, 3]}], name="Line001")
        self._assert_uses_predicate(scripts[0], "local shape = cvBase obj")
        self.assertNotIn("convertTo", scripts[0])

    def test_curve_model_token(self):
        self.assertIn("fn cmToken obj = (if cvIsSpline obj.baseobject then cvToken obj else lfToken obj)",
                      cm.functions())
        scripts = []
        with patch.object(cm, "run", _capture(scripts)), self.assertRaises(_Stop):
            cm._read("Line001", 0)
        self._assert_uses_predicate(scripts[0], "fn cmToken")


class DrawSplineTests(unittest.TestCase):
    def _script(self, **kwargs):
        client = MagicMock()
        client.send_command.return_value = {"result": "__ERROR__|stop"}
        with patch.object(sp, "client", client):
            out = sp.draw_spline(name="Line001", **kwargs)
        self.assertEqual(out, {"status": "error", "error": "stop"})
        script = client.send_command.call_args.args[0]
        self.assertIn(PREDICATE, script)
        self.assertEqual(BARE_GATE.findall(script.replace(PREDICATE, "")), [])
        bare = _strip_strings(script)
        self.assertEqual(bare.count("("), bare.count(")"))
        return script

    def _assert_guard(self, script, convert=False):
        self.assertLess(script.index(PREDICATE), script.index("if not (cvIsSpline obj.baseObject) do ("))
        self.assertIn("convertToSplineShape obj; wasConverted = true", script)
        rejection = 'if obj.modifiers.count > 0 do throw "Editable spline base required; convert=true explicitly collapses the stack"'
        if convert:
            self.assertNotIn(rejection, script)
        else:
            self.assertIn(rejection, script)

    def test_get_reports_line_as_editable(self):
        script = self._script(action="get")
        self.assertIn("((cvIsSpline obj.baseObject) as string)", script)
        self.assertLess(script.index(PREDICATE), script.index("cvIsSpline obj.baseObject"))

    def test_editing_actions_use_shared_guard(self):
        cases = [
            dict(action="add_spline", points=[[0, 0, 0], [1, 0, 0]]),
            dict(action="set_knots", knots=[{"spline": 1, "knot": 1, "pos": [0, 0, 1]}]),
            dict(action="insert_knot", segment=1),
            dict(action="delete_knot", knot_index=1),
            dict(action="delete_spline"),
        ]
        for kwargs in cases:
            with self.subTest(action=kwargs["action"]):
                self._assert_guard(self._script(**kwargs))

    def test_convert_flag_unchanged(self):
        self._assert_guard(self._script(action="delete_spline", convert=True), convert=True)

    def test_parametric_readback_hint_unchanged(self):
        client = MagicMock()
        client.send_command.return_value = {"result": "SHAPE|Rectangle|false|Rectangle|0\nNOKNOTS|err\n"}
        with patch.object(sp, "client", client):
            out = sp.draw_spline(action="get", name="Rectangle001")
        self.assertFalse(out["editable"])
        self.assertEqual(out["knots_unavailable"], "knot readback failed — parametric shape? "
                         "set_knots/add_spline convert it to SplineShape first")

    def test_line_get_parses_editable(self):
        client = MagicMock()
        client.send_command.return_value = {"result": "SHAPE|line|true|line|1\nSPL|1|false|2\n"}
        with patch.object(sp, "client", client):
            out = sp.draw_spline(action="get", name="Line001")
        self.assertTrue(out["editable"])
        self.assertEqual(out["base_class"], "line")


if __name__ == "__main__":
    unittest.main()
