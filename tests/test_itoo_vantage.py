"""Tests for itoo_vantage_check, the read-only iToo -> Chaos Vantage readiness check (fork issue #25).

Canned reports stand in for 3ds Max. tests/itoo_vantage_live_report.txt is a raw report captured from the scratch
Max 2027.2 (RailClone 7.3.5, Forest Pack 9.4.3) on 2026-10-11. The generated MAXScript is linted for anything that
writes, rebuilds, selects, renders or exports.
"""

import base64
import importlib
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_forest_tools import check_formats, check_live_pitfalls  # noqa: E402

iv = importlib.import_module("maxmcp.tools.itoo_vantage")

REPO = Path(__file__).resolve().parent.parent
LIVE_REPORT = Path(__file__).resolve().parent / "itoo_vantage_live_report.txt"

# Calls and commands that change the scene, rebuild an iToo object, select, render or export.
WRITE_CALLS = (
    "update()", ".update ", "segmentsupdate", "setproxymode", "setxmlstyle", "getstyledesc", "setstyledesc",
    "resetcreatedversion", "setcreatedversion", "upgradefromversion", "setnodescache", "loadlibraryitembypath",
    "instantiate", "exportdata", "exportfile", "vrscene", "addmodifier", "deletemodifier", "collapsestack",
    "converttomesh", "converttopoly", "convertto ", "replaceinstances", "setproperty", "setuserprop", "setappdata",
    "snapshot", "clearselection", "selectmore", "deselect", "theHold".lower(), "max undo", "max redo", "undo on",
    "animate on", "at time", "slidertime", "animationrange", "setsaverequired", "savemaxfile", "loadmaxfile",
    "mergemaxfile", "resetmaxfile", "holdmaxfile", "fetchmaxfile", "quitmax", "redrawviews", "completeredraw",
    "notifydependents", "meditmaterials", "loadtempmateriallibrary", "filein", "python.execute", "doscommand",
    "shelllaunch", "deletefile", "copyfile", "createfile", "renamefile", "makedir", "setinisetting", "registry.",
    "trees.create", "trees.delete", "trees.move", "trees.set", "trees.edit", "assignnewname", "freeze ", "unhide",
)
WRITE_WORDS = ("select", "delete", "render", "hide", "move", "rotate", "scale", "copy", "instance", "reference",
               "uniquename")


def _strip_strings(code):
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', code)


def _code(script):
    """Script lines without -- comments, with string literals emptied."""
    return [_strip_strings(line).split("--")[0] for line in script.splitlines()]


def write_lint(script):
    """Every write-like construct in a MAXScript: property assignments and the calls above. Returns the hits."""
    hits = []
    for line in _code(script):
        low = line.lower()
        # x.prop = v, x.prop[k] = v, x.prop += v (locals and array elements are fine: no dot before the '=')
        if re.search(r"\.\s*\w+\s*(\[[^\]]*\])*\s*[-+*/]?=(?!=)", line):
            hits.append(line.strip())
        for word in WRITE_CALLS:
            if word in low:
                hits.append(f"{word}: {line.strip()}")
        for word in WRITE_WORDS:
            if re.search(rf"(?<![\w.#]){word}\b(?!\s*[:=])", low):
                hits.append(f"{word}: {line.strip()}")
    return hits


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def v(value):
    """A typed report value (ivV)."""
    if value is iv.MISSING:
        return "x"
    if value is None:
        return "u"
    if isinstance(value, bool):
        return "b1" if value else "b0"
    if isinstance(value, int):
        return f"i{value}"
    if isinstance(value, float):
        return f"f{value:g}"
    if isinstance(value, str):
        return "s" + b64(value)
    if isinstance(value, dict):
        return "o" + b64(value["class"]) + ":" + b64(value["name"])
    raise TypeError(value)


def report(*rows, scene=None):
    s = {"save_required_before": True, "renderer": "V_Ray_7__update_4_hotfix_2_DR2", "max_version": 29000}
    s.update(scene or {})
    head = [f"S|{key}|{v(value)}" for key, value in s.items() if not key.startswith("after")]
    tail = [f"S|save_required_after|{v(s.get('after_save', s['save_required_before']))}", "S|elapsed_ms|i42"]
    return "\n".join(["IVC1", *head, *rows, *tail, "END", ""])


def obj_rows(kind, name, k, extra=(), handle=7):
    rows = [f"N|{kind}|{handle}|{b64(name)}"]
    rows += [f"K|{key}|{v(value)}" for key, value in k.items()]
    rows += list(extra)
    rows.append("NE|3")
    return rows


FOREST_K = {
    "disabled": False, "vmaxitems": 50000, "rmaxitems": 1000000, "maxFaces": 5, "rendermode": 0, "rmesh": 0,
    "vmesh": 2, "rskip": 0, "usefakeshadows": True, "consmat": True, "tintmode": 0, "tintmapmode": 0, "tintmin": 0,
    "tintmax": 0, "mode": 0, "manualupdate": False, "count": 1000,
    "material": {"class": "Multimaterial", "name": "forest_automat_1C7678B0"}, "item_types": 1,
}
RC_K = {
    "disabled": False, "rendermode": True, "rmesh": 0, "vmesh": 2, "nodemat": False, "proxymode": 0,
    "autoUpdate": True, "maxseg": 50000, "maxFaces": 5.0, "createdversion": 735, "proxyfile": "",
    "material": {"class": "Multimaterial", "name": "_RC_fencing"},
}
# The live P23_fenceA copy: 176 items, 12112 faces (maxFaces 0.0122 kept the output, 0.0121 emptied it)
SEGS = (("allum_rail_01", 96, 80), ("allum_post_01_main", 116, 8), ("allum_picket_01", 32, 80),
        ("allum_post_01_top", 118, 8))


def item(index=1, geom=2, prob=100.0, name="Box", source_class="Box", source="Box001", faces=-1):
    return f"IT|{index}|{v(geom)}|{v(prob)}|{b64(name)}|{b64(source_class or '')}|{b64(source or '')}|{faces}"


