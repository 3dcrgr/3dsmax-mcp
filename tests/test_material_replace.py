"""Tests for replace_material / batch_replace_materials.

Since the upstream 1.7.6 merge the result shape is upstream's (status rules,
replaced_count = objects + slots, source.found_in/candidates, per-entry
warnings, one MAXScript fallback call per batch). The fork adds the
direction hint on no_match, the blocked hint, include_sub_materials,
source_from (and the scene materials / material library as source places), a
preview that lists matches without a source, and depends_on_entries for the
fallback's preview.
"""

import json
import re
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
SLOT = {"parent_material": "Walls", "parent_class": "Multimaterial", "slot_index": 2, "slot_name": "(2)",
        "previous_material": "B", "object_count": 1}


def _client(native: bool, result) -> MagicMock:
    client = MagicMock()
    client.native_available = native
    raw = result if isinstance(result, str) else json.dumps(result)
    client.send_command.return_value = {"result": raw}
    return client


def _native_entry(src="A", tgt="B", status="replaced", objects=("Box001",), slots=(), skipped=(), **extra):
    entry = {"source_material": src, "target_material": tgt,
             "source": {"found_in": "object", "object": "Box009", "name": src, "class": "Standardmaterial",
                        "candidates": 1},
             "status": status, "preview": status == "preview",
             "match_count": len(objects) + len(slots) + len(skipped),
             "replaced_count": len(objects) + len(slots), "reassigned_count": len(objects),
             "replaced_objects": list(objects), "replaced_slot_count": len(slots), "replaced_slots": list(slots),
             "skipped_count": len(skipped), "skipped": list(skipped),
             "affected_count": len(objects), "affected_objects": list(objects),
             "include_sub_materials": True}
    entry.update(extra)
    return entry


def _payload(client, index=-1):
    return json.loads(client.send_command.call_args_list[index].args[0])


class ReplaceMaterialNativeTests(unittest.TestCase):
    def test_success_unchanged(self):
        native = _native_entry()
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out, native)
        self.assertEqual(client.send_command.call_args.kwargs["cmd_type"], "native:replace_material")

    def test_no_match_gets_direction_hint(self):
        native = _native_entry(HEBREW, PLASTER, status="no_match", objects=(),
                               warnings=[f"No material named '{PLASTER}' exists on objects, in their "
                                         "sub-material slots or in the Material Editor."])
        with patch.object(mr, "client", _client(True, native)):
            raw = mr.replace_material(source_material=HEBREW, target_material=PLASTER)
        out = json.loads(raw)
        self.assertEqual(out["status"], "no_match")
        self.assertEqual(len(out["warnings"]), 2)
        hint = out["warnings"][1]
        for text in (PLASTER, "swap", "assign_material", "source = material to apply", "Material Editor slot"):
            self.assertIn(text, hint)
        env = envelope_result(raw, elapsed_ms=1.0, tool_name="replace_material")
        self.assertTrue(env["ok"])
        self.assertEqual(env["warnings"], out["warnings"])

    def test_unicode_names_round_trip_in_payload(self):
        with patch.object(mr, "client", _client(True, _native_entry(PLASTER, HEBREW))) as client:
            mr.replace_material(source_material=PLASTER, target_material=HEBREW)
        payload = client.send_command.call_args.args[0]
        self.assertTrue(payload.isascii())
        decoded = json.loads(payload)
        self.assertEqual(decoded["source_material"], PLASTER)
        self.assertEqual(decoded["target_material"], HEBREW)

    def test_non_json_result_passthrough(self):
        with patch.object(mr, "client", _client(True, "not json")):
            self.assertEqual(mr.replace_material("A", "B"), "not json")

    def test_slot_only_replacement_unchanged(self):
        native = _native_entry(objects=(), slots=(SLOT,))
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out, native)

    def test_preview_missing_source_lists_matches(self):
        native = _native_entry(status="preview", objects=("Box001", "Box002"), slots=(SLOT,), source_exists=False)
        del native["source"]
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B", preview=True))
        self.assertEqual(out["replaced_slots"], [SLOT])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'A'", out["warnings"][0])
        self.assertIn("real run would fail", out["warnings"][0])


