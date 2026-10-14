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
    def test_effect_move_reorders_rows_preserves_parameter_arrays(self):
        raw=f"ITOOEFFECTS\nT|{TOKEN}\nEND\n"
        with patch.object(api.rc, "_send", return_value=raw) as send:
            result=api.forest_effects("forest", action="move", effect="id", position=2, expected_effects=TOKEN)
        script=send.call_args.args[0]
        self.assertIn("insertItem item a 2", script)
        self.assertIn("#efselspeclist,#efspeclist", script)
        self.assertNotIn("staged.efpaeffid=", script)
        self.assertIn("BAD_POSITION", script)
        self.assertTrue(result["undoable"])

    def test_effect_move_position_validation(self):
        for position in (0, -1, True, 1.5):
            with self.subTest(position=position), self.assertRaises(ValueError):
                api.forest_effects("forest",action="move",effect="id",position=position,expected_effects=TOKEN)
        with self.assertRaises(ValueError):
            api.forest_effects("forest",position=1)

    def test_effect_load_hash_and_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"fixture.eff"
            xml=api.effect_xml("fpItem.position.z = 10;", "Original Title").replace('Created through MCP','Original Description')
            p.write_text(xml,encoding="utf-8")
            info=api.read_effect_file(p)
            with patch.object(api.rc,"_send",return_value=f"ITOOEFFECTS\nT|{TOKEN}\nEND\n") as send:
                result=api.forest_effects("forest",action="load",path=str(p),expected_file_hash=info["sha256"],expected_effects=TOKEN)
            self.assertIn(api.rc._b64(xml),send.call_args.args[0])
            self.assertEqual(result["imported_sha256"],info["sha256"])
            with patch.object(api.rc,"_send") as send:
                for file_hash in ("", "0"*64):
                    with self.assertRaises(ValueError):
                        api.forest_effects("forest",action="load",path=str(p),expected_file_hash=file_hash,expected_effects=TOKEN)
                send.assert_not_called()

    def test_effect_load_nested_parameters_refused(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"fixture.eff"
            p.write_text('<effect><parameters><parameter name="Value"/></parameters><expression>fpItem.position.z = Value;</expression></effect>',encoding="utf-8")
            info=api.read_effect_file(p)
            self.assertFalse(info["parameter_free"])
            with patch.object(api.rc,"_send") as send:
                with self.assertRaisesRegex(ValueError,"EFFECT_FORMAT_UNSUPPORTED"):
                    api.forest_effects("forest",action="load",path=str(p),expected_file_hash=info["sha256"],expected_effects=TOKEN)
                send.assert_not_called()

    def test_effect_export_no_overwrite(self):
        xml=api.effect_xml("fpItem.position.z = 10;","Export Fixture")
        raw=f"ITOOEFFECTS\nT|{TOKEN}\nA|efidlist|1\nV|efidlist|1|s{api.rc._b64('id')}\nA|efxmllist|1\nV|efxmllist|1|s{api.rc._b64(xml)}\nEND\n"
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"fixture.eff"
            with patch.object(api.rc,"_send",return_value=raw) as send:
                result=api.forest_effects("forest",action="export",effect="id",path=str(p))
            self.assertEqual(p.read_text(encoding="utf-8"),xml)
            self.assertTrue(result["scene_read_only"])
            self.assertFalse(result["undoable"])
            self.assertNotIn("theHold.Begin",send.call_args.args[0])
            with patch.object(api.rc,"_send") as send:
                with self.assertRaisesRegex(ValueError,"EFFECT_FILE_EXISTS"):
                    api.forest_effects("forest",action="export",effect="id",path=str(p))
                send.assert_not_called()

    def test_effect_export_parameter_references_refused(self):
        xml=api.effect_xml("fpItem.position.z = 10;","Export Fixture")
        raw=f"ITOOEFFECTS\nT|{TOKEN}\nA|efidlist|1\nV|efidlist|1|s{api.rc._b64('id')}\nA|efxmllist|1\nV|efxmllist|1|s{api.rc._b64(xml)}\nA|efpaeffid|1\nV|efpaeffid|1|s{api.rc._b64('id')}\nEND\n"
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"fixture.eff"
            with patch.object(api.rc,"_send",return_value=raw), self.assertRaises(api.RailCloneError):
                api.forest_effects("forest",action="export",effect="id",path=str(p))
            self.assertFalse(p.exists())

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

    def test_forest_settings_reads_translation_extents_without_exposing_writes(self):
        raw = f"ITOOSETTINGS\nT|{TOKEN}\nP|applyTranslation|b1\nX|#transxmin|i-20\nX|#transxmax|i20\nX|#transymin|i-10\nX|#transymax|i10\nX|#transzmin|i0\nX|#transzmax|i0\nEND\n"
        with patch.object(api.rc, "_send", return_value=raw) as send:
            out = api.forest_settings("forest", action="inspect")
        script = send.call_args.args[0]
        self.assertEqual(out["settings"]["applyTranslation"], True)
        self.assertEqual(out["translation_extents"], {"x_min": -20, "x_max": 20, "y_min": -10,
                                                       "y_max": 10, "z_min": 0, "z_max": 0})
        self.assertNotIn("transxmin", api.FP_SETTINGS)
        self.assertNotIn("setProperty base #transxmin", script)
        self.assertIn("#transxmin", script)
        self.assertIn('rcRow ss #(\"X\", p as string', script)
        self.assertIn("isProperty obj p then getProperty obj p", script)
        self.assertNotIn("isProperty base p then getProperty base p)) to:ss", script)

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


class NumericParameterTests(unittest.TestCase):
    def state(self, kind="real", value=10):
        pid="{33333333-3333-3333-3333-333333333333}"
        xml=f'<effect><expression>fpItem.position.z = Lift;</expression><parameter id="{pid}" name="Lift" type="numerical" numtype="{kind}" limits="1" min="0" max="100" default="10"/></effect>'
        subtype,prefix,_=api.NUMERIC_KINDS[kind]
        arrays={"efpaid":[pid],"efpaeffid":["owner"],"efpatype":[0],"efpaname":["Lift"],"efpanumtype":[subtype],"efpalimit":[True],
                prefix+"val":[value],prefix+"min":[0],prefix+"max":[100],prefix+"def":[10]}
        return {"effects_token":TOKEN,"effects":[{"id":"owner","name":"MCP fixture","xml":xml}],"arrays":{k:{"values":v} for k,v in arrays.items()}}

    def test_numeric_rows_all_measured_subtypes(self):
        for kind in api.NUMERIC_KINDS:
            with self.subTest(kind=kind):
                row=api.parameter_rows(self.state(kind))[0]
                self.assertEqual(row["kind"],kind)
                self.assertTrue(row["writable"])

    def test_schema_mismatch_and_orphan_refused(self):
        state=self.state();state["arrays"]["efpaname"]["values"]=["Wrong"]
        with self.assertRaises(api.RailCloneError):api.parameter_rows(state)
        state=self.state();state["arrays"]["efpaeffid"]["values"]=["missing"]
        with self.assertRaises(api.RailCloneError):api.parameter_rows(state)

    def test_guarded_numeric_write(self):
        with patch.object(api,"forest_effects",side_effect=[self.state(),self.state(value=27)]),patch.object(api.rc,"_send",return_value="PARAMETERS_WRITTEN") as send:
            result=api.forest_effect_parameters("forest",action="set",values={"Lift":27},expected_effects=TOKEN)
        script=send.call_args.args[0]
        self.assertIn("staged.efpafloatval[1]=27.0",script)
        self.assertIn("STALE_EFFECTS",script)
        self.assertIn("SHARED_BASE",script)
        self.assertIn("theHold.Cancel()",script)
        self.assertNotIn("update_ui",script)
        self.assertEqual(result["parameters"][0]["value"],27)

    def test_limits_integer_and_duplicate_targets(self):
        for kind,values in [("real",{"Lift":101}),("int",{"Lift":1.5}),("real",{"Lift":True}),
                            ("real",{"Lift":2,"{33333333-3333-3333-3333-333333333333}":3})]:
            with self.subTest(values=values),patch.object(api,"forest_effects",return_value=self.state(kind)),patch.object(api.rc,"_send") as send:
                with self.assertRaises(ValueError):api.forest_effect_parameters("forest",action="set",values=values,expected_effects=TOKEN)
                send.assert_not_called()

    def test_stale_and_missing_token(self):
        with patch.object(api,"forest_effects") as read:
            with self.assertRaises(ValueError):api.forest_effect_parameters("forest",action="set",values={"Lift":2})
            read.assert_not_called()
        state=self.state();state["effects_token"]="CD-"*31+"CD"
        with patch.object(api,"forest_effects",return_value=state),self.assertRaises(api.RailCloneError):
            api.forest_effect_parameters("forest",action="set",values={"Lift":2},expected_effects=TOKEN)

    def test_parameter_definitions_and_append_storage(self):
        root=api.parse_effect_xml(self.state()["effects"][0]["xml"])
        rows=api.numeric_definitions(root)
        script=api.append_numeric_parameters(rows,"owner")
        self.assertIn("staged.efpafloatval=next_efpafloatval",script)
        self.assertIn("append next_efpanumtype 1",script)
        self.assertIn("EFFECT_PARAMETER_LIST_MISMATCH",script)
        self.assertEqual(len(api.PARAM_DEFAULTS),26)

    def test_reference_import_refused(self):
        root=api.parse_effect_xml('<effect><parameter id="{33333333-3333-3333-3333-333333333333}" name="Source" type="node"/></effect>')
        with self.assertRaisesRegex(ValueError,"PARAMETER_TYPE_UNSUPPORTED"):api.numeric_definitions(root)


class ReferenceParameterTests(unittest.TestCase):
    def state(self, kind="node"):
        pid="{33333333-3333-3333-3333-333333333333}"
        code, prop = next((c, p) for c, (k, p) in api.REFERENCE_KINDS.items() if k == kind)
        xml=f'<effect><parameter id="{pid}" name="Source" type="{kind}"/></effect>'
        arrays={"efpaid":[pid],"efpaeffid":["owner"],"efpatype":[code],"efpaname":["Source"],prop:[{"name":"Box", "handle":123}]}
        return {"effects_token":TOKEN,"effects":[{"id":"owner","name":"Fixture","xml":xml}],"arrays":{k:{"values":v} for k,v in arrays.items()}}

    def test_reference_rows(self):
        for kind in ("node", "spline"):
            row=api.parameter_rows(self.state(kind))[0]
            self.assertEqual(row["kind"],kind)
            self.assertEqual(row["value"],{"name":"Box","handle":123})
            self.assertTrue(row["bound"] and row["writable"])

    def test_reference_write_guards(self):
        for kind in ("node", "spline"):
            state=self.state(kind)
            with patch.object(api,"forest_effects",return_value=state),patch.object(api.rc,"_send",return_value="PARAMETERS_WRITTEN") as send:
                api.forest_effect_parameters("forest",action="set",values={"Source":{"name":"Box","handle":123}},expected_effects=TOKEN)
            script=send.call_args.args[0]
            for text in ("STALE_NODE_REFERENCE", "NODE_NOT_UNIQUE", "DEPENDENCY_CYCLE", "copy base", "theHold.Cancel()"):
                self.assertIn(text,script)
            self.assertIn("refs.dependencyLoopTest next_reference_1 obj",script)
            self.assertIn("refs.dependencyLoopTest next_reference_1 base",script)
            self.assertIn("NOT_SPLINE",script) if kind == "spline" else self.assertNotIn("NOT_SPLINE",script)

    def test_reference_literal_validation(self):
        self.assertEqual(api.reference_literal(None),"undefined")
        for value in (True, 1, "", {"name":"Box"}, {"name":"Box","handle":True}, {"name":"Box","handle":-1}, {"name":"Box","handle":123,"extra":1}):
            with self.subTest(value=value),self.assertRaises(ValueError):api.reference_literal(value)

    def test_reference_schema_and_storage_mismatch(self):
        state=self.state();state["effects"][0]["xml"]=state["effects"][0]["xml"].replace('type="node"','type="spline"')
        with self.assertRaises(api.RailCloneError):api.parameter_rows(state)

    def test_reference_definitions_require_complete_explicit_bindings(self):
        rows=api.parameter_definitions(api.parse_effect_xml(self.state()["effects"][0]["xml"]),allow_references=True)
        for bindings in (None, {}, {"wrong":"Box"}, {"Source":"Box", rows[0]["id"]:None}):
            with self.subTest(bindings=bindings),self.assertRaises(ValueError):api.bind_definitions(rows,bindings)
        api.bind_definitions(rows,{"Source":None})
        self.assertEqual(rows[0]["binding_literal"],"undefined")
        api.bind_definitions(rows,{rows[0]["id"]:{"name":"Box","handle":123}})
        api.bind_definitions(rows,{rows[0]["id"].strip('{}').upper():{"name":"Box","handle":123}})
        script=api.append_numeric_parameters(rows,"owner")
        self.assertIn("append next_efpatype 1",script)
        self.assertIn("append next_efpainode import_reference_0",script)
        self.assertIn("refs.dependencyLoopTest import_reference_0 obj",script)

    def test_spline_import_guards_and_all_parallel_arrays(self):
        rows=api.parameter_definitions(api.parse_effect_xml(self.state('spline')["effects"][0]["xml"]),allow_references=True)
        api.bind_definitions(rows,{"Source":"Shape"})
        script=api.append_numeric_parameters(rows,"owner")
        self.assertIn("append next_efpatype 5",script)
        self.assertIn("append next_efpaspline import_reference_0",script)
        self.assertIn("NOT_SPLINE",script)
        for p in api.PARAM_DEFAULTS:self.assertIn(f'staged.{p}=next_{p}',script)

    def test_binding_arguments_restricted_to_load_and_export(self):
        for args in ({"bindings":{}},{"export_reference_definitions":True},{"export_reference_definitions":1}):
            with patch.object(api.rc,"_send") as send,self.assertRaises(ValueError):api.forest_effects("forest",**args)
            send.assert_not_called()

    def test_reference_import_initializes_before_xml(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"fixture.eff";p.write_text(self.state('spline')["effects"][0]["xml"].replace('<effect>','<effect><expression>fpItem.position.z = 1;</expression>'),encoding='utf-8')
            file_hash=api.read_effect_file(p)['sha256']
            with patch.object(api.rc,'_send',return_value=f'ITOOEFFECTS\nT|{TOKEN}\nEND\n') as send:
                result=api.forest_effects('forest',action='load',path=str(p),expected_file_hash=file_hash,expected_effects=TOKEN,bindings={'Source':'Shape'})
            script=send.call_args.args[0]
            self.assertLess(script.index('staged.efpaspline=next_efpaspline'),script.index('staged.efxmllist=xmls'))
            self.assertEqual(result['initialized_parameters'],1)
            with patch.object(api.rc,'_send') as send,self.assertRaisesRegex(ValueError,'REFERENCE_BINDINGS_REQUIRED'):
                api.forest_effects('forest',action='load',path=str(p),expected_file_hash=file_hash,expected_effects=TOKEN)
            send.assert_not_called()

    def test_reference_export_definitions_opt_in(self):
        state=self.state();arrays={k:v['values'] for k,v in state['arrays'].items()}
        arrays.update(efidlist=['owner'],efnamelist=['Fixture'],efxmllist=[state['effects'][0]['xml']],efenablelist=[True])
        def encode(v):
            if isinstance(v,dict):return f"n{v['handle']}:{api.rc._b64(v['name'])}"
            if isinstance(v,bool):return 'b1' if v else 'b0'
            if isinstance(v,int):return f'i{v}'
            return 's'+api.rc._b64(v)
        raw=f'ITOOEFFECTS\nT|{TOKEN}\n'
        for k,values in arrays.items():
            raw+=f'A|{k}|{len(values)}\n'
            for i,v in enumerate(values,1):raw+=f'V|{k}|{i}|{encode(v)}\n'
        raw+='END\n'
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'reference.eff'
            with patch.object(api.rc,'_send',return_value=raw),self.assertRaisesRegex(ValueError,'REFERENCE_EXPORT_CONFIRMATION_REQUIRED'):
                api.forest_effects('forest',action='export',effect='owner',path=str(path))
            self.assertFalse(path.exists())
            with patch.object(api.rc,'_send',return_value=raw):
                result=api.forest_effects('forest',action='export',effect='owner',path=str(path),export_reference_definitions=True)
            self.assertEqual(result['required_bindings'][0]['name'],'Source')
            self.assertFalse(result['scene_bindings_exported'])
            self.assertNotIn('Box',path.read_text())
            with patch.object(api.rc,'_send') as send,self.assertRaisesRegex(ValueError,'EFFECT_FILE_EXISTS'):
                api.forest_effects('forest',action='export',effect='owner',path=str(path),export_reference_definitions=True)
            send.assert_not_called()
        state=self.state();state["arrays"]["efpainode"]["values"]=["not a node"]
        with self.assertRaises(api.RailCloneError):api.parameter_rows(state)


class TextureParameterTests(unittest.TestCase):
    def state(self):
        s=ReferenceParameterTests().state('texture')
        s['arrays']['efpatexmap']['values'][0]={'name':'Checker A','handle':123,'class':'Checker'}
        return s

    def test_texture_identity_readback(self):
        row=api.parameter_rows(self.state())[0]
        self.assertEqual(row['kind'],'texture');self.assertTrue(row['writable'])
        self.assertEqual(api.effect_value('t123:'+api.rc._b64('Checker A')+':'+api.rc._b64('Checker')),row['value'])
        self.assertIn('iaEffectValue v',api._EFFECT_LIB)
        self.assertIn('"t"+(rcH v)',api._EFFECT_LIB)

    def test_texture_identity_validation(self):
        self.assertEqual(api.texture_literal(None),'undefined')
        for value in ('Checker A',{}, {'name':'A','handle':True,'class':'Checker'},{'name':'A','handle':1,'class':''}):
            with self.subTest(value=value),self.assertRaises(ValueError):api.texture_literal(value)

    def test_guarded_texture_write(self):
        with patch.object(api,'forest_effects',return_value=self.state()),patch.object(api.rc,'_send',return_value='PARAMETERS_WRITTEN') as send:
            api.forest_effect_parameters('forest',action='set',values={'Source':{'name':'Checker A','handle':123,'class':'Checker'}},expected_effects=TOKEN)
        script=send.call_args.args[0]
        for text in ('NOT_TEXTURE_MAP','STALE_MAP_REFERENCE','refs.dependencyLoopTest next_reference_1 obj','staged.efpatexmap[1]=next_reference_1','theHold.Cancel()'):
            self.assertIn(text,script)

    def test_texture_import_storage(self):
        rows=api.parameter_definitions(api.parse_effect_xml(self.state()['effects'][0]['xml']),allow_references=True)
        api.bind_definitions(rows,{'Source':{'name':'Checker A','handle':123,'class':'Checker'}})
        script=api.append_numeric_parameters(rows,'owner')
        self.assertIn('append next_efpatype 3',script)
        self.assertIn('append next_efpatexmap import_reference_0',script)
        self.assertNotIn('append next_efpaspline import_reference_0',script)

    def test_texture_schema_and_class_required(self):
        state=self.state();del state['arrays']['efpatexmap']['values'][0]['class']
        with self.assertRaises(api.RailCloneError):api.parameter_rows(state)
        state=self.state();state['effects'][0]['xml']=state['effects'][0]['xml'].replace('type="texture"','type="controller"')
        with self.assertRaises(api.RailCloneError):api.parameter_rows(state)


if __name__ == "__main__":
    unittest.main()
