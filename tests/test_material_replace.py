"""Tests for replace_material / batch_replace_materials result post-processing."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout
# over an installed maxmcp.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.tool_response import envelope_result  # noqa: E402
from maxmcp.tools import material_replace as mr  # noqa: E402


class ImportTargetTests(unittest.TestCase):
    def test_imports_checkout(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(Path(mr.__file__).resolve(), repo / "maxmcp" / "tools" / "material_replace.py")


class NativeRegistryTests(unittest.TestCase):
    def test_replace_tools_are_in_native_registry(self):
        # gen_tool_registry only scans each tool's own body for cmd_type.
        from scripts.gen_tool_registry import extract_tools

        tools = {t["name"]: t["cmdType"] for t in extract_tools(Path(mr.__file__))}
        self.assertEqual(tools.get("replace_material"), "native:replace_material")
        self.assertEqual(tools.get("batch_replace_materials"), "native:batch_replace_materials")


HEBREW = "טיח לבן"
PLASTER = "Plaster_White_Smooth_300cm #0"


def _client(native: bool, result: dict | str) -> MagicMock:
    client = MagicMock()
    client.native_available = native
    raw = result if isinstance(result, str) else json.dumps(result)
    client.send_command.return_value = {"result": raw}
    return client


class ReplaceMaterialNativeTests(unittest.TestCase):
    def test_success_unchanged(self):
        native = {"source_material": "A", "target_material": "B", "replaced_count": 2,
                  "replaced_objects": ["Box001", "Box002"], "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out, native)
        self.assertEqual(client.send_command.call_args.kwargs["cmd_type"], "native:replace_material")

    def test_zero_match_is_no_match_with_warning(self):
        native = {"source_material": HEBREW, "target_material": PLASTER, "replaced_count": 0,
                  "replaced_objects": [], "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            raw = mr.replace_material(source_material=HEBREW, target_material=PLASTER)
        out = json.loads(raw)
        self.assertEqual(out["status"], "no_match")
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn(PLASTER, out["warnings"][0])
        self.assertIn("swap", out["warnings"][0])
        self.assertIn("assign_material", out["warnings"][0])
        env = envelope_result(raw, elapsed_ms=1.0, tool_name="replace_material")
        self.assertTrue(env["ok"])
        self.assertEqual(env["warnings"], out["warnings"])

    def test_preview_zero_is_no_match(self):
        native = {"source_material": "A", "target_material": "B", "affected_count": 0,
                  "affected_objects": [], "preview": True}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B", preview=True))
        self.assertEqual(out["status"], "no_match")
        self.assertTrue(out["preview"])
        self.assertEqual(len(out["warnings"]), 1)

    def test_preview_with_matches_unchanged(self):
        native = {"source_material": "A", "target_material": "B", "affected_count": 1,
                  "affected_objects": ["Box001"], "preview": True}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B", preview=True))
        self.assertEqual(out, native)

    def test_unicode_names_round_trip_in_payload(self):
        native = {"source_material": PLASTER, "target_material": HEBREW, "replaced_count": 1,
                  "replaced_objects": ["Wall"], "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)) as client:
            mr.replace_material(source_material=PLASTER, target_material=HEBREW)
        payload = client.send_command.call_args.args[0]
        self.assertTrue(payload.isascii())
        decoded = json.loads(payload)
        self.assertEqual(decoded["source_material"], PLASTER)
        self.assertEqual(decoded["target_material"], HEBREW)

    def test_non_json_result_passthrough(self):
        with patch.object(mr, "client", _client(True, "not json")):
            self.assertEqual(mr.replace_material("A", "B"), "not json")


class ReplaceMaterialMaxScriptTests(unittest.TestCase):
    def test_success_unchanged(self):
        ms = {"source_material": "A", "target_material": "B", "replaced_count": 1,
              "replaced_objects": ["Box001"], "status": "success"}
        with patch.object(mr, "client", _client(False, ms)) as client:
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out, ms)
        self.assertNotIn("cmd_type", client.send_command.call_args.kwargs)

    def test_zero_match_is_no_match(self):
        ms = {"source_material": "A", "target_material": "B", "replaced_count": 0,
              "replaced_objects": [], "status": "success"}
        with patch.object(mr, "client", _client(False, ms)):
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out["status"], "no_match")
        self.assertEqual(len(out["warnings"]), 1)

    def test_preview_zero_and_missing_source_warns(self):
        ms = {"source_material": "A", "target_material": "B", "source_exists": False,
              "affected_count": 0, "affected_objects": [], "preview": True}
        with patch.object(mr, "client", _client(False, ms)):
            out = json.loads(mr.replace_material("A", "B", preview=True))
        self.assertEqual(out["status"], "no_match")
        self.assertEqual(len(out["warnings"]), 2)
        self.assertIn("'A'", out["warnings"][0])

    def test_source_not_found_stays_error(self):
        ms = {"error": "source material 'A' not found on any object", "status": "failed"}
        with patch.object(mr, "client", _client(False, ms)):
            raw = mr.replace_material("A", "B")
        self.assertEqual(json.loads(raw), ms)
        self.assertFalse(envelope_result(raw, elapsed_ms=1.0)["ok"])


class BatchReplaceMaterialsTests(unittest.TestCase):
    def test_native_mixed_results(self):
        native = {
            "results": [
                {"source_material": "A", "target_material": "B", "replaced_count": 2,
                 "replaced_objects": [], "status": "replaced"},
                {"source_material": "A", "target_material": "C", "replaced_count": 0,
                 "status": "no_objects"},
                {"source_material": "X", "target_material": "B", "status": "error",
                 "error": "source material not found"},
            ],
            "total_replaced": 2, "preview": False, "dry_run": False,
        }
        reps = [{"source": "A", "target": "B"}, {"source": "A", "target": "C"}, {"source": "X", "target": "B"}]
        with patch.object(mr, "client", _client(True, native)):
            raw = mr.batch_replace_materials(reps)
        out = json.loads(raw)
        statuses = [r["status"] for r in out["results"]]
        self.assertEqual(statuses, ["replaced", "no_match", "error"])
        self.assertEqual(out["total_replaced"], 2)
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'C'", out["warnings"][0])
        self.assertNotIn("warnings", out["results"][1])
        env = envelope_result(raw, elapsed_ms=1.0, tool_name="batch_replace_materials")
        self.assertTrue(env["ok"])
        self.assertEqual(len(env["warnings"]), 1)

    def test_native_many_no_match_one_warning(self):
        native = {
            "results": [{"source_material": "A", "target_material": t, "replaced_count": 0,
                         "status": "no_objects"} for t in ("B", "C", "D")],
            "total_replaced": 0, "preview": False, "dry_run": False,
        }
        reps = [{"source": "A", "target": t} for t in ("B", "C", "D")]
        with patch.object(mr, "client", _client(True, native)):
            raw = mr.batch_replace_materials(reps)
        out = json.loads(raw)
        self.assertEqual([r["status"] for r in out["results"]], ["no_match"] * 3)
        self.assertTrue(all("warnings" not in r for r in out["results"]))
        self.assertEqual(len(out["warnings"]), 1)
        for t in ("'B'", "'C'", "'D'"):
            self.assertIn(t, out["warnings"][0])
        env = envelope_result(raw, elapsed_ms=1.0, tool_name="batch_replace_materials")
        self.assertEqual(json.dumps(env, ensure_ascii=False).count("No object has"), 2)

    def test_native_preview_zero_entry(self):
        native = {
            "results": [
                {"source_material": "A", "target_material": "B", "replaced_count": 1, "status": "preview"},
                {"source_material": "A", "target_material": "C", "replaced_count": 0, "status": "preview"},
            ],
            "total_replaced": 1, "preview": True, "dry_run": False,
        }
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                         {"source": "A", "target": "C"}], preview=True))
        self.assertEqual([r["status"] for r in out["results"]], ["preview", "no_match"])
        self.assertEqual(out["total_replaced"], 1)

    def test_maxscript_mixed_results(self):
        client = MagicMock()
        client.native_available = False
        client.send_command.side_effect = [
            {"result": json.dumps({"source_material": "A", "target_material": "B", "replaced_count": 3,
                                   "replaced_objects": [], "status": "success"})},
            {"result": json.dumps({"source_material": "A", "target_material": "C", "replaced_count": 0,
                                   "replaced_objects": [], "status": "success"})},
        ]
        reps = [{"source": "A", "target": "B"}, {"source": "A", "target": "C"}, {"source": "A"}]
        with patch.object(mr, "client", client):
            out = json.loads(mr.batch_replace_materials(reps))
        self.assertEqual([r["status"] for r in out["results"]], ["success", "no_match", "skipped"])
        self.assertEqual(out["total_replaced"], 3)
        self.assertNotIn("warnings", out["results"][1])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'C'", out["warnings"][0])

    def test_maxscript_preview_totals(self):
        client = MagicMock()
        client.native_available = False
        client.send_command.side_effect = [
            {"result": json.dumps({"source_material": "A", "target_material": "B", "source_exists": True,
                                   "affected_count": 2, "affected_objects": [], "preview": True})},
            {"result": json.dumps({"source_material": "A", "target_material": "C", "source_exists": True,
                                   "affected_count": 0, "affected_objects": [], "preview": True})},
        ]
        with patch.object(mr, "client", client):
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                         {"source": "A", "target": "C"}], dry_run=True))
        self.assertTrue(out["preview"])
        self.assertEqual(out["total_replaced"], 2)
        self.assertEqual(out["results"][1]["status"], "no_match")
        self.assertEqual(len(out["warnings"]), 1)

    def test_maxscript_preview_missing_source_matches_native(self):
        client = MagicMock()
        client.native_available = False
        client.send_command.side_effect = [
            {"result": json.dumps({"source_material": "A", "target_material": "B", "source_exists": True,
                                   "affected_count": 2, "affected_objects": [], "preview": True})},
            {"result": json.dumps({"source_material": "X", "target_material": "B", "source_exists": False,
                                   "affected_count": 3, "affected_objects": [], "preview": True})},
        ]
        with patch.object(mr, "client", client):
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                         {"source": "X", "target": "B"}], preview=True))
        self.assertEqual(out["total_replaced"], 2)
        self.assertEqual(out["results"][1]["status"], "error")
        self.assertEqual(out["results"][1]["error"], "source material not found")
        self.assertNotIn("warnings", out)

    def test_maxscript_failed_entry_kept(self):
        client = MagicMock()
        client.native_available = False
        client.send_command.return_value = {"result": json.dumps(
            {"error": "source material 'X' not found on any object", "status": "failed"})}
        with patch.object(mr, "client", client):
            out = json.loads(mr.batch_replace_materials([{"source": "X", "target": "B"}]))
        self.assertEqual(out["results"][0]["status"], "failed")
        self.assertEqual(out["total_replaced"], 0)
        self.assertNotIn("warnings", out)


SLOT = {"parent_material": "Walls", "parent_class": "Multimaterial", "slot_index": 2, "slot_name": "(2)"}


class SubMaterialSlotTests(unittest.TestCase):
    def test_payload_passes_include_sub_materials(self):
        native = {"replaced_count": 1, "replaced_slot_count": 0, "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)) as client:
            mr.replace_material("A", "B")
            self.assertIs(json.loads(client.send_command.call_args.args[0])["include_sub_materials"], True)
            mr.replace_material("A", "B", include_sub_materials=False)
            self.assertIs(json.loads(client.send_command.call_args.args[0])["include_sub_materials"], False)

    def test_slot_only_replacement_is_not_no_match(self):
        native = {"source_material": "A", "target_material": "B", "source_exists": True,
                  "source_found_in": "material_editor", "include_sub_materials": True,
                  "replaced_count": 0, "replaced_objects": [], "replaced_slot_count": 1,
                  "replaced_slots": [SLOT], "skipped": [], "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out, native)

    def test_preview_slot_only_is_not_no_match(self):
        native = {"affected_count": 0, "affected_objects": [], "affected_slot_count": 2,
                  "affected_slots": [SLOT, SLOT], "skipped": [], "source_exists": True, "preview": True}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B", preview=True))
        self.assertNotIn("status", out)
        self.assertNotIn("warnings", out)

    def test_no_match_only_when_objects_and_slots_are_zero(self):
        native = {"replaced_count": 0, "replaced_objects": [], "replaced_slot_count": 0,
                  "replaced_slots": [], "skipped": [], "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out["status"], "no_match")
        self.assertEqual(len(out["warnings"]), 1)
        warning = out["warnings"][0]
        self.assertIn("top-level or sub-material", warning)
        self.assertIn("Material Editor slot", warning)
        self.assertNotIn("not matched", warning)
        self.assertNotIn("must already be", warning)

    def test_no_match_warning_mentions_disabled_sub_materials(self):
        native = {"replaced_count": 0, "replaced_objects": [], "replaced_slot_count": 0, "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B", include_sub_materials=False))
        self.assertEqual(out["status"], "no_match")
        self.assertIn("include_sub_materials=False", out["warnings"][0])

    def test_preview_missing_source_warns_but_lists_targets(self):
        native = {"source_material": "A", "target_material": "B", "source_exists": False,
                  "affected_count": 2, "affected_objects": ["Box001", "Box002"], "affected_slot_count": 1,
                  "affected_slots": [SLOT], "skipped": [], "preview": True}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B", preview=True))
        self.assertNotIn("status", out)
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'A'", out["warnings"][0])
        self.assertIn("real run would fail", out["warnings"][0])
        self.assertEqual(out["affected_slots"], [SLOT])

    def test_skipped_and_ambiguous_source_warn(self):
        native = {"source_exists": True, "source_found_in": "node", "source_ambiguous": True,
                  "source_candidates": 2, "replaced_count": 0, "replaced_slot_count": 1,
                  "replaced_slots": [SLOT], "skipped": [dict(SLOT, reason="source_contains_parent")],
                  "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out["status"], "replaced")
        self.assertEqual(len(out["warnings"]), 2)
        self.assertIn("2 different materials are named 'A'", out["warnings"][0])
        self.assertIn("skipped", out["warnings"][1])

    def test_maxscript_script_embeds_names_and_flags(self):
        ms = {"replaced_count": 0, "replaced_slot_count": 1, "status": "success"}
        name = 'Wall "A" \\ ' + HEBREW
        with patch.object(mr, "client", _client(False, ms)) as client:
            out = json.loads(mr.replace_material(name, "B", include_sub_materials=False))
        script = client.send_command.call_args.args[0]
        self.assertEqual(out["status"], "success")
        self.assertIn('local srcName = "Wall \\"A\\" \\\\ ' + HEBREW + '"', script)
        self.assertIn("local includeSubs = false", script)
        self.assertIn("local isPreview = false", script)
        for token in ("meditMaterials", "sceneMaterials", "currentMaterialLibrary", "setSubMtl"):
            self.assertIn(token, script)
        self.assertEqual(script.count("("), script.count(")"))

    def test_maxscript_preview_flag(self):
        ms = {"affected_count": 1, "affected_slot_count": 0, "source_exists": True, "preview": True}
        with patch.object(mr, "client", _client(False, ms)) as client:
            mr.replace_material("A", "B", preview=True)
        script = client.send_command.call_args.args[0]
        self.assertIn("local isPreview = true", script)
        self.assertIn("local includeSubs = true", script)


class BatchSubMaterialSlotTests(unittest.TestCase):
    def test_native_payload_and_slot_counts(self):
        native = {
            "results": [
                {"source_material": "A", "target_material": "B", "replaced_count": 0, "replaced_objects": [],
                 "replaced_slot_count": 3, "replaced_slots": [SLOT] * 3, "skipped": [], "status": "replaced"},
                {"source_material": "A", "target_material": "C", "replaced_count": 0, "replaced_objects": [],
                 "replaced_slot_count": 0, "replaced_slots": [], "skipped": [], "status": "no_objects"},
            ],
            "total_replaced": 0, "total_replaced_slots": 3, "include_sub_materials": True,
            "preview": False, "dry_run": False,
        }
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                         {"source": "A", "target": "C"}]))
        self.assertIs(json.loads(client.send_command.call_args.args[0])["include_sub_materials"], True)
        self.assertEqual([r["status"] for r in out["results"]], ["replaced", "no_match"])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'C'", out["warnings"][0])
        self.assertNotIn("'B'", out["warnings"][0])

    def test_native_include_false_payload_and_warning(self):
        native = {"results": [{"source_material": "A", "target_material": "B", "replaced_count": 0,
                               "replaced_slot_count": 0, "status": "preview"}],
                  "total_replaced": 0, "preview": True, "dry_run": True}
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"}], dry_run=True,
                                                        include_sub_materials=False))
        self.assertIs(json.loads(client.send_command.call_args.args[0])["include_sub_materials"], False)
        self.assertEqual(out["results"][0]["status"], "no_match")
        self.assertIn("include_sub_materials=False", out["warnings"][0])

    def test_native_skipped_entry_warns_at_top_level(self):
        native = {"results": [{"source_material": "A", "target_material": "B", "replaced_count": 1,
                               "replaced_slot_count": 0, "skipped": [dict(SLOT, reason="parent_is_source")],
                               "status": "replaced"}],
                  "total_replaced": 1, "preview": False, "dry_run": False}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"}]))
        self.assertEqual(out["results"][0]["status"], "replaced")
        self.assertNotIn("warnings", out["results"][0])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'A' -> 'B'", out["warnings"][0])

    def test_maxscript_slot_totals_and_flag(self):
        client = MagicMock()
        client.native_available = False
        client.send_command.side_effect = [
            {"result": json.dumps({"source_material": "A", "target_material": "B", "source_exists": True,
                                   "replaced_count": 1, "replaced_slot_count": 2, "status": "success"})},
            {"result": json.dumps({"source_material": "A", "target_material": "C", "source_exists": True,
                                   "replaced_count": 0, "replaced_slot_count": 1, "status": "success"})},
            {"result": json.dumps({"source_material": "A", "target_material": "D", "source_exists": True,
                                   "replaced_count": 0, "replaced_slot_count": 0, "status": "success"})},
        ]
        with patch.object(mr, "client", client):
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": t} for t in "BCD"],
                                                        include_sub_materials=False))
        self.assertTrue(all("local includeSubs = false" in c.args[0] for c in client.send_command.call_args_list))
        self.assertEqual([r["status"] for r in out["results"]], ["success", "success", "no_match"])
        self.assertEqual(out["total_replaced"], 1)
        self.assertEqual(out["total_replaced_slots"], 3)
        self.assertFalse(out["include_sub_materials"])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'D'", out["warnings"][0])

    def test_maxscript_preview_missing_source_is_error_entry(self):
        client = MagicMock()
        client.native_available = False
        client.send_command.return_value = {"result": json.dumps(
            {"source_material": "X", "target_material": "B", "source_exists": False, "affected_count": 1,
             "affected_slot_count": 4, "preview": True})}
        with patch.object(mr, "client", client):
            out = json.loads(mr.batch_replace_materials([{"source": "X", "target": "B"}], preview=True))
        self.assertEqual(out["results"][0]["status"], "error")
        self.assertIs(out["results"][0]["source_exists"], False)
        self.assertEqual(out["total_replaced"], 0)
        self.assertEqual(out["total_replaced_slots"], 0)
        self.assertNotIn("warnings", out)


class SkippedOnlyTests(unittest.TestCase):
    """All matches loop-guarded/refused: "blocked", never the swap hint."""

    def test_skipped_only_is_blocked_not_no_match(self):
        native = {"source_material": "Walls", "target_material": "Plaster", "source_exists": True,
                  "replaced_count": 0, "replaced_objects": [], "replaced_slot_count": 0, "replaced_slots": [],
                  "skipped": [dict(SLOT, reason="parent_is_source")], "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("Walls", "Plaster"))
        self.assertEqual(out["status"], "blocked")
        text = " ".join(out["warnings"])
        self.assertNotIn("if reversed", text)
        self.assertIn("parent_is_source", text)
        self.assertIn("not reversed", text)

    def test_native_blocked_status_kept(self):
        native = {"source_material": "Walls", "target_material": "Plaster", "replaced_count": 0,
                  "replaced_slot_count": 0, "skipped": [dict(SLOT, reason="reference_loop")], "status": "blocked"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("Walls", "Plaster"))
        self.assertEqual(out["status"], "blocked")
        self.assertFalse(any("if reversed" in w for w in out["warnings"]))

    def test_preview_skipped_only_is_blocked(self):
        native = {"affected_count": 0, "affected_objects": [], "affected_slot_count": 0, "affected_slots": [],
                  "skipped": [dict(SLOT, reason="source_contains_parent")], "source_exists": True,
                  "target_material": "Plaster", "preview": True}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("Walls", "Plaster", preview=True))
        self.assertEqual(out["status"], "blocked")
        self.assertFalse(any("if reversed" in w for w in out["warnings"]))

    def test_batch_skipped_only_entries(self):
        native = {"results": [
            {"source_material": "Walls", "target_material": "Plaster", "replaced_count": 0,
             "replaced_slot_count": 0, "skipped": [dict(SLOT, reason="set_failed")], "status": "no_objects"},
            {"source_material": "Walls", "target_material": "Brick", "replaced_count": 0,
             "replaced_slot_count": 0, "skipped": [dict(SLOT, reason="parent_is_source")], "status": "blocked"},
        ], "total_replaced": 0, "preview": False, "dry_run": False}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.batch_replace_materials([{"source": "Walls", "target": "Plaster"},
                                                         {"source": "Walls", "target": "Brick"}]))
        self.assertEqual([r["status"] for r in out["results"]], ["blocked", "blocked"])
        self.assertFalse(any("if reversed" in w for w in out["warnings"]))

    def test_skipped_warning_names_each_reason(self):
        skipped = [dict(SLOT, reason="set_failed"), dict(SLOT, reason="set_failed"),
                   dict(SLOT, reason="source_contains_parent")]
        native = {"replaced_count": 1, "replaced_slot_count": 0, "skipped": skipped, "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out["status"], "replaced")
        self.assertEqual(len(out["warnings"]), 1)
        warning = out["warnings"][0]
        self.assertIn("3 sub-material slot(s)", warning)
        self.assertIn("2 set_failed: the parent material did not accept the assignment", warning)
        self.assertIn("1 source_contains_parent", warning)


class SourceFromTests(unittest.TestCase):
    def test_payload_and_echo(self):
        native = {"source_material": "Oak", "target_material": "Oak", "source_from": "material_editor",
                  "source_exists": True, "source_found_in": "material_editor", "replaced_count": 3,
                  "replaced_slot_count": 0, "skipped": [], "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.replace_material("Oak", "Oak", source_from="material_editor"))
        self.assertEqual(json.loads(client.send_command.call_args.args[0])["source_from"], "material_editor")
        self.assertEqual(out, native)

    def test_default_payload_has_no_source_from(self):
        native = {"replaced_count": 1, "replaced_slot_count": 0, "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)) as client:
            mr.replace_material("A", "B")
        self.assertNotIn("source_from", json.loads(client.send_command.call_args.args[0]))

    def test_invalid_source_from_is_rejected_before_sending(self):
        client = _client(True, {})
        with patch.object(mr, "client", client):
            raw = mr.replace_material("A", "B", source_from="medit")
            batch_raw = mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                    {"source": "C", "target": "D", "source_from": "x"}])
        client.send_command.assert_not_called()
        self.assertEqual(json.loads(raw)["status"], "failed")
        self.assertIn("material_editor", json.loads(raw)["error"])
        self.assertIn("replacements[2].source_from", json.loads(batch_raw)["error"])
        self.assertFalse(envelope_result(raw, elapsed_ms=1.0)["ok"])

    def test_not_echoed_warns(self):
        native = {"source_material": "Oak", "target_material": "Pine", "source_exists": True,
                  "source_found_in": "node", "replaced_count": 1, "replaced_slot_count": 0, "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("Oak", "Pine", source_from="material_editor"))
        self.assertTrue(any("not echoed" in w for w in out["warnings"]))

    def test_same_name_without_source_from_warns(self):
        native = {"source_material": "Oak", "target_material": "Oak", "source_exists": True,
                  "source_found_in": "node", "replaced_count": 0, "replaced_slot_count": 0, "skipped": [],
                  "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("Oak", "Oak"))
        self.assertTrue(any("source_from" in w and "both named 'Oak'" in w for w in out["warnings"]))

    def test_ambiguous_warning_points_to_source_from(self):
        native = {"source_material": "Oak", "target_material": "Pine", "source_exists": True,
                  "source_found_in": "node", "source_ambiguous": True, "source_candidates": 2,
                  "replaced_count": 1, "replaced_slot_count": 0, "status": "replaced"}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("Oak", "Pine"))
        self.assertIn("Pass source_from", out["warnings"][0])

    def test_maxscript_filters_by_place(self):
        ms = {"source_from": "material_library", "source_exists": True, "replaced_count": 1,
              "replaced_slot_count": 0, "status": "success"}
        with patch.object(mr, "client", _client(False, ms)) as client:
            out = json.loads(mr.replace_material("A", "B", source_from="material_library"))
        script = client.send_command.call_args.args[0]
        self.assertIn('local srcFrom = "material_library"', script)
        for place in mr._SOURCE_FROM:
            self.assertIn(f'srcFrom == "{place}"', script)
        self.assertEqual(script.count("("), script.count(")"))
        self.assertNotIn("warnings", out)

    def test_maxscript_default_srcfrom_empty(self):
        with patch.object(mr, "client", _client(False, {"replaced_count": 1, "status": "success"})) as client:
            mr.replace_material("A", "B")
        self.assertIn('local srcFrom = ""', client.send_command.call_args.args[0])

    def test_batch_default_and_per_entry(self):
        native = {"results": [
            {"source_material": "A", "target_material": "B", "source_from": "material_library",
             "replaced_count": 1, "replaced_slot_count": 0, "status": "replaced"},
            {"source_material": "C", "target_material": "D", "source_from": "node",
             "replaced_count": 1, "replaced_slot_count": 0, "status": "replaced"},
        ], "total_replaced": 2, "preview": False, "dry_run": False}
        reps = [{"source": "A", "target": "B"}, {"source": "C", "target": "D", "source_from": "node"}]
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.batch_replace_materials(reps, source_from="material_library"))
        payload = json.loads(client.send_command.call_args.args[0])
        self.assertEqual(payload["source_from"], "material_library")
        self.assertEqual(payload["replacements"][1]["source_from"], "node")
        self.assertNotIn("warnings", out)

    def test_batch_maxscript_per_entry_source_from(self):
        client = MagicMock()
        client.native_available = False
        client.send_command.side_effect = [
            {"result": json.dumps({"source_from": "node", "source_exists": True, "replaced_count": 1,
                                   "status": "success"})},
            {"result": json.dumps({"source_exists": True, "replaced_count": 1, "status": "success"})},
        ]
        with patch.object(mr, "client", client):
            mr.batch_replace_materials([{"source": "A", "target": "B", "source_from": "node"},
                                        {"source": "C", "target": "D"}])
        scripts = [c.args[0] for c in client.send_command.call_args_list]
        self.assertIn('local srcFrom = "node"', scripts[0])
        self.assertIn('local srcFrom = ""', scripts[1])


class BatchOrderTests(unittest.TestCase):
    """Entries apply in order; preview cannot simulate that, so dependent entries are flagged."""

    def _preview(self, reps, results):
        native = {"results": results, "total_replaced": 0, "preview": True, "dry_run": False}
        with patch.object(mr, "client", _client(True, native)):
            return json.loads(mr.batch_replace_materials(reps, preview=True))

    @staticmethod
    def _entry(src, tgt, count=1, **extra):
        return dict({"source_material": src, "target_material": tgt, "source_exists": True,
                     "replaced_count": count, "replaced_slot_count": 0, "status": "preview"}, **extra)

    def test_reversed_pair_preview_is_flagged(self):
        reps = [{"source": "A", "target": "B"}, {"source": "B", "target": "A"}]
        out = self._preview(reps, [self._entry("A", "B"), self._entry("B", "A")])
        self.assertNotIn("depends_on_entries", out["results"][0])
        self.assertEqual(out["results"][1]["depends_on_entries"], [1])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("Entries 2", out["warnings"][0])
        self.assertIn("does not swap", out["warnings"][0])
        self.assertIn("preview", out["warnings"][0])

    def test_chain_and_repeated_target_flagged(self):
        reps = [{"source": "A", "target": "B"}, {"source": "B", "target": "C"},
                {"source": "D", "target": "B"}, {"source": "A", "target": "E"}]
        out = self._preview(reps, [self._entry("A", "B"), self._entry("B", "C"),
                                   self._entry("D", "B"), self._entry("A", "E")])
        deps = [r.get("depends_on_entries") for r in out["results"]]
        # 2: source B was entry 1's target; 3: target B was moved by entry 1 and
        # gains entry 2's users; 4: same source as entry 1 only, which is harmless.
        self.assertEqual(deps, [None, [1], [1, 2], None])
        self.assertIn("Entries 2, 3", out["warnings"][0])

    def test_independent_entries_not_flagged(self):
        reps = [{"source": "A", "target": "B"}, {"source": "A", "target": "C"}]
        out = self._preview(reps, [self._entry("A", "B"), self._entry("A", "C")])
        self.assertTrue(all("depends_on_entries" not in r for r in out["results"]))
        self.assertNotIn("warnings", out)

    def test_apply_flags_source_removed_by_earlier_entry(self):
        native = {"results": [
            dict(self._entry("A", "B"), status="replaced"),
            {"source_material": "B", "target_material": "C", "source_exists": False, "status": "error",
             "error": "source material not found"},
        ], "total_replaced": 1, "preview": False, "dry_run": False}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                         {"source": "B", "target": "C"}]))
        self.assertEqual(out["results"][1]["depends_on_entries"], [1])
        self.assertIn("ran on the scene left by earlier entries", out["warnings"][0])

    def test_errored_earlier_entry_is_not_a_dependency(self):
        reps = [{"source": "X", "target": "B"}, {"source": "A", "target": "B"}]
        out = self._preview(reps, [{"source_material": "X", "target_material": "B", "source_exists": False,
                                    "status": "error", "error": "source material not found"},
                                   self._entry("A", "B")])
        self.assertTrue(all("depends_on_entries" not in r for r in out["results"]))

    def test_maxscript_preview_reversed_pair_flagged(self):
        client = MagicMock()
        client.native_available = False
        client.send_command.side_effect = [
            {"result": json.dumps({"source_material": "A", "target_material": "B", "source_exists": True,
                                   "affected_count": 2, "affected_objects": [], "preview": True})},
            {"result": json.dumps({"source_material": "B", "target_material": "A", "source_exists": True,
                                   "affected_count": 3, "affected_objects": [], "preview": True})},
        ]
        with patch.object(mr, "client", client):
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                         {"source": "B", "target": "A"}], dry_run=True))
        self.assertEqual(out["results"][1]["depends_on_entries"], [1])
        self.assertTrue(any("does not swap" in w for w in out["warnings"]))


if __name__ == "__main__":
    unittest.main()
