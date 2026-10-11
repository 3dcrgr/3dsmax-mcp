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
    '\t\t<connector id="spline" name="Spline" target_id="{B16C}" target_name="Path"/>\n'
    '\t\t<connector id="base_surface" name="Surface" target_id="{890f}" target_name="Surface"/>\n'
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


def out_raw(xml=OUTPUT, tm="1,0,0,0,1,0,0,0,1,100,-200,0", shared="", rebuilt="1", auto_off="0", proxy="0"):
    return "RCOUT|602061|%s|%s|%s|%s|%s|%s|%s" % (b64("RC_Fence"), tm, shared, rebuilt, auto_off, proxy, b64(xml))


class OutputTests(unittest.TestCase):
    def test_rebuilds_and_flags_placeholders(self):
        with patch.object(rc, "client", client_with(out_raw())) as client:
            out = rc.get_railclone_output(name="RC_Fence", limit=2)
        script = scripts(client)[0]
        # Live (RC 7.3.5): seed = seed rebuilds without touching the segments; segmentsUpdate renames linked
        # segments to their source node, so a read never runs it unless reload_segments asks for it
        self.assertIn("rebuilt = false or not autoOff\n"
                      "xml = if rebuilt then (rcRebuildOutput base false) else (api.getXMLOutput())", script)
        self.assertLess(script.index("local autoOff ="), script.index("rebuilt = false or not autoOff"))
        self.assertNotIn("segmentsUpdate", script.replace(rc._FUNCTIONS, ""))
        self.assertIn("if reloadSegs then (", rc._FUNCTIONS)
        self.assertIn(") else (base.seed = base.seed)", rc._FUNCTIONS)
        code = "\n".join(line for line in script.splitlines() if not line.strip().startswith("--"))
        self.assertNotIn("notifyDependents", code)
        self.assertEqual((out["segments_reloaded"], out["auto_update"]), (False, True))
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
        body = scripts(client)[0].replace(rc._FUNCTIONS, "")
        self.assertNotIn("rcRebuildOutput", body)
        self.assertIn("xml = api.getXMLOutput()", body)
        self.assertFalse(out["rebuilt"])
        self.assertEqual(out["shared_base_with"], ["RC_Fence001"])
        self.assertTrue(any(w.startswith("SHARED_BASE") for w in out["warnings"]))
        self.assertEqual(out["xml"], OUTPUT)

    def test_reload_segments_is_opt_in(self):
        with patch.object(rc, "client", client_with(out_raw())) as client:
            out = rc.get_railclone_output(name="RC_Fence", reload_segments=True)
        self.assertIn("(rcRebuildOutput base true)", scripts(client)[0])
        self.assertTrue(out["segments_reloaded"])
        self.assertTrue(any(w.startswith("SEGMENTS_RELOADED") and "renamed every linked segment" in w
                            for w in out["warnings"]))
        with patch.object(rc, "client", client_with()) as client:
            with self.assertRaises(ValueError):
                rc.get_railclone_output(name="RC_Fence", rebuild=False, reload_segments=True)
            with self.assertRaises(ValueError):
                rc.get_railclone_output(name="RC_Fence", reload_segments="yes")
            with self.assertRaises(ValueError):
                rc.get_railclone_output(name="RC_Fence", rebuild=False, force_rebuild=True)
            with self.assertRaises(ValueError):
                rc.get_railclone_output(name="RC_Fence", force_rebuild=1)
        client.send_command.assert_not_called()

    def test_auto_update_off(self):
        # Live: with autoUpdate off no scripted call rebuilds; a forced read pulses it on around the read
        self.assertIn("if wasOff do base.autoUpdate = true", rc._FUNCTIONS)
        functions = rc._FUNCTIONS
        pulse = functions.index("if wasOff do base.autoUpdate = true")
        self.assertLess(pulse, functions.index("xml = api.getXMLOutput()"))
        self.assertLess(functions.index("xml = api.getXMLOutput()"),
                        functions.index("if wasOff do base.autoUpdate = false", pulse))
        # By default a manual-mode RailClone is left alone: the pulse would replace the geometry the user froze
        with patch.object(rc, "client", client_with(out_raw(auto_off="1", rebuilt="0"))) as client:
            out = rc.get_railclone_output(name="RC_Fence")
        self.assertIn("rebuilt = false or not autoOff", scripts(client)[0])
        self.assertEqual((out["auto_update"], out["rebuilt"]), (False, False))
        kept = [w for w in out["warnings"] if w.startswith("AUTO_UPDATE_OFF")]
        self.assertEqual(len(kept), 1)
        self.assertIn("left alone", kept[0])
        self.assertIn("force_rebuild=true", kept[0])
        # force_rebuild=true does the pulse and says the new geometry is kept
        with patch.object(rc, "client", client_with(out_raw(auto_off="1"))) as client:
            out = rc.get_railclone_output(name="RC_Fence", force_rebuild=True)
        self.assertIn("rebuilt = true or not autoOff", scripts(client)[0])
        self.assertTrue(out["rebuilt"])
        forced = [w for w in out["warnings"] if w.startswith("AUTO_UPDATE_OFF")]
        self.assertEqual(forced, [rc._AUTO_UPDATE_NOTE])
        # reload_segments on a manual-mode RailClone without force: segmentsUpdate isn't run (it is a rebuild)
        with patch.object(rc, "client", client_with(out_raw(auto_off="1", rebuilt="0"))):
            out = rc.get_railclone_output(name="RC_Fence", reload_segments=True)
        self.assertFalse(out["segments_reloaded"])
        self.assertTrue(any(w.startswith("RELOAD_SKIPPED") for w in out["warnings"]))
        self.assertFalse(any(w.startswith("SEGMENTS_RELOADED") for w in out["warnings"]))
        with patch.object(rc, "client", client_with(out_raw(auto_off="1", rebuilt="0"))):
            out = rc.get_railclone_output(name="RC_Fence", rebuild=False)
        self.assertTrue(any(w.startswith("NOT_REBUILT") and "autoUpdate is off" in w for w in out["warnings"]))

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


