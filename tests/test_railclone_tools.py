"""Tests for the RailClone tools: duplicate-attribute XML, output rebuild/bounds, inspect,
set_param, library search and from_library (mocked 3ds Max)."""

import base64
import os
import re
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import railclone_lib as rclib  # noqa: E402
from maxmcp.tools import railclone as rc  # noqa: E402


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def unb64(text):
    return base64.b64decode(text).decode("utf-8")


def V(value):
    """Encode a value the way the MAXScript rcV helper does."""
    if value is None:
        return "u"
    if isinstance(value, bool):
        return "b1" if value else "b0"
    if isinstance(value, int):
        return "i%d" % value
    if isinstance(value, float):
        return "f%r" % value
    if isinstance(value, str):
        return "s" + b64(value)
    if isinstance(value, dict) and "handle" in value:
        return "n%d:%s" % (value["handle"], b64(value["name"]))
    if isinstance(value, tuple) and value[0] in ("m", "o"):
        return value[0] + b64(value[1]) + ":" + b64(value[2])
    raise TypeError(value)


def client_with(*results):
    client = MagicMock()
    client.send_command.side_effect = [{"result": r} for r in results]
    return client


def scripts(client):
    return [c.args[0] for c in client.send_command.call_args_list]


def decoded_strings(script):
    """Every rcDecode "<b64>" literal in a script, decoded."""
    return [unb64(m) for m in re.findall(r'rcDecode "([A-Za-z0-9+/=]*)"', script)]


PICKETS = "{193bc6be-8d9b-494a-863e-f5b746a88c0f}"
POSTS = "{b45cbf7c-9c51-49c8-a14d-30758ed13a9b}"

STYLE_DUP = (
    '<?xml version="1.0"?>\n'
    '<scene version="210" masterScale="0.393701" styleDesc="Parametric fence">\n'
    '\t<node id="1" on="1" baseClass="Object" class="Segment" name="rail" x="-298.3" y="-206.6" y="-206.6">\n'
    '\t\t<parameter id="_paronoff" name="On/Off" value="1"/>\n'
    '\t</node>\n'
    '\t<node id="2" on="1" baseClass="Object" class="Segment" name="picket" x="-298.3" y="-1" y="-1">\n'
    f'\t\t<connector id="_exp_10_0" name="Left Padding" parameter="10" target_id="{PICKETS}" '
    'target_name="Pickets Distance"/>\n'
    '\t</node>\n'
    f'\t<node id="{PICKETS}" on="1" baseClass="Parameter" class="Numeric" name="Pickets Distance" x="-502" '
    'y="24" y="24"/>\n'
    f'\t<node id="{POSTS}" on="1" baseClass="Parameter" class="Numeric" name="Posts Distance" x="1" y="2"/>\n'
    '\t<node id="g1" on="1" baseClass="Generator" class="Linear1S" name="Pickets" x="0" y="0" y="0">\n'
    f'\t\t<connector id="_exp_distance_0" name="Evenly Distance" parameter="distance" target_id="{POSTS}" '
    'target_name="Posts Distance"/>\n'
    '\t\t<connector id="in_0" name="Segment" target_id="2"/>\n'
    '\t</node>\n'
    '</scene>\n'
)

OUTPUT = (
    '<RailClone version="1">'
    '<item index="0" instanced="0" source_segment="post" tm="[1,0,0][0,1,0][0,0,1][0,0,0]" '
    'box="[-2.5,-2.5,0][2.5,2.5,120]" tags=""/>'
    '<item index="1" instanced="0" tm="[1,0,0][0,1,0][0,0,1][112.5,0,0]" '
    'box="[2.14748e+09,2.14748e+09,2.14748e+09][-2.14748e+09,-2.14748e+09,-2.14748e+09]" tags=""/>'
    '<item index="2" instanced="1" source_segment="rail" tm="[1,0,0][0,1,0][0,0,1][300,0,100]" '
    'box="[0,-1,100][600,1,105]" tags=""/>'
    '</RailClone>'
)

TOKEN = "-".join(["AB"] * 32)


