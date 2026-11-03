"""#47 follow-up: a line-1 parse error must not quote the undo prefix as the agent's own code."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maxmcp.max_client import MaxBridgeError  # noqa: E402
from test_undo_record import PREFIX, _load_execute  # noqa: E402


def _parse_error(line: str) -> MaxBridgeError:
    payload = json.dumps({"type": "NativeError", "code": "BAD_PARAM", "retryable": False,
                          "message": "MAXScript execution failed (parse error): Syntax error: at =, "
                                     "expected <factor> --  In line: " + line})
    return MaxBridgeError(payload, {"success": False, "error": payload})


class UndoPrefixInErrorTests(unittest.TestCase):
    def _run(self, code, error, **kwargs):
        client = mock.Mock()
        client.send_command.side_effect = error
        execute = _load_execute(client)
        with self.assertRaises(MaxBridgeError) as ctx:
            execute.execute_maxscript(code=code, **kwargs)
        return client.send_command.call_args.args[0], ctx.exception

    def test_line_one_parse_error_quotes_only_the_agents_code(self):
        sent, exc = self._run("x = = 1", _parse_error(PREFIX + "x = = 1"))
        self.assertTrue(sent.startswith(PREFIX))  # the script was wrapped
        message = json.loads(exc.bridge_message)["message"]
        self.assertTrue(message.endswith("In line: x = = 1"), message)
        self.assertNotIn("MCP execute_maxscript", str(exc))
        self.assertNotIn("MCP execute_maxscript", exc.bridge_response["error"])
        self.assertEqual(json.loads(exc.bridge_message)["code"], "BAD_PARAM")  # structure kept

    def test_later_line_error_is_unchanged(self):
        error = _parse_error("c = = 3")
        before = error.bridge_message
        _, exc = self._run("a = 1\nb = 2\nc = = 3", error)
        self.assertEqual(exc.bridge_message, before)

    def test_unwrapped_script_is_left_alone(self):
        # undo=False sends the script as is; a script that itself contains the prefix text keeps it.
        line = PREFIX + "x = = 1"
        error = _parse_error(line)
        before = error.bridge_message
        sent, exc = self._run(line, error, undo=False)
        self.assertEqual(sent, line)
        self.assertEqual(exc.bridge_message, before)

    def test_plain_text_error_is_cleaned_too(self):
        error = MaxBridgeError("Syntax error -- In line: " + PREFIX + "x = = 1", {"error": "same"})
        _, exc = self._run("x = = 1", error)
        self.assertEqual(exc.bridge_message, "Syntax error -- In line: x = = 1")
        self.assertEqual(str(exc), "MAXScript error: Syntax error -- In line: x = = 1")


if __name__ == "__main__":
    unittest.main()
