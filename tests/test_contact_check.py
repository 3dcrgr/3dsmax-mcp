"""Tests for contact_check: thresholds in mm, skipped nodes, work/time budgets (#14, #15)."""

import base64
import importlib
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout
# over an installed maxmcp.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import contact_check as cc  # noqa: E402

tool = importlib.import_module("maxmcp.tools.contact_check")


def b64(name: str) -> str:
    return base64.b64encode(name.encode("utf-8")).decode("ascii")


def head(*, tol="0.01", near="1", upm="0.1", nodes=2, total=1, checked=1, work=0, done=0,
         elapsed=12, limit=30000, wait=-1, stop="none") -> str:
    return (f"U|{tol}|{near}|#centimeters|{upm}|{nodes}|{total}|{checked}|{work}|{done}|{elapsed}|{limit}"
            f"|{wait}|{stop}")


def n_rec(handle, name, faces=12, closed=1, verts=8) -> str:
    return f"N|{handle}|{b64(name)}|{faces}|{closed}|{verts}"


def p_rec(ha, hb, depth="0", gap="0.5", inside=(0, 0), cross=(0, 0)) -> str:
    return f"P|{ha}|{hb}|{depth}|{gap}|{inside[0]}|{inside[1]}|{cross[0]}|{cross[1]}||1,2,3|"


def report(*lines: str) -> str:
    return "\n".join(lines) + "\nEND\n"