class IncludeSubMaterialsTests(unittest.TestCase):
    def test_payload(self):
        with patch.object(mr, "client", _client(True, _native_entry())) as client:
            mr.replace_material("A", "B")
            self.assertIs(_payload(client)["include_sub_materials"], True)
            mr.replace_material("A", "B", include_sub_materials=False)
            self.assertIs(_payload(client)["include_sub_materials"], False)

    def test_echoed_false_has_no_warning(self):
        native = _native_entry(include_sub_materials=False)
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B", include_sub_materials=False))
        self.assertNotIn("warnings", out)

    def test_not_echoed_warns(self):
        # A stock upstream bridge always searches sub-material slots.
        native = _native_entry(slots=(SLOT,))
        del native["include_sub_materials"]
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("A", "B", include_sub_materials=False))
        self.assertTrue(any("include_sub_materials=False was not echoed" in w for w in out["warnings"]))


class BlockedTests(unittest.TestCase):
    """All matches skipped: "blocked", never the swap hint."""

    def test_blocked_hint(self):
        skipped = [dict(SLOT, kind="sub_material", reason="would_create_loop"),
                   dict(SLOT, kind="sub_material", reason="would_create_loop"),
                   dict(SLOT, kind="sub_material", reason="set_rejected")]
        native = _native_entry("Walls", "Plaster", status="blocked", objects=(), skipped=skipped,
                               warnings=["Every match of 'Plaster' was skipped, so nothing was replaced; "
                                         "see skipped for reasons."])
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("Walls", "Plaster"))
        self.assertEqual(out["status"], "blocked")
        text = " ".join(out["warnings"])
        self.assertNotIn("if reversed", text)
        self.assertIn("not reversed", text)
        self.assertIn("2 would_create_loop, 1 set_rejected", text)

    def test_batch_blocked_entry_hint_stays_on_entry(self):
        native = {"results": [_native_entry("Walls", "Brick", status="blocked", objects=(),
                                            skipped=[dict(SLOT, reason="parent_is_source")])],
                  "total_replaced": 0, "status": "no_changes", "preview": False, "dry_run": False,
                  "include_sub_materials": True}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.batch_replace_materials([{"source": "Walls", "target": "Brick"}]))
        self.assertIn("not reversed", out["results"][0]["warnings"][-1])
        self.assertNotIn("warnings", out)