def area(index=1, active=True, typ=3, incexc=0, boundary=0):
    return f"AR|{index}|{v(active)}|{v(typ)}|{v(incexc)}|{v(boundary)}"


def cm(cls="Forest_Color", path="item 1 Box001/mtl/Diffuse", name="fc", s1cls="Bitmaptexture", s1name="Map #1",
       s1inputs=0, s1file="wood.jpg", assigned=1, enabled=1, slots=10, mapidmode=0, tintmode=0, tintmapmode=0,
       tintmin=100, tintmax=100, override=False, tintvariation=0.0, tintmap=None):
    return "|".join(["CM", b64(cls), b64(path), b64(name), b64(s1cls or ""), b64(s1name or ""), str(s1inputs),
                     b64(s1file or ""), str(assigned), str(enabled), str(slots), v(mapidmode), v(tintmode),
                     v(tintmapmode), v(tintmin), v(tintmax), v(override), v(tintvariation), v(tintmap)])


def op(path="item 1 Box001/leaf", mtl="VRayMtl", slot="Opacity", map_class="VRayBitmap", map_name="alpha"):
    return f"OP|{b64(path)}|{b64(mtl)}|{b64(slot)}|{b64(map_class)}|{b64(map_name)}"


def seg(index, name, faces, ref_class="Editable_mesh", source_class="", source="", mapping=False, mtl_class="",
        mtl_subs=0):
    return (f"SG|{index}|{b64(name)}|{b64(ref_class)}|{b64(source_class)}|{b64(source)}|{faces}|{v(mapping)}|"
            f"{b64(mtl_class)}|{mtl_subs}")


def rc_extra(segs=SEGS, out=True, style=(0, 0, 0), mods=(), seg_overrides=None):
    rows = []
    for index, cls, name, enabled, in_renders in mods:
        rows.append(f"MD|{index}|{b64(cls)}|{b64(name)}|{v(enabled)}|{v(in_renders)}")
    if out is True:
        total = sum(n for _, _, n in segs)
        rows.append(f"OUT|29023|{total}|{total}")
        rows += [f"OS|{b64(name)}|{n}" for name, _, n in segs]
    elif out is not None:
        rows += out
    if style is not None:
        rows.append(f"XS|3000|{style[0]}|{style[1]}|{style[2]}")
    for i, (name, faces, _) in enumerate(segs, 1):
        rows.append((seg_overrides or {}).get(i) or seg(i, name, faces))
    return rows


def forest(k=None, extra=(), name="F", items=None, areas=None):
    kk = dict(FOREST_K)
    kk.update(k or {})
    rows = list(items if items is not None else [item()]) + list(areas if areas is not None else [area()])
    return obj_rows("forest", name, kk, rows + list(extra))


def railclone(k=None, extra=None, name="RC", **kw):
    kk = dict(RC_K)
    kk.update(k or {})
    return obj_rows("railclone", name, kk, extra if extra is not None else rc_extra(**kw))


def run(*rows, scene=None, max_paths=5, details=False, xshadow=(), missing=(), not_itoo=(), skipped=()):
    pre = [f"XL|{h}|{b64(n)}" for h, n in xshadow] + [f"MISS|{b64(n)}" for n in missing]
    pre += [f"NI|{b64(n)}|{b64(c)}" for n, c in not_itoo] + [f"SKIP|{h}|{b64(n)}" for h, n in skipped]
    return iv.analyze(iv.parse_report(report(*pre, *rows, scene=scene)), max_paths, details)


def codes(result, index=0):
    return {f["code"]: f for f in result["objects"][index]["findings"]}


def unknown(result, index=0):
    return {u["check"]: u["reason"] for u in result["objects"][index]["unknown"]}


class ImportTargetTests(unittest.TestCase):
    def test_imports_checkout(self):
        self.assertEqual(Path(iv.__file__).resolve(), REPO / "maxmcp" / "tools" / "itoo_vantage.py")