def inspect_raw(extra_params=(), rebuilt=True):
    rows = ["RCINSPECT"] + head_rows() + [
        "K|api_version|" + V(400), "K|rc_runtime|" + V(735), "K|createdversion|" + V(0), "K|proxymode|" + V(0),
        "K|rebuilt|" + V(rebuilt)] + (
        ["K|style_desc|" + V("Parametric fence")] if rebuilt else []) + [
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
        for forbidden in ("theHold.Begin", "segmentsUpdate", "setXMLStyle"):
            self.assertNotIn(forbidden, self.script.replace(rc._FUNCTIONS, ""))
        body = self.script.replace(rc._FUNCTIONS, "")
        # getStyleDesc empties the output export until the next rebuild (live): read it only when this call
        # rebuilds, then rebuild and read the output (seed = seed: the segments are never reloaded by a read)
        self.assertIn("local rcBuild = true and (false or not ((isProperty base #autoUpdate) and "
                      "base.autoUpdate == false))", body)
        self.assertIn('if rcBuild do rcRow ss #("K", "style_desc", rcV (try (api.getStyleDesc())', body)
        self.assertIn("local ox = if rcBuild then (rcRebuildOutput base false) else (api.getXMLOutput())", body)
        self.assertLess(body.index("local rcBuild"), body.index("getStyleDesc"))
        self.assertLess(body.index("getStyleDesc"), body.index("(rcRebuildOutput base false)"))

    def test_segments_are_read_after_the_rebuild(self):
        # Live: segmentsUpdate (reload_segments) renames each linked segment and the output's source_segment to
        # the source node's name. Read before the rebuild, sname kept the old name and items came out 0.
        body = self.script.replace(rc._FUNCTIONS, "")
        self.assertLess(body.index("(rcRebuildOutput base false)"), body.index('local row = #("S", '))
        self.assertLess(body.index("#sname"), body.index('format "END'))
        renamed = inspect_raw().replace(V("post"), V("P23_srcBox2")).replace(
            b64(OUTPUT), b64(OUTPUT.replace('source_segment="post"', 'source_segment="P23_srcBox2"')))
        with patch.object(rc, "client", client_with(renamed)) as client:
            out = rc.railclone_inspect(name="RC_Fence", reload_segments=True)
        body = scripts(client)[0].replace(rc._FUNCTIONS, "")
        self.assertLess(body.index("(rcRebuildOutput base true)"), body.index('local row = #("S", '))
        self.assertEqual([(s["name"], s["items"]) for s in out["segments"]], [("rail", 1), ("P23_srcBox2", 1)])

    def test_reload_segments(self):
        with patch.object(rc, "client", client_with(inspect_raw())) as client:
            out = rc.railclone_inspect(name="RC_Fence", reload_segments=True)
        self.assertIn("local ox = if rcBuild then (rcRebuildOutput base true)", scripts(client)[0])
        self.assertTrue(out["output"]["segments_reloaded"])
        self.assertFalse(self.out["output"]["segments_reloaded"])
        self.assertTrue(any(w.startswith("SEGMENTS_RELOADED") for w in out["warnings"]))
        self.assertFalse(any(w.startswith("SEGMENTS_RELOADED") for w in self.out["warnings"]))
        with self.assertRaises(ValueError):
            rc.railclone_inspect(name="RC_Fence", reload_segments=1)

    def test_auto_update_off_is_left_alone(self):
        # A manual-mode RailClone isn't rebuilt by a read: the last build is returned, style_desc left out
        raw = inspect_raw(rebuilt=False).replace("K|seed|", "K|autoUpdate|b0\nK|seed|")
        with patch.object(rc, "client", client_with(raw)):
            out = rc.railclone_inspect(name="RC_Fence")
        self.assertIs(out["node"]["settings"]["autoUpdate"], False)
        self.assertIsNone(out["node"]["style_desc"])
        self.assertFalse(out["output"]["rebuilt"])
        kept = [w for w in out["warnings"] if w.startswith("AUTO_UPDATE_OFF")]
        self.assertEqual(len(kept), 1)
        self.assertIn("left alone", kept[0])
        self.assertIn("style_desc is left out", kept[0])
        self.assertFalse(any(w.startswith("AUTO_UPDATE_OFF") for w in self.out["warnings"]))
        with patch.object(rc, "client", client_with(raw)):
            out = rc.railclone_inspect(name="RC_Fence", reload_segments=True)
        self.assertFalse(out["output"]["segments_reloaded"])
        self.assertTrue(any(w.startswith("RELOAD_SKIPPED") for w in out["warnings"]))
        # force_rebuild=true pulses autoUpdate and says the new geometry is kept
        forced = inspect_raw().replace("K|seed|", "K|autoUpdate|b0\nK|seed|")
        with patch.object(rc, "client", client_with(forced)) as client:
            out = rc.railclone_inspect(name="RC_Fence", force_rebuild=True)
        self.assertIn("local rcBuild = true and (true or not", scripts(client)[0])
        self.assertTrue(out["output"]["rebuilt"])
        self.assertIn(rc._AUTO_UPDATE_NOTE, out["warnings"])

    def test_rebuild_false(self):
        with patch.object(rc, "client", client_with(inspect_raw(rebuilt=False))) as client:
            out = rc.railclone_inspect(name="RC_Fence", rebuild=False)
        self.assertIn("local rcBuild = false and (false or not", scripts(client)[0])
        self.assertFalse(out["output"]["rebuilt"])
        self.assertTrue(any(w.startswith("NOT_REBUILT") for w in out["warnings"]))
        with patch.object(rc, "client", client_with()) as client:
            for kw in ({"rebuild": False, "reload_segments": True}, {"rebuild": False, "force_rebuild": True},
                       {"rebuild": "no"}, {"force_rebuild": 1}):
                with self.subTest(kw=kw), self.assertRaises(ValueError):
                    rc.railclone_inspect(name="RC_Fence", **kw)
        client.send_command.assert_not_called()

    def test_node(self):
        node = self.out["node"]
        self.assertEqual((self.out["handle"], self.out["name"]), (602061, "RC_Fence"))
        self.assertEqual(node["style_desc"], "Parametric fence")
        # RailClone_Pro.version() is an API number (400 on 7.1.1 and 7.3.5 alike), relabelled from plugin_version
        self.assertEqual(node["api_version"], 400)
        self.assertNotIn("plugin_version", node)
        self.assertEqual((node["rc_version"], node["rc_version_int"]), ("7.3.5", 735))
        self.assertEqual((node["createdversion"], node["savedversion"], node["proxymode"]), (0, "2.4.2b Pro", 0))
        self.assertEqual(node["material"], {"class": "Multimaterial", "name": "_RC_fencing", "ids": [1, 2]})
        self.assertEqual((node["display_mode"], node["render_mode"]), (2, 0))
        self.assertEqual(node["settings"]["seed"], 123456)
        self.assertEqual(node["shared_base_with"], ["RC_Fence001"])
        self.assertAlmostEqual(node["mm_per_unit"], 10.0)

    def test_bases(self):
        bases = self.out["bases"]
        self.assertEqual(bases[0]["node"], {"name": "Path600", "handle": 555})
        self.assertEqual((bases[0]["type_name"], bases[1]["type_name"]), ("spline", "surface"))
        self.assertEqual((bases[0]["role"], bases[1]["role"]), ("path", "surface"))  # from the graph
        self.assertIsNone(bases[1]["node"])
        no_style = inspect_raw().replace("X|" + b64(STYLE_DUP) + "\n", "")
        with patch.object(rc, "client", client_with(no_style)):
            out = rc.railclone_inspect(name="RC_Fence", include_graph=False)
        self.assertEqual([b["role"] for b in out["bases"]], [None, None])

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
        # patype 1 is a plain float (live survey: angles in stock styles, any float in macro parameters)
        self.assertEqual((p["Z Rotation"]["type_name"], p["Z Rotation"]["value"]), ("float", 45.0))

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
                                              "by_segment": {"post": 1, "rail": 1}, "rebuilt": True,
                                              "segments_reloaded": False, "proxy_cache": False})
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
        self.assertNotIn("api.getXMLStyle()", scripts(client)[0].replace(rc._FUNCTIONS, ""))


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
        # the read doesn't call getStyleDesc: it would empty the output export until a rebuild (live)
        self.assertNotIn("getStyleDesc", sent[0])
        script = sent[1]
        self.assertIn("base.paunitval[2] = 60.0", script)
        self.assertIn('if (rcH base) != "602062" do throw "STALE_PARAMS', script)
        self.assertIn(PICKETS, decoded_strings(script))
        self.assertIn("(rcAt ptype 2) != 3", script)
        self.assertIn('theHold.Accept "MCP RailClone params"', script)
        self.assertLess(script.index("theHold.Begin()"), script.index("base.paunitval[2]"))
        body = script.replace(rc._FUNCTIONS, "")
        self.assertEqual(body.count("rcRebuildOutput base false"), 2)  # before and after, never segmentsUpdate
        self.assertNotIn("segmentsUpdate", body)
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

    def test_string_parameters_are_read_only(self):
        # patype 6 is String (pastrval) per the docs; no stock style uses it, so it is read but never written
        rows = PARAM_ROWS + ["|".join(["P", "8"] + [V(v) for v in (
            "{c8}", "Label Text", 6, 0, 0.0, 0.0, False, "Hello", "", False, 0, 0, 0.0, 0.0, 0.0, 0.0, "")])]
        raw = "\n".join(["RCINSPECT"] + head_rows() + rows + ["END"]) + "\n"
        with patch.object(rc, "client", client_with(raw)) as client:
            with self.assertRaises(ValueError) as ctx:
                rc.railclone_set_param({"Label Text": "x"}, name="RC_Fence")
        self.assertEqual(client.send_command.call_count, 1)
        self.assertIn("string parameter (type 6)", str(ctx.exception))
        view = rclib.param_view(dict(zip(rclib.PARAM_FIELDS, ["{c8}", "Label Text", 6, 0, 0.0, 0.0, False, "Hello",
                                                              "", False, 0, 0, 0.0, 0.0, 0.0, 0.0, ""])), 8, 0.1)
        self.assertEqual((view["type_name"], view["array"], view["value"], view["read_only"]),
                         ("string", "pastrval", "Hello", True))
        self.assertNotIn("raw", view)
        self.assertNotIn("pastrval", rclib.WRITABLE_ARRAYS)

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

    def test_dialog_blocking_the_read_is_retryable_and_sets_nothing(self):
        import json
        from maxmcp.helpers.precheck import PrecheckBlocked
        from maxmcp.max_client import DialogBlocked
        client = MagicMock()
        client.send_command.side_effect = DialogBlocked("r1", "maxscript", [{"title": "Missing External Files"}])
        with patch.object(rc, "client", client):
            with self.assertRaises(PrecheckBlocked) as ctx:
                rc.railclone_set_param({"Pickets Distance": 20}, name="RC_Fence")
        self.assertEqual(client.send_command.call_count, 1)  # never the write
        payload = json.loads(ctx.exception.bridge_message)
        self.assertEqual((payload["code"], payload["retryable"]), ("BLOCKED_BY_DIALOG", True))
        self.assertIn("No parameter was set", payload["message"])
        self.assertIn("Then repeat railclone_set_param", payload["hint"])
        self.assertFalse(payload["details"]["changed"])

    def test_dialog_blocking_the_write_is_not_retryable(self):
        from maxmcp.max_client import DialogBlocked
        client = MagicMock()
        client.send_command.side_effect = [{"result": read_raw()},
                                           DialogBlocked("r2", "maxscript", [{"title": "RailClone"}])]
        with patch.object(rc, "client", client):
            with self.assertRaises(DialogBlocked) as ctx:  # the write may run: "do not repeat"
                rc.railclone_set_param({"Pickets Distance": 20}, name="RC_Fence")
        self.assertFalse(ctx.exception.retryable)

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
    <bitmap>metal1.jpg</bitmap>
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
        items = [{"name": n, "route": "\\L\\" + n, "category": "Fences\\Metal", "names": [n]}
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
        # live: a bare <bitmap> file name is in the index folder's thumbnails\ (683 of 683 stock thumbnails)
        self.assertEqual(first["thumbnail"], os.path.join(self.fences, "thumbnails", "metal1.jpg"))
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

    def test_leading_backslash_paths_are_index_relative(self):
        # The docs' template writes "\objects\x.max"; live, index.xml paths are relative to the index folder
        # (686 of 687 stock maxfiles), and a naive join put such a path on the drive or share root
        folder = os.path.join(self.root, "Lead")
        os.makedirs(os.path.join(folder, "thumbnails"))
        Path(folder, "thumbnails", "lead.jpg").write_bytes(b"x")
        Path(folder, "index.xml").write_text(
            '<library><item><name>Lead</name><bitmap>\\thumbnails\\lead.jpg</bitmap>'
            '<maxfile>\\objects\\lead.max</maxfile><object>o</object>'
            '<matlib>/matlibs/lead.mat</matlib><material>m</material></item>'
            '<item><name>Abs</name><bitmap>\\\\srv\\share\\t.jpg</bitmap><maxfile>\\\\srv\\share\\a.max</maxfile>'
            '<object>o</object><matlib>D:\\lib\\a.mat</matlib></item></library>', encoding="utf-8")
        lead, absolute = rclib.parse_index(os.path.join(folder, "index.xml"), "Railclone Library", self.root)
        self.assertEqual(lead["maxfile"], os.path.join(folder, "objects", "lead.max"))
        self.assertEqual(lead["matlib"], os.path.join(folder, "matlibs", "lead.mat"))
        self.assertEqual(lead["thumbnail"], os.path.join(folder, "thumbnails", "lead.jpg"))
        self.assertEqual(absolute["maxfile"], "\\\\srv\\share\\a.max")  # UNC and drive paths stay absolute
        self.assertEqual(absolute["matlib"], "D:\\lib\\a.mat")
        self.assertEqual(absolute["thumbnail"], "\\\\srv\\share\\t.jpg")
        for text, rel in (("\\objects\\x.max", "objects\\x.max"), ("/objects/x.max", "objects/x.max"),
                          ("objects\\x.max", "objects\\x.max"), ("..\\x.max", "..\\x.max"),
                          ("\\\\srv\\share\\x.max", None), ("C:\\x.max", None), ("C:x.max", None)):
            self.assertEqual(rclib.library_relative(text), rel, text)


def lib_raw(*, placed=48, extra=()):
    rows = ["RCLIB", "K|handle|" + V(900), "K|name|" + V("RC_Metal_Fence_1"),
            "K|style|" + V(""), "K|style_desc|" + V("Parametric metal fence"),
            "K|material|" + V(("m", "Multimaterial", "_RC_fencing")), "K|renderer|" + V("vray"),
            "K|matlib|" + V("C:\\matlib\\vray_v2.mat"), "K|fixed_maps|" + V(0),
            "B|1|" + "|".join(V(v) for v in ("{b16c}", "Path", 0, {"name": "Path600", "handle": 555})),
            "B|2|" + "|".join(V(v) for v in ("{890f}", "Surface", 1, None)),
            "O|after|%d|%d|%s:%d" % (placed + 3, placed, b64("allum_picket_01"), placed)] + list(extra)
    return "\n".join(rows + ["END"]) + "\n"


def roots_raw(roots):
    """The roots report of the read-only registry script, as 3ds Max would send it."""
    rows = ["RCROOTS"] + ["R|%s|%s" % (b64(r["name"]), b64(r["path"])) for r in roots]
    return "\n".join(rows + ["END"]) + "\n"


class FromLibraryTests(LibraryFixture):
    ROUTE1 = "\\Railclone Library\\Architecture\\Exterior\\Fences\\Metal\\Metal Fence 1"
    ROUTE_RAIL = "\\Railclone Library\\Architecture\\Interior\\Baseboards\\Baseboard 002"

    def call(self, raw, max_roots=None, **kw):
        kw.setdefault("allow_undo_flush", True)
        roots = self.roots if max_roots is None else max_roots
        with patch.object(rc, "client", client_with(roots_raw(roots), raw)) as client:
            out = rc.railclone_from_library(**kw)
        sent = scripts(client)
        self.assertEqual(len(sent), 2)
        self.assert_roots_read(sent[0])
        return out, sent[1]

    def assert_roots_read(self, script):
        """The first call only reads 3ds Max's RailClone LibraryDir key."""
        self.assertIn(rclib.LIBRARY_KEY, decoded_strings(script))
        body = mxs_body(script)
        self.assertIn('(dotNetClass "").CurrentUser.OpenSubKey keyPath', body)
        self.assertIn("rk.GetValue nm", body)
        for word in ("SetValue", "CreateSubKey", "DeleteValue", "DeleteSubKey", "RailClone_Pro", "mergeMAXFile",
                     "loadLibraryItemByPath", "theHold", "undo", "delete", "select"):
            self.assertNotIn(word, body)

    def test_max_item_needs_undo_flush_consent(self):
        for item in ("Metal Fence 1", "Saepes"):  # .max by merge, .max by route (.pmat matlib)
            exc = self.refused(ValueError, item=item, splines=["Path600"])
            self.assertIn("clears 3ds Max's undo history", str(exc))
            self.assertIn("allow_undo_flush=true", str(exc))
            self.assertIn("remove_with", str(exc))
            self.assertIn("Nothing was loaded; only the library roots were read", str(exc))
        exc = self.refused(ValueError, item=self.ROUTE1, method="route")
        self.assertIn("allow_undo_flush=true", str(exc))

    def test_max_roots_are_the_authority(self):
        # The server's registry view can be a stale private copy (Claude Desktop's package keeps one). Here
        # it points the root at an old folder: the route is resolved against 3ds Max's roots instead.
        stale = [{"name": "Railclone Library", "path": os.path.join(self.tmp, "old")},
                 {"name": "CRGR 2", "path": os.path.join(self.tmp, "gone")}]
        with patch.object(rclib, "library_roots", return_value=(stale, [])):
            out, script = self.call(lib_raw(), item=self.ROUTE1, splines=["Path600"])
        self.assertIn(os.path.join(self.fences, "objects", "fencing_metal1.max"), decoded_strings(script))
        differ = [w for w in out["warnings"] if w.startswith("ROOTS_DIFFER")]
        self.assertEqual(len(differ), 1)
        for text in ("'CRGR' is only in 3ds Max", "'CRGR 2' is not in 3ds Max",
                     f"'Railclone Library' is {self.root} in 3ds Max"):
            self.assertIn(text, differ[0])
        # the same roots on both sides: no warning
        out, _ = self.call(lib_raw(), item=self.ROUTE1, splines=["Path600"])
        self.assertFalse(any(w.startswith("ROOTS") for w in out["warnings"]))

    def test_root_unknown_to_max_is_refused_before_anything_is_created(self):
        # A route RailClone doesn't know opens a modal dialog: it never reaches loadLibraryItemByPath
        crgr_only = [r for r in self.roots if r["name"] == "CRGR"]
        exc = self.refused(rclib.RailCloneError, max_roots=crgr_only, item=self.ROUTE_RAIL)
        self.assertEqual(exc.code, "ROUTE_INVALID")
        self.assertIn("'Railclone Library' is not registered", str(exc))
        exc = self.refused(rclib.RailCloneError, max_roots=crgr_only, item="Metal Fence 1")
        self.assertEqual(exc.code, "ITEM_NOT_FOUND")
        with patch.object(rc, "client", client_with("RCROOTS\nX|missing\nEND\n")) as client:
            with self.assertRaises(rclib.RailCloneError) as ctx:
                rc.railclone_from_library(item=self.ROUTE_RAIL)
        self.assertEqual(ctx.exception.code, "NO_LIBRARY_ROOTS")
        self.assertEqual(client.send_command.call_count, 1)

    def test_unreadable_max_roots_fall_back_to_the_server_view(self):
        with patch.object(rc, "client", client_with("RCERROR|-- Runtime error: dotNet failed", lib_raw())) as client:
            out = rc.railclone_from_library(item=self.ROUTE_RAIL, splines=["Path600"])
        self.assertEqual(client.send_command.call_count, 2)
        self.assertTrue(any(w.startswith("ROOTS_NOT_CHECKED") for w in out["warnings"]))

    def test_dialog_blocking_the_roots_read_is_retryable(self):
        import json
        from maxmcp.helpers.precheck import PrecheckBlocked
        from maxmcp.max_client import DialogBlocked
        client = MagicMock()
        client.send_command.side_effect = DialogBlocked("r1", "maxscript", [{"title": "Missing External Files"}])
        with patch.object(rc, "client", client):
            with self.assertRaises(PrecheckBlocked) as ctx:
                rc.railclone_from_library(item=self.ROUTE_RAIL)
        self.assertEqual(client.send_command.call_count, 1)  # never the load
        payload = json.loads(ctx.exception.bridge_message)
        self.assertEqual((payload["code"], payload["retryable"]), ("BLOCKED_BY_DIALOG", True))
        self.assertIn("Nothing was loaded", payload["message"])
        self.assertFalse(payload["details"]["changed"])

    def test_root_differences(self):
        server = [{"name": "A", "path": "C:\\lib\\a\\"}, {"name": "B", "path": "C:\\lib\\b"}]
        same = [{"name": "A", "path": "c:/LIB/a"}, {"name": "B", "path": "C:\\lib\\b"}]
        self.assertEqual(rclib.root_differences(server, same), [])
        moved = [{"name": "A", "path": "D:\\lib\\a"}, {"name": "C", "path": "C:\\lib\\c"}]
        self.assertEqual(rclib.root_differences(server, moved),
                         ["'C' is only in 3ds Max (C:\\lib\\c)", "'B' is not in 3ds Max (this server sees C:\\lib\\b)",
                          "'A' is D:\\lib\\a in 3ds Max, C:\\lib\\a\\ here"])

    def assert_undo_off_load(self, script, load_call):
        """The whole load runs with undo off: no undo step, no copy, nothing deleted unless it fails."""
        body = mxs_body(script)
        self.assertNotIn("theHold", body.split("if theHold.Holding()", 1)[1].split("\n", 1)[1])
        self.assertNotIn("copy ", body)
        self.assertNotIn("mergedRc", body)
        start = body.index("with undo off (\n    try (\n")
        catch = body.index("    ) catch (", start)
        end = body.index("\n)\n", catch)
        load = body.index(load_call)
        self.assertLess(start, load)
        for step in ("base.banode[pathIdx[k]] = splines[k]", "o.pos = splines[1].pos",
                     "setProperty pair[1] pair[2] cand"):
            self.assertLess(load, body.index(step))
            self.assertLess(body.index(step), catch)
        self.assertLess(body.index("loaded = true"), load)
        # deletes happen only in the error path, with undo off; the history is cleared once more after a load
        self.assertEqual(body.count("delete n"), 1)
        self.assertTrue(start < body.index("delete n") and catch < body.index("delete n") < end)
        self.assertIn("if loaded do (try (clearUndoBuffer()) catch ())", body)
        self.assertLess(body.index("delete n"), body.index("clearUndoBuffer()"))
        self.assertNotIn('undo "', script)

    def test_merge_runs_with_undo_off(self):
        out, script = self.call(lib_raw(), item="Metal Fence 1", splines=["Path600"])
        self.assert_undo_off_load(script, "mergeMAXFile maxFile")
        body = mxs_body(script)
        self.assertLess(body.index("mergeMAXFile maxFile"), body.index("loadTempMaterialLibrary matPath"))
        self.assertLess(body.index("loadTempMaterialLibrary matPath"), body.index("    ) catch ("))
        self.assertIn("if libMtl != undefined then o.material = libMtl", body)
        self.assertEqual((out["undoable"], out["undo"], out["undo_history_cleared"]), (False, None, True))
        self.assertEqual(out["remove_with"], [{"name": "RC_Metal_Fence_1", "handle": 900}])

    def test_route_for_max_item_runs_with_undo_off(self):
        raw = lib_raw(extra=["N|" + V({"name": "Leftover", "handle": 901})])
        out, script = self.call(raw, item="Saepes", splines=["Path600"])
        self.assertIn('local methodName = "route"', script)
        self.assert_undo_off_load(script, "api.loadLibraryItemByPath routeArg")
        self.assertNotIn("mergeMAXFile", mxs_body(script))
        self.assertEqual((out["undoable"], out["undo"], out["undo_history_cleared"]), (False, None, True))
        self.assertEqual(out["extra_nodes"], ["Leftover"])
        self.assertEqual(out["remove_with"], [{"name": "RC_Metal_Fence_1", "handle": 900},
                                              {"name": "Leftover", "handle": 901}])

    def test_rail_route_is_one_undo_step(self):
        out, script = self.call(lib_raw(), item=self.ROUTE_RAIL, splines=["Path600"], allow_undo_flush=False)
        self.assertIn('theHold.Accept "MCP RailClone from library"', script)
        self.assertIn('undo "MCP RailClone cleanup" on', script)
        body = mxs_body(script)
        begin = body.index("theHold.Begin()")
        accept = body.index('theHold.Accept ""')
        for step in ("o = RailClone_Pro()", "api.loadLibraryItemByPath routeArg", "base.banode[pathIdx[k]] = splines[k]",
                     "setProperty pair[1] pair[2] cand"):
            self.assertTrue(begin < body.index(step) < accept, step)
        # nothing is deleted with undo off: the cancel removes the node, leftovers go in a recorded step
        self.assertNotIn("with undo off", body)
        self.assertNotIn("clearUndoBuffer", body)
        self.assertNotIn("copy ", body)
        self.assertEqual(body.count("delete n"), 1)
        self.assertIn('if left.count > 0 do undo "" on (for n in left do (try (delete n) catch ()))', body)
        cancel = body.index("if ownsHold do (try (theHold.Cancel()) catch ())")
        self.assertTrue(accept < cancel < body.index("delete n"))
        self.assertEqual((out["undoable"], out["undo"], out["undo_history_cleared"]),
                         (True, "MCP RailClone from library", False))
        self.assertEqual(out["remove_with"], [{"name": "RC_Metal_Fence_1", "handle": 900}])

    def test_merge_never_in_an_undo_step(self):
        entry = {"route": self.ROUTE1, "maxfile": "x.max", "object": "o", "material": "m"}
        with self.assertRaises(ValueError):
            rc._from_library_body(entry, "merge", [], "", "RC_x", "", "", undo_step=True)

    def test_style_desc_read_before_the_rebuild(self):
        _, script = self.call(lib_raw(), item=self.ROUTE_RAIL, allow_undo_flush=False)
        body = script.replace(rc._FUNCTIONS, "")
        # getStyleDesc empties the output export until the next rebuild (live)
        self.assertLess(body.index("getStyleDesc()"), body.index("rcOutStats (rcRebuildOutput rcBase false)"))
        self.assertNotIn(".getXMLOutput()", body)  # read inside rcRebuildOutput, after its rebuild

    def refused(self, exc_type, reads_roots=True, max_roots=None, **kw):
        """The call fails before anything is created: at most 3ds Max's library roots were read."""
        responses = [roots_raw(self.roots if max_roots is None else max_roots)] if reads_roots else []
        with patch.object(rc, "client", client_with(*responses)) as client:
            with self.assertRaises(exc_type) as ctx:
                rc.railclone_from_library(**kw)
        sent = scripts(client)
        self.assertEqual(len(sent), len(responses))
        for script in sent:
            self.assert_roots_read(script)
        return ctx.exception

    def test_merge_by_name(self):
        out, script = self.call(lib_raw(), item="Metal Fence 1", splines=["Path600"], name="Fence A")
        self.assertIn('local methodName = "merge"', script)
        # #useSceneMtlDups: the merge never replaces a scene material of the same name (one tuned for Vantage)
        self.assertIn("mergeMAXFile maxFile #(objName) #autoRenameDups #useSceneMtlDups #neverReparent quiet:true",
                      script)
        self.assertNotIn("#useMergedMtlDups", mxs_body(script))
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
        self.assertNotIn("theHold.Accept", script)  # a merge clears the undo history: no undo step
        self.assertIn("rcRebuildOutput rcBase false", script)
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
        # refused in Python before 3ds Max is asked for anything
        self.refused(ValueError, reads_roots=False, item=self.ROUTE1, method="copy")
        self.refused(ValueError, reads_roots=False, item=self.ROUTE1, splines=["A", "A"])
        self.refused(ValueError, reads_roots=False, item=self.ROUTE1, splines=42)
        self.refused(ValueError, reads_roots=False, item="")
        os.remove(os.path.join(self.fences, "objects", "fencing_metal1.max"))
        exc = self.refused(rclib.RailCloneError, item=self.ROUTE1)
        self.assertEqual(exc.code, "FILE_MISSING")

    def test_splines_accept_one_name_or_a_comma_list(self):
        # Small models send a single name; FastMCP validates arguments against the signature (StrList)
        from mcp.server.fastmcp.utilities.func_metadata import func_metadata
        meta = func_metadata(rc.railclone_from_library)
        for given, expected in (("Line001", ["Line001"]), ("Line001, Line002", ["Line001", "Line002"]),
                                ('["Line001"]', ["Line001"]), (["Line001"], ["Line001"])):
            with self.subTest(given=given):
                args = meta.arg_model.model_validate(meta.pre_parse_json({"item": "x", "splines": given}))
                self.assertEqual(args.model_dump_one_level()["splines"], expected)
        args = meta.arg_model.model_validate({"item": "x"})
        self.assertIsNone(args.model_dump_one_level()["splines"])

    def test_empty_output_not_verified(self):
        out, _ = self.call(lib_raw(placed=0), item=self.ROUTE1, splines=["Path600"])
        self.assertFalse(out["verified"])
        self.assertTrue(any(w.startswith("EMPTY_OUTPUT") for w in out["warnings"]))

    def test_load_failure_code(self):
        raw = "RCERROR|-- Runtime error: LOAD_FAILED: loadLibraryItemByPath returned 1 for x"
        with patch.object(rc, "client", client_with(roots_raw(self.roots), raw)):
            with self.assertRaises(rclib.RailCloneError) as ctx:
                rc.railclone_from_library(item=self.ROUTE_RAIL)
        self.assertEqual(ctx.exception.code, "LOAD_FAILED")

    def test_splines_go_by_base_role(self):
        # Live: batype is 0 for a path and a clipping area alike, so by order the second spline of a window
        # (X Spline + Clipping area) became its clipping area. Bases now get splines by their graph role.
        raw = lib_raw().replace(
            "B|1|" + "|".join(V(v) for v in ("{b16c}", "Path", 0, {"name": "Path600", "handle": 555})),
            "B|1|" + "|".join(V(v) for v in ("{b16c}", "X Spline", 0, {"name": "Path600", "handle": 555}, "path"))
            + "\nB|3|" + "|".join(V(v) for v in ("{c1ce}", "Clipping area", 0, {"name": "Outline", "handle": 556},
                                                 "clip")))
        out, script = self.call(raw, item=self.ROUTE_RAIL, splines=["Path600"], clipping_splines=["Outline"],
                                allow_undo_flush=False)
        self.assertEqual(decoded_strings(script).count("Outline"), 1)
        self.assertIn("local clipNames = #(rcDecode", script)
        body = mxs_body(script)
        roles = body.index("baseRoles = rcBaseRoles base")
        for step in ("base.banode[pathIdx[k]] = splines[k]", "base.banode[clipIdx[k]] = clipSplines[k]"):
            self.assertLess(roles, body.index(step))
        self.assertIn('(rcAt baseRoles i) == "path" collect i', script)
        self.assertIn('(rcAt baseRoles i) == "clip" collect i', script)
        self.assertNotIn("spIdx", body)
        self.assertEqual([(b["index"], b["name"], b["role"]) for b in out["bases"]],
                         [(1, "X Spline", "path"), (3, "Clipping area", "clip"), (2, "Surface", None)])
        self.assertEqual(out["bases"][1]["node"], {"name": "Outline", "handle": 556})
        self.assertFalse(any(w.startswith("NO_SPLINES") for w in out["warnings"]))
        # without splines the warning names the bases by role
        out, _ = self.call(raw, item=self.ROUTE_RAIL, allow_undo_flush=False)
        warning = next(w for w in out["warnings"] if w.startswith("NO_SPLINES"))
        self.assertIn("path base(s) ['X Spline'] (splines)", warning)
        self.assertIn("clipping-area base(s) ['Clipping area'] (clipping_splines)", warning)

    def test_role_errors_remove_the_load(self):
        for code in ("ROLE_UNKNOWN", "TOO_MANY_SPLINES", "TOO_MANY_CLIPPING_SPLINES"):
            raw = f"RCERROR|-- Runtime error: {code}: 2 spline(s) for 1 path base(s)"
            with self.subTest(code=code), patch.object(rc, "client", client_with(roots_raw(self.roots), raw)):
                with self.assertRaises(rclib.RailCloneError) as ctx:
                    rc.railclone_from_library(item=self.ROUTE_RAIL, splines=["A", "B"])
                self.assertEqual(ctx.exception.code, code)
        for script in (rc._from_library_body({"route": self.ROUTE_RAIL, "maxfile": "x.rail"}, "route", ["A"], "",
                                             "RC_x", "", "", undo_step=True, clipping_splines=["B"]),):
            body = script.replace(rc._FUNCTIONS, "")
            # the refusals are thrown inside the load's try, so the cancel/cleanup path removes the new node
            setup = body.index("baseRoles = rcBaseRoles base")
            self.assertTrue(body.index("theHold.Begin()") < setup < body.index('throw ("ROLE_UNKNOWN: ')
                            < body.index('theHold.Accept "'))
        self.refused(ValueError, reads_roots=False, item=self.ROUTE1, splines=["A"], clipping_splines=["A"])
        self.refused(ValueError, reads_roots=False, item=self.ROUTE1, clipping_splines=[""])


PROXY_OUTPUT = (
    # Live (RailClone 7.3.5, probe p19/p22): under Proxy Cache getXMLOutput lists every item without source_segment
    '<RailClone version="1">'
    '<item index="0" instanced="1" tm="[1,0,0][0,1,0][0,0,1][0,0,0]" box="[-2.55,-2.55,119.93][2.55,2.55,127.07]" '
    'tags=""/>'
    '<item index="1" instanced="1" tm="[1,0,0][0,1,0][0,0,1][10,0,0]" box="[-2.55,-2.55,0][2.55,2.55,120]" tags=""/>'
    '<item index="2" instanced="0" tm="[1,0,0][0,1,0][0,0,1][20,0,0]" '
    'box="[2.14748e+09,2.14748e+09,2.14748e+09][-2.14748e+09,-2.14748e+09,-2.14748e+09]" tags=""/>'
    '</RailClone>'
)


class VersionTests(unittest.TestCase):
    """Issue #26: RailClone versions, library-load bugs, selectors and Proxy Cache (live probes p02-p22)."""

    def inspect(self, raw, **kw):
        with patch.object(rc, "client", client_with(raw)) as client:
            out = rc.railclone_inspect(name="RC_Fence", **kw)
        return out, scripts(client)[0]

    def test_version_text_and_saved_version(self):
        self.assertEqual(rclib.version_text(735), "7.3.5")
        self.assertEqual(rclib.version_text(613), "6.1.3")
        for value in (0, -1, None, True, 7.35, "735"):
            self.assertIsNone(rclib.version_text(value), value)
        self.assertEqual(rclib.version_text(1004), "1004")
        self.assertEqual(rclib.saved_version("7.0.8 Pro"), 708)
        self.assertEqual(rclib.saved_version("2.4.2b Pro"), 242)
        self.assertEqual(rclib.saved_version("7.3.5 Pro"), 735)
        for text in ("n/a", "", None, "7.10.1 Pro"):
            self.assertIsNone(rclib.saved_version(text), text)

    def test_runtime_version_is_read_from_a_new_instance(self):
        # RailClone_Pro.version() is 400 on 7.1.1 and 7.3.5 alike: an API number. A new instance's createdversion
        # is the running version (735 on 7.3.5, railclonepro.dlo 7.3.5.687), and creating it adds no node.
        self.assertIn("fn rcRuntime = (try ((createInstance RailClone_Pro).createdversion) catch undefined)",
                      rc._FUNCTIONS)
        _, script = self.inspect(inspect_raw())
        body = script.replace(rc._FUNCTIONS, "")
        self.assertIn('rcRow ss #("K", "rc_runtime", rcV (rcRuntime()))', body)
        self.assertIn('rcRow ss #("K", "api_version", rcV (try (RailClone_Pro.version()) catch undefined))', body)
        self.assertIn("#savedversion, #createdversion, #proxymode,", body)
        self.assertNotIn("plugin_version", body)
        out, _ = self.inspect(inspect_raw().replace("K|rc_runtime|" + V(735), "K|rc_runtime|u"))
        self.assertEqual((out["node"]["rc_version"], out["node"]["rc_version_int"]), (None, None))

    def test_selector_index_mismatch(self):
        # paintval is the index the panel shows, pafloatval the value the engine builds with (live, p07)
        swapped = param_row(8, "{c8}", "Top Window", 5, ival=0, fval=2.0, sel="No Top|1;Top|2")
        outside = param_row(9, "{c9}", "Frames", 5, ival=7, fval=10.0, sel="Wood|1;PVC|10")
        out, _ = self.inspect(inspect_raw(extra_params=[swapped, outside]))
        mismatch = [w for w in out["warnings"] if w.startswith("SELECTOR_INDEX_MISMATCH")]
        self.assertEqual(len(mismatch), 2)
        self.assertIn("'Top Window': the option index (paintval) 0 is 'No Top' = 1, but the value (pafloatval) is "
                      "2, option 'Top'", mismatch[0])
        self.assertIn("outside its 2 options", mismatch[1])
        params = {p["name"]: p for p in out["params"]}
        self.assertTrue(params["Top Window"]["index_mismatch"])
        self.assertNotIn("index_mismatch", params["Spandrel Type"])  # index 1 = Solid = 2.0: they agree
        self.assertFalse(any(w.startswith("SELECTOR_INDEX_MISMATCH") for w in
                             self.inspect(inspect_raw())[0]["warnings"]))
        # stale paselector text on a non-selector (seen live on distance parameters) is never checked
        self.assertIsNone(rclib.selector_mismatch(dict(zip(rclib.PARAM_FIELDS, [
            "{p}", "Height", 3, 1, 0.0, 200.0, True, "", "X Spline|0;Closed Spline|1", False, 0, 0, 0.0, 0.0, 0.0,
            0.0, ""]))))

    def test_selector_bug_version(self):
        old = inspect_raw().replace("K|rc_runtime|" + V(735), "K|rc_runtime|" + V(711))
        out, _ = self.inspect(old)
        bug = [w for w in out["warnings"] if w.startswith("RC_SELECTOR_BUG_VERSION")]
        self.assertEqual(len(bug), 1)
        self.assertIn("RailClone 7.1.1 is older than 7.3.1", bug[0])
        self.assertIn("1 selector parameter(s)", bug[0])
        self.assertEqual(out["node"]["rc_version"], "7.1.1")
        self.assertFalse(any(w.startswith("RC_SELECTOR_BUG_VERSION") for w in
                             self.inspect(inspect_raw())[0]["warnings"]))  # 7.3.5
        self.assertIsNone(rclib.selector_bug_warning(711, 0))  # no selector in the style
        self.assertIsNone(rclib.selector_bug_warning(731, 3))
        self.assertIsNone(rclib.selector_bug_warning(None, 3))

    def test_style_may_predate_mapping_fix(self):
        route = "\\Railclone Library\\Architecture\\Exterior\\Windows\\Window 1 - PVC"
        routed = inspect_raw().replace("K|style|" + V(""), "K|style|" + V(route))
        for created, saved, fires in ((711, "n/a", True), (735, "7.0.8 Pro", True), (735, "7.3.5 Pro", False),
                                      (0, "n/a", False), (713, "7.1.3 Pro", False)):
            raw = routed.replace("K|createdversion|" + V(0), "K|createdversion|" + V(created)).replace(
                "K|savedversion|" + V("2.4.2b Pro"), "K|savedversion|" + V(saved))
            with self.subTest(created=created, saved=saved):
                out, _ = self.inspect(raw)
                found = [w for w in out["warnings"] if w.startswith("STYLE_MAY_PREDATE_MAPPING_FIX")]
                self.assertEqual(len(found), int(fires))
                if fires:
                    self.assertIn(route, found[0])
                    self.assertIn("may have been loaded before that fix", found[0])
                    self.assertIn("railclone_from_library", found[0])
        # a merged library object has no style route: createdversion 0 and savedversion "2.4.2b Pro" (Metal Fence 1)
        out, _ = self.inspect(inspect_raw())
        self.assertFalse(any(w.startswith("STYLE_MAY_PREDATE") for w in out["warnings"]))
        self.assertIn("created by RailClone 6.1.3 and last saved by RailClone 7.0.8 Pro",
                      rclib.mapping_predate_warning("\\L\\x", 613, "7.0.8 Pro"))

    def test_inspect_under_proxy_cache(self):
        # Live: Proxy Cache lists 176 items with no source_segment; the inspect said 176 placeholders, EMPTY_OUTPUT
        raw = inspect_raw().replace("K|proxymode|" + V(0), "K|proxymode|" + V(1)).replace(
            "O|" + b64(OUTPUT), "O|" + b64(PROXY_OUTPUT))
        out, _ = self.inspect(raw)
        self.assertEqual(out["node"]["proxymode"], 1)
        self.assertEqual(out["output"], {"total": 3, "placed": 2, "placeholders": 1, "by_segment": {},
                                         "rebuilt": True, "segments_reloaded": False, "proxy_cache": True})
        self.assertEqual([s["items"] for s in out["segments"]], [None, None])
        codes = [w.split(":")[0] for w in out["warnings"]]
        self.assertIn("PROXY_CACHE", codes)
        self.assertNotIn("EMPTY_OUTPUT", codes)
        # without Proxy Cache the same items are placeholders: no source_segment
        plain = inspect_raw().replace("O|" + b64(OUTPUT), "O|" + b64(PROXY_OUTPUT))
        out, _ = self.inspect(plain)
        self.assertEqual((out["output"]["placed"], out["output"]["placeholders"]), (0, 3))
        self.assertIn("EMPTY_OUTPUT", [w.split(":")[0] for w in out["warnings"]])
        self.assertNotIn("PROXY_CACHE", [w.split(":")[0] for w in out["warnings"]])
        self.assertFalse(rclib.is_placeholder({"box": "[0,0,0][1,1,1]"}, proxy=True))
        self.assertTrue(rclib.is_placeholder({"box": "[2,0,0][1,1,1]"}, proxy=True))

    def test_output_under_proxy_cache(self):
        with patch.object(rc, "client", client_with(out_raw(PROXY_OUTPUT, proxy="1"))) as client:
            out = rc.get_railclone_output(name="RC_Fence")
        self.assertIn("local proxy = if isProperty base #proxymode then base.proxymode else 0", scripts(client)[0])
        self.assertEqual((out["proxymode"], out["proxy_cache"]), (1, True))
        self.assertEqual((out["placed"], out["placeholders"], out["by_segment"]), (2, 1, {}))
        self.assertEqual([i["placeholder"] for i in out["items"]], [False, False, True])
        codes = [w.split(":")[0] for w in out["warnings"]]
        self.assertIn("PROXY_CACHE", codes)
        self.assertNotIn("EMPTY_OUTPUT", codes)
        with patch.object(rc, "client", client_with(out_raw())):
            out = rc.get_railclone_output(name="RC_Fence")
        self.assertEqual((out["proxymode"], out["proxy_cache"]), (0, False))
        self.assertFalse(any(w.startswith("PROXY_CACHE") for w in out["warnings"]))

    def test_set_param_under_proxy_cache(self):
        read = read_raw().replace("RCINSPECT\n", "RCINSPECT\nK|proxymode|" + V(1) + "\n")
        write = setp_raw({("before", 1, "paunitval"): 10.0, ("after", 1, "paunitval"): 20.0},
                         before="0:", after="0:")
        with patch.object(rc, "client", client_with(read, write)):
            out = rc.railclone_set_param({"Pickets Distance": 20}, name="RC_Fence")
        self.assertTrue(out["output"]["proxy_cache"])
        self.assertEqual((out["output"]["after"]["placed"], out["output"]["after"]["total"]), (None, 40))
        codes = [w.split(":")[0] for w in out["warnings"]]
        self.assertIn("PROXY_CACHE", codes)
        self.assertNotIn("EMPTY_OUTPUT", codes)
        # live: a 20 cm picket distance left a Proxy Cache fence at 176 items: the output is the cached build
        proxy = next(w for w in out["warnings"] if w.startswith("PROXY_CACHE"))
        self.assertIn("placed and placeholders are left out", proxy)
        self.assertIn("the geometry can stay as cached", proxy)

    def test_reload_segments_docs(self):
        # Two live tests on RailClone 7.3.5: segmentsUpdate never changed geometry; it renamed linked segments
        for fn in (rc.railclone_inspect, rc.get_railclone_output):
            doc = re.sub(r"\s+", " ", fn.__doc__)
            with self.subTest(fn=fn.__name__):
                self.assertIn("renames linked segments after their sources, with no geometry effect on RailClone "
                              "7.3.5; use force_rebuild to pick up source edits", doc)
        self.assertIn("renamed every linked segment (sname) after its source node", rc._RELOAD_NOTE)
        self.assertIn("use force_rebuild to pick up source edits", rc._RELOAD_NOTE)


class FromLibraryVersionTests(LibraryFixture):
    ROUTE_RAIL = FromLibraryTests.ROUTE_RAIL

    def call(self, raw, **kw):
        with patch.object(rc, "client", client_with(roots_raw(self.roots), raw)) as client:
            out = rc.railclone_from_library(item=self.ROUTE_RAIL, splines=["Path600"], **kw)
        return out, scripts(client)[1]

    def test_versions_reported(self):
        raw = lib_raw(extra=["K|api_version|" + V(400), "K|rc_runtime|" + V(735), "K|createdversion|" + V(735),
                             "K|savedversion|" + V("n/a"), "K|proxymode|" + V(0), "K|selectors|" + V(2)])
        out, script = self.call(raw)
        tail = script[script.index('format "RCLIB'):]
        self.assertIn('rcRow ss #("K", "rc_runtime", rcV (rcRuntime()))', tail)
        self.assertIn("for p in #(#createdversion, #savedversion, #proxymode) do", tail)
        self.assertIn("for t in (rcArr rcBase #patype) where t == 5 do nSel += 1", tail)
        self.assertEqual((out["rc_version"], out["rc_version_int"], out["api_version"]), ("7.3.5", 735, 400))
        self.assertEqual((out["createdversion"], out["savedversion"], out["proxymode"]), (735, "n/a", 0))
        self.assertFalse(any(w.startswith("RC_") for w in out["warnings"]))

    def test_old_runtime_warns(self):
        raw = lib_raw(extra=["K|rc_runtime|" + V(711), "K|selectors|" + V(2)])
        out, _ = self.call(raw)
        codes = [w.split(":")[0] for w in out["warnings"]]
        self.assertIn("RC_LIBRARY_MAPPING_BUG_VERSION", codes)
        self.assertIn("RC_SELECTOR_BUG_VERSION", codes)
        out, _ = self.call(lib_raw(extra=["K|rc_runtime|" + V(720), "K|selectors|" + V(0)]))
        self.assertEqual([w.split(":")[0] for w in out["warnings"] if w.startswith("RC_")], [])
        out, _ = self.call(lib_raw(extra=["K|rc_runtime|" + V(720), "K|selectors|" + V(1)]))
        self.assertEqual([w.split(":")[0] for w in out["warnings"] if w.startswith("RC_")],
                         ["RC_SELECTOR_BUG_VERSION"])

    def test_proxy_cache_load(self):
        out, _ = self.call(lib_raw(placed=0, extra=["K|proxymode|" + V(1)]))
        self.assertTrue(out["verified"])  # 3 items listed, none naming its segment
        self.assertEqual((out["item_count"], out["output"]["placed"]), (3, None))
        codes = [w.split(":")[0] for w in out["warnings"]]
        self.assertIn("PROXY_CACHE", codes)
        self.assertNotIn("EMPTY_OUTPUT", codes)


MXS_KEYWORDS = frozenset("""about and animate as at by case catch collect continue coordsys do dontcollect else exit
    fn for from function global if in local macroscript mapped max not of off on or parameters persistent plugin
    rcmenu return rollout set struct then throw to tool try undo utility when where while with""".split())


def mxs_code(line):
    """A MAXScript line without its string literals and its -- comment."""
    out, i, quoted = [], 0, False
    while i < len(line):
        ch = line[i]
        if quoted:
            if ch == "\\":
                i += 1
            elif ch == '"':
                quoted = False
                out.append('""')
        elif ch == '"':
            quoted = True
        elif line.startswith("--", i):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def mxs_body(script):
    """A sent script without the shared functions, string literals and comments."""
    return "\n".join(mxs_code(line) for line in script.replace(rc._FUNCTIONS, "").split("\n"))


def generated_scripts():
    """Every MAXScript the RailClone tools send, generated against a mocked bridge."""
    out = {"functions": rc._FUNCTIONS}
    calls = {
        "get_style": lambda: rc.get_railclone_style(name="RC"),
        "set_style": lambda: rc.set_railclone_style(STYLE_DUP, TOKEN, name="RC"),
        "output": lambda: rc.get_railclone_output(name="RC"),
        "inspect": lambda: rc.railclone_inspect(handle=7, name="RC"),
        "set_param": lambda: rc.railclone_set_param({"Pickets Distance": 20, "Mirror": False}, name="RC_Fence"),
    }
    for label, call in calls.items():
        with patch.object(rc, "client", client_with(read_raw(), "RCERROR|-- stop")) as client:
            try:
                call()
            except (rclib.RailCloneError, ValueError):
                pass
        for n, script in enumerate(scripts(client)):
            out[f"{label}#{n}"] = script
    entry = {"route": "\\L\\Cat\\Item", "maxfile": "C:\\lib\\item.max", "object": "rc_item", "material": "mtl"}
    for method, step in (("merge", False), ("route", False), ("route", True)):
        out[f"from_library_{method}_{step}"] = rc._from_library_body(
            entry, method, ["Path600"], "Name", "RC_Item", "C:\\m.mat", "C:\\maps\\", undo_step=step,
            clipping_splines=["Outline"])
    with patch.object(rc, "client", client_with("RCERROR|-- stop")) as client:
        rc._max_library_roots()
    out["library_roots"] = scripts(client)[0]
    return out


class MaxScriptLintTests(unittest.TestCase):
    """Live MAXScript facts (3ds Max 2026, RailClone 7.1.1) checked on every generated script."""

    @classmethod
    def setUpClass(cls):
        cls.scripts = generated_scripts()

    def test_scripts_were_generated(self):
        self.assertTrue({"get_style#0", "set_style#0", "output#0", "inspect#0", "set_param#0", "set_param#1",
                         "from_library_route_True", "library_roots"} <= set(self.scripts))

    def test_no_for_collect_in_call_arguments(self):
        # Live: a for-collect in a call's arguments that reads the caller's locals failed with
        # "Bad free thunk local member index" or read the wrong variable. Collect into a local first.
        allowed = re.compile(r"(?:.*\b(?:do|then|else)\s+)?(?:local\s+)?\w+\s*=")
        for label, script in self.scripts.items():
            for line in script.split("\n"):
                code = mxs_code(line)
                for match in re.finditer(r"\bfor\b", code):
                    if not re.search(r"\bcollect\b", code[match.end():]):
                        continue
                    prefix = code[:match.start()].strip()
                    self.assertTrue(prefix == "" or allowed.fullmatch(prefix), f"{label}: {line.strip()}")

    def test_no_keyword_variable_names(self):
        # Live: "local at = ..." was a parse error
        for label, script in self.scripts.items():
            code = "\n".join(mxs_code(line) for line in script.split("\n"))
            names = re.findall(r"\blocal\s+(\w+)", code) + re.findall(r"\bfor\s+(\w+)\s+(?:in|=)", code)
            for params in re.findall(r"\bfn\s+\w+\s+([^=\n]*)=", code):
                names += [p.lstrip("&") for p in params.split()]
            bad = sorted({n for n in names if n.lower() in MXS_KEYWORDS})
            self.assertEqual(bad, [], label)

    def test_handles_converted_before_formatting(self):
        # Live: getHandleByAnim returns an IntegerPtr; it must go through "as integer64"
        for label, script in self.scripts.items():
            code = "\n".join(mxs_code(line) for line in script.split("\n"))
            total = len(re.findall(r"\bgetHandleByAnim\b", code))
            converted = len(re.findall(r"\(getHandleByAnim [\w.]+\) as integer64", code))
            self.assertEqual(total, converted, label)

    def test_style_desc_is_followed_by_a_rebuild(self):
        # Live: getStyleDesc() empties the output export until the next rebuild; never leave it empty
        seen = 0
        for label, script in self.scripts.items():
            code = script.replace(rc._FUNCTIONS, "")
            for match in re.finditer(r"getStyleDesc", code):
                seen += 1
                after = code[match.end():]
                self.assertIn("rcRebuild", after, label)
                if "getXMLOutput()" in after:
                    self.assertLess(after.index("rcRebuild"), after.index("getXMLOutput()"), label)
        self.assertGreaterEqual(seen, 4)  # inspect, and the three from-library scripts

    def test_no_script_reloads_segments_by_default(self):
        # Live (RC 7.3.5): segmentsUpdate renames every linked segment to its source node; only an explicit
        # reload_segments=true may run it, never a read, a parameter write or a library load
        for label, script in self.scripts.items():
            if label == "functions":
                continue
            body = script.replace(rc._FUNCTIONS, "")
            self.assertNotIn("segmentsUpdate", body, label)
            self.assertNotIn("rcRebuildOutput base true", body, label)
            self.assertNotIn("rcRebuildOutput rcBase true", body, label)

    def test_undo_records_never_outlive_their_nodes(self):
        # Live: an undo record that refers to a node deleted with undo off crashed a later undo
        holds = 0
        for label, script in self.scripts.items():
            if "theHold.Begin()" not in script:
                continue
            holds += 1
            for line in script.split("\n"):
                code = mxs_code(line)
                if re.search(r"\bdelete\b", code):
                    self.assertIn('undo "', line, f"{label}: a script with an undo step deletes without undo: "
                                                  f"{line.strip()}")
        self.assertGreaterEqual(holds, 3)  # set_style, set_param, from-library .rail


ROLE_STYLE = """<scene version="400">
  <node id="{P}" baseClass="Object" class="Spline" name="Path"/>
  <node id="{C}" baseClass="Object" class="Spline" name="Boundary Spline"/>
  <node id="{X}" baseClass="Object" class="Spline" name="X Spline"/>
  <node id="{W}" baseClass="Object" class="Spline" name="Clipping area"/>
  <node id="{N}" baseClass="Object" class="Spline" name="Clipping area 2"/>
  <node id="{O}" baseClass="Object" class="Spline" name="Offset path"/>
  <node id="{S}" baseClass="Object" class="Surface" name="Surface"/>
  <node id="{U}" baseClass="Object" class="Spline" name="Unused"/>
  <node id="{M}" baseClass="Object" class="Spline" name="Both"/>
  <node id="{G}" baseClass="Object" class="Spline" name="Odd"/>
  <node id="{B}" baseClass="Object" class="Spline" name="Spline"/>
  <node id="op1" baseClass="Operator" class="Offset" name="Offset">
    <connector id="node" name="Spline" target_id="{O}"/>
  </node>
  <node id="op2" baseClass="Operator" class="Basic Ops" name="Spline Op1">
    <groupConnectors id="nodes" name="Splines">
      <connector id="new_spline" name="Spline" target_id="{B}"/>
      <connector id="new_spline" name="&lt;empty spline&gt;" target_id=""/>
    </groupConnectors>
  </node>
  <node id="g5" baseClass="Generator" class="Linear1S" name="Glass">
    <connector id="spline" target_id="op2"/>
  </node>
  <node id="g1" baseClass="Generator" class="Linear1S" name="L1S">
    <connector id="spline" target_id="{p}"/>
    <connector id="base_surface" target_id="{S}"/>
    <connector id="_exp_x_0" target_id="{P}"/>
  </node>
  <node id="g2" baseClass="Generator" class="Array2S" name="A2S">
    <connector id="clipping_area" target_id="{C}"/>
    <connector id="spline" target_id="op1"/>
    <connector id="spline_x" target_id="{M}"/>
    <connector id="clipping_area" target_id="{M}"/>
    <connector id="default" target_id="{G}"/>
  </node>
  <node id="{mac}" baseClass="Macro" class="Macro" name="Window">
    <macro>
      <node id="{im_x}" baseClass="InputMacro" class="InputMacro" name="X Spline">
        <parameter id="type" value="1"/>
      </node>
      <node id="{im_w}" baseClass="InputMacro" class="InputMacro" name="Clipping area">
        <parameter id="type" value="3"/>
      </node>
      <node id="{im_n}" baseClass="InputMacro" class="InputMacro" name="Clipping area">
        <parameter id="type" value="1"/>
      </node>
      <node id="g3" baseClass="Generator" class="Array2S" name="Inner">
        <connector id="spline_x" target_id="{im_x}"/>
        <connector id="clipping_area" target_id="{im_n}"/>
      </node>
    </macro>
    <connector id="{mac}{im_x}" name="X Spline" target_id="{X}"/>
    <connector id="{mac}{im_w}" name="Clipping area" target_id="{W}"/>
    <connector id="{mac}{im_n}" name="Clipping area" target_id="{N}"/>
  </node>
</scene>"""


class BaseRoleTests(unittest.TestCase):
    """The role of each base from the style graph (live probes p12/p13 on RailClone 7.3.5)."""

    def test_roles(self):
        ids = ["{P}", "{C}", "{X}", "{W}", "{N}", "{O}", "{S}", "{U}", "{M}", "{G}", "{B}"]
        roles = dict(zip(ids, rclib.base_roles(ET.fromstring(ROLE_STYLE), ids)))
        self.assertEqual(roles, {
            "{P}": "path",      # L1S spline input (ids match case-insensitively; _exp_ connectors are parameters)
            "{C}": "clip",      # A2S clipping area
            "{X}": "path",      # macro input of type 1, followed to the inner A2S's spline_x
            "{W}": "clip",      # macro input of type 3
            "{N}": "clip",      # a type-1 InputMacro named "Clipping area" that feeds clipping_area inside
            "{O}": "path",      # through an operator to a spline input
            "{S}": "surface",
            "{U}": "unused",
            "{M}": "mixed",
            "{G}": "unknown",   # a generator input that is no path, clipping area or surface
            # an operator input in a <groupConnectors> list (live: Glass Railing 03's Basic Ops "Splines")
            "{B}": "path",
        })
        self.assertEqual(rclib.combine_roles({"path", "unknown"}), "path")
        self.assertEqual(rclib.combine_roles({"segment", "unknown"}), "unknown")

    def test_maxscript_walk_matches(self):
        # rcBaseRoles is the same walk in MAXScript; compared live on all 233 stock items in the scratch Max
        for cid, role in rclib.GENERATOR_INPUTS.items():
            self.assertIn(f'cid == "{cid}"', rc._FUNCTIONS)
            self.assertIn(f'appendIfUnique found "{role}"', rc._FUNCTIONS)
        for kind, role in rclib.INPUT_MACRO_ROLES.items():
            self.assertIn(f'if mt == {kind} then appendIfUnique found "{role}"', rc._FUNCTIONS)
        self.assertIn('else if cKinds[k] == "Operator" then append queue cOwners[k]', rc._FUNCTIONS)
        self.assertIn(f"steps < {rclib.ROLE_STEPS}", rc._FUNCTIONS)
        self.assertIn('if own != undefined and own.Name == "groupConnectors" do own = own.ParentNode', rc._FUNCTIONS)


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
