"""execute_maxscript failures: parse error vs. interrupted script (quitMax etc.)."""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp import tool_response  # noqa: E402
from maxmcp.max_client import MaxBridgeError  # noqa: E402
from maxmcp.tool_response import ErrorCode, make_structured_tool  # noqa: E402

# Mirrors HandlerHelpers::MaxScriptFailureMessage (native/include/mcp_bridge/handler_helpers.h).
PARSE = "MAXScript execution failed (parse error): Syntax error: at ), expected <factor>"
INTERRUPTED = (
    "MAXScript did not complete (not a parse error): the script was interrupted, e.g. by "
    "quitMax/resetMaxFile/exit, an escape/abort, or a system exception. If it ended or "
    "reset the Max session that is expected."
)
SHUTTING_DOWN = (
    "MAXScript did not complete: 3ds Max is shutting down (e.g. after quitMax), "
    "so the syntax check was skipped. If the script ended the Max session that is expected."
)
UNCLASSIFIED = (
    "MAXScript execution failed: the script did not complete and the syntax check "
    "could not run, so this is either a parse error or an interruption "
    "(quitMax/resetMaxFile/exit, escape/abort, or a system exception)."
)


def _native_error(code, message):
    """Bridge response as HandleMaxScript's StructuredErrorPayload produces it."""
    payload = json.dumps({"type": "NativeError", "message": message, "code": code, "retryable": False})
    return MaxBridgeError(payload, {"success": False, "error": payload})


def _load_execute(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.execute", None)
        return importlib.import_module("maxmcp.tools.execute")


class ExecuteFailureEnvelopeTests(unittest.TestCase):
    def _run(self, exc, code="quitMax #noPrompt"):
        client = mock.Mock()
        client.send_command.side_effect = exc
        execute = _load_execute(client)
        tool = make_structured_tool(execute.execute_maxscript)
        with mock.patch.dict("os.environ", {"MCP_TRIPBACK_MODE": "minimal"}):
            return tool(code=code)

    def test_interrupted_script_is_not_a_parse_error(self):
        envelope = self._run(_native_error("MAXSCRIPT_INTERRUPTED", INTERRUPTED))
        self.assertFalse(envelope["ok"])
        error = envelope["error"]
        self.assertEqual(error["code"], ErrorCode.MAXSCRIPT_INTERRUPTED.value)
        self.assertFalse(error["retryable"])
        self.assertIn("not a parse error", error["message"])
        self.assertNotIn("(parse error)", error["message"])

    def test_parse_error_stays_bad_param(self):
        envelope = self._run(_native_error("BAD_PARAM", PARSE), code="(1 +")
        error = envelope["error"]
        self.assertEqual(error["code"], ErrorCode.BAD_PARAM.value)
        self.assertTrue(error["message"].startswith("MAXScript execution failed (parse error)"))
        self.assertFalse(error["retryable"])


class HybridHandlerPayloadTests(unittest.TestCase):
    """RunMAXScript throws the same structured payload; its explicit code must win over keywords."""

    def test_explicit_codes_survive_keyword_text(self):
        # Compiler detail can quote script text such as "Track not found" or "bridge".
        quoted = PARSE + ' In line: throw "Track not found: bridge named pipe" +'
        for code, message in (("BAD_PARAM", quoted), ("MAXSCRIPT_INTERRUPTED", INTERRUPTED)):
            error = tool_response._error_from_exception(_native_error(code, message))
            self.assertEqual(error["code"], code)
            self.assertFalse(error["retryable"])


class PlainMessageClassificationTests(unittest.TestCase):
    """Fallback for unstructured text (e.g. an older bridge): goes through _classify_error_code."""

    def test_interruptions(self):
        for message in (INTERRUPTED, SHUTTING_DOWN, "MAXScript error: " + INTERRUPTED):
            self.assertEqual(tool_response._classify_error_code(message), ErrorCode.MAXSCRIPT_INTERRUPTED)

    def test_parse_and_unclassified_are_bad_param(self):
        for message in (PARSE, "MAXScript execution failed (parse error)", UNCLASSIFIED):
            self.assertEqual(tool_response._classify_error_code(message), ErrorCode.BAD_PARAM)

    def test_never_bridge_down_or_retryable(self):
        for message in (PARSE, INTERRUPTED, SHUTTING_DOWN, UNCLASSIFIED):
            error = tool_response._error_from_exception(RuntimeError(message))
            self.assertNotEqual(error["code"], ErrorCode.BRIDGE_DOWN.value)
            self.assertFalse(error["retryable"])


class QuietListenerSourceTests(unittest.TestCase):
    """Issue #8: agent scripts must not print compile errors into the user's Listener."""

    NATIVE = Path(__file__).resolve().parent.parent / "native"

    def _call_args(self, rel_path, function):
        text = (self.NATIVE / rel_path).read_text(encoding="utf-8")
        start = text.index(function)
        call = text.index("ExecuteMAXScriptScript(", start)
        args = text[call:text.index(");", call)]
        return [line.split("//")[0].strip().rstrip(",") for line in args.splitlines()[1:]]

    def test_agent_scripts_run_with_quiet_errors(self):
        for rel_path, function in (
            ("src/command_dispatcher.cpp", "static std::string HandleMaxScript("),
            ("include/mcp_bridge/handler_helpers.h", "inline std::string RunMAXScript("),
        ):
            with self.subTest(function=function):
                args = self._call_args(rel_path, function)
                # (script, source, quietErrors, fpv, logQuietErrors): log file only, never Listener.
                self.assertEqual(args[2], "TRUE")
                self.assertEqual(args[4], "TRUE")


if __name__ == "__main__":
    unittest.main()
