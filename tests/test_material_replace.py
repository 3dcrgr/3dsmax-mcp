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


if __name__ == "__main__":
    unittest.main()