class SourceFromTests(unittest.TestCase):
    def test_payload_and_echo(self):
        native = _native_entry("Oak", "Oak", source_from="material_editor")
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.replace_material("Oak", "Oak", source_from="material_editor"))
        self.assertEqual(_payload(client)["source_from"], "material_editor")
        self.assertEqual(out, native)

    def test_object_alias_sent_as_node(self):
        native = _native_entry(source_from="node")
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.replace_material("A", "B", source_from="object"))
        self.assertEqual(_payload(client)["source_from"], "node")
        self.assertNotIn("warnings", out)

    def test_default_payload_has_no_source_from(self):
        with patch.object(mr, "client", _client(True, _native_entry())) as client:
            mr.replace_material("A", "B")
        self.assertNotIn("source_from", _payload(client))

    def test_invalid_source_from_is_rejected_before_sending(self):
        client = _client(True, {})
        with patch.object(mr, "client", client):
            raw = mr.replace_material("A", "B", source_from="medit")
            entry_raw = mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                    {"source": "C", "target": "D", "source_from": "x"}])
            default_raw = mr.batch_replace_materials([{"source": "A", "target": "B"}], source_from="x")
        client.send_command.assert_not_called()
        self.assertEqual(json.loads(raw)["status"], "failed")
        self.assertIn("material_editor", json.loads(raw)["error"])
        self.assertIn("replacements[2].source_from", json.loads(entry_raw)["error"])
        self.assertIn("source_from", json.loads(default_raw)["error"])
        self.assertFalse(envelope_result(raw, elapsed_ms=1.0)["ok"])

    def test_not_echoed_warns(self):
        with patch.object(mr, "client", _client(True, _native_entry("Oak", "Pine"))):
            out = json.loads(mr.replace_material("Oak", "Pine", source_from="material_editor"))
        self.assertTrue(any("not echoed" in w for w in out["warnings"]))

    def test_same_name_without_source_from_warns(self):
        native = _native_entry("Oak", "Oak", status="unchanged", objects=(),
                               skipped=[{"kind": "object", "object": "Box001", "reason": "already_source"}])
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("Oak", "Oak"))
        self.assertTrue(any("source_from" in w and "both named 'Oak'" in w for w in out["warnings"]))

    def test_ambiguous_source_points_to_source_from(self):
        native = _native_entry("Oak", "Pine", warnings=["2 materials are named 'Oak'; used the one on object "
                                                        "'Box009'."])
        native["source"]["candidates"] = 2
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.replace_material("Oak", "Pine"))
        self.assertEqual(len(out["warnings"]), 2)
        self.assertIn("Pass source_from", out["warnings"][1])

    def test_batch_default_and_per_entry(self):
        native = {"results": [_native_entry("A", "B", source_from="material_library"),
                              _native_entry("C", "D", source_from="node")],
                  "total_replaced": 2, "status": "replaced", "preview": False, "dry_run": False,
                  "include_sub_materials": True}
        reps = [{"source": "A", "target": "B"}, {"source": "C", "target": "D", "source_from": "object"}]
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.batch_replace_materials(reps, source_from="material_library"))
        payload = _payload(client)
        self.assertEqual(payload["source_from"], "material_library")
        self.assertEqual(payload["replacements"][1]["source_from"], "node")
        self.assertNotIn("source_from", payload["replacements"][0])
        self.assertEqual(reps[1]["source_from"], "object")  # caller's entry not mutated
        self.assertNotIn("warnings", out)

    def test_batch_not_echoed_warns_once(self):
        native = {"results": [_native_entry("A", "B"), _native_entry("C", "D")],
                  "total_replaced": 2, "status": "replaced", "preview": False, "dry_run": False}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                         {"source": "C", "target": "D"}],
                                                        source_from="material_editor"))
        self.assertEqual(sum("not echoed" in w for w in out["warnings"]), 1)


class NativeBatchTests(unittest.TestCase):
    def test_mixed_results_one_direction_hint(self):
        native = {
            "results": [
                _native_entry("A", "B"),
                _native_entry("A", "C", status="no_match", objects=(),
                              warnings=["No material named 'C' exists on objects, in their sub-material "
                                        "slots or in the Material Editor."]),
                _native_entry("A", "D", status="no_match", objects=()),
                {"source_material": "X", "target_material": "B", "status": "error",
                 "error": "Source material 'X' not found on objects, in sub-material slots or in the "
                          "Material Editor"},
            ],
            "total_replaced": 1, "total_skipped": 0, "error_count": 1, "status": "replaced",
            "preview": False, "dry_run": False, "include_sub_materials": True,
        }
        reps = [{"source": "A", "target": t} for t in "BCD"] + [{"source": "X", "target": "B"}]
        with patch.object(mr, "client", _client(True, native)) as client:
            raw = mr.batch_replace_materials(reps)
        out = json.loads(raw)
        self.assertEqual(client.send_command.call_args.kwargs["cmd_type"], "native:batch_replace_materials")
        self.assertIs(_payload(client)["include_sub_materials"], True)
        self.assertEqual([r["status"] for r in out["results"]], ["replaced", "no_match", "no_match", "error"])
        self.assertEqual(len(out["results"][1]["warnings"]), 1)  # native's own warning stays on the entry
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'C', 'D'", out["warnings"][0])
        self.assertIn("swap", out["warnings"][0])
        self.assertNotIn("'B'", out["warnings"][0])
        env = envelope_result(raw, elapsed_ms=1.0, tool_name="batch_replace_materials")
        self.assertTrue(env["ok"])

    def test_native_preview_chains_without_dependency_flags(self):
        # The native handler plans a preview chain through its overlay, so the
        # fallback's depends_on_entries is not added.
        native = {"results": [_native_entry("A", "B", status="preview"), _native_entry("B", "A", status="preview")],
                  "total_replaced": 2, "status": "preview", "preview": True, "dry_run": False,
                  "include_sub_materials": True}
        with patch.object(mr, "client", _client(True, native)):
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"},
                                                         {"source": "B", "target": "A"}], preview=True))
        self.assertTrue(all("depends_on_entries" not in r for r in out["results"]))
        self.assertNotIn("warnings", out)

    def test_include_false_not_echoed_warns(self):
        native = {"results": [_native_entry()], "total_replaced": 1, "status": "replaced",
                  "preview": False, "dry_run": False}
        with patch.object(mr, "client", _client(True, native)) as client:
            out = json.loads(mr.batch_replace_materials([{"source": "A", "target": "B"}],
                                                        include_sub_materials=False))
        self.assertIs(_payload(client)["include_sub_materials"], False)
        self.assertTrue(any("include_sub_materials=False" in w for w in out["warnings"]))


