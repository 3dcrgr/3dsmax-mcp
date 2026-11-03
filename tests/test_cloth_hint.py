"""Issue #52: Cloth modifier calls need Cloth current, and collision-object properties fault."""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers.error_hints import (  # noqa: E402
    CLOTH_HINT_MESSAGE,
    CLOTH_SIMULATE_WARNING,
    cloth_hint,
    cloth_success_warning,
    current_modifier_hint,
    resolve_error_hint,
)

DROP = 'c = Cloth(); addModifier #($Throw, $Sofa) c; c.setObjectType $Sofa 1 true; c.simulateLocal 1 2.0 true'
FAULT = "-- Unknown system exception: EXCEPTION_ACCESS_VIOLATION"
CURRENT = "select $Throw; max modify mode; modPanel.setCurrentObject c; "


def _load_execute(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.execute", None)
        return importlib.import_module("maxmcp.tools.execute")


class ClothHintTests(unittest.TestCase):
    def test_fault_on_cloth_script(self):
        hint = cloth_hint(DROP, FAULT)
        self.assertEqual(hint["message"], CLOTH_HINT_MESSAGE)
        self.assertIn("modPanel.setCurrentObject", hint["message"])
        self.assertIn("COLLISION", hint["message"])

    def test_fault_fires_even_when_cloth_is_current(self):
        # The collision-property fault happens with Cloth current too.
        script = CURRENT + 'c.getObjectProperty $Sofa "offset"'
        self.assertIsNotNone(cloth_hint(script, FAULT))

    def test_any_error_without_set_current_object(self):
        self.assertIsNotNone(cloth_hint(DROP, "-- Runtime error: whatever"))

    def test_plain_error_with_cloth_current_is_left_alone(self):
        self.assertIsNone(cloth_hint(CURRENT + DROP, "-- Type error: wrong argument"))

    def test_cloth_methods_on_a_modifier_reached_by_index(self):
        self.assertIsNotNone(cloth_hint("m = $Throw.modifiers[1]; m.simulateLocal 1 2.0 true", FAULT))
        self.assertIsNotNone(cloth_hint('$Throw.modifiers[#Cloth].setObjectProperty $Throw "dynFric" 0.8', FAULT))

    def test_other_scripts_do_not_match(self):
        for script in ("box()", "$Box001.simulate = true", "physx.simulate()", "", None):
            self.assertIsNone(cloth_hint(script, FAULT), script)

    def test_edit_poly_hint_wins_when_both_apply(self):
        script = "ep = $Slab.modifiers[#Edit_Poly]; ep.ButtonOp #Slice; " + DROP
        self.assertIn("Edit_Poly", current_modifier_hint(script, FAULT)["message"])
        self.assertEqual(current_modifier_hint(DROP, FAULT)["message"], CLOTH_HINT_MESSAGE)

    def test_resolve_error_hint_uses_it(self):
        hint = resolve_error_hint(tool_name="execute_maxscript", error={"message": FAULT}, script=DROP)
        self.assertEqual(hint["message"], CLOTH_HINT_MESSAGE)


class ClothWarningTests(unittest.TestCase):
    def test_simulate_without_current_warns(self):
        self.assertEqual(cloth_success_warning(DROP), CLOTH_SIMULATE_WARNING)
        self.assertEqual(cloth_success_warning("c = $T.modifiers[#Cloth]; c.simulate false"),
                         CLOTH_SIMULATE_WARNING)

    def test_simulate_with_current_or_no_simulate_is_quiet(self):
        self.assertIsNone(cloth_success_warning(CURRENT + DROP))
        self.assertIsNone(cloth_success_warning("c = Cloth(); addModifier $T c"))
        self.assertIsNone(cloth_success_warning("physx.simulate()"))

    def _execute(self, result):
        client = mock.Mock()
        response = {"result": result}
        client.send_command.return_value = response
        out = _load_execute(client).execute_maxscript(code=DROP)
        return response, out

    def test_execute_maxscript_adds_the_warning_on_success(self):
        response, _ = self._execute("OK")
        self.assertIn(CLOTH_SIMULATE_WARNING, response["meta"]["mcpWarnings"])

    def test_execute_maxscript_hint_on_failure_and_no_warning(self):
        response, out = self._execute("__MCP_MS_ERR__:" + FAULT)
        self.assertNotIn("mcpWarnings", response["meta"])
        self.assertEqual(json.loads(out)["hint"]["message"], CLOTH_HINT_MESSAGE)


if __name__ == "__main__":
    unittest.main()


class VRayOutputHintTests(unittest.TestCase):
    def test_read_only_output_points_at_the_settings_object(self):
        from maxmcp.helpers.error_hints import resolve_error_hint
        hint = resolve_error_hint(error={"message": "Attempt to set read-only property: output_saveRawFile"})
        self.assertIn("renderers.current[1].value", hint["message"])
        self.assertEqual(hint["suggested_tools"], ["plugin_patch"])

    def test_other_read_only_errors_are_left_alone(self):
        from maxmcp.helpers.error_hints import vray_output_hint
        self.assertIsNone(vray_output_hint("Attempt to set read-only property: width"))
