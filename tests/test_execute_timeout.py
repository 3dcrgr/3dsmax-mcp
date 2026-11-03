"""Issue #48 (3): execute_maxscript's optional timeout, passed to the client like upstream's long tools."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_undo_record import _Case, _load_execute  # noqa: E402


class TimeoutTests(_Case):
    def test_default_leaves_the_client_default(self):
        _, kwargs, _, _ = self.run_script("1")
        self.assertIsNone(kwargs["timeout"])

    def test_timeout_is_passed_and_clamped(self):
        for given, expected in ((300, 300.0), (5000, 600.0), (1, 10.0), (10, 10.0), (600, 600.0)):
            _, kwargs, _, _ = self.run_script("1", timeout=given)
            self.assertEqual(kwargs["timeout"], expected, given)

    def test_unsaved_check_keeps_the_default_timeout(self):
        client = mock.Mock()
        client.send_command.return_value = {"result": "false"}
        execute = _load_execute(client)
        execute.execute_maxscript(code="loadMaxFile @\"C:/a.max\"", timeout=500)
        first, last = client.send_command.call_args_list[0], client.send_command.call_args_list[-1]
        self.assertEqual(first.args[0], "getSaveRequired()")
        self.assertNotIn("timeout", first.kwargs)
        self.assertEqual(last.kwargs["timeout"], 500.0)


if __name__ == "__main__":
    unittest.main()
