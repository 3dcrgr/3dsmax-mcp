"""Tests for scene_qa duplicate_group_heads (#16): stacked copies of whole groups."""

import base64
import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sq = importlib.import_module("maxmcp.tools.scene_qa")

IDENT = (1, 0, 0, 0, 1, 0, 0, 0, 1)
ROT90 = (0, 1, 0, -1, 0, 0, 0, 0, 1)
SCALE2 = (2, 0, 0, 0, 2, 0, 0, 0, 2)


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def h_rec(handle, name, pos=(0.0, 0.0, 0.0), parent=0, children=3, objects=4,
          in_scope=1, layer="0", nid=1, eid=None, rows=IDENT):
    x, y, z = pos
    rows_s = ",".join(f"{v:.6f}" for v in rows)
    eid = nid if eid is None else eid
    return (f"H|{handle}|{parent}|{children}|{objects}|{x:.6f}|{y:.6f}|{z:.6f}|{rows_s}"
            f"|{in_scope}|{nid}|{eid}|{b64(name)}|{b64(layer)}")


def n_rec(nid, tokens):
    return f"N|{nid}|{b64(''.join(t + chr(10) for t in sorted(tokens)))}"


def report(*records, heads=10, truncated=False, names_truncated=False):
    lines = [f"G|{heads}", *records]
    if truncated:
        lines.append("T|heads")
    if names_truncated:
        lines.append("T|names")
    return "\n".join(lines) + "\nEND"


def find_sets(raw, tol=0.001):
    return sq.find_duplicate_group_sets(sq.parse_duplicate_group_output(raw), tol)


def handles_of(found):
    return [[h["handle"] for h in s] for s in found["sets"]]


NATIVE_ISSUE = {"code": "empty_group", "severity": "warning",
                "message": "Group head has no children.",
                "node": {"name": "G0", "handle": 9, "class": "Dummy", "layer": "0"},
                "details": {"repairable": False}}


def native_scan(issues=None):
    issues = list(issues if issues is not None else [NATIVE_ISSUE])
    return {
        "scene_seq": 7, "scanned_nodes": 100,
        "checks": ["group_integrity", "name_collisions"],
        "mesh_checks_included": False,
        "summary": {"issue_count": len(issues),
                    "by_code": {i["code"]: 1 for i in issues},
                    "by_severity": {"warning": len(issues)} if issues else {}},
        "issues": issues, "truncated": False, "scope": "scene", "action": "scan",
    }


def make_client(native_result, ms_result, native=True):
    client = MagicMock()
    client.native_available = native

    def send(command, cmd_type="maxscript", timeout=None, **_):
        if cmd_type.startswith("native:"):
            text = native_result if isinstance(native_result, str) else json.dumps(native_result)
            return {"result": text}
        if isinstance(ms_result, Exception):
            raise ms_result
        return {"result": ms_result}

    client.send_command.side_effect = send
    return client


PROPS = ["Box/prop_a", "Box/prop_b", "Box/prop_c"]
# Clones renamed by Max (Prop -> Prop001): same normalized names, new exact ids.
VITRINE = report(
    n_rec(1, PROPS),
    h_rec(100, "GRP_Vitrine_Props", (10, 20, 0), objects=104, nid=1, eid=1),
    h_rec(200, "GRP_Vitrine_Props001", (10, 20, 0), objects=104, nid=1, eid=2),
    h_rec(300, "GRP_Vitrine_Props002", (10.0004, 20, 0), objects=104, nid=1, eid=3),
    h_rec(400, "GRP_Vitrine_Elsewhere", (500, 20, 0), objects=104, nid=1, eid=1),
)


