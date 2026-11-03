"""plugin_patch writes V-Ray GPU output_* settings through its V-Ray settings sub-anim (MCP_ISSUES #49)."""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import plugin_schema  # noqa: E402
from maxmcp.max_client import MaxBridgeError  # noqa: E402

GPU = [1770671000, 1323107829]
OWNER = {"class_ref": {"class_id": GPU, "superclass_id": 3840}, "handle": "13879",
         "root_binding": {"root": "renderer"}, "session": "28400:9"}
RAW_REF = {"block_id": 0, "param_id": 72, "route": "pb2"}
NAME_REF = {"block_id": 0, "param_id": 73, "route": "pb2"}
STATE = {"fields": [RAW_REF, NAME_REF], "hash": "h1"}


def prop(ref, name, kind, read_only=True):
    return {"property_ref": ref, "name": name, "type": kind, "read_only": read_only}


def schema(class_id=GPU, token="s1", hash_="h1", props=None):
    return {"identity": {"class_id": class_id}, "schema_token": token, "state_token": {"fields": STATE["fields"],
                                                                                      "hash": hash_},
            "properties": props if props is not None else [prop(RAW_REF, "output_saverawfile", "bool"),
                                                           prop(NAME_REF, "output_rawfilename", "filename")]}


def edit(ref=RAW_REF, value=False, **extra):
    return {"owner_ref": OWNER, "property_ref": ref, "expected_schema": "s1", "expected_state": STATE,
            "value": value, **extra}


def refusal(code="READ_ONLY"):
    return MaxBridgeError(json.dumps({"code": code, "message": "The plugin marks this parameter read-only.",
                                      "retryable": False, "type": "NativeError"}), {})


def _load(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.plugin_edit", None)
        return importlib.import_module("maxmcp.tools.plugin_edit")


class VRayGpuOutputTests(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.client.send_command.return_value = {
            "result": 'OK\noutput_saverawfile\ttrue\tfalse\ttrue\noutput_rawfilename\t\tD:\\r\\a.vrimg\ttrue'}
        self.pe = _load(self.client)
        self.native = mock.patch.object(self.pe, "native", side_effect=refusal())
        self.native.start()
        self.addCleanup(self.native.stop)
        self.inspect = mock.patch.object(self.pe.plugin_schema, "inspect", return_value=schema())
        self.inspected = self.inspect.start()
        self.addCleanup(self.inspect.stop)

    def test_read_only_refusal_writes_through_the_settings_subanim(self):
        out = self.pe.plugin_patch([edit(), edit(NAME_REF, 'D:\\r\\a "x".vrimg')])
        self.assertEqual((out["status"], out["route"]), ("applied", "vray_gpu_settings_subanim"))
        self.assertEqual([p["name"] for p in out["properties"]], ["output_saverawfile", "output_rawfilename"])
        self.assertEqual(out["properties"][0]["before"], "true")
        self.assertTrue(all(p["matched"] for p in out["properties"]))
        self.assertIn("renderers.current[1].value", out["warnings"][0])
        script = self.client.send_command.call_args.args[0]
        self.assertIn("local s = try (r[1].value) catch (undefined)", script)
        self.assertIn("#(#output_saverawfile, #output_rawfilename)", script)
        self.assertIn('#(false, "D:\\\\r\\\\a \\"x\\".vrimg")', script)
        self.assertIn('undo "MCP V-Ray GPU output settings" on', script)
        self.assertIn("cid[1] == 1770671000 and cid[2] == 1323107829", script)
        self.assertIn("theHold.Holding()", script)
        self.assertEqual(self.inspected.call_args.kwargs["fields"], STATE["fields"])

    def test_other_refusals_and_owners_are_not_rerouted(self):
        self.pe.native.side_effect = refusal("STALE_STATE")
        with self.assertRaises(MaxBridgeError):
            self.pe.plugin_patch([edit()])
        self.pe.native.side_effect = refusal()
        node_owner = {**edit(), "owner_ref": {"node": {"name": "Box001"}, "scope": "base_object"}}
        cases = [
            ("not V-Ray GPU", [edit()], schema(class_id=[1, 2])),
            ("not output_*", [edit()], schema(props=[prop(RAW_REF, "gi_on", "bool")])),
            ("writable", [edit()], schema(props=[prop(RAW_REF, "output_saverawfile", "bool", read_only=False)])),
            ("type", [edit(value="yes")], schema()),
            ("node owner", [node_owner], schema()),
            ("shared", [edit(sharing="all_instances")], schema()),
        ]
        for label, edits, inspected in cases:
            with self.subTest(label):
                self.inspected.return_value = inspected
                with self.assertRaises(MaxBridgeError):
                    self.pe.plugin_patch(edits)
        self.client.send_command.assert_not_called()

    def test_stale_guards_refuse_before_writing(self):
        for inspected, code in ((schema(token="s2"), "STALE_SCHEMA"), (schema(hash_="h2"), "STALE_STATE")):
            with self.subTest(code):
                self.inspected.return_value = inspected
                with self.assertRaises(plugin_schema.PluginGuardError) as ctx:
                    self.pe.plugin_patch([edit()])
                self.assertEqual(ctx.exception.code, code)
        self.client.send_command.assert_not_called()

    def test_max_side_refusals(self):
        for reply, code in (("ERR|not_vray_gpu", "NOT_VRAY_GPU"), ("ERR|user_busy", "USER_BUSY"),
                            ("ERR|mismatch", "VERIFY_MISMATCH"), ("ERR|error|-- boom", "ERROR")):
            with self.subTest(reply):
                self.client.send_command.return_value = {"result": '"%s"' % reply}
                with self.assertRaises(plugin_schema.PluginGuardError) as ctx:
                    self.pe.plugin_patch([edit()])
                self.assertEqual(ctx.exception.code, code)

    def test_literals(self):
        lit = self.pe._mxs_literal
        self.assertEqual(lit(True, "bool"), "true")
        self.assertEqual(lit(3, "int"), "3")
        self.assertEqual(lit(0.5, "float"), "0.5")
        self.assertIsNone(lit(True, "int"))
        self.assertIsNone(lit(1, "bool"))
        self.assertIsNone(lit(float("nan"), "float"))
        self.assertIsNone(lit([1], "point3"))


if __name__ == "__main__":
    unittest.main()