def _strip_strings(script):
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', script)


def _fb(found=True, objects=(), slots=(), skipped=(), match=None, target_exists=None, candidates=1,
        found_in="object", affected=None):
    item = {"source_found": found,
            "source": {"name": "A", "class": "Standardmaterial", "found_in": found_in} if found else None,
            "source_candidates": candidates if found else 0,
            "match_count": len(objects) + len(slots) + len(skipped) if match is None else match,
            "target_exists": bool(objects or slots or skipped) if target_exists is None else target_exists,
            "replaced_objects": list(objects), "replaced_slots": list(slots), "skipped": list(skipped),
            "affected_objects": list(objects if affected is None else affected)}
    return item


class FallbackScriptTests(unittest.TestCase):
    def _script(self, **kwargs):
        with patch.object(mr, "client", _client(False, json.dumps([_fb(objects=["Box001"])]))) as client:
            mr.replace_material(**kwargs)
        return client.send_command.call_args.args[0]

    def test_script_embeds_names_flags_and_places(self):
        name = 'Wall "A" \\ ' + HEBREW
        script = self._script(source_material=name, target_material="B", include_sub_materials=False,
                              source_from="material_library")
        self.assertIn('#("Wall \\"A\\" \\\\ ' + HEBREW + '", "B", "material_library")', script)
        self.assertIn("mcpReplaceOne e[1] e[2] true e[3] false", script)
        self.assertIn('undo "MCP replace_material" on (', script)
        for token in ("meditMaterials", "sceneMaterials", "currentMaterialLibrary", "setSubMtl",
                      "refs.dependencyLoopTest", 'srcFrom == "node"'):
            self.assertIn(token, script)
        bare = _strip_strings(script)
        self.assertEqual(bare.count("("), bare.count(")"))
        self.assertNotIn("__ENTRIES__", script)
        self.assertNotIn("__RUN__", script)

    def test_preview_runs_outside_undo(self):
        script = self._script(source_material="A", target_material="B", preview=True)
        self.assertIn("mcpReplaceOne e[1] e[2] false e[3] true", script)
        self.assertNotIn("undo ", script)
        self.assertIn('#("A", "B", "")', script)

    def test_object_alias_becomes_node(self):
        self.assertIn('#("A", "B", "node")', self._script(source_material="A", target_material="B",
                                                          source_from="object"))

    def test_no_for_collect_in_call_arguments(self):
        # Fork rule, live: a for-collect read inside a call's arguments failed with
        # "Bad free thunk local member index"; collect into a local first.
        allowed = re.compile(r"(?:.*\b(?:do|then|else)\s+)?(?:local\s+)?\w+\s*=")
        script = self._script(source_material="A", target_material="B")
        for line in script.split("\n"):
            code = _strip_strings(line.split("--")[0])
            for match in re.finditer(r"\bfor\b", code):
                if re.search(r"\bcollect\b", code[match.end():]):
                    prefix = code[:match.start()].strip()
                    self.assertTrue(prefix == "" or allowed.fullmatch(prefix), line.strip())