class ImportTargetTests(unittest.TestCase):
    def test_imports_checkout(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(Path(sq.__file__).resolve(), repo / "maxmcp" / "tools" / "scene_qa.py")


class ParseAndClusterTests(unittest.TestCase):
    def test_parse_records(self):
        parsed = sq.parse_duplicate_group_output(VITRINE)
        self.assertEqual(parsed["group_heads_scanned"], 10)
        self.assertEqual(len(parsed["candidates"]), 4)
        first = parsed["candidates"][0]
        self.assertEqual(first["name"], "GRP_Vitrine_Props")
        self.assertEqual(first["rows"], list(map(float, IDENT)))
        self.assertEqual(sorted(parsed["names"][1].elements()), PROPS)
        self.assertFalse(parsed["truncated"])
        self.assertFalse(parsed["names_truncated"])

    def test_parse_truncation_markers(self):
        parsed = sq.parse_duplicate_group_output(report(truncated=True, names_truncated=True))
        self.assertTrue(parsed["truncated"])
        self.assertTrue(parsed["names_truncated"])

    def test_incomplete_or_error_report_raises(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            sq.parse_duplicate_group_output("G|3\nH|1")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            sq.parse_duplicate_group_output("__ERROR__|boom")
        with self.assertRaisesRegex(RuntimeError, "transform"):
            sq.parse_duplicate_group_output(
                report(h_rec(1, "A").replace("1.000000,0.000000,0.000000,", "", 1)))

    def test_position_tolerance(self):
        self.assertEqual(handles_of(find_sets(VITRINE, 0.001)), [[100, 200, 300]])
        self.assertEqual(handles_of(find_sets(VITRINE, 0.0001)), [[100, 200]])

    def test_renamed_children_match_by_normalized_names(self):
        sets = find_sets(VITRINE)["sets"]
        self.assertEqual(sq._name_match(sets[0]), "normalized")
        exact = report(h_rec(1, "A", nid=1, eid=1), h_rec(2, "A001", nid=1, eid=1))
        self.assertEqual(sq._name_match(find_sets(exact)["sets"][0]), "exact")

    def test_different_children_not_matched(self):
        raw = report(n_rec(1, PROPS), n_rec(2, ["Box/other_x", "Box/other_y", "Box/other_z"]),
                     h_rec(1, "A", nid=1), h_rec(2, "B", nid=2))
        self.assertEqual(find_sets(raw)["sets"], [])

    def test_rotated_or_scaled_copies_not_flagged(self):
        raw = report(h_rec(1, "Blade"), h_rec(2, "Blade001", rows=ROT90),
                     h_rec(3, "Blade002", rows=SCALE2))
        found = find_sets(raw)
        self.assertEqual(found["sets"], [])
        self.assertEqual(found["transform_mismatch_heads"], 3)
        raw2 = report(h_rec(1, "Blade"), h_rec(2, "Blade001"), h_rec(3, "Blade002", rows=ROT90))
        found2 = find_sets(raw2)
        self.assertEqual(handles_of(found2), [[1, 2]])
        self.assertEqual(found2["transform_mismatch_heads"], 1)

    def test_copies_with_one_child_detached_are_partial_duplicates(self):
        children = [f"Mesh/prop_{i}" for i in range(16)]
        records = [n_rec(1, children), h_rec(100, "GRP", children=16, objects=17, nid=1)]
        for i in range(16):
            records.append(n_rec(2 + i, children[:i] + children[i + 1:]))
            records.append(h_rec(200 + i, f"GRP{i + 1:03d}", children=15, objects=16,
                                 nid=2 + i))
        found = find_sets(report(*records))
        self.assertEqual(len(found["sets"]), 1)
        heads = found["sets"][0]
        self.assertEqual(len(heads), 17)
        self.assertEqual(heads[0]["handle"], 100)
        self.assertEqual(sq._name_match(heads), "partial")
        issue = sq._duplicate_issue(heads)
        self.assertEqual(issue["details"]["extra_objects"], 16 * 16)
        self.assertEqual(issue["details"]["name_match"], "partial")
        self.assertIn("compare the members", issue["details"]["suggested_fix"])

    def test_partial_match_needs_child_names(self):
        records = [n_rec(1, PROPS), h_rec(1, "A", nid=1), h_rec(2, "A001", children=2, nid=2)]
        raw = report(*records, names_truncated=True)
        self.assertEqual(find_sets(raw)["sets"], [])
        raw_ok = report(*records, n_rec(2, PROPS[:2]))
        self.assertEqual(handles_of(find_sets(raw_ok)), [[1, 2]])

    def test_partial_match_threshold(self):
        raw = report(n_rec(1, PROPS), n_rec(2, ["Box/prop_a"]),
                     h_rec(1, "A", nid=1), h_rec(2, "B", children=1, nid=2))
        self.assertEqual(find_sets(raw)["sets"], [])

    def test_ancestor_head_never_matches_its_descendant(self):
        raw = report(n_rec(1, ["Dummy/inner", "Box/a"]), n_rec(2, ["Dummy/inner"]),
                     h_rec(10, "Outer", children=2, nid=1),
                     h_rec(11, "Inner", parent=10, children=1, nid=2))
        self.assertEqual(find_sets(raw)["sets"], [])

    def test_nested_duplicates_are_implied_by_outer_set(self):
        raw = report(
            h_rec(10, "Outer", objects=20, nid=1),
            h_rec(11, "Outer001", objects=20, nid=1),
            h_rec(12, "Inner", (5, 0, 0), parent=10, objects=5, nid=2),
            h_rec(13, "Inner001", (5, 0, 0), parent=11, objects=5, nid=2),
        )
        found = find_sets(raw)
        self.assertEqual(handles_of(found), [[10, 11]])
        self.assertEqual(found["nested_sets_implied"], 1)

    def test_duplicates_inside_one_group_are_not_implied(self):
        raw = report(
            h_rec(10, "Outer", objects=20, nid=1),
            h_rec(11, "Outer001", objects=20, nid=1),
            h_rec(12, "Inner", (5, 0, 0), parent=10, objects=5, nid=2),
            h_rec(13, "Inner001", (5, 0, 0), parent=10, objects=5, nid=2),
        )
        found = find_sets(raw)
        self.assertEqual(len(found["sets"]), 2)
        self.assertEqual(found["nested_sets_implied"], 0)

    def test_out_of_scope_sets_dropped(self):
        raw = report(h_rec(10, "A", in_scope=0), h_rec(11, "A001", in_scope=0))
        self.assertEqual(find_sets(raw)["sets"], [])

    def test_script_embeds_targets_and_is_read_only(self):
        script = sq.build_duplicate_group_script("targets", [5, 6], ['Wall "A"'])
        self.assertIn("#(5, 6)", script)
        self.assertIn('#("Wall \\"A\\"")', script)
        self.assertIn('local scopeMode = "targets"', script)
        self.assertIn("local cell = 0.010000", script)
        self.assertNotIn("__", script.replace("__ERROR__", ""))
        for word in ("delete ", "ungroup", "explodeGroup", "setGroupMember", ".parent =",
                     ".name =", "setName"):
            self.assertNotIn(word, script)

    def test_script_normalizes_names_and_walks_ancestor_heads(self):
        script = sq.build_duplicate_group_script("scene", tolerance=1.0)
        self.assertIn("local cell = 4.000000", script)
        self.assertIn('trimRight (nm as string) "0123456789"', script)
        self.assertIn('"_mcp"', script)
        self.assertIn("toLower", script)
        self.assertIn("fn mcpNearestHead", script)
        self.assertIn("(mcpNearestHead h)", script)
        self.assertIn("mcpRows h.transform", script)
        self.assertGreater(sq.position_cell(0.3), 2 * 0.3)

    def test_target_refs(self):
        hs, ns = sq._target_refs(["N"], [1], [{"handle": 2}, {"name": "R"}, {"path": "/a/b"}])
        self.assertEqual(hs, [1, 2])
        self.assertEqual(ns, ["N", "R"])


class ToolMergeTests(unittest.TestCase):
    def run_tool(self, native_result, ms_result, **kwargs):
        client = make_client(native_result, ms_result)
        with patch.object(sq, "client", client):
            out = sq.scene_qa(**kwargs)
        return out, client

    def test_duplicates_merged_into_scan(self):
        out, client = self.run_tool(native_scan(), VITRINE)
        result = json.loads(out)
        self.assertEqual(result["issues"][0], NATIVE_ISSUE)
        dup = result["issues"][1]
        self.assertEqual(dup["code"], "duplicate_group_heads")
        self.assertEqual(dup["severity"], "warning")
        self.assertEqual(dup["node"]["handle"], 100)
        details = dup["details"]
        self.assertEqual([h["name"] for h in details["heads"]],
                         ["GRP_Vitrine_Props", "GRP_Vitrine_Props001", "GRP_Vitrine_Props002"])
        self.assertEqual(details["head_count"], 3)
        self.assertEqual(details["child_count"], 3)
        self.assertEqual(details["name_match"], "normalized")
        self.assertEqual(details["object_total"], 312)
        self.assertEqual(details["extra_objects"], 208)
        self.assertFalse(details["repairable"])
        self.assertIn("never deletes", details["suggested_fix"])
        self.assertEqual(result["summary"]["issue_count"], 2)
        self.assertEqual(result["summary"]["by_code"]["duplicate_group_heads"], 1)
        self.assertEqual(result["summary"]["by_severity"]["warning"], 2)
        self.assertIn("duplicate_group_heads", result["checks"])
        info = result["duplicate_group_heads"]
        self.assertEqual(info["sets_found"], 1)
        self.assertEqual(info["sets_listed"], 1)
        self.assertEqual(info["sets_by_name_match"], {"normalized": 1})
        self.assertEqual(info["extra_heads"], 2)
        self.assertTrue(info["report_only"])
        # Native payload never sees the Python-side token.
        native_call = [c for c in client.send_command.call_args_list
                       if c.kwargs.get("cmd_type", "").startswith("native:")][0]
        self.assertNotIn("checks", json.loads(native_call.args[0]))
        ms_call = [c for c in client.send_command.call_args_list
                   if not c.kwargs.get("cmd_type")][0]
        self.assertIn("local cell = 0.010000", ms_call.args[0])

    def test_output_keeps_utf8_and_compact_encoding(self):
        issue = {**NATIVE_ISSUE, "node": {**NATIVE_ISSUE["node"], "name": "כיסא"}}
        raw = report(h_rec(1, "קבוצה"), h_rec(2, "קבוצה001"))
        for ms in (raw, RuntimeError("x")):
            out, _ = self.run_tool(native_scan([issue]), ms)
            self.assertIn("כיסא", out)
            self.assertNotIn("\\u", out)
            self.assertNotIn('": ', out)
        out, _ = self.run_tool(native_scan([issue]), raw)
        self.assertIn("קבוצה001", out)

    def test_no_duplicates_adds_no_issues(self):
        raw = report(h_rec(10, "A", (0, 0, 0)), h_rec(11, "B", (100, 0, 0)))
        base = native_scan()
        out, _ = self.run_tool(base, raw)
        result = json.loads(out)
        self.assertEqual(result["issues"], base["issues"])
        self.assertEqual(result["summary"], base["summary"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["duplicate_group_heads"]["sets_found"], 0)

    def test_skip_flag_returns_native_result_untouched(self):
        native = json.dumps(native_scan())
        out, client = self.run_tool(native, VITRINE, check_duplicate_groups=False)
        self.assertEqual(out, native)
        self.assertEqual(client.send_command.call_count, 1)

    def test_explicit_checks_without_token_skip(self):
        native = json.dumps(native_scan())
        out, client = self.run_tool(native, VITRINE, checks=["group_integrity"])
        self.assertEqual(out, native)
        self.assertEqual(client.send_command.call_count, 1)
        sent = json.loads(client.send_command.call_args.args[0])
        self.assertEqual(sent["checks"], ["group_integrity"])

    def test_only_duplicate_token_sends_empty_native_checks(self):
        out, client = self.run_tool(native_scan([]), VITRINE, checks=["Duplicate_Group_Heads"])
        native_call = [c for c in client.send_command.call_args_list
                       if c.kwargs.get("cmd_type", "").startswith("native:")][0]
        self.assertEqual(json.loads(native_call.args[0])["checks"], [])
        self.assertEqual(len(json.loads(out)["issues"]), 1)

    def test_max_issues_caps_listing_but_counts_all(self):
        out, _ = self.run_tool(native_scan(), VITRINE, max_issues=1)
        result = json.loads(out)
        self.assertEqual(len(result["issues"]), 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["summary"]["issue_count"], 2)
        self.assertEqual(result["duplicate_group_heads"]["sets_listed"], 0)

    def test_query_error_does_not_break_scan(self):
        out, _ = self.run_tool(native_scan(), RuntimeError("pipe busy"))
        result = json.loads(out)
        self.assertEqual(result["issues"], [NATIVE_ISSUE])
        self.assertIn("pipe busy", result["duplicate_group_heads"]["error"])

    def test_non_json_native_result_passes_through(self):
        out, _ = self.run_tool("Native error text", VITRINE)
        self.assertEqual(out, "Native error text")

    def test_dry_run_fix_merges_into_before(self):
        native = {"action": "fix", "dry_run": True, "fixes": ["name_collisions"],
                  "before": native_scan(), "planned": []}
        out, _ = self.run_tool(native, VITRINE, action="fix", fixes=["name_collisions"],
                               dry_run=True)
        result = json.loads(out)
        self.assertEqual(result["before"]["issues"][1]["code"], "duplicate_group_heads")
        self.assertEqual(result["planned"], [])

    def test_applied_fix_queries_before_fix_and_never_deletes(self):
        native = {"action": "fix", "dry_run": False, "fixes": ["name_collisions"],
                  "applied": [], "applied_count": 0,
                  "before": native_scan(), "after": native_scan([])}
        out, client = self.run_tool(native, VITRINE, action="fix", fixes=["name_collisions"])
        kinds = [c.kwargs.get("cmd_type", "maxscript") for c in client.send_command.call_args_list]
        self.assertEqual(kinds, ["maxscript", "native:scene_qa_fix"])
        sent = json.loads(client.send_command.call_args_list[1].args[0])
        self.assertEqual(sent["fixes"], ["name_collisions"])
        result = json.loads(out)
        self.assertEqual(result["applied"], [])
        after_dup = result["after"]["issues"][0]
        self.assertTrue(after_dup["details"]["detected_before_fix"])
        self.assertNotIn("detected_before_fix", result["before"]["issues"][1]["details"])
        self.assertNotIn("note", result["after"]["duplicate_group_heads"])

    def test_applied_rename_adds_note(self):
        applied = [{"fix": "name_collisions", "before": {"name": "Leg"},
                    "after": {"name": "Leg001"}}]
        native = {"action": "fix", "dry_run": False, "fixes": ["name_collisions"],
                  "applied": applied, "applied_count": 1,
                  "before": native_scan(), "after": native_scan([])}
        out, _ = self.run_tool(native, VITRINE, action="fix", fixes=["name_collisions"])
        result = json.loads(out)
        self.assertIn("delete the extra heads first",
                      result["after"]["duplicate_group_heads"]["note"])
        self.assertNotIn("note", result["before"]["duplicate_group_heads"])
        clean = report(h_rec(10, "A", (0, 0, 0)))
        out, _ = self.run_tool(native, clean, action="fix", fixes=["name_collisions"])
        self.assertNotIn("note", json.loads(out)["after"]["duplicate_group_heads"])

    def test_fix_tokens_passed_through_unchanged(self):
        out, client = self.run_tool(native_scan(), VITRINE, action="fix",
                                    fixes=["name_collisions"], dry_run=True)
        native_call = [c for c in client.send_command.call_args_list
                       if c.kwargs.get("cmd_type", "").startswith("native:")][0]
        self.assertNotIn("duplicate_group_heads", json.loads(native_call.args[0])["fixes"])

    def test_tolerance_validation(self):
        with patch.object(sq, "client", make_client(native_scan(), VITRINE)):
            with self.assertRaises(ValueError):
                sq.scene_qa(group_position_tolerance=-1)
            with self.assertRaises(ValueError):
                sq.scene_qa(group_position_tolerance=float("nan"))

    def test_set_listing_is_bounded(self):
        records = []
        for i in range(60):
            records.append(h_rec(1000 + 2 * i, f"G{i}", (i * 10, 0, 0), nid=i + 1))
            records.append(h_rec(1001 + 2 * i, f"G{i}_copy", (i * 10, 0, 0), nid=i + 1))
        out, _ = self.run_tool(native_scan([]), report(*records))
        result = json.loads(out)
        self.assertEqual(len(result["issues"]), sq._MAX_SETS_LISTED)
        self.assertEqual(result["summary"]["issue_count"], 60)
        self.assertEqual(result["duplicate_group_heads"]["sets_found"], 60)
        self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
