"""Merge review of 1.7.5: save prompts, lost calls, old bridges and the #12 quiet override."""

import importlib
import json
import os
import re
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp import max_client, tool_response  # noqa: E402
from maxmcp.max_client import (ALWAYS_ASK, DialogBlocked, MaxBridgeError, MaxClient,  # noqa: E402
                               MaxNotRespondingAfterDispatch, RequestOutcomeUnknown)
from maxmcp.tool_response import make_structured_tool  # noqa: E402

DIALOG = {"dialog_id": "4242:7", "title": "3ds Max", "kind": "win32", "main_thread": True,
          "text": "Save changes?", "buttons": [{"label": "Save"}, {"label": "Don't Save"}]}


def _load(module, client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop(f"maxmcp.tools.{module}", None)
        return importlib.import_module(f"maxmcp.tools.{module}")


def _minimal():
    return mock.patch.dict(os.environ, {"MCP_TRIPBACK_MODE": "minimal"})


def _client():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MCP_MAX_PIPE", None)
        os.environ.pop("MCP_MAX_PID", None)
        return MaxClient(transport="pipe")


class SavePromptAdviceTests(unittest.TestCase):
    """The BLOCKED_BY_DIALOG hint carries the always-ask rule, even for unattended work."""

    def test_blocked_hint_says_always_ask_before_save_or_discard(self):
        exc = DialogBlocked("r1", "maxscript", [dict(DIALOG)])
        hint = json.loads(exc.bridge_message)["hint"]
        self.assertIn(ALWAYS_ASK, hint)
        self.assertIn("Don't Save", hint)
        self.assertIn("even when working unattended", hint)
        with _minimal():
            envelope = tool_response.envelope_exception(exc, elapsed_ms=1.0)
        self.assertIn("Don't Save", json.dumps(envelope))

    def test_bridge_status_says_always_ask(self):
        bridge = _load("bridge", mock.Mock())
        payload = json.loads(bridge._blocked_by_dialog_status([dict(DIALOG)]))
        self.assertIn(ALWAYS_ASK, payload["message"])


class OutcomeUnknownCodeTests(unittest.TestCase):
    """A request sent but never answered is not BAD_PARAM: fixing arguments and calling again replays it."""

    def test_request_outcome_unknown_has_its_own_code(self):
        exc = RequestOutcomeUnknown("Pipe closed after dispatch. The request may have committed; "
                                    "inspect before retrying.")
        error = tool_response._error_from_exception(exc)
        self.assertEqual(error["code"], "REQUEST_OUTCOME_UNKNOWN")
        self.assertFalse(error["retryable"])
        self.assertTrue(error["details"]["request_sent"])
        self.assertEqual(tool_response.ErrorCode.REQUEST_OUTCOME_UNKNOWN.value, "REQUEST_OUTCOME_UNKNOWN")

    def test_not_responding_after_dispatch_keeps_its_code_and_details(self):
        exc = MaxNotRespondingAfterDispatch("3ds Max (PID 1) is not responding",
                                            {"request_sent": True, "blocked_for_s": 25.0})
        error = tool_response._error_from_exception(exc)
        self.assertEqual(error["code"], "MAX_NOT_RESPONDING")
        self.assertFalse(error["retryable"])
        self.assertEqual(error["details"], {"request_sent": True, "blocked_for_s": 25.0})

    def _status(self, process_state=None):
        client = mock.Mock()
        client.transport = "auto"
        client.blocked_request_ids.return_value = []
        health = {"pid": 4242, "mainThread": {"state": "responsive", "pumping": True, "heartbeatAgeMs": 100},
                  "executor": {"running": None, "queued": 0}, "clients": {"inflight": []}}

        def send(command, cmd_type="maxscript", timeout=None):
            if cmd_type == "health":
                return {"result": json.dumps(health), "requestId": "h", "meta": {}}
            raise RequestOutcomeUnknown("Pipe closed after dispatch. The request may have committed; "
                                        "inspect before retrying.")

        client.send_command.side_effect = send
        bridge = _load("bridge", client)
        process = {"pid": 4242, "alive": process_state != "exited", "state": process_state or "responsive",
                   "main_window_found": True, "window_hung": False, "cpu_seconds_per_second": 0.0,
                   "cpu_sample_s": 1.0, "exit_code": None}
        with mock.patch.object(bridge, "diagnose_process", return_value=process):
            return json.loads(bridge.get_bridge_status())

    def test_bridge_status_reports_a_ping_lost_after_dispatch(self):
        payload = self._status()
        self.assertFalse(payload["pong"])
        self.assertFalse(payload["connected"])
        self.assertEqual(payload["bridge_state"], "connection_lost")
        self.assertEqual(payload["bridge_code"], "REQUEST_OUTCOME_UNKNOWN")
        self.assertTrue(payload["request_sent"])
        self.assertNotIn("error", payload)

    def test_bridge_status_reports_an_exited_max(self):
        payload = self._status("exited")
        self.assertEqual(payload["bridge_state"], "not_responding")
        self.assertFalse(payload["retryable"])
        self.assertIn("exited", payload["message"])


class _Reader:
    def __init__(self, data=b"", error=None):
        self.done = threading.Event()
        self.done.set()
        self.data, self.error = data, error

    def result(self):
        if self.error is not None:
            raise self.error
        return self.data


class LostBlockedCallTests(unittest.TestCase):
    """A BLOCKED_BY_DIALOG call whose connection broke is "lost", never a clean failure."""

    def report(self, reader):
        client = _client()
        client._blocked["r1"] = {"command": "maxscript", "reader": reader, "blocked_at": 0.0, "sent_at": 0.0}
        return client.blocked_calls()[0]

    def assert_lost(self, item):
        self.assertEqual(item["status"], "lost")
        self.assertIs(item["ok"], False)
        self.assertEqual(item["outcome"], "unknown")
        self.assertIs(item["request_sent"], True)
        self.assertIn("inspect before retrying", item["error"])

    def test_broken_pipe_is_lost(self):
        self.assert_lost(self.report(_Reader(error=BrokenPipeError("Pipe closed while reading response."))))

    def test_connection_error_is_lost(self):
        self.assert_lost(self.report(_Reader(error=ConnectionError("Failed reading from pipe: Win32 error 6"))))

    def test_mismatched_reply_is_lost(self):
        reply = json.dumps({"success": True, "result": "x", "requestId": "other"}).encode() + b"\n"
        self.assert_lost(self.report(_Reader(reply)))

    def test_bridge_error_is_still_completed(self):
        reply = json.dumps({"success": False, "error": "boom", "requestId": "r1"}).encode() + b"\n"
        item = self.report(_Reader(reply))
        self.assertEqual((item["status"], item["ok"], item["error"]), ("completed", False, "boom"))
        self.assertNotIn("outcome", item)

    def test_success_is_completed(self):
        reply = json.dumps({"success": True, "result": "{\"a\": 1}", "requestId": "r1"}).encode() + b"\n"
        item = self.report(_Reader(reply))
        self.assertEqual((item["status"], item["ok"], item["result"]), ("completed", True, {"a": 1}))


class OldBridgeDialogTests(unittest.TestCase):
    """A bridge without native:max_dialogs (before 1.7.5) gives BRIDGE_OUTDATED, not BAD_PARAM."""

    def tool(self, error):
        client = mock.Mock()
        client.transport = "auto"
        client.send_command.side_effect = error
        client.blocked_calls.return_value = []
        return make_structured_tool(_load("mainthread", client).max_dialogs)

    def test_unknown_command_type_is_bridge_outdated(self):
        payload = json.dumps({"type": "NativeError", "code": "BAD_PARAM", "retryable": False,
                              "message": "Unknown command type: native:max_dialogs"})
        with _minimal():
            envelope = self.tool(MaxBridgeError(payload, {"success": False}))(action="inspect")
        self.assertEqual(envelope["error"]["code"], "BRIDGE_OUTDATED")
        self.assertFalse(envelope["error"]["retryable"])
        self.assertIn("answer the dialog in Max", envelope["error"]["message"])

    def test_plain_unknown_command_message_is_bridge_outdated(self):
        with _minimal():
            envelope = self.tool(MaxBridgeError("Unknown command type: native:max_dialogs", {"success": False}))(
                action="respond", dialog_id="d", expected_dialog="t", button="OK")
        self.assertEqual(envelope["error"]["code"], "BRIDGE_OUTDATED")
        self.assertIn("Nothing was pressed", envelope["error"]["message"])

    def test_other_bridge_errors_pass_through(self):
        payload = json.dumps({"type": "NativeError", "code": "STALE_DIALOG", "retryable": False,
                              "message": "the dialog changed or closed; inspect again"})
        with _minimal():
            envelope = self.tool(MaxBridgeError(payload, {"success": False}))(
                action="respond", dialog_id="d", expected_dialog="t", button="OK")
        self.assertEqual(envelope["error"]["code"], "STALE_DIALOG")


class QuietTrueFileCommandTests(unittest.TestCase):
    """#12: quiet=True alone no longer discards unsaved work on a reset/open/fetch/quit script."""

    def sent(self, code, **kwargs):
        client = mock.Mock()
        response = {"result": "ok"}
        client.send_command.return_value = response
        _load("execute", client).execute_maxscript(code=code, **kwargs)
        return client.send_command.call_args.kwargs["request_fields"], response

    def test_quiet_true_on_a_file_command_shows_prompts(self):
        for code in ("resetMaxFile()", "loadMaxFile @\"C:/a.max\"", "FETCHMAXFILE()", "quitMax()",
                     "checkForSave()", "max  reset\n file", "max file new", "max\tfile open", "max fetch",
                     "core.FileReset false", "core.FileFetch()", "core.LoadFromFile @\"C:/a.max\" 0 true"):
            fields, response = self.sent(code, quiet=True)
            self.assertEqual(fields, {"quiet": False}, code)
            self.assertEqual(response["meta"]["quietOverride"], "file_command_quiet_ignored")

    def test_allow_discard_keeps_quiet_mode(self):
        fields, response = self.sent("resetMaxFile()", quiet=True, allow_discard=True)
        self.assertEqual(fields, {"quiet": True})
        self.assertNotIn("meta", response)

    def test_quiet_true_elsewhere_is_unchanged(self):
        for code in ("1+1", "mergeMaxFile @\"C:/a.max\"", "saveMaxFile @\"C:/a.max\"", "max hold"):
            fields, response = self.sent(code, quiet=True)
            self.assertEqual(fields, {"quiet": True}, code)
            self.assertNotIn("meta", response)

    def test_default_and_quiet_false_are_unchanged(self):
        self.assertIsNone(self.sent("resetMaxFile()")[0])
        self.assertEqual(self.sent("resetMaxFile()", quiet=False)[0], {"quiet": False})
        self.assertIsNone(self.sent("resetMaxFile()", allow_discard=True)[0])

    def test_matches_the_native_policy_list(self):
        execute = _load("execute", mock.Mock())
        body = (Path(__file__).resolve().parent.parent / "native" / "include" / "mcp_bridge"
                / "quiet_policy.h").read_text(encoding="utf-8")
        names = body.split("kNames[] = {", 1)[1].split("};", 1)[0]
        self.assertEqual(set(execute._SCENE_FILE_COMMANDS), set(re.findall(r'"([^"]+)"', names)))

    def test_docstring_warns_about_discarding(self):
        doc = _load("execute", mock.Mock()).execute_maxscript.__doc__
        self.assertIn("allow_discard=True", doc)
        self.assertIn("discard", doc)
        self.assertIn("fileIn", doc)


class QuietOverrideReportTests(unittest.TestCase):
    """meta.quietOverride reaches the agent as a transport field and a warning."""

    def test_transport_carries_the_override(self):
        client = _client()
        client._local.last_response = {"requestId": "r", "meta": {"transport": "namedpipe",
                                                                 "quietOverride": "file_command"}}
        self.assertEqual(client.get_last_transport()["quiet_override"], "file_command")
        client._local.last_response = {"requestId": "r", "meta": {"transport": "namedpipe"}}
        self.assertNotIn("quiet_override", client.get_last_transport())

    def test_warning_on_success_and_error(self):
        for override, text in (("file_command", "Quiet mode was off"),
                               ("file_command_quiet_ignored", "allow_discard=True")):
            transport = {"transport": "namedpipe", "quiet_override": override}
            with _minimal():
                ok = make_structured_tool(lambda: "ok", transport_provider=lambda: transport)()
                failed = make_structured_tool(mock.Mock(side_effect=DialogBlocked("r", "maxscript", [DIALOG]),
                                                        __name__="execute_maxscript"),
                                              transport_provider=lambda: transport)()
            self.assertTrue(ok["ok"])
            self.assertIn(text, ok["warnings"][0])
            self.assertEqual(failed["error"]["code"], "BLOCKED_BY_DIALOG")
            self.assertIn(text, failed["warnings"][-1])

    def test_no_warning_without_override(self):
        with _minimal():
            ok = make_structured_tool(lambda: "ok", transport_provider=lambda: {"transport": "namedpipe"})()
        self.assertNotIn("warnings", ok)

    def test_execute_override_reaches_the_envelope(self):
        client = _client()
        execute = _load("execute", client)

        def send(command, cmd_type="maxscript", timeout=None, request_fields=None, *, probe=False):
            response = {"success": True, "result": "ok", "requestId": "r", "meta": {"transport": "namedpipe"}}
            client._local.last_response = response
            return response

        with mock.patch.object(client, "send_command", side_effect=send), _minimal():
            envelope = make_structured_tool(execute.execute_maxscript,
                                            transport_provider=client.get_last_transport)(
                code="resetMaxFile()", quiet=True)
        self.assertTrue(envelope["ok"])
        self.assertIn("quiet=True was not applied", envelope["warnings"][0])


if __name__ == "__main__":
    unittest.main()
