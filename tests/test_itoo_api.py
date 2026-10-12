"""iToo additions: API coverage, input validation and generated guard scripts."""
import base64
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from maxmcp.tools import itoo_api as api

TOKEN = "-".join(["AB"] * 32)


class InventoryTests(unittest.TestCase):
    def test_interface_signatures_and_coverage(self):
        text = """  Interface: trees
    <integer>count()
    <integerPtr>getRenderDataRaw()
    <void>update_ui()
    <void>resetCreatedVersion()
    itemID is In and Out parameter"""
        rows = api.parse_interfaces(text, "forest")
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["coverage"] for r in rows], ["tool", "renderer_internal", "ui", "missing"])
        self.assertTrue(all(r["interface"] == "trees" for r in rows))

    def test_unknown_plugin_refused_without_send(self):
        with patch.object(api.rc, "_send") as send:
            with self.assertRaises(ValueError):
                api.itoo_api_inventory("arbitrary")
            send.assert_not_called()

    def test_read_does_not_call_methods(self):
        b = base64.b64encode(b"Interface: global\n <integer>version()").decode()
        with patch.object(api.rc, "_send", return_value=f"ITOOINVENTORY\nI|{b}\nV|i400\nEND\n") as send:
            res = api.itoo_api_inventory()
        script = send.call_args.args[0]
        self.assertNotIn("getStyleDesc()", script)
        self.assertNotIn("openBrowser()", script)
        self.assertEqual(res["api_version"], 400)
        self.assertTrue(res["read_only"])