class ImportTargetTests(unittest.TestCase):
    def test_imports_checkout(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(Path(rc.__file__).resolve(), repo / "maxmcp" / "tools" / "railclone.py")
        self.assertEqual(Path(rclib.__file__).resolve(), repo / "maxmcp" / "helpers" / "railclone_lib.py")


class SanitizeTests(unittest.TestCase):
    def test_duplicate_attributes_removed(self):
        with self.assertRaises(ET.ParseError):
            ET.fromstring(STYLE_DUP.split("\n", 1)[1])
        fixed, removed, conflicts = rclib.sanitize_xml(STYLE_DUP)
        self.assertEqual((removed, conflicts), (4, 0))
        root = ET.fromstring(fixed.split("\n", 1)[1])
        self.assertEqual(len(list(root.iter("node"))), 5)
        self.assertIn('x="-298.3" y="-206.6">', fixed)

    def test_conflicting_duplicates_keep_first(self):
        fixed, removed, conflicts = rclib.sanitize_xml('<a x="1" y="2" y="3" y="2"/>')
        self.assertEqual((fixed, removed, conflicts), ('<a x="1" y="2"/>', 2, 1))

    def test_clean_text_unchanged(self):
        text = '<scene version="400"><node id="1" x="1" y="2"/></scene>'
        self.assertEqual(rclib.sanitize_xml(text), (text, 0, 0))

    def test_maxscript_uses_the_same_pattern(self):
        match = re.search(r'fn rcSanitize text = .*rcDecode "([^"]+)"\) "\$1"', rc._FUNCTIONS)
        self.assertIsNotNone(match)
        self.assertEqual(unb64(match.group(1)), rclib.DUP_ATTR_PATTERN)
        self.assertEqual(rclib.DUP_ATTR_PATTERN, r'(\s([\w:.-]+)="([^"]*)")(?:\s\2="[^"]*")+')


class StyleTests(unittest.TestCase):
    def test_get_style_with_duplicate_attributes(self):
        raw = "RCXML|602061|%s|%s|%s" % (b64("RC_Fence"), TOKEN, b64(STYLE_DUP))
        with patch.object(rc, "client", client_with(raw)) as client:
            out = rc.get_railclone_style(name="RC_Fence")
        self.assertEqual(out["node_count"], 5)
        self.assertEqual(out["duplicate_attributes_removed"], 4)
        self.assertEqual(out["xml"], STYLE_DUP)  # verbatim, duplicates included
        self.assertEqual(out["style_token"], TOKEN)
        self.assertEqual(out["schema"]["version"], "210")
        self.assertIn("getXMLStyle", scripts(client)[0])

    def test_set_style_sends_deduplicated_xml_and_sanitises_readback(self):
        raw = "RCXML|602061|%s|%s|%s" % (b64("RC_Fence"), "-".join(["CD"] * 32), b64(STYLE_DUP))
        with patch.object(rc, "client", client_with(raw)) as client:
            out = rc.set_railclone_style(xml=STYLE_DUP, expected_style=TOKEN, name="RC_Fence")
        script = scripts(client)[0]
        self.assertIn("doc.LoadXml (rcSanitize readback)", script)
        self.assertIn(f'rcToken obj before != "{TOKEN}"', script)
        sent = re.search(r'local inputXml = rcDecode "([^"]+)"', script).group(1)
        self.assertEqual(unb64(sent), rclib.sanitize_xml(STYLE_DUP)[0])
        self.assertEqual((out["status"], out["undoable"], out["sent_duplicates_removed"]), ("Ok", True, 4))
        self.assertEqual(out["node_count"], 5)

    def test_set_style_refuses_bad_token_without_sending(self):
        with patch.object(rc, "client", client_with()) as client:
            with self.assertRaises(ValueError):
                rc.set_railclone_style(xml=STYLE_DUP, expected_style="nope", name="RC_Fence")
        client.send_command.assert_not_called()

    def test_api_error_codes(self):
        self.assertIn("XML_API_UNAVAILABLE", rc._FUNCTIONS)
        self.assertNotIn("7.3.5", rc._FUNCTIONS)


def out_raw(xml=OUTPUT, tm="1,0,0,0,1,0,0,0,1,100,-200,0", shared="", rebuilt="1"):
    return "RCOUT|602061|%s|%s|%s|%s|%s" % (b64("RC_Fence"), tm, shared, rebuilt, b64(xml))


class OutputTests(unittest.TestCase):
    def test_rebuilds_and_flags_placeholders(self):
        with patch.object(rc, "client", client_with(out_raw())) as client:
            out = rc.get_railclone_output(name="RC_Fence", limit=2)
        script = scripts(client)[0]
        self.assertIn("rcRebuild base\nrebuilt = true", script)
        self.assertIn("segmentsUpdate 1 n", script)
        self.assertIn("base.seed = base.seed", script)
        code = "\n".join(line for line in script.splitlines() if not line.strip().startswith("--"))
        self.assertNotIn("notifyDependents", code)
        # backward-compatible fields
        self.assertEqual((out["total"], out["offset"], out["truncated"], out["next_offset"]), (3, 0, True, 2))
        self.assertEqual(out["items"][0]["source_segment"], "post")
        self.assertEqual(out["items"][0]["box"], "[-2.5,-2.5,0][2.5,2.5,120]")
        self.assertEqual([i["placeholder"] for i in out["items"]], [False, True])
        self.assertEqual((out["placed"], out["placeholders"]), (2, 1))
        self.assertEqual(out["by_segment"], {"post": 1, "rail": 1})
        self.assertEqual(out["space"], "object")
        self.assertTrue(out["rebuilt"])
        self.assertEqual(out["bounds_object"], {"min": [-2.5, -2.5, 0.0], "max": [600.0, 2.5, 120.0]})
        self.assertEqual(out["bounds_world"], {"min": [97.5, -202.5, 0.0], "max": [700.0, -197.5, 120.0]})
        self.assertNotIn("xml", out)

    def test_world_bounds_follow_rotation(self):
        xml = ('<RailClone version="1"><item index="0" source_segment="rail" box="[0,-1,100][600,1,105]"/>'
               '</RailClone>')
        with patch.object(rc, "client", client_with(out_raw(xml, tm="0,1,0,-1,0,0,0,0,1,0,0,0"))):
            out = rc.get_railclone_output(name="RC_Fence")
        self.assertEqual(out["bounds_world"], {"min": [-1.0, 0.0, 100.0], "max": [1.0, 600.0, 105.0]})

    def test_no_rebuild_and_shared_base(self):
        raw = out_raw(shared=b64("RC_Fence001"), rebuilt="0")
        with patch.object(rc, "client", client_with(raw)) as client:
            out = rc.get_railclone_output(name="RC_Fence", rebuild=False, include_xml=True)
        self.assertNotIn("rcRebuild base\nrebuilt = true", scripts(client)[0])
        self.assertFalse(out["rebuilt"])
        self.assertEqual(out["shared_base_with"], ["RC_Fence001"])
        self.assertTrue(any(w.startswith("SHARED_BASE") for w in out["warnings"]))
        self.assertEqual(out["xml"], OUTPUT)

    def test_empty_output(self):
        with patch.object(rc, "client", client_with(out_raw('<RailClone version="1"/>'))):
            out = rc.get_railclone_output(name="RC_Fence")
        self.assertEqual((out["total"], out["placed"], out["bounds_world"]), (0, 0, None))
        self.assertTrue(any(w.startswith("EMPTY_OUTPUT") for w in out["warnings"]))

    def test_rebuild_must_be_bool(self):
        with self.assertRaises(ValueError):
            rc.get_railclone_output(name="RC_Fence", rebuild="yes")

    def test_runtime_error_code(self):
        raw = "RCERROR|-- Runtime error: NOT_RAILCLONE: target has no RailClone interface"
        with patch.object(rc, "client", client_with(raw)):
            with self.assertRaises(rclib.RailCloneError) as ctx:
                rc.get_railclone_output(name="Box001")
        self.assertEqual(ctx.exception.code, "NOT_RAILCLONE")


def param_row(i, paid, name, ptype, *, ival=0, fval=0.0, uval=0.0, bval=False, sel="", limit=False,
              imin=-99999, imax=99999, fmin=0.0, fmax=1e7, umin=0.0, umax=1e7, desc=""):
    values = [paid, name, ptype, ival, fval, uval, bval, "", sel, limit, imin, imax, fmin, fmax, umin, umax, desc]
    return "|".join(["P", str(i)] + [V(v) for v in values])


PARAM_ROWS = [
    param_row(1, POSTS, "Posts Distance", 3, uval=150.0, fval=200.0, limit=True, desc="Distance between posts"),
    param_row(2, PICKETS, "Pickets Distance", 3, uval=10.0, limit=True),
    param_row(3, "{c3}", "Spandrel Type", 5, ival=1, fval=2.0, sel="Glass|1;Solid |2"),
    param_row(4, "{c4}", "--Settings--", 7),
    param_row(5, "{c5}", "Repetitions", 0, ival=100004, limit=True),
    param_row(6, "{c6}", "Mirror", 4, bval=True),
    param_row(7, "{c7}", "Z Rotation", 1, fval=45.0, limit=True, fmin=-360.0, fmax=360.0),
]


def head_rows(handle=602061, base_handle="602062"):
    return [
        "K|handle|" + V(handle), "K|name|" + V("RC_Fence"), "K|class|" + V("RailClone_Pro"),
        "K|base_handle|" + V(base_handle), "K|upm|" + V(0.1), "K|units|" + V("centimeters"),
    ]


def inspect_raw(extra_params=()):
    rows = ["RCINSPECT"] + head_rows() + [
        "K|plugin_version|" + V(400), "K|style_desc|" + V("Parametric fence"),
        "K|style|" + V(""), "K|savedversion|" + V("2.4.2b Pro"), "K|disabled|" + V(False),
        "K|seed|" + V(123456), "K|gscale|" + V(100.0), "K|vmesh|" + V(2), "K|rmesh|" + V(0),
        "K|stylelink|" + V(None), "K|maxseg|" + V(5000),
        "K|node_tm|" + V("1,0,0,0,1,0,0,0,1,0,0,0"), "K|layer|" + V("0"),
        "K|material|" + V(("m", "Multimaterial", "_RC_fencing")), "K|material_ids|" + V("1,2"),
        "D|" + V({"name": "RC_Fence001", "handle": 700}),
        "B|1|" + "|".join(V(v) for v in ("{b16c}", "Path", 0, {"name": "Path600", "handle": 555}, 0.0, 2.54e7,
                                         True, "")),
        "B|2|" + "|".join(V(v) for v in ("{890f}", "Surface", 1, None, 0.0, 1e7, True, "")),
    ] + PARAM_ROWS + list(extra_params) + [
        "S|1|" + "|".join(V(v) for v in ("1", "rail", None, ("o", "Editable_mesh", "Editable Mesh"), None,
                                         False, True, True, True)),
        "S|2|" + "|".join(V(v) for v in ("{1566}", "post", {"name": "PostSrc", "handle": 556},
                                         ("o", "Box", "Box"), ("m", "VRayMtl", "steel"), True, True, False,
                                         True)),
        "X|" + b64(STYLE_DUP),
        "O|" + b64(OUTPUT),
        "END",
    ]
    return "\n".join(rows) + "\n"


class InspectTests(unittest.TestCase):
    def setUp(self):
        with patch.object(rc, "client", client_with(inspect_raw())) as client:
            self.out = rc.railclone_inspect(name="RC_Fence")
        self.script = scripts(client)[0]

    def test_script_reads_everything_in_one_call(self):
        for needle in ("#paid", "#paselector", "#banode", "#sobjref", "getXMLStyle", "getXMLOutput",
                       "getStyleDesc", "units.decodeValue \"1mm\"", "objectTransform"):
            self.assertIn(needle, self.script)
        for forbidden in ("theHold.Begin", "segmentsUpdate 1", "setXMLStyle"):
            self.assertNotIn(forbidden, self.script.replace(rc._FUNCTIONS, ""))
        body = self.script.replace(rc._FUNCTIONS, "")
        # rebuilt (undo off) before reading, and getStyleDesc last: it empties the output export (live)
        self.assertLess(body.index("rcRebuild base"), body.index("api.getXMLOutput()"))
        self.assertLess(body.index("api.getXMLOutput()"), body.index("getStyleDesc"))

    def test_node(self):
        node = self.out["node"]
        self.assertEqual((self.out["handle"], self.out["name"]), (602061, "RC_Fence"))
        self.assertEqual(node["style_desc"], "Parametric fence")
        self.assertEqual(node["plugin_version"], 400)
        self.assertEqual(node["material"], {"class": "Multimaterial", "name": "_RC_fencing", "ids": [1, 2]})
        self.assertEqual((node["display_mode"], node["render_mode"]), (2, 0))
        self.assertEqual(node["settings"]["seed"], 123456)
        self.assertEqual(node["shared_base_with"], ["RC_Fence001"])
        self.assertAlmostEqual(node["mm_per_unit"], 10.0)

    def test_bases(self):
        bases = self.out["bases"]
        self.assertEqual(bases[0]["node"], {"name": "Path600", "handle": 555})
        self.assertEqual((bases[0]["type_name"], bases[1]["type_name"]), ("spline", "surface"))
        self.assertIsNone(bases[1]["node"])

    def test_params_typed(self):
        p = {x["name"]: x for x in self.out["params"]}
        posts = p["Posts Distance"]
        self.assertEqual((posts["type_name"], posts["array"], posts["value"]), ("distance", "paunitval", 150.0))
        self.assertAlmostEqual(posts["value_mm"], 1500.0)
        self.assertEqual(posts["limit"], {"on": True, "min": 0.0, "max": 1e7})
        self.assertEqual(posts["used_by"], [{"node": "Pickets", "node_type": "Generator/Linear1S",
                                             "input": "Evenly Distance"}])
        sel = p["Spandrel Type"]
        self.assertEqual(sel["options"], [{"label": "Glass", "value": 1, "index": 0},
                                          {"label": "Solid ", "value": 2, "index": 1}])
        self.assertEqual((sel["value"], sel["selected"], sel["ui_index"]),
                         (2.0, {"label": "Solid ", "index": 1}, 1))
        self.assertEqual(p["--Settings--"]["type_name"], "separator")
        self.assertNotIn("value", p["--Settings--"])
        self.assertTrue(p["Repetitions"]["out_of_limits"])
        self.assertEqual((p["Mirror"]["type_name"], p["Mirror"]["value"]), ("bool", True))
        self.assertEqual((p["Z Rotation"]["type_name"], p["Z Rotation"]["value"]), ("angle", 45.0))

    def test_segments_graph_output(self):
        seg = self.out["segments"]
        self.assertTrue(seg[0]["embedded"])
        self.assertEqual(seg[0]["source_class"], "Editable_mesh")
        self.assertEqual((seg[0]["items"], seg[1]["items"]), (1, 1))
        self.assertEqual(seg[1]["source_node"], {"name": "PostSrc", "handle": 556})
        self.assertEqual(seg[1]["source_material"], "steel")
        graph = self.out["graph"]
        self.assertEqual(graph["node_count"], 5)
        self.assertEqual(graph["duplicate_attributes_removed"], 4)
        self.assertEqual(graph["by_type"]["Parameter/Numeric"], 2)
        self.assertEqual({(e["connector"], e["param_index"]) for e in graph["exposed"]},
                         {("_exp_10_0", 2), ("_exp_distance_0", 1)})
        self.assertEqual(self.out["output"], {"total": 3, "placed": 2, "placeholders": 1,
                                              "by_segment": {"post": 1, "rail": 1}, "rebuilt": True})
        codes = {w.split(":")[0] for w in self.out["warnings"]}
        self.assertIn("SHARED_BASE", codes)
        self.assertIn("PARAM_OUT_OF_LIMITS", codes)
        self.assertNotIn("NO_PATH", codes)

    def test_no_path_warning(self):
        raw = inspect_raw().replace(V({"name": "Path600", "handle": 555}), V(None))
        with patch.object(rc, "client", client_with(raw)):
            out = rc.railclone_inspect(name="RC_Fence")
        self.assertTrue(any(w.startswith("NO_PATH") for w in out["warnings"]))

    def test_malformed_report(self):
        with patch.object(rc, "client", client_with("RCINSPECT\nK|handle|i1\n")):
            with self.assertRaises(rclib.RailCloneError) as ctx:
                rc.railclone_inspect(name="RC_Fence")
        self.assertEqual(ctx.exception.code, "INVALID_READBACK")

    def test_no_graph(self):
        with patch.object(rc, "client", client_with(inspect_raw())) as client:
            rc.railclone_inspect(name="RC_Fence", include_graph=False)
        self.assertNotIn("api.getXMLStyle()", scripts(client)[0])


def setp_raw(values, before="5:%s:48" % b64("picket"), after="5:%s:24" % b64("picket")):
    def stats(label, segs):
        total, placed, rest = segs.split(":", 2)
        return f"O|{label}|{total}|{placed}|{rest}"
    rows = ["RCSETP", stats("before", "60:%s" % before), stats("after", "40:%s" % after)]
    for (stage, n, array), value in values.items():
        rows.append(f"V|{stage}|{n}|{array}|{V(value)}")
    return "\n".join(rows + ["END"]) + "\n"


def read_raw():
    return "\n".join(["RCINSPECT"] + head_rows() + PARAM_ROWS + ["END"]) + "\n"


class SetParamTests(unittest.TestCase):
    def run_set(self, params, write=None):
        results = [read_raw()] + ([write] if write else [])
        with patch.object(rc, "client", client_with(*results)) as client:
            out = rc.railclone_set_param(params, name="RC_Fence")
        return out, scripts(client)

    def refused(self, params):
        with patch.object(rc, "client", client_with(read_raw())) as client:
            with self.assertRaises(ValueError) as ctx:
                rc.railclone_set_param(params, name="RC_Fence")
        self.assertEqual(client.send_command.call_count, 1)  # read only, nothing written
        return str(ctx.exception)

    def test_distance_by_name_and_unit_string(self):
        write = setp_raw({("before", 1, "paunitval"): 10.0, ("after", 1, "paunitval"): 60.0})
        out, sent = self.run_set({"Pickets Distance": "60cm"}, write)
        self.assertEqual(len(sent), 2)
        script = sent[1]
        self.assertIn("base.paunitval[2] = 60.0", script)
        self.assertIn('if (rcH base) != "602062" do throw "STALE_PARAMS', script)
        self.assertIn(PICKETS, decoded_strings(script))
        self.assertIn("(rcAt ptype 2) != 3", script)
        self.assertIn('theHold.Accept "MCP RailClone params"', script)
        self.assertLess(script.index("theHold.Begin()"), script.index("base.paunitval[2]"))
        self.assertEqual(script.replace(rc._FUNCTIONS, "").count("rcRebuild base"), 2)
        self.assertIn("getAnimByHandle 602061", script)
        change = out["changes"][0]
        self.assertEqual((change["index"], change["array"], change["before"], change["after"]),
                         (2, "paunitval", 10.0, 60.0))
        self.assertAlmostEqual(change["value_mm"], 600.0)
        self.assertEqual(out["output"]["before"]["by_segment"], {"picket": 48})
        self.assertEqual(out["output"]["by_segment_delta"], {"picket": -24})
        self.assertEqual(out["warnings"], [])

    def test_by_id_and_selector_label(self):
        write = setp_raw({("before", 1, "pafloatval"): 2.0, ("after", 1, "pafloatval"): 1.0,
                          ("before", 1, "paintval"): 1, ("after", 1, "paintval"): 0})
        out, sent = self.run_set({"{C3}": "Glass"}, write)
        self.assertIn("base.pafloatval[3] = 1.0", sent[1])
        self.assertIn("base.paintval[3] = 0", sent[1])
        change = out["changes"][0]
        self.assertEqual((change["label"], change["option_index"], change["ui_index_after"]), ("Glass", 0, 0))

    def test_selector_value_and_trimmed_label(self):
        selector = dict(zip(rclib.PARAM_FIELDS, ["{c3}", "Spandrel Type", 5, 1, 2.0, 0.0, False, "",
                                                 "Glass|1;Solid |2", False, 0, 0, 0.0, 0.0, 0.0, 0.0, ""]))
        plan = rclib.plan_param_writes([selector], {"Spandrel Type": "Solid"}, 0.1)
        self.assertEqual(plan[0]["writes"], [("pafloatval", "2.0"), ("paintval", "1")])
        plan = rclib.plan_param_writes([selector], {"Spandrel Type": 1}, 0.1)
        self.assertEqual(plan[0]["label"], "Glass")

    def test_distance_units(self):
        posts = dict(zip(rclib.PARAM_FIELDS, ["{p}", "Height", 3, 0, 0.0, 10.0, False, "", "", False, 0, 0,
                                              0.0, 0.0, 0.0, 0.0, ""]))
        for text, scene in (("600mm", 60.0), ("1m", 100.0), ("2 in", 5.08), (12.5, 12.5)):
            plan = rclib.plan_param_writes([posts], {"Height": text}, 0.1)
            self.assertAlmostEqual(plan[0]["value"], scene)
        with self.assertRaises(ValueError):
            rclib.plan_param_writes([posts], {"Height": "6 furlongs"}, 0.1)

    def test_bool_int_angle_writes(self):
        write = setp_raw({("before", 1, "paboolval"): True, ("after", 1, "paboolval"): False,
                          ("before", 2, "paintval"): 100004, ("after", 2, "paintval"): 4,
                          ("before", 3, "pafloatval"): 45.0, ("after", 3, "pafloatval"): 90.0})
        out, sent = self.run_set({"Mirror": False, "Repetitions": 4, "Z Rotation": 90}, write)
        for line in ("base.paboolval[6] = false", "base.paintval[5] = 4", "base.pafloatval[7] = 90.0"):
            self.assertIn(line, sent[1])
        self.assertEqual([c["after"] for c in out["changes"]], [False, 4, 90.0])

    def test_refusals(self):
        self.assertIn("separator", self.refused({"--Settings--": 1}))
        self.assertIn("outside the parameter limits", self.refused({"Repetitions": 100000}))
        self.assertIn("outside the parameter limits", self.refused({"Z Rotation": 400.0}))
        self.assertIn("PARAM_NOT_FOUND", self.refused({"Height": 3}))
        self.assertIn("whole number", self.refused({"Repetitions": 2.5}))
        self.assertIn("whole number", self.refused({"Repetitions": True}))
        self.assertIn("not exactly one selector option", self.refused({"Spandrel Type": "Wood"}))
        self.assertIn("needs true or false", self.refused({"Mirror": "yes"}))
        self.assertIn("same parameter", self.refused({"Posts Distance": 1.0, POSTS: 2.0}))
        self.assertIn("outside the parameter limits", self.refused({"Posts Distance": -5}))

    def test_ambiguous_name(self):
        rows = PARAM_ROWS + [param_row(8, "{c8}", "Mirror", 4)]
        raw = "\n".join(["RCINSPECT"] + head_rows() + rows + ["END"]) + "\n"
        with patch.object(rc, "client", client_with(raw)):
            with self.assertRaises(ValueError) as ctx:
                rc.railclone_set_param({"Mirror": True}, name="RC_Fence")
        self.assertIn("AMBIGUOUS_PARAM", str(ctx.exception))

    def test_empty_params_refused_before_sending(self):
        with patch.object(rc, "client", client_with()) as client:
            with self.assertRaises(ValueError):
                rc.railclone_set_param({}, name="RC_Fence")
        client.send_command.assert_not_called()

    def test_stale_error_code(self):
        stale = "RCERROR|-- Runtime error: STALE_PARAMS: parameter 2 changed since it was read; read again"
        with patch.object(rc, "client", client_with(read_raw(), stale)):
            with self.assertRaises(rclib.RailCloneError) as ctx:
                rc.railclone_set_param({"Pickets Distance": 20}, name="RC_Fence")
        self.assertEqual(ctx.exception.code, "STALE_PARAMS")
        self.assertTrue(ctx.exception.retryable)


INDEX_FENCES = """<?xml version="1.0" encoding="utf-8"?>
<library version="1" masterScale="0.393700787402">
  <select class="material" setmacro="RENDERER"><option>@RC_getRenderer()</option></select>
  <item class="maxobject">
    <uuid>{1111}</uuid>
    <name lang="english">Metal Fence 1</name>
    <family>Fences</family>
    <description>Parametric metal fence</description>
    <bitmap>thumbs\\metal1.jpg</bitmap>
    <maxfile>objects\\fencing_metal1.max</maxfile>
    <object>_RC_fencing_allum_1</object>
    <matlib>$(LIBROOTPATH)\\..\\..\\matlib\\$(RENDERER)_v2.mat</matlib>
    <material>_RC_fencing</material>
    <available>LITE</available>
  </item>
  <item class="maxobject">
    <uuid>{2222}</uuid>
    <name lang="english">Metal Fence 2</name>
    <name lang="latin">Saepes</name>
    <maxfile>objects\\fencing_metal2.max</maxfile>
    <object>_RC_fencing_allum_2</object>
    <matlib>matlibs\\fence_$(SELECTEDRENDERER).pmat</matlib>
    <material>_RC_fencing</material>
  </item>
</library>
"""

INDEX_BASEBOARDS = """<?xml version="1.0" encoding="utf-8"?>
<library version="1">
  <maxscript file="iTooLibraryFunctions.ms" script='addMapDirectory @"$(LIBFULLPATH)\\maps";' execute_on="item_select"/>
  <item class="maxobject">
    <uuid>{3333}</uuid>
    <name>Baseboard 002</name>
    <maxfile>objects\\baseboard_002.rail</maxfile>
    <object>rc_baseboard_002</object>
    <matlib>matlibs\\rc_skirting_$(SELECTEDRENDERER)_v001.pmat</matlib>
    <material>rc_skirting_collection_mtl</material>
  </item>
</library>
"""


class LibraryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rclib_")
        self.root = os.path.join(self.tmp, "lib", "default1")
        fences = os.path.join(self.root, "Architecture", "Exterior", "Fences", "Metal")
        boards = os.path.join(self.root, "Architecture", "Interior", "Baseboards")
        for folder in (os.path.join(fences, "objects"), os.path.join(boards, "objects"),
                       os.path.join(boards, "maps")):
            os.makedirs(folder)
        Path(fences, "index.xml").write_text(INDEX_FENCES, encoding="utf-8")
        Path(boards, "index.xml").write_text(INDEX_BASEBOARDS, encoding="utf-8")
        Path(fences, "objects", "fencing_metal1.max").write_bytes(b"\xd0\xcf\x11\xe0")
        Path(fences, "objects", "fencing_metal2.max").write_bytes(b"\xd0\xcf\x11\xe0")
        Path(boards, "objects", "baseboard_002.rail").write_bytes(b"\x00\x00\x01\xff")
        self.fences, self.boards = fences, boards
        self.roots = [{"name": "Railclone Library", "path": self.root},
                      {"name": "CRGR", "path": os.path.join(self.tmp, "offline_share")}]
        self.patch = patch.object(rclib, "library_roots", return_value=(list(self.roots), []))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class LibraryTests(LibraryFixture):
    def test_exact_name_ranks_first(self):
        from maxmcp.helpers import railclone_lib as rclib
        items = [{"name": n, "route": "\L\\" + n, "category": "Fences\Metal", "names": [n]}
                 for n in ("2 Red Brick Wall Fence 1 1m", "Metal Fence 10", "Metal Fence 1", "Old Metal Fence 1x")]
        ranked = [it["name"] for it in rclib.match_items(items, "metal fence 1")]
        self.assertEqual(ranked[:3], ["Metal Fence 1", "Metal Fence 10", "Old Metal Fence 1x"])
        self.assertEqual(ranked[-1], "2 Red Brick Wall Fence 1 1m")

    def test_search_and_routes(self):
        out = rc.railclone_library(query="metal fence")
        self.assertEqual(out["total"], 2)
        first = out["items"][0]
        self.assertEqual(first["route"], "\\Railclone Library\\Architecture\\Exterior\\Fences\\Metal\\Metal Fence 1")
        self.assertEqual(first["category"], "Architecture\\Exterior\\Fences\\Metal")
        self.assertEqual(first["maxfile"], os.path.join(self.fences, "objects", "fencing_metal1.max"))
        self.assertTrue(first["maxfile_exists"])
        self.assertEqual(first["matlib"], os.path.normpath(os.path.join(self.root, "..", "..", "matlib",
                                                                        "$(RENDERER)_v2.mat")))
        self.assertEqual(first["matlib_macros"], ["RENDERER"])
        self.assertEqual(first["thumbnail"], os.path.join(self.fences, "thumbs", "metal1.jpg"))
        self.assertEqual((first["object"], first["material"], first["uuid"]),
                         ("_RC_fencing_allum_1", "_RC_fencing", "{1111}"))
        self.assertEqual(first["load_method"], "merge")
        self.assertEqual(out["items"][1]["load_method"], "route")  # .pmat matlib
        self.assertEqual(out["items"][1]["names"], ["Metal Fence 2", "Saepes"])
        self.assertNotIn("category_dir", first)
        self.assertTrue(any(w.startswith("ROOT_UNREACHABLE: 'CRGR'") for w in out["warnings"]))
        self.assertEqual([r["reachable"] for r in out["roots"]], [True, False])

    def test_filters_and_limit(self):
        out = rc.railclone_library(category="interior/baseboards")
        self.assertEqual([i["name"] for i in out["items"]], ["Baseboard 002"])
        self.assertEqual(out["items"][0]["load_method"], "route")
        self.assertTrue(out["items"][0]["library_scripts"])
        out = rc.railclone_library(limit=1)
        self.assertEqual((out["total"], len(out["items"]), out["truncated"]), (3, 1, True))
        self.assertEqual(rc.railclone_library(root="railclone library")["total"], 3)
        with self.assertRaises(ValueError):
            rc.railclone_library(root="Nope")
        with self.assertRaises(ValueError):
            rc.railclone_library(limit=0)

    def test_scan_cap_marks_incomplete(self):
        scan = rclib.scan_library(self.roots[:1], deadline=rclib.time.monotonic() + 10, cap=1)
        self.assertFalse(scan["complete"])
        self.assertTrue(any(w.startswith("SCAN_INCOMPLETE") for w in scan["warnings"]))

    def test_bad_index_is_skipped(self):
        Path(self.boards, "index.xml").write_text("<!DOCTYPE x><library/>", encoding="utf-8")
        out = rc.railclone_library()
        self.assertEqual(out["total"], 2)
        self.assertTrue(any(w.startswith("INDEX_UNREADABLE") for w in out["warnings"]))


def lib_raw(*, placed=48, extra=()):
    rows = ["RCLIB", "K|handle|" + V(900), "K|name|" + V("RC_Metal_Fence_1"),
            "K|style|" + V(""), "K|style_desc|" + V("Parametric metal fence"),
            "K|material|" + V(("m", "Multimaterial", "_RC_fencing")), "K|renderer|" + V("vray"),
            "K|matlib|" + V("C:\\matlib\\vray_v2.mat"), "K|fixed_maps|" + V(0),
            "B|1|" + "|".join(V(v) for v in ("{b16c}", "Path", 0, {"name": "Path600", "handle": 555})),
            "B|2|" + "|".join(V(v) for v in ("{890f}", "Surface", 1, None)),
            "O|after|%d|%d|%s:%d" % (placed + 3, placed, b64("allum_picket_01"), placed)] + list(extra)
    return "\n".join(rows + ["END"]) + "\n"


class FromLibraryTests(LibraryFixture):
    ROUTE1 = "\\Railclone Library\\Architecture\\Exterior\\Fences\\Metal\\Metal Fence 1"
    ROUTE_RAIL = "\\Railclone Library\\Architecture\\Interior\\Baseboards\\Baseboard 002"

    def call(self, raw, **kw):
        kw.setdefault("allow_undo_flush", True)
        with patch.object(rc, "client", client_with(raw)) as client:
            out = rc.railclone_from_library(**kw)
        return out, scripts(client)[0]

    def test_max_item_needs_undo_flush_consent(self):
        exc = self.refused(ValueError, item="Metal Fence 1", splines=["Path600"])
        self.assertIn("clears 3ds Max's undo history", str(exc))
        self.assertIn("allow_undo_flush=true", str(exc))
        out, script = self.call(lib_raw(), item="Metal Fence 1", splines=["Path600"])
        self.assertTrue(out["undo_history_cleared"])
        # the material library is loaded before the undo step (inside, it wipes the step)
        self.assertLess(script.index("loadTempMaterialLibrary matPath"), script.index("theHold.Begin()"))
        self.assertLess(script.index("mergeMAXFile maxFile"), script.index("theHold.Begin()"))
        self.assertIn("o = copy mergedRc", script)
        self.assertIn("if mergedRc != undefined do with undo off (delete mergedRc)", script)

    def refused(self, exc_type, **kw):
        with patch.object(rc, "client", client_with()) as client:
            with self.assertRaises(exc_type) as ctx:
                rc.railclone_from_library(**kw)
        client.send_command.assert_not_called()
        return ctx.exception

    def test_merge_by_name(self):
        out, script = self.call(lib_raw(), item="Metal Fence 1", splines=["Path600"], name="Fence A")
        self.assertIn('local methodName = "merge"', script)
        self.assertIn("mergeMAXFile maxFile #(objName) #autoRenameDups #useMergedMtlDups #neverReparent quiet:true",
                      script)
        strings = decoded_strings(script)
        self.assertIn(os.path.join(self.fences, "objects", "fencing_metal1.max"), strings)
        self.assertIn("_RC_fencing_allum_1", strings)
        self.assertIn("Path600", strings)
        self.assertIn("Fence A", strings)
        matlib = [s for s in strings if s.endswith(".mat")]
        self.assertEqual(matlib, [os.path.normpath(os.path.join(self.root, "..", "..", "matlib",
                                                                "$(RENDERER)_v2.mat"))])
        self.assertFalse(any(s.endswith(".pmat") for s in strings))
        self.assertIn("loadTempMaterialLibrary matPath", script)
        self.assertIn('theHold.Accept "MCP RailClone from library"', script)
        self.assertIn("theHold.Cancel()", script)
        self.assertIn("rcRebuild rcBase", script)
        self.assertEqual((out["method"], out["route"], out["handle"]), ("merge", self.ROUTE1, 900))
        self.assertTrue(out["verified"])
        self.assertEqual(out["output"]["placed"], 48)
        self.assertEqual(out["bases"][0]["node"], {"name": "Path600", "handle": 555})
        self.assertEqual(out["material"], {"class": "Multimaterial", "name": "_RC_fencing"})

    def test_route_for_rail_item(self):
        raw = lib_raw(extra=["M|" + b64("RC_wood_001.jpg"), "W|" + b64("NO_MATERIAL: test")])
        raw = raw.replace("K|style|" + V(""), "K|style|" + V(self.ROUTE_RAIL))
        out, script = self.call(raw, item=self.ROUTE_RAIL, splines=["Path600"], allow_undo_flush=False)
        self.assertIn('local methodName = "route"', script)
        self.assertFalse(out["undo_history_cleared"])  # .rail items keep the undo history
        strings = decoded_strings(script)
        self.assertIn(self.ROUTE_RAIL, strings)
        self.assertIn(os.path.join(self.boards, "maps") + "\\", strings)
        self.assertIn("RC_Baseboard_002", strings)
        self.assertNotIn(".pmat", " ".join(strings))
        sel = script.index("select o")
        load = script.index("api.loadLibraryItemByPath routeArg")
        restore = script.index("rcRestoreSel prevSel", load)
        guard = script.index("if not (selection.count == 1 and selection[1] == o) do throw \"SELECT_FAILED")
        self.assertLess(sel, guard)
        self.assertLess(guard, load)
        self.assertLess(load, restore)
        self.assertIn('append warnings ("REBUILD_FAILED: "', script)
        self.assertIn("if code != 0 do throw", script)
        self.assertEqual(out["method"], "route")
        self.assertEqual(out["maps"]["missing"], ["RC_wood_001.jpg"])
        codes = {w.split(":")[0] for w in out["warnings"]}
        self.assertTrue({"MISSING_MAPS", "NO_MATERIAL"} <= codes)
        self.assertNotIn("STYLE_ROUTE_MISMATCH", codes)

    def test_auto_uses_route_for_pmat(self):
        out, script = self.call(lib_raw(), item="Saepes", splines=[])
        self.assertIn('local methodName = "route"', script)
        self.assertTrue(any(w.startswith("NO_SPLINES") for w in out["warnings"]))

    def test_fix_maps_off(self):
        _, script = self.call(lib_raw(), item=self.ROUTE_RAIL, fix_maps=False)
        self.assertNotIn(os.path.join(self.boards, "maps") + "\\", decoded_strings(script))

    def test_route_validation(self):
        bad_case = self.ROUTE1.replace("Railclone Library", "RailClone Library")
        exc = self.refused(rclib.RailCloneError, item=bad_case)
        self.assertEqual(exc.code, "ROUTE_INVALID")
        self.assertIn("'Railclone Library'", str(exc))
        exc = self.refused(rclib.RailCloneError, item=self.ROUTE1.replace("Metal\\", "metal\\"))
        self.assertIn("on disk it is 'Metal'", str(exc))
        exc = self.refused(rclib.RailCloneError, item=self.ROUTE1.replace("\\", "/"))
        self.assertEqual(exc.code, "ROUTE_INVALID")
        exc = self.refused(rclib.RailCloneError, item=self.ROUTE1 + "0")
        self.assertEqual(exc.code, "ROUTE_INVALID")
        exc = self.refused(rclib.RailCloneError, item=self.ROUTE1[1:])
        self.assertIn("starts with a backslash", str(exc))
        exc = self.refused(rclib.RailCloneError, item="Metal Fence 9")
        self.assertEqual(exc.code, "ITEM_NOT_FOUND")
        self.assertIn(self.ROUTE1, exc.details["similar"])

    def test_param_refusals(self):
        self.refused(ValueError, item=self.ROUTE_RAIL, method="merge")
        self.refused(ValueError, item=self.ROUTE1, method="copy")
        self.refused(ValueError, item=self.ROUTE1, splines=["A", "A"])
        self.refused(ValueError, item=self.ROUTE1, splines="Path600")
        self.refused(ValueError, item="")
        os.remove(os.path.join(self.fences, "objects", "fencing_metal1.max"))
        exc = self.refused(rclib.RailCloneError, item=self.ROUTE1)
        self.assertEqual(exc.code, "FILE_MISSING")

    def test_empty_output_not_verified(self):
        out, _ = self.call(lib_raw(placed=0), item=self.ROUTE1, splines=["Path600"])
        self.assertFalse(out["verified"])
        self.assertTrue(any(w.startswith("EMPTY_OUTPUT") for w in out["warnings"]))

    def test_load_failure_code(self):
        raw = "RCERROR|-- Runtime error: LOAD_FAILED: loadLibraryItemByPath returned 1 for x"
        with patch.object(rc, "client", client_with(raw)):
            with self.assertRaises(rclib.RailCloneError) as ctx:
                rc.railclone_from_library(item=self.ROUTE_RAIL)
        self.assertEqual(ctx.exception.code, "LOAD_FAILED")


class DecodeTests(unittest.TestCase):
    def test_values(self):
        self.assertIsNone(rc._dv("u"))
        self.assertIs(rc._dv("b1"), True)
        self.assertEqual(rc._dv("i-3"), -3)
        self.assertEqual(rc._dv("f1e+07"), 1e7)
        self.assertIsNone(rc._dv("finf"))
        self.assertIsNone(rc._dv("f-nan(ind)"))
        self.assertEqual(rc._dv("s" + b64("Ä|b")), "Ä|b")
        self.assertEqual(rc._dv("n12:" + b64("Box")), {"name": "Box", "handle": 12})
        self.assertEqual(rc._dv("p1,2,3"), [1.0, 2.0, 3.0])

    def test_selector_parse(self):
        self.assertEqual(rclib.parse_selector("A|1;B b|2.5;"),
                         [{"label": "A", "value": 1, "index": 0}, {"label": "B b", "value": 2.5, "index": 1}])
        self.assertEqual(rclib.parse_selector(""), [])

    def test_placeholder_rules(self):
        self.assertTrue(rclib.is_placeholder({"box": "[0,0,0][1,1,1]"}))
        self.assertTrue(rclib.is_placeholder({"source_segment": "a", "box": "[2,0,0][1,1,1]"}))
        self.assertFalse(rclib.is_placeholder({"source_segment": "a", "box": "[0,0,0][1,1,1]"}))


class RegistrationTests(unittest.TestCase):
    def test_registration(self):
        from maxmcp import server
        from maxmcp.tool_discovery import TOOLSET_SPECS
        from scripts.gen_tool_smoke import MUTATE_TOOLS, SKIP_DEFAULT
        new = {"railclone_inspect", "railclone_set_param", "railclone_library", "railclone_from_library"}
        self.assertIn("railclone", server.SPECIALTY_TOOL_MODULES)
        self.assertEqual(sum("railclone" in s.modules for s in TOOLSET_SPECS), 1)
        self.assertTrue({"railclone_inspect", "railclone_library"} <= server._READ_ONLY_TOOLS)
        self.assertTrue({"railclone_inspect", "railclone_library"} <= server._IDEMPOTENT_TOOLS)
        self.assertIn("railclone_set_param", server._DESTRUCTIVE_TOOLS)
        self.assertFalse({"railclone_set_param", "railclone_from_library"} & server._READ_ONLY_TOOLS)
        self.assertEqual(server._tool_annotations("railclone_inspect"), {"readOnlyHint": True, "idempotentHint": True})
        self.assertNotIn("readOnlyHint", server._tool_annotations("railclone_from_library"))
        self.assertTrue(new <= SKIP_DEFAULT)
        self.assertTrue({"railclone_set_param", "railclone_from_library"} <= MUTATE_TOOLS)
        self.assertNotIn("7.3.5", server.max_assistant())

    def test_tools_are_decorated(self):
        from maxmcp.tool_discovery import _module_tool_names
        names = set(_module_tool_names(Path(rc.__file__)))
        self.assertTrue({"get_railclone_style", "set_railclone_style", "get_railclone_output", "railclone_inspect",
                         "railclone_set_param", "railclone_library", "railclone_from_library"} <= names)

    def test_docstring_first_paragraphs_are_short(self):
        for fn in (rc.get_railclone_style, rc.set_railclone_style, rc.get_railclone_output, rc.railclone_inspect,
                   rc.railclone_set_param, rc.railclone_library, rc.railclone_from_library):
            first = re.sub(r"\s+", " ", fn.__doc__.split("\n\n", 1)[0]).strip()
            self.assertLess(len(first), 300, fn.__name__)


if __name__ == "__main__":
    unittest.main()
