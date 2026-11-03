"""Vantage crash events (MCP_ISSUES #51) and V-Ray output errors from Max.log (#49)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import vantage_diagnostics as vd  # noqa: E402


def event(app, module, version="32.0.16.1742", time="2026-11-01T13:49:24.07Z", provider="Application Error"):
    return ("<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='%s'/>"
            "<EventID>1000</EventID><TimeCreated SystemTime='%s'/></System><EventData>"
            "<Data Name='AppName'>%s</Data><Data Name='AppVersion'>3.4.0.0</Data>"
            "<Data Name='ModuleName'>%s</Data><Data Name='ModuleVersion'>%s</Data>"
            "<Data Name='ExceptionCode'>c0000005</Data><Data Name='FaultingOffset'>0000000000044e54</Data>"
            "</EventData></Event>" % (provider, time, app, module, version))


NGX = event("vantage.exe", "_nvngx.dll")
OLDER = event("Vantage.exe", "vantage_core.dll", "3.4.0.0", time="2026-11-01T11:00:00.00Z")
MAX = event("3dsmax.exe", "vray.dll", "7.40.4.0")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class CrashEventTests(unittest.TestCase):
    def setUp(self):
        vd._cache.clear()
        self.clock = Clock()
        self.calls = []

    def read(self, text, **kwargs):
        def runner(args, timeout):
            self.calls.append((args, timeout))
            if isinstance(text, BaseException):
                raise text
            return text
        with patch.object(vd.os, "name", "nt"):
            return vd.recent_crashes(runner=runner, now=self.clock, **kwargs)

    def test_parses_only_vantage_events_newest_first(self):
        events = vd.parse_crash_events("\ufeff" + OLDER + MAX + NGX)
        self.assertEqual([e["module"] for e in events], ["_nvngx.dll", "vantage_core.dll"])
        self.assertEqual(events[0], {"time": "2026-11-01T13:49:24.07Z", "app": "vantage.exe", "app_version": "3.4.0.0",
                                     "module": "_nvngx.dll", "module_version": "32.0.16.1742",
                                     "exception": "c0000005", "offset": "0000000000044e54"})

    def test_found_with_dlss_hint(self):
        out = self.read(NGX + MAX + OLDER)
        self.assertEqual((out["state"], out["count"], out["window_hours"]), ("found", 2, 24))
        self.assertIn("DLSS Frame Generation off", out["hint"])
        self.assertIn("update the NVIDIA driver", out["hint"])
        warning = vd.crash_warning(out)
        self.assertIn("crashed 2 time(s) in the last 24 h", warning)
        self.assertIn("2026-11-01 13:49:24 UTC, in _nvngx.dll 32.0.16.1742", warning)

    def test_query_is_cheap_and_bounded(self):
        self.read("")
        [(args, timeout)] = self.calls
        self.assertLessEqual(timeout, 3.0)
        self.assertEqual(args[:3], ["wevtutil", "qe", "Application"])
        self.assertIn("/rd:true", args)
        self.assertIn("/c:200", args)
        query = next(a for a in args if a.startswith("/q:"))
        self.assertIn("Provider[@Name='Application Error']", query)
        self.assertIn("(EventID=1000)", query)
        self.assertIn("timediff(@SystemTime) <= 86400000", query)

    def test_other_crashes_only_is_none(self):
        out = self.read(MAX)
        self.assertEqual(out, {"state": "none", "window_hours": 24})
        self.assertIsNone(vd.crash_warning(out))

    def test_failures_are_unknown(self):
        for failure in (subprocess.TimeoutExpired("wevtutil", 3), OSError("missing"), None, "<Event>broken"):
            with self.subTest(failure=failure):
                vd._cache.clear()
                self.assertEqual(self.read(failure), {"state": "unknown", "window_hours": 24})

    def test_not_windows_is_unknown(self):
        with patch.object(vd.os, "name", "posix"):
            self.assertEqual(vd.recent_crashes(runner=lambda a, t: NGX, now=self.clock)["state"], "unknown")

    def test_cached_for_a_minute(self):
        self.read(NGX)
        self.clock.t += 30
        self.assertEqual(self.read(MAX)["state"], "found")
        self.assertEqual(len(self.calls), 1)
        self.clock.t += 31
        self.assertEqual(self.read(MAX)["state"], "none")
        self.assertEqual(len(self.calls), 2)

    def test_real_runner_returns_none_on_error_code(self):
        done = subprocess.CompletedProcess([], 1, b"", b"bad")
        with patch.object(vd.subprocess, "run", return_value=done) as run:
            self.assertIsNone(vd._run_wevtutil(["wevtutil"], 3.0))
        self.assertEqual(run.call_args.kwargs["timeout"], 3.0)
        self.assertFalse(run.call_args.kwargs["check"])


class MaxLogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "Max.log")
        self.old = "2026/10/30 11:00:00 ERR: [700] [1] Cannot create output image file old.vrimg\r\n"
        with open(self.path, "wb") as f:
            f.write(self.old.encode())

    def append(self, text):
        with open(self.path, "ab") as f:
            f.write(text.encode())

    def test_parse_log_mark(self):
        self.assertEqual(vd.parse_log_mark("123|700|C:\\x\\Max.log"), {"path": "C:\\x\\Max.log", "size": 123, "pid": 700})
        self.assertEqual(vd.parse_log_mark("123L|700|p")["size"], 123)
        self.assertIsNone(vd.parse_log_mark(""))
        self.assertIsNone(vd.parse_log_mark("x|700|p"))
        self.assertIsNone(vd.parse_log_mark("12|700|"))
        self.assertIsNone(vd.parse_log_mark("12|abc|p")["pid"])

    def test_reads_only_new_lines_of_this_max(self):
        mark = {"path": self.path, "size": len(self.old), "pid": 700}
        self.append("2026/11/03 10:00:00 INF: [700] [1] V-Ray: starting IPR\r\n"
                    "2026/11/03 10:00:01 ERR: [700] [2] [V-Ray] Cannot create output image file \"D:\\r\\a.vrimg\"\r\n"
                    "2026/11/03 10:00:01 ERR: [701] [2] [V-Ray] Cannot create output image file other max\r\n"
                    "2026/11/03 10:00:02 ERR: [700] [2] error opening file: Permission denied\r\n")
        hits = vd.vray_output_errors(mark)
        self.assertEqual(len(hits), 2)
        self.assertIn("a.vrimg", hits[0])
        self.assertIn("Permission denied", hits[1])
        self.assertNotIn("old.vrimg", " ".join(hits))

    def test_new_log_smaller_than_mark_is_read_from_start(self):
        with open(self.path, "wb") as f:
            f.write(b"2026/11/03 10:00:01 ERR: [700] [2] Failed to open output file x.exr\r\n")
        self.assertEqual(len(vd.vray_output_errors({"path": self.path, "size": 10 ** 6, "pid": 700})), 1)

    def test_no_mark_or_missing_file(self):
        self.assertEqual(vd.vray_output_errors(None), [])
        self.assertEqual(vd.vray_output_errors({"path": self.path + ".gone", "size": 0, "pid": 1}), [])

    def test_at_most_five_lines(self):
        self.append("".join("ERR: [700] [1] Cannot create output image file %d\r\n" % i for i in range(9)))
        hits = vd.vray_output_errors({"path": self.path, "size": len(self.old), "pid": 700})
        self.assertEqual(len(hits), 5)
        self.assertTrue(hits[-1].endswith("file 8"))


if __name__ == "__main__":
    unittest.main()
