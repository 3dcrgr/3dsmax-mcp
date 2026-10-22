"""Issue #47: hint for Edit_Poly modifier methods called while the modifier is not current."""

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers.error_hints import (  # noqa: E402
    EDIT_POLY_HINT_MESSAGE,
    edit_poly_hint,
    resolve_error_hint,
)
from maxmcp.tool_response import make_structured_tool  # noqa: E402

SLICE = 'ep = $Slab.modifiers[#Edit_Poly]; ep.SetOperation #Slice; ep.ButtonOp #Slice'
FAULT = "-- Unknown system exception: EXCEPTION_ACCESS_VIOLATION"


def _load_execute(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.execute", None)
        return importlib.import_module("maxmcp.tools.execute")


class EditPolyHintTests(unittest.TestCase):
    def test_access_violation_on_edit_poly_script(self):
        hint = edit_poly_hint(SLICE, FAULT)
        self.assertIsNotNone(hint)
        self.assertIn("modPanel.setCurrentObject", hint["message"])
        self.assertIn("mesh_edit", hint["suggested_tools"])

    def test_fault_fires_even_when_script_sets_current_object(self):
        script = "select $Slab; max modify mode; modPanel.setCurrentObject $Slab.modifiers[1]; " + SLICE
        self.assertIsNotNone(edit_poly_hint(script, FAULT))

    def test_any_error_without_set_current_object(self):
        self.assertIsNotNone(edit_poly_hint(SLICE, "-- Runtime error: something else"))

    def test_plain_error_with_set_current_object_is_left_alone(self):
        script = "modPanel.setCurrentObject $Slab.modifiers[#Edit_Poly]; " + SLICE
        self.assertIsNone(edit_poly_hint(script, "-- Type error: wrong argument"))

    def test_method_through_modifiers_index_without_class_name(self):
        script = "m = $Slab.modifiers[1]; m.GetFaceNormal 3"
        self.assertIsNotNone(edit_poly_hint(script, FAULT))

    def test_editable_poly_and_other_scripts_do_not_match(self):
        self.assertIsNone(edit_poly_hint("polyop.getFaceNormal $Slab 1", FAULT))
        self.assertIsNone(edit_poly_hint("$Slab.EditablePoly.ButtonOp #Slice", FAULT))
        self.assertIsNone(edit_poly_hint("", FAULT))
        self.assertIsNone(edit_poly_hint(None, FAULT))

    def test_resolve_prefers_edit_poly_over_generic_suggestions(self):
        script = "addModifier $Slab (Edit_Poly()); " + SLICE
        hint = resolve_error_hint(
            tool_name="execute_maxscript", error={"message": FAULT}, script=script)
        self.assertEqual(hint["message"], EDIT_POLY_HINT_MESSAGE)

    def test_resolve_keeps_generic_suggestions_for_other_scripts(self):
        hint = resolve_error_hint(
            tool_name="execute_maxscript", error={"message": "x"}, script="addModifier $a (Bend())")
        self.assertIn("add_modifier", hint["suggested_tools"])


class ExecuteMaxscriptEnvelopeTests(unittest.TestCase):
    def test_caught_access_violation_gets_edit_poly_hint(self):
        client = mock.Mock()
        client.send_command.return_value = {"result": "__MCP_MS_ERR__:" + FAULT}
        execute = _load_execute(client)
        tool = make_structured_tool(execute.execute_maxscript)
        with mock.patch.dict("os.environ", {"MCP_TRIPBACK_MODE": "minimal"}):
            envelope = tool(code=SLICE)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["hint"]["message"], EDIT_POLY_HINT_MESSAGE)


if __name__ == "__main__":
    unittest.main()