class FallbackResultTests(unittest.TestCase):
    def _run(self, items, *args, **kwargs):
        with patch.object(mr, "client", _client(False, json.dumps(items))) as client:
            raw = mr.replace_material(*args, **kwargs)
        self.assertNotIn("cmd_type", client.send_command.call_args.kwargs)
        return raw, json.loads(raw)

    def test_replaced_objects_and_slots(self):
        _, out = self._run([_fb(objects=["Box001"], slots=[SLOT], affected=["Box001", "Box002"])], "A", "B")
        self.assertEqual(out["status"], "replaced")
        self.assertEqual(out["replaced_count"], 2)
        self.assertEqual(out["reassigned_count"], 1)
        self.assertEqual(out["replaced_slots"], [SLOT])
        self.assertEqual(out["source"]["found_in"], "object")
        self.assertTrue(out["include_sub_materials"])
        self.assertNotIn("warnings", out)

    def test_no_match_has_both_warnings(self):
        raw, out = self._run([_fb(match=0, target_exists=False)], "A", "B")
        self.assertEqual(out["status"], "no_match")
        self.assertEqual(len(out["warnings"]), 2)
        self.assertIn("swap", out["warnings"][1])
        self.assertTrue(envelope_result(raw, elapsed_ms=1.0)["ok"])

    def test_no_match_mentions_disabled_sub_materials(self):
        _, out = self._run([_fb(match=0, target_exists=False)], "A", "B", include_sub_materials=False)
        self.assertIn("include_sub_materials=False", out["warnings"][0])
        self.assertFalse(out["include_sub_materials"])

    def test_no_objects_and_unchanged(self):
        _, out = self._run([_fb(match=0, target_exists=True)], "A", "B")
        self.assertEqual(out["status"], "no_objects")
        self.assertIn("arguments are reversed", out["warnings"][-1])
        _, out = self._run([_fb(skipped=[{"kind": "object", "object": "Box001", "reason": "already_source"}])],
                           "A", "B")
        self.assertEqual(out["status"], "unchanged")

    def test_blocked_gets_fork_hint(self):
        _, out = self._run([_fb(skipped=[dict(SLOT, kind="sub_material", reason="would_create_loop")])], "A", "B")
        self.assertEqual(out["status"], "blocked")
        self.assertIn("not reversed", out["warnings"][-1])

    def test_source_from_echoed_by_fallback(self):
        _, out = self._run([_fb(objects=["Box001"], found_in="material_editor")], "A", "B",
                           source_from="material_editor")
        self.assertEqual(out["source_from"], "material_editor")
        self.assertNotIn("warnings", out)

    def test_ambiguous_source(self):
        _, out = self._run([_fb(objects=["Box001"], candidates=3, found_in="scene_materials")], "A", "B")
        self.assertIn("3 materials are named 'A'; used the one found in the scene materials.", out["warnings"][0])
        self.assertIn("Pass source_from", out["warnings"][1])

    def test_missing_source_fails_real_run(self):
        raw, out = self._run([{"source_found": False}], "A", "B")
        self.assertEqual(out["status"], "failed")
        self.assertIn("Source material 'A' not found", out["error"])
        self.assertFalse(envelope_result(raw, elapsed_ms=1.0)["ok"])

    def test_missing_source_with_source_from_names_place(self):
        _, out = self._run([{"source_found": False}], "A", "B", source_from="material_editor")
        self.assertIn("source_from=material_editor", out["error"])

    def test_preview_missing_source_lists_matches(self):
        _, out = self._run([_fb(found=False, objects=["Box001"], slots=[SLOT])], "A", "B", preview=True)
        self.assertEqual(out["status"], "preview")
        self.assertIs(out["source_exists"], False)
        self.assertEqual(out["replaced_objects"], ["Box001"])
        self.assertTrue(any("real run would fail" in w for w in out["warnings"]))

    def test_unexpected_result_fails(self):
        with patch.object(mr, "client", _client(False, "garbage")):
            out = json.loads(mr.replace_material("A", "B"))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["raw"], "garbage")