class ValueTests(unittest.TestCase):
    def test_measured_segment_types(self):
        self.assertEqual(api.SEGMENT_FIELDS["sfixedsize"], "point")
        self.assertEqual(api.SEGMENT_FIELDS["sSizeScale"], "bool")
        self.assertEqual(api.SEGMENT_FIELDS["szdeform"], "int")
        self.assertEqual(api.SEGMENT_FIELDS["smapchans"], "channels")

    def test_channels(self):
        self.assertIn(api.rc._b64("1,2"), api._literal("1,2", "channels"))
        for value in [1, "100", "1;delete objects", ""]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                api._literal(value, "channels")

    def test_numbers(self):
        for value in [True, "1", float("inf"), float("nan")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                api._literal(value, "number")
        self.assertEqual(api._literal(1.5, "number"), "1.5")

    def test_bool(self):
        self.assertEqual(api._literal(False, "bool"), "false")
        with self.assertRaises(ValueError):
            api._literal(1, "bool")

    def test_point(self):
        self.assertEqual(api._literal([1, 2, 3], "point"), "[1.0,2.0,3.0]")
        for value in [[1, 2], [1, 2, float("nan")], "[1,2,3]"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                api._literal(value, "point")

    def test_enums(self):
        for typ, bad in [("align", -1), ("align", 5), ("slice", 21), ("int", 1.5), ("int", 2**40)]:
            with self.subTest(typ=typ, bad=bad), self.assertRaises(ValueError):
                api._literal(bad, typ)

    def test_node_is_encoded(self):
        source = 'a";delete objects;--\n雪'
        literal = api._literal(source, "node")
        self.assertNotIn(source, literal)
        self.assertIn(base64.b64encode(source.encode()).decode(), literal)
        self.assertEqual(api._literal(None, "node"), "undefined")


class SlotsTests(unittest.TestCase):
    def test_inspect_parses_fields_and_token(self):
        raw = f"ITOOSLOTS\nT|{TOKEN}\nS|1|sid|s{api.rc._b64('id')}\nS|1|srenderable|b1\nEND\n"
        with patch.object(api.rc, "_send", return_value=raw):
            result = api.railclone_slots(name="rc")
        self.assertEqual(result["slots_token"], TOKEN)
        self.assertEqual(result["slots"][0]["sid"], "id")
        self.assertTrue(result["slots"][0]["srenderable"])

    def test_edit_needs_token(self):
        with patch.object(api.rc, "_send") as send:
            with self.assertRaises(ValueError):
                api.railclone_slots(name="rc", action="edit", slot="id", values={"srenderable": True})
            send.assert_not_called()

    def test_unsupported_property_never_sent(self):
        with patch.object(api.rc, "_send") as send:
            with self.assertRaises(ValueError):
                api.railclone_slots(name="rc", action="edit", slot="id", expected_slots=TOKEN,
                                   values={"arbitrary;delete objects": 1})
            send.assert_not_called()

    def test_source_guard_and_hold(self):
        with patch.object(api.rc, "_send", return_value=f"ITOOSLOTS\nT|{TOKEN}\nEND\n") as send:
            api.railclone_slots(name="rc", action="edit", slot="Segment", expected_slots=TOKEN,
                               values={"sobjnode": "source"})
        script = send.call_args.args[0]
        for part in ["STALE_SLOTS", "SHARED_BASE", "USER_BUSY", "base.sobjref[idx]", "base.sobjmtl[idx]", "theHold.Cancel()"]:
            self.assertIn(part, script)
        self.assertLess(script.index("SLOT_LIST_MISMATCH"), script.index("theHold.Begin()"))
        self.assertNotIn("autoUpdate =", script)
        self.assertIn("refs.dependencyLoopTest next_sobjnode base", script)


class ControlsTests(unittest.TestCase):
    def test_proxy_change_requires_confirmation(self):
        with self.assertRaises(ValueError):
            api.railclone_proxy(name="rc", action="set")

    def test_proxy_paths(self):
        for p in ["relative.rcproxy", "C:/cache/a.txt", "C:/cache/a.rcproxy\x00"]:
            with self.subTest(path=p), self.assertRaises(ValueError):
                api.railclone_proxy(name="rc", action="set", mode="external", path=p, confirm_change=True)

    def test_proxy_no_overwrite_and_no_registry_edit(self):
        raw = "ITOOPROXY\nP|i2|s|s\nEND\n"
        with patch.object(api.rc, "_send", return_value=raw) as send:
            api.railclone_proxy(name="rc", action="set", mode="external", path="C:/cache/a.rcproxy", confirm_change=True)
        script = send.call_args.args[0]
        self.assertIn("OVERWRITE_REQUIRED", script)
        self.assertIn("CACHE_WRITE_FAILED", script)
        self.assertNotIn("fullMeshProxy", script)

    def test_compatibility_requires_confirmation(self):
        with self.assertRaises(ValueError):
            api.railclone_metadata(name="rc", reset_compatibility=True)

    def test_delete_instances_requires_confirmation(self):
        with self.assertRaises(ValueError):
            api.railclone_instances(name="rc", action="delete")

    def test_instance_guards_and_selection_restoration(self):
        raw = "ITOOINSTANCES\nN|123|" + api.rc._b64("new") + "\nS|b0\nEND\n"
        with patch.object(api.rc, "_send", return_value=raw) as send:
            res = api.railclone_instances(name="rc", max_items=10)
        script = send.call_args.args[0]
        self.assertEqual(res["created"][0]["handle"], 123)
        for text in ["ITEM_LIMIT", "AUTO_UPDATE_OFF", "PROXY_CACHE", "rcRestoreSel prev) catch (rcRestoreSel prev", "Instantiate 0"]:
            self.assertIn(text, script)
        self.assertIn("local n=counts[1]", script)


class EffectsTests(unittest.TestCase):
    def test_effect_properties_skip_curve_control(self):
        self.assertIn("isKindOf a ArrayParameter or isKindOf a Array", api._EFFECT_LIB)

    def test_code_xml_roundtrip(self):
        text = 'fpItem.position.z = fpItem.position.z + 10;'
        root = api.ET.fromstring(api.effect_xml(text, "Snow & <plants>"))
        self.assertEqual(root.findtext("expression"), text)
        self.assertEqual(root.get("title"), "Snow & <plants>")
        self.assertIsNone(root.find("parameter"))

    def test_effect_inputs(self):
        for code in ["", "\x00", "x" * 100001]:
            with self.subTest(code=code[:10]), self.assertRaises(ValueError):
                api.effect_xml(code, "effect")

    def test_effects_stale_guard_and_staging(self):
        raw = f"ITOOEFFECTS\nT|{TOKEN}\nA|efidlist|0\nEND\n"
        with patch.object(api.rc, "_send", return_value=raw) as send:
            res = api.forest_effects("forest", action="add", code="fpItem.position.z = fpItem.position.z + 10;",
                                     effect_name="Raise", expected_effects=TOKEN)
        script = send.call_args.args[0]
        for text in ["STALE_EFFECTS", "SHARED_BASE", "staged=copy base", "obj.baseObject=staged", "theHold.Cancel()"]:
            self.assertIn(text, script)
        self.assertNotIn("trees.update()", script)
        self.assertTrue(res["undoable"])
        self.assertIn("CUSTOM_EDIT_UNSUPPORTED", script)
        self.assertFalse(res["compile_verified"])

    def test_compound_assignment_refused(self):
        with self.assertRaises(ValueError):
            api.effect_xml("fpItem.position.z += 10;", "bad")


class CatalogTests(unittest.TestCase):
    def test_read_catalog_without_eval(self):
        with patch.object(api.rc, "_send", return_value="ITOOCATALOG\nK|macro_count|i3\nEND\n") as send:
            result = api.itoo_catalog(macro="RENDERER")
        self.assertEqual(result["macro_count"], 3)
        self.assertIn("api.getMacro", send.call_args.args[0])
        self.assertNotIn("evalMacro", send.call_args.args[0])
        self.assertNotIn("openBrowser", send.call_args.args[0])

    def test_key_length_rejected(self):
        with self.assertRaises(ValueError):
            api.itoo_catalog(macro="x" * 257)

    def test_remove_needs_confirmation(self):
        with self.assertRaises(ValueError):
            api.forest_effects("forest", action="remove", effect="id", expected_effects=TOKEN)

    def test_get_filters_exact_id(self):
        raw = f"ITOOEFFECTS\nT|{TOKEN}\nA|efidlist|1\nV|efidlist|1|s{api.rc._b64('id')}\nEND\n"
        with patch.object(api.rc, "_send", return_value=raw):
            res = api.forest_effects("forest", action="get", effect="id")
        self.assertEqual(res["effect"]["id"], "id")


class AdditionalToolTests(unittest.TestCase):
    def test_settings_ranges(self):
        for settings, fields in [({"units_x": 0}, api.FP_SETTINGS), ({"direction": 101}, api.FP_SETTINGS),
                                 ({"seed": -1}, api.RC_SETTINGS), ({"vmesh": 5}, api.RC_SETTINGS),
                                 ({"rmesh": 2}, api.RC_SETTINGS), ({"geomlist": 3}, api.FP_SETTINGS)]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                api.settings_values(settings, fields)

    def test_settings_native_types(self):
        self.assertEqual(api.settings_values({"units_x": 100}, api.FP_SETTINGS)["units_x"], "100.0")
        with self.assertRaises(ValueError):
            api.settings_values({"autoUpdate": 1}, api.RC_SETTINGS)

    def test_settings_confirmation_and_tokens(self):
        raw=f"ITOOSETTINGS\nT|{TOKEN}\nP|autoUpdate|b1\nEND\n"
        with patch.object(api.rc, "_send", return_value=raw) as send:
            api.railclone_settings("rc", action="set", settings={"autoUpdate": True}, expected_settings=TOKEN)
        script=send.call_args.args[0]
        for text in ["NOT_RAILCLONE", "STALE_SETTINGS", "REBUILD_CONFIRMATION_REQUIRED", "theHold.Cancel()"]:
            self.assertIn(text,script)

    def test_settings_missing_token(self):
        with patch.object(api.rc,"_send") as send:
            with self.assertRaises(ValueError):
                api.forest_settings("forest",action="set",settings={"seed": 2})
            send.assert_not_called()

    def test_effect_library_file_metadata_and_entities(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"test.eff"
            p.write_text(api.effect_xml("fpItem.position.z = 10;","MCP Fixture"),encoding="utf-8")
            info=api.read_effect_file(p)
            self.assertEqual(info["title"],"MCP Fixture")
            self.assertTrue(info["parameter_free"])
            self.assertEqual(len(info["sha256"]),64)
            bad='<!DOCTYPE effect [<!ENTITY e "hello">]><effect><expression>&e;</expression></effect>'
            for encoding in ["utf-8","utf-16"]:
                p.write_bytes(bad.encode(encoding))
                with self.assertRaises(ValueError):
                    api.read_effect_file(p)

    def test_effect_library_search_and_code_optin(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"fixture.eff"
            p.write_text(api.effect_xml("fpItem.position.z = 10;","MCP Fixture"),encoding="utf-8")
            r=api.forest_effect_library(query="fixture",roots=[td])
            self.assertEqual(r["matched"],1)
            self.assertNotIn("code",r["items"][0])
            r=api.forest_effect_library(action="get",path=str(p),include_code=True)
            self.assertIn("code",r["item"])

    def test_effect_library_relative_root_refused(self):
        with self.assertRaises(ValueError):
            api.forest_effect_library(roots=["relative"])


if __name__ == "__main__":
    unittest.main()
