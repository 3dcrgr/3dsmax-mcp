"""V-Ray output errors from Max.log (MCP_ISSUES #49)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.helpers import vantage_diagnostics as vd  # noqa: E402


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
