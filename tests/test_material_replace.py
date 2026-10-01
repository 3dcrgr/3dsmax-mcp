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


if __name__ == "__main__":
    unittest.main()