class ImportTargetTests(unittest.TestCase):
    def test_imports_checkout(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(Path(cc.__file__).resolve(), repo / "maxmcp" / "helpers" / "contact_check.py")
        self.assertEqual(Path(tool.__file__).resolve(), repo / "maxmcp" / "tools" / "contact_check.py")


class ThresholdTests(unittest.TestCase):
    """#14: explicit thresholds are millimetres and convert like the defaults."""

    def test_defaults(self):
        self.assertEqual(cc.resolve_thresholds(0, 0), (0.1, 10.0))

    def test_explicit_near_gap_only(self):
        self.assertEqual(cc.resolve_thresholds(0, 2), (0.1, 2.0))

    def test_explicit_both(self):
        self.assertEqual(cc.resolve_thresholds(0.01, 2), (0.01, 2.0))

    def test_large_tolerance_raises_default_near_gap(self):
        self.assertEqual(cc.resolve_thresholds(20, 0), (20.0, 20.0))

    def test_near_gap_below_default_tolerance_is_clear_error(self):
        with self.assertRaisesRegex(ValueError, r"near_gap \(0.05 mm\).*tolerance \(0.1 mm, the default\)"):
            cc.resolve_thresholds(0, 0.05)

    def test_near_gap_below_explicit_tolerance(self):
        with self.assertRaisesRegex(ValueError, "millimetres"):
            cc.resolve_thresholds(1, 0.5)

    def test_bad_values(self):
        for bad in (-1, True, float("nan"), float("inf"), "2", 1e6):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cc.resolve_thresholds(bad, 0)
        with self.assertRaisesRegex(ValueError, "at least"):
            cc.resolve_thresholds(1e-9, 0)

    def test_validate_args_returns_mm(self):
        out = cc.validate_args(["a"], ["b"], 0, 2, 200, 200000)
        self.assertEqual(out, (["a"], ["b"], 0.1, 2.0, 200, 200000))

    def test_script_sends_float_literals_in_mm(self):
        # The live bug: near_gap=2 reached MAXScript as the Integer literal 2,
        # which formattedPrint ".7g" printed as garbage -> "invalid thresholds".
        script = cc.build_script(["a"], ["b"], 0.1, 2.0, 200, 200000)
        self.assertIn('local upm = units.decodeValue "1mm"', script)
        self.assertIn("local tol = (0.100000000 as float) * upm", script)
        self.assertIn("local near = (2.000000000 as float) * upm", script)
        self.assertNotRegex(script, r"local (tol|near) = \d+\s*\n")

    def test_build_script_rejects_unresolved_thresholds(self):
        with self.assertRaises(ValueError):
            cc.build_script(["a"], [], 0.0, 10.0, 200, 200000)


class BudgetValidationTests(unittest.TestCase):
    def test_defaults_ok(self):
        self.assertEqual(cc.validate_budgets(cc.DEFAULT_WORK_BUDGET, cc.DEFAULT_TIME_BUDGET_S, 120.0),
                         (cc.DEFAULT_WORK_BUDGET, 30.0))

    def test_time_budget_stays_below_request_timeout(self):
        with self.assertRaisesRegex(ValueError, "from 1 to 45"):
            cc.validate_budgets(1000, 100, 120.0)
        # 60-90 s used to pass; the MCP host (often 60 s) gives up before MaxClient does
        with self.assertRaisesRegex(ValueError, "from 1 to 45"):
            cc.validate_budgets(1000, 60, 120.0)
        self.assertEqual(cc.validate_budgets(1000, 45, 120.0), (1000, 45.0))
        with self.assertRaisesRegex(ValueError, "from 1 to 40"):
            cc.validate_budgets(1000, 45, 60.0)
        self.assertEqual(cc.validate_budgets(1000, 40, 60.0), (1000, 40.0))

    def test_unknown_timeout_uses_default(self):
        self.assertEqual(cc.validate_budgets(1000, 45, MagicMock()), (1000, 45.0))

    def test_bad_budgets(self):
        for wb, tb in ((0, 30), (True, 30), (1.5, 30), (cc.MAX_WORK_BUDGET + 1, 30), (10, 0.5), (10, True)):
            with self.subTest(wb=wb, tb=tb), self.assertRaises(ValueError):
                cc.validate_budgets(wb, tb, 120.0)


class ScriptDeadlineTests(unittest.TestCase):
    """#15: the generated MAXScript carries the budget and deadline checks."""

    def setUp(self):
        self.script = cc.build_script(["a", "b"], ["c"], 0.1, 10.0, 200, 200000,
                                      work_budget=12345, time_budget_s=7.5, sent_ms=1759700000123)

    def test_budget_literals(self):
        self.assertIn("local budgetMs = 7500", self.script)
        self.assertIn("budget = 12345 as integer64", self.script)

    def test_deadline_checks_everywhere(self):
        s = self.script
        self.assertIn("fn ccElapsed t0", s)
        self.assertIn("d += 86400000", s)  # timeStamp wraps at midnight
        one_way = s[s.index("fn ccOneWay"):s.index("fn ccP ")]
        # periodic checks inside both the vertex loop and the edge loop
        self.assertEqual(one_way.count("if tick >= 256 do (tick = 0; if (ccElapsed t0) > limitMs do expired = true)"), 2)
        self.assertIn("if expired then undefined else #(", one_way)
        main = s[s.index("local t0 = timeStamp()"):]
        # candidate search, mesh snapshot, between pairs, after accelerator builds
        self.assertGreaterEqual(main.count("(ccElapsed t0) > limitMs"), 4)
        self.assertIn('stop = "deadline_search"', main)
        self.assertIn('why = "deadline"', main)

    def test_cheapest_first_and_work_estimate(self):
        s = self.script
        self.assertIn("qsort order ccCmp", s)
        self.assertIn("workTotal += w", s)
        self.assertIn('why = "work_budget"', s)
        self.assertLess(s.index("workTotal += w"), s.index("qsort order ccCmp"))
        self.assertLess(s.index("qsort order ccCmp"), s.index("RayMeshGridIntersect()"))

    def test_non_mesh_nodes_are_skipped_not_thrown(self):
        s = self.script
        self.assertNotIn('throw ("Not a mesh-convertible geometry node', s)
        self.assertIn("append skipped m[1]; undefined", s)
        self.assertIn('format "S|%|%|not_mesh|0\\n"', s)
        self.assertIn("isAgainst and setB.count == 0", s)

    def test_all_names_skipped_is_an_error_like_against(self):
        # review: an empty names side used to return complete=true with zero pairs
        s = self.script
        self.assertIn('if isAgainst and setA.count == 0 do throw ("No mesh-convertible node in names; skipped:"', s)
        self.assertLess(s.index("setA.count == 0 do throw"), s.index("candidate pairs by padded"))

    def test_queue_wait_counts_against_budget(self):
        s = self.script
        self.assertIn('(dotNetClass "System.DateTimeOffset").UtcNow.ToUnixTimeMilliseconds()) - 1759700000123L', s)
        self.assertIn(f"if wq >= 0 and wq <= {cc.MAX_QUEUE_WAIT_MS} do waitMs = wq as integer", s)
        self.assertIn("if waitMs > 0 do limitMs = budgetMs - waitMs", s)
        self.assertIn("if limitMs < 1 do limitMs = -1", s)
        # the wait is read before any scene work, and the header reports budget + wait
        self.assertLess(s.index("ToUnixTimeMilliseconds"), s.index("local namesIn"))
        self.assertIn('budgetMs as string + "|" + waitMs as string + "|" + stop', s)

    def test_default_sent_ms_is_now(self):
        with patch.object(cc.time, "time", return_value=1759700000.5):
            s = cc.build_script(["a", "b"], [], 0.1, 10.0, 200, 200000)
        self.assertIn("- 1759700000500L", s)

    def test_template_formats_cleanly(self):
        self.assertNotIn("%(", self.script)
        self.assertEqual(self.script.count("("), self.script.count(")"))


class ParseFullReportTests(unittest.TestCase):
    def test_complete_report(self):
        raw = report(head(total=1, checked=1, work=16, done=16),
                     n_rec(10, "Leg"), n_rec(11, "Floor"),
                     p_rec(10, 11, gap="0.005"))
        out = cc.parse_report(raw)
        self.assertTrue(out["complete"])
        self.assertIsNone(out["stop_reason"])
        self.assertEqual(out["pairs_total"], 1)
        self.assertEqual(out["candidate_pairs"], 1)
        self.assertEqual(out["pairs_checked"], 1)
        self.assertEqual(out["work_estimate"], 16)
        self.assertEqual(out["work_checked"], 16)
        self.assertEqual(out["elapsed_ms"], 12)
        self.assertEqual(out["time_budget_ms"], 30000)
        self.assertEqual(out["tolerance_mm"], 0.1)
        self.assertEqual(out["near_gap_mm"], 10.0)
        self.assertEqual(out["summary"]["touching"], 1)
        self.assertEqual(out["warnings"], [])
        self.assertEqual(out["skipped_nodes"], [])

    def test_explicit_near_gap_echo(self):
        # near_gap=2 mm in a cm scene -> 0.2 scene units
        raw = report(head(near="0.2", total=1, checked=1, work=16, done=16),
                     n_rec(10, "Leg"), n_rec(11, "Floor"), p_rec(10, 11, gap="0.15"))
        out = cc.parse_report(raw)
        self.assertEqual(out["near_gap"], 0.2)
        self.assertEqual(out["near_gap_mm"], 2.0)
        self.assertEqual(out["pairs"][0]["status"], "near_gap")

    def test_invalid_thresholds_names_values(self):
        raw = report(head(tol="0.01", near="9.8e-324", total=0, checked=0))
        with self.assertRaisesRegex(RuntimeError, r"invalid thresholds \(tolerance 0.01, near_gap 9.8e-324"):
            cc.parse_report(raw)

    def test_maxscript_error_passthrough(self):
        with self.assertRaisesRegex(RuntimeError, "Node name must resolve uniquely"):
            cc.parse_report("__ERROR__|Node name must resolve uniquely: X")


class ParseSkippedNodeTests(unittest.TestCase):
    def test_not_mesh_node_skipped_with_warning(self):
        raw = report(head(nodes=2, total=1, checked=1, work=16, done=16),
                     f"S|99|{b64('Wall_StandardNew_10')}|not_mesh|0",
                     n_rec(10, "Leg"), n_rec(11, "Floor"), p_rec(10, 11))
        out = cc.parse_report(raw)
        self.assertTrue(out["complete"])  # every candidate pair was measured
        self.assertEqual(out["skipped_nodes"], [{"name": "Wall_StandardNew_10", "reason": "not_mesh"}])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("not mesh-convertible", out["warnings"][0])
        self.assertIn("Wall_StandardNew_10", out["warnings"][0])

    def test_face_limit_node_pairs_unchecked(self):
        raw = report(head(nodes=3, total=2, checked=1, work=16 + 500008, done=16),
                     n_rec(10, "Leg"), n_rec(11, "Floor"),
                     f"S|12|{b64('Garland')}|face_limit|300000",
                     p_rec(10, 11),
                     "X|10|12|500008|skipped_node")
        out = cc.parse_report(raw)
        self.assertFalse(out["complete"])
        self.assertIsNone(out["stop_reason"])
        self.assertEqual(out["unchecked_pairs"], [{"a": "Leg", "b": "Garland", "work": 500008, "reason": "skipped_node"}])
        self.assertTrue(any("face count above max_faces" in w for w in out["warnings"]))
        self.assertTrue(any("involve a skipped node" in w for w in out["warnings"]))


class ParseBudgetTests(unittest.TestCase):
    def _garland_report(self, stop, reason, elapsed=29990):
        # two small pairs checked, two garland pairs not checked
        return report(
            head(nodes=4, total=4, checked=2, work=16 + 16 + 2 * (147967 + 8), done=32,
                 elapsed=elapsed, stop=stop),
            n_rec(10, "plate_016"), n_rec(11, "Cylinder498"), n_rec(13, "cup"),
            n_rec(12, "Mesher003", faces=53733, closed=0, verts=147967),
            p_rec(10, 11), p_rec(10, 13),
            f"X|12|10|147975|{reason}", f"X|12|11|147975|{reason}")

    def test_work_budget_partial(self):
        out = cc.parse_report(self._garland_report("work_budget", "work_budget"))
        self.assertFalse(out["complete"])
        self.assertEqual(out["stop_reason"], "work_budget")
        self.assertEqual((out["pairs_total"], out["pairs_checked"]), (4, 2))
        self.assertEqual(out["work_estimate"], 32 + 2 * 147975)
        self.assertEqual(out["heaviest_nodes"][0],
                         {"name": "Mesher003", "verts": 147967, "faces": 53733, "unchecked_pairs": 2})
        self.assertEqual(len(out["unchecked_pairs"]), 2)
        warn = " ".join(out["warnings"])
        self.assertIn("Work budget reached: 2 pair(s)", warn)
        self.assertIn("Mesher003 (147967 verts)", warn)
        # what was checked is still reported normally
        self.assertEqual(sum(out["summary"].values()), 2)

    def test_deadline_partial(self):
        out = cc.parse_report(self._garland_report("deadline", "deadline"))
        self.assertFalse(out["complete"])
        self.assertEqual(out["stop_reason"], "deadline")
        self.assertTrue(any("30 s time budget: 2 of 4 candidate pairs checked" in w for w in out["warnings"]))

    def test_queue_wait_reported(self):
        raw = report(head(nodes=4, total=4, checked=2, work=16 + 16 + 2 * (147967 + 8), done=32,
                          elapsed=17990, wait=12000, stop="deadline"),
                     n_rec(10, "plate_016"), n_rec(11, "Cylinder498"), n_rec(13, "cup"),
                     n_rec(12, "Mesher003", faces=53733, closed=0, verts=147967),
                     p_rec(10, 11), p_rec(10, 13),
                     "X|12|10|147975|deadline", "X|12|11|147975|deadline")
        out = cc.parse_report(raw)
        self.assertEqual(out["queue_wait_ms"], 12000)
        self.assertTrue(any("30 s time budget (12.0 s of it spent queued" in w for w in out["warnings"]))

    def test_queue_wait_unknown(self):
        out = cc.parse_report(self._garland_report("deadline", "deadline"))
        self.assertIsNone(out["queue_wait_ms"])
        self.assertFalse(any("queued" in w for w in out["warnings"]))

    def test_whole_budget_spent_queued(self):
        raw = report(head(nodes=2, total=0, checked=0, elapsed=0, wait=31000, stop="deadline_search"))
        out = cc.parse_report(raw)
        self.assertFalse(out["complete"])
        self.assertEqual(out["queue_wait_ms"], 31000)
        self.assertIn("31.0 s of it spent queued", out["warnings"][0])

    def test_deadline_during_search(self):
        raw = report(head(nodes=40, total=0, checked=0, stop="deadline_search", elapsed=30001))
        out = cc.parse_report(raw)
        self.assertFalse(out["complete"])
        self.assertEqual(out["stop_reason"], "deadline_search")
        self.assertIn("0 of 0+ candidate pairs", out["warnings"][0])

    def test_unchecked_truncation(self):
        lines = [head(nodes=60, total=59, checked=0, work=59 * 16, done=0, stop="deadline"), n_rec(1, "hub")]
        lines += [n_rec(100 + k, f"n{k}") for k in range(59)]
        lines += [f"X|1|{100 + k}|16|deadline" for k in range(59)]
        out = cc.parse_report(report(*lines))
        self.assertEqual(len(out["unchecked_pairs"]), 50)
        self.assertEqual(out["unchecked_pairs_truncated"], 9)
        self.assertLessEqual(len(out["heaviest_nodes"]), 5)


class ParseValidationTests(unittest.TestCase):
    def _bad(self, raw, pattern):
        with self.assertRaisesRegex(RuntimeError, pattern):
            cc.parse_report(raw)

    def test_old_header_rejected(self):
        self._bad("U|0.01|1|#centimeters|2|1|5\n" + n_rec(1, "a") + "\nEND\n", "malformed")
        # the 13-field header (before queue wait) is rejected too
        self._bad("U|0.01|1|#centimeters|0.1|2|0|0|0|0|12|30000|none\nEND\n", "malformed")

    def test_bad_queue_wait(self):
        for wait in (-2, cc.MAX_QUEUE_WAIT_MS + 1):
            with self.subTest(wait=wait):
                self._bad(report(head(total=0, checked=0, wait=wait)), "queue wait")

    def test_count_mismatch(self):
        self._bad(report(head(total=2, checked=1, work=16, done=16), n_rec(10, "a"), n_rec(11, "b"), p_rec(10, 11)),
                  "counts")

    def test_work_mismatch(self):
        self._bad(report(head(total=1, checked=1, work=99, done=16), n_rec(10, "a"), n_rec(11, "b"), p_rec(10, 11)),
                  "counts")

    def test_unknown_stop(self):
        self._bad(report(head(total=0, checked=0, stop="later")), "unknown stop")

    def test_budget_stop_needs_skipped_pairs(self):
        self._bad(report(head(total=1, checked=1, work=16, done=16, stop="work_budget"),
                         n_rec(10, "a"), n_rec(11, "b"), p_rec(10, 11)), "work budget stop")

    def test_bad_reasons(self):
        self._bad(report(head(total=0, checked=0), f"S|5|{b64('w')}|weird|0"), "skipped-node")
        self._bad(report(head(total=1, checked=0, work=16), n_rec(10, "a"), n_rec(11, "b"), "X|10|11|16|bored"),
                  "unchecked-pair")

    def test_repeated_pair_across_records(self):
        self._bad(report(head(total=2, checked=1, work=32, done=16), n_rec(10, "a"), n_rec(11, "b"),
                         p_rec(10, 11), "X|11|10|16|deadline"), "repeated pair")

    def test_unreferenced_mesh_node(self):
        self._bad(report(head(nodes=3, total=1, checked=1, work=16, done=16), n_rec(10, "a"), n_rec(11, "b"),
                         n_rec(12, "c"), p_rec(10, 11)), "unmeasured node")


class ToolTests(unittest.TestCase):
    def _client(self, raw: str) -> MagicMock:
        client = MagicMock()
        client.timeout = 120.0
        client.send_command.return_value = {"result": raw}
        return client

    def test_explicit_near_gap_and_tolerance(self):
        raw = report(head(tol="0.001", near="0.2", total=1, checked=1, work=16, done=16),
                     n_rec(10, "Leg"), n_rec(11, "Floor"), p_rec(10, 11, gap="0.1"))
        with patch.object(tool, "client", self._client(raw)) as client:
            out = tool.contact_check(names=["Leg"], against=["Floor"], near_gap=2, tolerance=0.01)
        script = client.send_command.call_args[0][0]
        self.assertIn("(0.010000000 as float) * upm", script)
        self.assertIn("(2.000000000 as float) * upm", script)
        self.assertEqual(out["near_gap_mm"], 2.0)
        self.assertEqual(out["tolerance_mm"], 0.01)
        self.assertEqual(out["pairs"][0]["status"], "near_gap")
        self.assertTrue(out["complete"])

    def test_budgets_reach_script(self):
        raw = report(head(total=0, checked=0))
        with patch.object(tool, "client", self._client(raw)) as client:
            tool.contact_check(names=["a", "b"], work_budget=5000, time_budget_s=12)
        script = client.send_command.call_args[0][0]
        self.assertIn("local budgetMs = 12000", script)
        self.assertRegex(script, r"ToUnixTimeMilliseconds\(\)\) - \d{13}L")
        self.assertIn("budget = 5000 as integer64", script)

    def test_bad_threshold_rejected_before_send(self):
        with patch.object(tool, "client", self._client("")) as client:
            with self.assertRaisesRegex(ValueError, "near_gap"):
                tool.contact_check(names=["a", "b"], near_gap=0.01)
            with self.assertRaisesRegex(ValueError, "time_budget_s"):
                tool.contact_check(names=["a", "b"], time_budget_s=200)
        client.send_command.assert_not_called()

    def test_docstring_states_units_and_cost(self):
        doc = tool.contact_check.__doc__
        self.assertIn("MILLIMETRES", doc)
        self.assertIn("garlands", doc)
        self.assertIn("max 45 s", doc)
        self.assertNotIn("max 90 s", doc)
        self.assertTrue(re.search(r"work_budget", doc))


if __name__ == "__main__":
    unittest.main()