class RegistrationTests(unittest.TestCase):
    def test_registered_read_only_and_skipped_by_smoke(self):
        from maxmcp import server
        from maxmcp.tool_discovery import TOOLSET_SPECS, _module_tool_names
        from scripts.gen_tool_catalog import MODULE_CATEGORY
        from scripts.gen_tool_smoke import MUTATE_TOOLS, SKIP_DEFAULT
        self.assertIn("itoo_vantage", server.SPECIALTY_TOOL_MODULES)
        self.assertNotIn("itoo_vantage", server.CORE_TOOL_MODULES)
        self.assertEqual(server._tool_annotations("itoo_vantage_check"), {"readOnlyHint": True, "idempotentHint": True})
        self.assertNotIn("itoo_vantage_check", server._DESTRUCTIVE_TOOLS)
        specs = [s for s in TOOLSET_SPECS if "itoo_vantage" in s.modules]
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].name, "rendering")
        self.assertIn("itoo_vantage_check", SKIP_DEFAULT)
        self.assertNotIn("itoo_vantage_check", MUTATE_TOOLS)
        self.assertEqual(MODULE_CATEGORY["itoo_vantage"], "Render")
        self.assertEqual(_module_tool_names(Path(iv.__file__)), ("itoo_vantage_check",))

    def test_progressive_profile_indexes_the_tool(self):
        from maxmcp import server
        from maxmcp.tool_discovery import ProgressiveToolCatalog
        catalog = ProgressiveToolCatalog(  # raises if a module isn't in exactly one toolset
            package="maxmcp", tools_dir=Path(iv.__file__).resolve().parent, hidden_mcp=MagicMock(),
            allowed_modules=server.CORE_TOOL_MODULES + server.SPECIALTY_TOOL_MODULES)
        self.assertEqual(set(catalog.module_tools.get("itoo_vantage", ())), {"itoo_vantage_check"})

    def test_python_only_tool_stays_out_of_native_registry(self):
        from scripts.gen_tool_registry import extract_tools
        self.assertEqual(extract_tools(Path(iv.__file__)), [])

    def test_plugin_descriptors_and_docs(self):
        from maxmcp.tools import plugins
        for key in ("forestpack", "railclone"):
            self.assertIn("itoo_vantage_check", plugins.PLUGIN_OVERLAYS[key]["workflow_tools"])
        self.assertIn("itoo_vantage_check", (REPO / "skills" / "3dsmax-mcp-dev" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("itoo_vantage_check", (REPO / "docs" / "CHANGELOG.md").read_text(encoding="utf-8"))
        doc = (REPO / "docs" / "fork" / "itoo-vantage-check.md").read_text(encoding="utf-8")
        for code in iv.CODES:
            self.assertIn(code, doc, f"{code} is not documented")

    def test_docstring_first_paragraph_is_short(self):
        first = re.sub(r"\s+", " ", iv.itoo_vantage_check.__doc__.split("\n\n", 1)[0]).strip()
        self.assertLess(len(first), 300)


class ScriptTests(unittest.TestCase):
    """The generated MAXScript only reads."""

    def script(self, names=("A", "B")):
        return iv.build_script(list(names), 12345, 678)

    def test_no_write_like_calls(self):
        self.assertEqual(write_lint(self.script()), [])
        self.assertEqual(write_lint(self.script(())), [])

    def test_lint_catches_writes(self):
        for bad in ("nd.seed = nd.seed", "b.gscale = b.gscale", "rc.railclone.segmentsUpdate 1 4",
                    "nd.trees.update()", "t.update()", "nd.material = m", "nd.baseobject.sobjmtl[1] = m",
                    "f.maplist[1] = undefined", "x.pos += [1,0,0]", "select nd", "clearSelection()", "delete nd",
                    "render vfb:false", "api.getStyleDesc()", "rc.railclone.setProxyMode 1 \"\"",
                    "RailClone_Pro.Instantiate 0 \"\" false true false false", "max undo", "hide nd",
                    "setProperty b #disabled true", "snapshotAsMesh nd", "addModifier nd (Uvwmap())",
                    "with undo on (x)", "at time 5 (nd.pos)", "sliderTime = 5", "completeRedraw()"):
            with self.subTest(bad=bad):
                self.assertTrue(write_lint(bad), bad)
        # reads, locals and array elements are not writes
        for good in ("local x = nd.seed", "ctx[2] += 1", "hist[gid] += 1", "ctx[7][1] = true",
                     "if nd.material == undefined do x = 1", 'format "a=%\\n" b.seed to:ss',
                     "local c = (classOf o.baseobject) as string", "if isProperty b #rendermode do r = b.rendermode"):
            with self.subTest(good=good):
                self.assertEqual(write_lint(good), [], good)

    def test_maxscript_pitfalls(self):
        script = self.script()
        self.assertEqual(script.count("("), script.count(")"))
        self.assertNotIn("@@", script)
        check_formats(self, script)
        check_live_pitfalls(self, script)
        self.assertNotIn("collect", script)  # no for-collect at all (Bad free thunk)

    def test_names_budget_and_caps_are_substituted(self):
        name = 'Fence "1" | ëÿ \\ x'
        script = iv.build_script([name], 12345, 678)
        self.assertNotIn(name, script)
        self.assertIn(f'#("{b64(name)}")', script)
        self.assertIn("t0 + 12345", script)
        self.assertIn("#(#(), 0, 678, false, deadline, t0, needVer)", script)
        self.assertIn("local wanted = #()", iv.build_script([], 1000))

    def test_export_is_read_before_the_style_and_never_rebuilt(self):
        body = iv.MXS_LIB[iv.MXS_LIB.index("fn ivRail"):]
        self.assertLess(body.index("getXMLOutput()"), body.index("getXMLStyle()"))
        self.assertNotIn("getStyleDesc", iv.MXS_LIB + iv.MXS_MAIN)

    def test_plugin_classes_are_compared_by_name(self):
        # Referencing Forest_Color etc. as globals fails when a plugin is missing; compare class names instead.
        for cls in ("Forest_Pro", "RailClone_Pro", "ForestIvy", "Forest_Color", "RailClone_Color", "Forest_Material",
                    "VRayMultiSubTex", "VRayProxy", "XShadow"):
            for line in _code(iv.MXS_LIB + iv.MXS_MAIN):
                self.assertNotRegex(line, rf"isKindOf [^\n]*\b{cls}\b", cls)
                self.assertNotRegex(line, rf"==\s*{cls}\b", cls)


class ParseTests(unittest.TestCase):
    def test_errors(self):
        for raw, code in ((None, "INVALID_READBACK"), ("IVERR|-- Runtime error: boom", "MAXSCRIPT_ERROR"),
                          ("__MCP_MS_ERR__: -- Syntax error", "MAXSCRIPT_ERROR"), ("OK", "INVALID_READBACK"),
                          ("IVC1\nS|renderer|u\n", "INVALID_READBACK"),
                          ("IVC1\nK|disabled|b0\nEND\n", "INVALID_READBACK"),
                          ("IVC1\nN|forest|1|" + b64("F") + "\nK|count|q12\nEND\n", "INVALID_READBACK"),
                          ("IVC1\nN|forest|1|" + b64("F") + "\nZZ|1\nEND\n", "INVALID_READBACK"),
                          ("IVC1\nN|forest|1|" + b64("F") + "\nIT|1|i2\nEND\n", "INVALID_READBACK")):
            with self.subTest(raw=raw), self.assertRaises(iv.ItooVantageError) as ctx:
                iv.parse_report(raw)
            self.assertEqual(ctx.exception.code, code)

    def test_values(self):
        self.assertIsNone(iv._dv("u"))
        self.assertIs(iv._dv("x"), iv.MISSING)
        self.assertIs(iv._dv("b1"), True)
        self.assertEqual(iv._dv("i-3"), -3)
        self.assertEqual(iv._dv("f0.0135"), 0.0135)
        self.assertIsNone(iv._dv("f-nan(ind)"))
        self.assertIsNone(iv._dv("finf"))
        self.assertEqual(iv._dv("s" + b64("Ä|b")), "Ä|b")
        self.assertEqual(iv._dv("o" + b64("VRayMtl") + ":" + b64("m:1")), {"class": "VRayMtl", "name": "m:1"})

    def test_live_report(self):
        facts = iv.parse_report(LIVE_REPORT.read_text(encoding="utf-8"))
        self.assertEqual(facts["missing"], ["IVK_nope"])
        self.assertEqual(facts["not_itoo"], [{"name": "IVK_omni", "class": "Omnilight"}])
        self.assertEqual([x["name"] for x in facts["xshadow_lights"]], ["IVK_omni"])
        self.assertEqual([o["name"] for o in facts["objects"]],
                         ["IVK_rc_main", "IVK_rc_off", "IVK_rc_proxy", "IVK_fp_main", "IVK_ivy", "VC_board",
                          "VC_edge_forest", "VC_glass"])
        main = facts["objects"][0]
        self.assertEqual(main["out"], {"chars": 29023, "items": 176, "sourced": 176})
        self.assertEqual([s["faces"] for s in main["segs"]], [96, 116, 32, 118])


class LiveReportTests(unittest.TestCase):
    """The analysis of the report captured live (see the fixtures in docs/fork/itoo-vantage-check.md)."""

    @classmethod
    def setUpClass(cls):
        cls.result = iv.analyze(iv.parse_report(LIVE_REPORT.read_text(encoding="utf-8")), 3)
        cls.by_name = {o["name"]: o for o in cls.result["objects"]}

    def found(self, name):
        return {f["code"] for f in self.by_name[name]["findings"]}

    def test_objects(self):
        r = self.result
        self.assertTrue(r["complete"])
        self.assertTrue(r["read_only"])
        self.assertEqual(r["summary"]["kinds"], {"forest": 2, "railclone": 5, "ivy": 1})
        self.assertEqual(r["not_found"], ["IVK_nope"])
        self.assertEqual({f["code"] for f in r["scene_findings"]}, {"XSHADOW_LIGHT", "FALLBACK"})
        self.assertEqual(r["warnings"], [])
        self.assertEqual(r["scene"]["save_required_after"], True)
        self.assertEqual(set(r["codes"]), {f["code"] for o in r["objects"] for f in o["findings"]}
                         | {f["code"] for f in r["scene_findings"]})
        self.assertEqual(set(r["fallbacks"]), {"forest", "railclone"})

    def test_railclones(self):
        self.assertEqual(self.found("IVK_rc_main"), {
            "RC_RENDER_BOXES", "RC_SEGMENT_MATERIAL", "RC_SOBJMTL_NOT_ONE_SLOT_MULTI", "COLORMAP_VARIATION_LOST",
            "OPACITY_MAP_NOT_BITMAP", "RC_MODIFIERS_OFF", "RC_INSTANCING_OFF", "RC_SEGMENT_MAPPING_MESH",
            "COLORMAP_SLOT1", "OPACITY_MAPPED"})
        main = self.by_name["IVK_rc_main"]
        self.assertEqual(main["state"]["export"]["estimated_faces"], 12112)
        sob = next(f for f in main["findings"] if f["code"] == "RC_SOBJMTL_NOT_ONE_SLOT_MULTI")
        self.assertIn("[1] allum_rail_01: Standardmaterial", sob["where"][0])
        self.assertNotIn("[2]", sob["where"][0])  # the one-slot Multimaterial is fine
        self.assertEqual(self.found("IVK_rc_off"), {"RC_DISABLED"})
        self.assertEqual(self.by_name["IVK_rc_off"]["fallback"], "railclone")
        self.assertIn("caps", {u["check"] for u in self.by_name["IVK_rc_off"]["unknown"]})
        self.assertEqual(self.found("IVK_rc_proxy"), {"RC_PROXY_CACHE_EMBEDDED"})
        self.assertIn("faces", {u["check"] for u in self.by_name["IVK_rc_proxy"]["unknown"]})
        self.assertEqual(self.found("VC_board"), {"COLORMAP_SLOT1_FLAT", "RC_MATERIAL_OPERATOR",
                                                  "COLORMAP_VARIATION_LOST"})
        self.assertEqual(self.found("VC_glass"), {"RC_MODIFIERS"})

    def test_forests_and_ivy(self):
        self.assertEqual(self.found("IVK_fp_main"), {
            "FOREST_RENDER_MAX_ITEMS_EXCEEDED", "COLORMAP_SLOT1_FLAT", "FOREST_MESH_RENDER_MODE",
            "CPU_ONLY_BILLBOARD", "XSHADOW_BILLBOARDS", "ID_MAP_IN_FOREST", "COLORMAP_VARIATION_LOST",
            "FORESTCOLOR_GPU_TINT_FROM_MAP", "EDGE_MODE_UNVERIFIED", "FPCOLOR_HARMLESS", "OPACITY_MAPPED"})
        self.assertEqual(self.by_name["IVK_fp_main"]["state"]["estimated_faces"], 50232)
        self.assertEqual(self.found("VC_edge_forest"), {"COLORMAP_SLOT1_EMPTY", "COLORMAP_VARIATION_LOST",
                                                        "OPACITY_MAP_NOT_BITMAP", "FPCOLOR_HARMLESS",
                                                        "OPACITY_MAPPED"})
        self.assertEqual(self.found("IVK_ivy"), {"IVY_RENDER_INSTANCING"})


class ColorMapTests(unittest.TestCase):
    def check(self, *cms, k=None):
        return codes(run(*forest(k=k, extra=cms)))

    def test_slot1(self):
        found = self.check(cm(s1cls="", s1name="", s1file=""))
        self.assertEqual(found["COLORMAP_SLOT1_EMPTY"]["severity"], "error")
        self.assertIn("slot 1 = empty", found["COLORMAP_SLOT1_EMPTY"]["where"][0])
        for flat in (dict(s1cls="VRayColor"), dict(s1cls="Color_Correction", s1inputs=0),
                     dict(s1cls="output", s1inputs=0), dict(s1cls="Mix", s1inputs=0)):
            with self.subTest(flat=flat):
                found = self.check(cm(**flat))
                self.assertIn("COLORMAP_SLOT1_FLAT", found)
                self.assertNotIn("COLORMAP_SLOT1", found)
        found = self.check(cm(s1cls="Color_Correction", s1inputs=1))
        self.assertIn("COLORMAP_SLOT1", found)
        found = self.check(cm(s1cls="VRayBitmap", s1name="oak", s1file="oak.jpg"))
        self.assertEqual(found["COLORMAP_SLOT1"]["where"][0],
                         "item 1 Box001/mtl/Diffuse: Forest_Color 'fc', slot 1 = VRayBitmap 'oak' (oak.jpg)")
        self.assertNotIn("COLORMAP_SLOT1_FLAT", found)
        found = self.check(cm(s1cls="RailClone_Color", s1inputs=2))
        self.assertIn("COLORMAP_SLOT1_NESTED", found)

    def test_variation_is_always_lost(self):
        found = self.check(cm(), cm(path="b", assigned=0, enabled=0))
        self.assertEqual(found["COLORMAP_VARIATION_LOST"]["count"], 2)
        self.assertEqual(found["COLORMAP_VARIATION_LOST"]["severity"], "warn")
        self.assertIn("1 of 10 map slots set (1 enabled)", found["COLORMAP_VARIATION_LOST"]["where"][0])

    def test_gpu_tint_from_map(self):
        # the Forest object's tint applies when override is off: tint off (0/0) means nothing to flag
        self.assertNotIn("FORESTCOLOR_GPU_TINT_FROM_MAP", self.check(cm(), k={"tintmode": 1}))
        found = self.check(cm(), k={"tintmode": 1, "tintmapmode": 1, "tintmin": 10, "tintmax": 50})
        self.assertIn("the Forest object (override off), tintmapmode 1 (As Texture on Surface [inf])",
                      found["FORESTCOLOR_GPU_TINT_FROM_MAP"]["where"][0])
        # the map's own tint applies when override is on
        found = self.check(cm(override=True, tintmode=1, tintmin=25, tintmax=100))
        self.assertIn("the map (override on), tintmapmode 0 (Random Values [inf])",
                      found["FORESTCOLOR_GPU_TINT_FROM_MAP"]["where"][0])
        self.assertNotIn("FORESTCOLOR_GPU_TINT_FROM_MAP", self.check(cm(override=True, tintmode=0)))
        # RailClone Color: no GPU mode check (not documented for it)
        self.assertNotIn("FORESTCOLOR_GPU_TINT_FROM_MAP",
                         self.check(cm(cls="RailClone_Color", override=True, tintmode=1, tintmin=1, tintmax=2)))
        r = run(*forest(k={"tintmode": iv.MISSING}, extra=[cm()]))
        self.assertIn("forest_color_gpu_mode", unknown(r))
        self.assertIn("tintmode", unknown(r))

    def test_in_railclone_and_ivy(self):
        r = run(*railclone(extra=rc_extra() + [cm(cls="RailClone_Color", s1cls="")]),
                *obj_rows("ivy", "Ivy", {"renderinstancing": False, "maxbranches": 100, "maxleaves": 100,
                                         "material": None}, [cm(s1cls="VRayColor")]))
        self.assertIn("COLORMAP_SLOT1_EMPTY", codes(r, 0))
        self.assertEqual(set(codes(r, 1)), {"COLORMAP_SLOT1_FLAT", "COLORMAP_VARIATION_LOST"})


class MaterialTests(unittest.TestCase):
    def test_opacity(self):
        found = codes(run(*forest(extra=[op(), op(path="b", map_class="output"), op(path="c", map_class="Checker")])))
        self.assertEqual(found["OPACITY_MAPPED"]["count"], 3)
        self.assertEqual(found["OPACITY_MAPPED"]["severity"], "info")
        self.assertEqual(found["OPACITY_MAP_NOT_BITMAP"]["count"], 2)

    def test_forest_material_and_paths(self):
        rows = [f"FM|{b64(f'matList[{i}]/fm')}|{b64('fm')}" for i in range(1, 8)]
        found = codes(run(*forest(extra=rows), max_paths=2))
        fm = found["CPU_ONLY_FOREST_MATERIAL"]
        self.assertEqual((fm["count"], len(fm["where"]), fm.get("where_truncated")), (7, 2, True))

    def test_id_maps(self):
        def ms(from_id, path="item 1 Box001/m/Diffuse"):
            return f"MS|{b64(path)}|{b64('mst')}|{v(from_id)}"
        found = codes(run(*forest(extra=[ms(0), ms(1, "b"), ms(12, "c")])))
        self.assertEqual(found["ID_MAP_IN_FOREST"]["count"], 1)
        self.assertEqual(found["ID_MAP_IN_FOREST"]["severity"], "warn")
        found = codes(run(*forest(k={"consmat": False}, extra=[ms(0)])))
        self.assertIn("ID_MAP_UNCONSOLIDATED", found)
        self.assertNotIn("ID_MAP_IN_FOREST", found)
        r = run(*forest(extra=[ms(iv.MISSING)]))
        self.assertIn("multisubtex_mode", unknown(r))
        # RailClone doesn't consolidate: no ID-map finding there
        self.assertNotIn("ID_MAP_IN_FOREST", codes(run(*railclone(extra=rc_extra() + [ms(0)]))))

    def test_node_material(self):
        found = codes(run(*forest(extra=[f"FPC|{b64('node/forest_automat_X[3]/FPColor ID 0')}"] * 3)))
        self.assertEqual(found["FPCOLOR_HARMLESS"]["where"], ["3 FPColor material(s)"])
        self.assertIn("FOREST_NO_AUTOMATERIAL", codes(run(*forest(k={"material": None}))))
        mine = {"class": "VRayMtl", "name": "Grass"}
        self.assertEqual(codes(run(*forest(k={"material": mine})))["FOREST_NODE_MATERIAL_REPLACED"]["severity"],
                         "warn")
        self.assertIn("FOREST_NODE_MATERIAL", codes(run(*forest(k={"material": mine, "consmat": False}))))

    def test_walk_truncated_and_read_errors(self):
        r = run(*forest(extra=["WT|4001", f"NERR|{b64('-- Unknown property: foo')}", f"U|boundary_check|{b64('no')}"]))
        u = unknown(r)
        self.assertIn("4001", u["materials"])
        self.assertIn("Unknown property", u["read"])
        self.assertEqual(u["boundary_check"], "no")
        self.assertFalse(r["complete"])
        self.assertTrue(any(w.startswith("PARTIAL_READ") for w in r["warnings"]))


class ForestTests(unittest.TestCase):
    def test_healthy(self):
        r = run(*forest())
        o = r["objects"][0]
        self.assertEqual((o["status"], o["findings"], o["unknown"]), ("ok", [], []))
        self.assertNotIn("fallback", o)
        self.assertEqual(o["state"]["viewport_thinning"], "unlikely")
        self.assertNotIn("items", o["state"])
        self.assertIn("items", run(*forest(), details=True)["objects"][0]["state"])

    def test_disabled_and_empty(self):
        r = run(*forest(k={"disabled": True}))
        self.assertEqual(r["objects"][0]["status"], "error")
        self.assertEqual(r["objects"][0]["fallback"], "forest")
        self.assertIn("forest", r["fallbacks"])
        self.assertIn("FALLBACK", {f["code"] for f in r["scene_findings"]})
        self.assertIn("FOREST_NO_ITEMS", codes(run(*forest(k={"count": 0}))))
        self.assertIn("count", unknown(run(*forest(k={"count": None}))))

    def test_viewport_thinning(self):
        # live: 143700 items at vmaxitems 50000 gave trees.count() 35800; 1437 stayed 1437
        for count, vmax, state in ((35800, 50000, "possible"), (12501, 50000, "possible"), (12500, 50000, "unlikely"),
                                   (1437, 50000, "unlikely"), (143700, 0, "none")):
            with self.subTest(count=count, vmax=vmax):
                r = run(*forest(k={"count": count, "vmaxitems": vmax}))
                self.assertEqual(r["objects"][0]["state"]["viewport_thinning"], state)
                self.assertEqual("FOREST_VIEWPORT_THINNING" in codes(r), state == "possible")
                self.assertEqual("render_item_count" in unknown(r), state == "possible")

    def test_render_caps(self):
        found = codes(run(*forest(k={"count": 516, "rmaxitems": 100})))
        self.assertEqual(found["FOREST_RENDER_MAX_ITEMS_EXCEEDED"]["severity"], "error")
        found = codes(run(*forest(k={"count": 35800, "rmaxitems": 100000})))
        self.assertIn("FOREST_RENDER_MAX_ITEMS_RISK", found)
        self.assertIn("FOREST_RENDER_MAX_ITEMS_NEAR", codes(run(*forest(k={"count": 9000, "rmaxitems": 10000}))))
        self.assertIn("FOREST_RENDER_MAX_ITEMS_UNLIMITED", codes(run(*forest(k={"rmaxitems": 0}))))

    def test_mesh_mode_faces(self):
        items = [item(1, faces=12), item(2, geom=1, name="bb", source_class=None, source=None),
                 item(3, name="Cone", faces=288)]
        hist = ["HIST|1|178", "HIST|2|171", "HIST|3|167"]
        r = run(*forest(k={"rendermode": 1}, items=items, extra=hist))
        self.assertEqual(r["objects"][0]["state"]["estimated_faces"], 50232)  # live IVK_fp_main
        self.assertIn("FOREST_MESH_RENDER_MODE", codes(r))
        self.assertIn("bb", unknown(r)["faces"])
        found = codes(run(*forest(k={"rendermode": 1, "maxFaces": 1}, items=[item(1, faces=10)],
                                  extra=["HIST|1|200000"])))
        self.assertIn("FOREST_MAX_FACES_EXCEEDED", found)
        found = codes(run(*forest(k={"rendermode": 1, "maxFaces": 1}, items=[item(1, faces=10)],
                                  extra=["HIST|1|90000"])))
        self.assertIn("FOREST_MAX_FACES_NEAR", found)
        self.assertIn("faces", unknown(run(*forest(k={"rendermode": 1}))))  # no histogram
        self.assertNotIn("FOREST_MESH_RENDER_MODE", codes(run(*forest(k={"rendermode": 0}))))

    def test_display_settings(self):
        found = codes(run(*forest(k={"rmesh": 2, "rskip": 1})))
        self.assertEqual(set(found), {"FOREST_RENDER_DISPLAY", "FOREST_RENDER_SIMPLIFICATION"})

    def test_items(self):
        items = [item(1), item(2, geom=1, name="bb", source_class=None, source=None), item(3, geom=3, name="x"),
                 item(4, geom=0, name="gap", source_class=None, source=None), item(5, geom=7, name="odd")]
        found = codes(run(*forest(items=items)))
        self.assertEqual(found["CPU_ONLY_BILLBOARD"]["where"], ["item 2 'bb'"])
        self.assertIn("XSHADOW_BILLBOARDS", found)
        self.assertEqual(found["GEOMLIST_CRASH_RISK"]["severity"], "error")
        self.assertEqual(found["GEOMLIST_UNKNOWN"]["where"], ["item 5 'odd': 7"])
        self.assertNotIn("XSHADOW_BILLBOARDS", codes(run(*forest(k={"usefakeshadows": False}, items=items))))

    def test_edge_mode(self):
        proxy = item(1, source_class="VRayProxy", source="tree_proxy")
        found = codes(run(*forest(items=[proxy], areas=[area(boundary=2)])))
        self.assertIn("EDGE_MODE_ON_PROXIES", found)
        r = run(*forest(items=[proxy], areas=[area(boundary=2, active=False), area(2, boundary=0)]))
        self.assertNotIn("EDGE_MODE_ON_PROXIES", codes(r))
        self.assertIn("proxy_items", unknown(r))
        self.assertIn("EDGE_MODE_UNVERIFIED", codes(run(*forest(areas=[area(boundary=2)]))))
        self.assertIn("boundary_check", unknown(run(*forest(areas=[area(boundary=None)]))))


class RailCloneTests(unittest.TestCase):
    def test_healthy(self):
        r = run(*railclone())
        o = r["objects"][0]
        self.assertEqual((o["status"], o["findings"], o["unknown"]), ("ok", [], []))
        self.assertEqual(o["state"]["export"], {"state": "present", "items": 176, "estimated_faces": 12112,
                                                "max_segments": 50000, "max_faces_millions": 5.0})
        self.assertNotIn("segment_list", o["state"])
        detail = run(*railclone(), details=True)["objects"][0]["state"]
        self.assertEqual([s["items"] for s in detail["segment_list"]], [80, 8, 80, 8])
        self.assertEqual(r["notes"], [iv.EXPORT_NOTE])

    def test_disabled_and_empty_export(self):
        empty = ["OUT|26|0|0"]
        r = run(*railclone(k={"disabled": True}, out=empty))
        self.assertEqual(set(codes(r)), {"RC_DISABLED"})
        self.assertIn("no current export", unknown(r)["caps"])
        r = run(*railclone(out=empty))
        self.assertEqual(set(codes(r)), {"RC_EXPORT_EMPTY"})
        self.assertIn("no current export", unknown(r)["caps"])
        r = run(*railclone(out=None))
        self.assertIn("couldn't be read", unknown(r)["caps"])
        self.assertEqual(r["objects"][0]["state"]["export"]["state"], "unreadable")

    def test_caps(self):
        # live: 12112 faces; maxFaces 0.0122 kept the output, 0.0121 emptied it
        found = codes(run(*railclone(k={"maxFaces": 0.0135, "maxseg": 200})))
        self.assertEqual(set(found), {"RC_MAXFACES_NEAR", "RC_MAXSEG_NEAR"})
        self.assertIn("about 12112 faces", found["RC_MAXFACES_NEAR"]["where"][0])
        found = codes(run(*railclone(k={"maxFaces": 0.0121, "maxseg": 100})))
        self.assertEqual({f["severity"] for f in found.values()}, {"error"})
        self.assertEqual(set(found), {"RC_MAXFACES_EXCEEDED", "RC_MAXSEG_EXCEEDED"})
        found = codes(run(*railclone(k={"maxFaces": 0.0, "maxseg": 0})))
        self.assertEqual(set(found), {"RC_MAXFACES_UNLIMITED", "RC_MAXSEG_UNLIMITED"})
        self.assertIn("RC_AUTOUPDATE_OFF", codes(run(*railclone(k={"autoUpdate": False}))))

    def test_partial_face_estimate(self):
        proxy = seg(3, "allum_picket_01", -1, ref_class="VRayProxy")
        r = run(*railclone(seg_overrides={3: proxy}))
        self.assertEqual(r["objects"][0]["state"]["export"]["estimated_faces"], 12112 - 80 * 32)
        self.assertTrue(r["objects"][0]["state"]["export"]["estimated_faces_partial"])
        self.assertIn("allum_picket_01", unknown(r)["faces"])
        # a proxy cache exports items without source_segment
        r = run(*railclone(out=["OUT|24569|176|0"]))
        self.assertIsNone(r["objects"][0]["state"]["export"]["estimated_faces"])
        self.assertIn("no source segment", unknown(r)["faces"])

    def test_render_settings(self):
        found = codes(run(*railclone(k={"rendermode": False, "rmesh": 1, "nodemat": True}, style=(2, 1, 0))))
        self.assertEqual(set(found), {"RC_INSTANCING_OFF", "RC_RENDER_BOXES", "RC_SEGMENT_MATERIAL",
                                      "RC_MATERIAL_OPERATOR_MESH"})
        found = codes(run(*railclone(style=(2, 1, 0), seg_overrides={1: seg(1, "allum_rail_01", 96, mapping=True)})))
        self.assertEqual(set(found), {"RC_MATERIAL_OPERATOR", "RC_SEGMENT_MAPPING"})
        self.assertEqual(found["RC_MATERIAL_OPERATOR"]["where"], ["2 enabled Material operator node(s)"])
        self.assertIn("rmesh", unknown(run(*railclone(k={"rmesh": 3}))))
        self.assertIn("material_operator", unknown(run(*railclone(style=None))))
        self.assertNotIn("RC_MATERIAL_OPERATOR", codes(run(*railclone(style=(0, 3, 0)))))  # only disabled ones

    def test_modifiers(self):
        found = codes(run(*railclone(mods=[(1, "Bend", "Bend", True, True), (2, "Uvwmap", "UVW", True, False)])))
        self.assertEqual(found["RC_MODIFIERS"]["where"], ["[1] Bend 'Bend'"])
        found = codes(run(*railclone(mods=[(1, "Uvwmap", "UVW", True, False), (2, "Bend", "B", False, True)])))
        self.assertEqual(set(found), {"RC_MODIFIERS_OFF"})

    def test_proxy_cache(self):
        self.assertIn("RC_PROXY_CACHE_EMBEDDED", codes(run(*railclone(k={"proxymode": 1}))))
        found = codes(run(*railclone(k={"proxymode": 2, "proxyfile": r"C:\c\a.rcproxy", "proxyfile_exists": False})))
        self.assertEqual(set(found), {"RC_PROXY_CACHE_FILE", "RC_PROXY_FILE_MISSING"})
        self.assertIn("proxymode", unknown(run(*railclone(k={"proxymode": 9}))))

    def test_proxy_segments_on_mesh_mode(self):
        proxy = {3: seg(3, "allum_picket_01", -1, ref_class="", source_class="VRayProxy", source="picket_proxy")}
        found = codes(run(*railclone(k={"rendermode": False}, seg_overrides=proxy), scene={"railclone_version": "7,3,5,687"}))
        self.assertEqual(found["RC_PROXY_MESH_REVERTABLE"]["where"], ["[3] allum_picket_01"])
        self.assertNotIn("RC_PROXY_MESH_REVERTABLE", codes(run(*railclone(k={"rendermode": False}, seg_overrides=proxy),
                                                               scene={"railclone_version": "6,1,0,0"})))
        r = run(*railclone(k={"rendermode": False}, seg_overrides=proxy))
        self.assertIn("railclone_version", unknown(r))
        self.assertNotIn("RC_PROXY_MESH_REVERTABLE", codes(run(*railclone(seg_overrides=proxy),
                                                               scene={"railclone_version": "7,3,5,687"})))

    def test_sobjmtl_wrapping(self):
        overrides = {1: seg(1, "allum_rail_01", 96, mtl_class="Standardmaterial"),
                     2: seg(2, "allum_post_01_main", 116, mtl_class="Multimaterial", mtl_subs=1),
                     4: seg(4, "allum_post_01_top", 118, mtl_class="Multimaterial", mtl_subs=2)}
        found = codes(run(*railclone(seg_overrides=overrides)))
        self.assertEqual(found["RC_SOBJMTL_NOT_ONE_SLOT_MULTI"]["where"],
                         ["[1] allum_rail_01: Standardmaterial (0 subs), [4] allum_post_01_top: Multimaterial (2 subs)"])

    def test_missing_properties_are_unknown(self):
        r = run(*railclone(k={"nodemat": iv.MISSING}))
        self.assertEqual(unknown(r)["nodemat"], "no such property on this build")
        self.assertNotIn("RC_SEGMENT_MATERIAL", codes(r))


class SceneTests(unittest.TestCase):
    def test_scene_findings_and_lists(self):
        r = run(*forest(), xshadow=[(5, "Omni001")], missing=["Nope"], not_itoo=[("Box001", "Box")],
                scene={"renderer": "Default_Scanline_Renderer"})
        self.assertEqual({f["code"] for f in r["scene_findings"]}, {"XSHADOW_LIGHT", "RENDERER_NOT_VRAY"})
        self.assertEqual(r["not_found"], ["Nope"])
        self.assertEqual(r["not_itoo"], [{"name": "Box001", "class": "Box"}])
        self.assertEqual(r["codes"]["XSHADOW_LIGHT"]["severity"], "warn")
        self.assertEqual(r["fallbacks"], {})

    def test_dirty_scene_and_budget(self):
        r = run(*forest(), scene={"save_required_before": False, "after_save": True})
        self.assertTrue(any(w.startswith("SCENE_MARKED_DIRTY") for w in r["warnings"]))
        r = run(*forest(), scene={"save_required_before": False})
        self.assertEqual(r["warnings"], [])
        r = run(*forest(), skipped=[(9, "Late")])
        self.assertFalse(r["complete"])
        self.assertEqual(r["not_checked"], ["Late"])
        self.assertTrue(any(w.startswith("TIME_BUDGET") for w in r["warnings"]))

    def test_summary(self):
        r = run(*forest(k={"disabled": True}), *railclone(k={"nodemat": True}), *forest(name="ok"))
        self.assertEqual(r["summary"]["status"], {"error": 1, "warn": 1, "ok": 1})
        self.assertEqual(r["summary"]["findings"], {"error": 1, "warn": 1, "info": 1})  # + the scene FALLBACK info
        self.assertEqual(r["summary"]["kinds"], {"forest": 2, "railclone": 1, "ivy": 0})

    def test_code_table(self):
        tags = {"doc", "live", "inf", "memory"}
        for code, (severity, basis, message, fix) in iv.CODES.items():
            with self.subTest(code=code):
                self.assertIn(severity, ("error", "warn", "info"))
                self.assertTrue(set(basis.split("+")) <= tags, basis)
                self.assertTrue(message.endswith((".", ")")), message)
                self.assertTrue(fix is None or fix.endswith("."), fix)
        self.assertEqual(set(iv.BASIS), tags)


class ToolTests(unittest.TestCase):
    def call(self, raw=None, **kwargs):
        client = MagicMock()
        client.send_command.return_value = {"ok": True, "result": raw if raw is not None else report(*forest())}
        with patch.object(iv, "client", client):
            result = iv.itoo_vantage_check(**kwargs)
        return client, result

    def test_one_read_only_call(self):
        client, result = self.call(names=["F", "F", "RC"], time_budget_s=12.5)
        self.assertEqual(client.send_command.call_count, 1)
        script = client.send_command.call_args.args[0]
        self.assertEqual(client.send_command.call_args.kwargs, {"timeout": 72.5, "probe": True})
        self.assertEqual(write_lint(script), [])
        self.assertIn(f'#("{b64("F")}", "{b64("RC")}")', script)
        self.assertIn("t0 + 12500", script)
        self.assertEqual(result["objects"][0]["name"], "F")

    def test_validation(self):
        for kwargs in ({"names": [""]}, {"names": ["a\x00b"]}, {"names": [3]}, {"names": {"a": 1}},
                       {"names": [f"n{i}" for i in range(501)]}, {"time_budget_s": 0.5}, {"time_budget_s": 301},
                       {"time_budget_s": True}, {"time_budget_s": float("nan")}, {"max_paths": 0},
                       {"max_paths": True}, {"details": "yes"}):
            with self.subTest(kwargs=kwargs):
                client = MagicMock()
                with patch.object(iv, "client", client), self.assertRaises(ValueError):
                    iv.itoo_vantage_check(**kwargs)
                client.send_command.assert_not_called()
        self.assertEqual(iv._names("Fence"), ["Fence"])
        self.assertEqual(iv._names("  "), [])

    def test_bridge_errors(self):
        for response, code in (({"ok": False, "error": {"code": "MAX_BUSY", "message": "busy"}}, "MAX_BUSY"),
                               ({"error": "pipe closed"}, "BRIDGE_ERROR"), ("nope", "BRIDGE_ERROR"),
                               ({"ok": True, "result": "IVERR|-- boom"}, "MAXSCRIPT_ERROR")):
            with self.subTest(response=response):
                client = MagicMock()
                client.send_command.return_value = response
                with patch.object(iv, "client", client), self.assertRaises(iv.ItooVantageError) as ctx:
                    iv.itoo_vantage_check()
                self.assertEqual(ctx.exception.code, code)
        self.assertTrue(iv.ItooVantageError("MAX_BUSY", "busy").retryable)

    def test_details_flag_reaches_the_analysis(self):
        _, result = self.call(details=True)
        self.assertIn("items", result["objects"][0]["state"])


if __name__ == "__main__":
    unittest.main()