class FallbackBatchTests(unittest.TestCase):
    def _batch(self, reps, items, **kwargs):
        client = _client(False, json.dumps(items))
        with patch.object(mr, "client", client):
            out = json.loads(mr.batch_replace_materials(reps, **kwargs))
        self.assertEqual(client.send_command.call_count, 1)  # one script, in order, one undo
        return out, client.send_command.call_args.args[0]

    def test_mixed_results_and_totals(self):
        reps = [{"source": "A", "target": "B"}, {"source": "A", "target": "C"}, {"source": "A"},
                {"source": "X", "target": "B", "source_from": "material_editor"}]
        items = [_fb(objects=["Box001", "Box002"], slots=[SLOT]), _fb(match=0, target_exists=False),
                 {"source_found": False}]
        out, script = self._batch(reps, items, include_sub_materials=False)
        self.assertIn('#("A", "B", ""), #("A", "C", ""), #("X", "B", "material_editor")', script)
        self.assertIn("e[3] false)", script)
        self.assertEqual([r["status"] for r in out["results"]], ["replaced", "no_match", "error", "error"])
        self.assertEqual(out["results"][2]["error"], "missing source or target")
        self.assertEqual(out["total_replaced"], 3)
        self.assertEqual(out["error_count"], 2)
        self.assertEqual(out["status"], "replaced")
        self.assertFalse(out["include_sub_materials"])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'C'", out["warnings"][0])

    def test_preview_missing_source_is_error_entry(self):
        out, _ = self._batch([{"source": "X", "target": "B"}],
                             [_fb(found=False, objects=["Box001"])], preview=True)
        self.assertEqual(out["results"][0]["status"], "error")
        self.assertIs(out["results"][0]["source_exists"], False)
        self.assertEqual(out["total_replaced"], 0)
        self.assertEqual(out["status"], "no_changes")

    def test_preview_reversed_pair_flagged(self):
        out, script = self._batch([{"source": "A", "target": "B"}, {"source": "B", "target": "A"}],
                                  [_fb(objects=["Box001"]), _fb(objects=["Box002"])], dry_run=True)
        self.assertNotIn("undo ", script)
        self.assertTrue(out["preview"] and out["dry_run"])
        self.assertNotIn("depends_on_entries", out["results"][0])
        self.assertEqual(out["results"][1]["depends_on_entries"], [1])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("Entries 2", out["warnings"][0])
        self.assertIn("does not swap", out["warnings"][0])

    def test_preview_chain_and_repeated_target_flagged(self):
        reps = [{"source": "A", "target": "B"}, {"source": "B", "target": "C"},
                {"source": "D", "target": "B"}, {"source": "A", "target": "E"}]
        out, _ = self._batch(reps, [_fb(objects=["Box001"])] * 4, preview=True)
        deps = [r.get("depends_on_entries") for r in out["results"]]
        # 2: source B was entry 1's target; 3: target B was moved by entry 1 and
        # gains entry 2's users; 4: same source as entry 1 only, which is harmless.
        self.assertEqual(deps, [None, [1], [1, 2], None])
        self.assertIn("Entries 2, 3", out["warnings"][0])

    def test_preview_independent_entries_get_upstream_note(self):
        out, _ = self._batch([{"source": "A", "target": "B"}, {"source": "A", "target": "C"}],
                             [_fb(objects=["Box001"]), _fb(objects=["Box002"])], preview=True)
        self.assertTrue(all("depends_on_entries" not in r for r in out["results"]))
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("previews each entry against the current scene", out["warnings"][0])

    def test_errored_earlier_entry_is_not_a_dependency(self):
        out, _ = self._batch([{"source": "X", "target": "B"}, {"source": "A", "target": "B"}],
                             [{"source_found": False}, _fb(objects=["Box001"])], preview=True)
        self.assertTrue(all("depends_on_entries" not in r for r in out["results"]))

    def test_real_run_not_flagged(self):
        out, script = self._batch([{"source": "A", "target": "B"}, {"source": "B", "target": "A"}],
                                  [_fb(objects=["Box001"]), _fb(objects=["Box002"])])
        self.assertIn('undo "MCP replace_material" on (', script)
        self.assertTrue(all("depends_on_entries" not in r for r in out["results"]))
        self.assertNotIn("warnings", out)


if __name__ == "__main__":
    unittest.main()
