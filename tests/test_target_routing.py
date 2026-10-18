"""#19 (shared selection: selection_generation, expected_pid) and #22 (default target re-resolves after its Max exits)."""
import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from maxmcp import max_client, tool_response  # noqa: E402
from maxmcp.max_client import MaxClient, MaxTargetMismatchError, expect_pid  # noqa: E402

assert Path(max_client.__file__).resolve().parent.parent == REPO_ROOT, max_client.__file__

OLD, NEW = 4242, 5151
OLD_PIPE = rf"\\.\pipe\3dsmax-mcp-pid-{OLD}"
NEW_PIPE = rf"\\.\pipe\3dsmax-mcp-pid-{NEW}"
THIRD = 6262
THIRD_PIPE = rf"\\.\pipe\3dsmax-mcp-pid-{THIRD}"
HANDLE = 777
REPLY = b'{"success": true, "result": "ok", "requestId": ""}\n'


class FakeKernel:
    def __init__(self) -> None:
        self.writes = 0
        self.closed: list[int] = []

    def WriteFile(self, handle, data, size, written, overlapped):
        self.writes += 1
        written._obj.value = size
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("MCP_MAX_PIPE", "MCP_MAX_PID")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gone: set[int] = set()
        self.live = {OLD_PIPE: True, NEW_PIPE: True}
        self.default_pipe = OLD_PIPE
        self.default_source = "default"
        self.kernel = FakeKernel()
        self.opened: list[str] = []
        for target, value in (
            ("_kernel32", self.kernel),
            ("process_gone", lambda pid, started=None: pid in self.gone),
            ("process_start_time", mock.Mock(return_value=1)),
            ("quick_hung_check", mock.Mock(return_value=None)),
        ):
            p = mock.patch.object(max_client, target, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (
            ("_ensure_pipe_handle", lambda client, deadline, pipe: self._open(client, deadline, pipe)),
            ("_await_response", lambda *a, **k: REPLY),
            ("_default_target", lambda client: client._target(self.default_pipe, self.default_source)),
            ("_live_instances", lambda client: [{"pipe": pipe, "updated": 0} for pipe, up in self.live.items() if up]),
            ("_probe_pipe_available", lambda client, pipe=None: self.live.get(pipe, False)),
        ):
            p = mock.patch.object(MaxClient, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = MaxClient(transport="pipe")

    def _open(self, client, deadline, pipe_name):
        if not self.live.get(pipe_name):
            raise ConnectionError(f"Named pipe {pipe_name} not found.")
        self.opened.append(pipe_name)
        client._pipe_handle = HANDLE
        return HANDLE

    def send(self, client=None):
        client = client or self.client
        client.send_command("1")
        return client.get_last_transport()

    def max_exits(self, pid=OLD, pipe=OLD_PIPE):
        self.gone.add(pid)
        self.live[pipe] = False


class DefaultRebindTests(_Case):
    """#22: a target the server picked itself follows the default once its Max has exited."""

    def test_exited_default_re_resolves_with_a_note(self):
        first = self.send()
        self.assertEqual((first["target_pid"], first["target_source"]), (OLD, "default"))
        self.max_exits()
        self.default_pipe = NEW_PIPE
        moved = self.send()
        self.assertEqual((moved["target_pid"], moved["target_source"]), (NEW, "default"))
        self.assertEqual(moved["retargeted_from"], {"pid": OLD, "pipe": OLD_PIPE, "source": "default"})
        self.assertGreater(moved["selection_generation"], first["selection_generation"])
        self.assertNotIn("retargeted_from", self.send())  # reported once

    def test_two_other_instances_refuse_instead_of_guessing(self):
        self.send()
        self.max_exits()
        self.live[THIRD_PIPE] = True  # e.g. a production Max and a scratch Max both running
        self.default_pipe = NEW_PIPE
        with self.assertRaisesRegex(max_client.AmbiguousMaxInstanceError, f"PIDs {NEW}, {THIRD}") as ctx:
            self.send()
        self.assertIn("Nothing was sent", str(ctx.exception))
        self.assertEqual(self.client._bound_target["target_pid"], OLD)
        self.assertEqual(self.opened, [OLD_PIPE])
        self.default_source = "claim"  # the user claimed one in Max: unambiguous again
        self.assertEqual(self.send()["target_pid"], NEW)

    def test_auto_transport_keeps_the_ambiguity_error(self):
        client = MaxClient()
        self.send(client)
        self.max_exits()
        self.live[THIRD_PIPE] = True
        with mock.patch.object(MaxClient, "_send_via_tcp") as tcp:
            with self.assertRaises(max_client.AmbiguousMaxInstanceError):
                client.send_command("1")
        tcp.assert_not_called()

    def test_claimed_target_also_re_resolves(self):
        self.client._bind(self.client._target(OLD_PIPE, "claim"))
        self.max_exits()
        self.default_pipe = NEW_PIPE
        self.assertEqual(self.send()["target_pid"], NEW)

    def test_explicit_selection_still_fails_hard(self):
        self.client.select_max_instance(OLD)
        self.max_exits()
        self.default_pipe = NEW_PIPE
        with self.assertRaisesRegex(ConnectionError, "not found"):
            self.send()
        self.assertEqual(self.client._bound_target["target_pid"], OLD)
        self.assertNotIn(NEW_PIPE, self.opened)

    def test_environment_target_still_fails_hard(self):
        with mock.patch.dict(os.environ, {"MCP_MAX_PID": str(OLD)}):
            client = MaxClient(transport="pipe")
        self.send(client)
        self.max_exits()
        self.default_pipe = NEW_PIPE
        with self.assertRaises(ConnectionError):
            self.send(client)
        self.assertNotIn(NEW_PIPE, self.opened)

    def test_alive_default_without_pipe_is_kept(self):
        self.send()
        self.live[OLD_PIPE] = False  # bridge gone, process alive (busy, hung, restarting the bridge)
        self.default_pipe = NEW_PIPE
        with self.assertRaises(ConnectionError):
            self.send()
        self.assertEqual(self.client._bound_target["target_pid"], OLD)

    def test_control_channel_never_re_resolves(self):
        self.send()
        self.max_exits()
        self.default_pipe = NEW_PIPE
        control = MaxClient(transport="pipe", pipe_name=OLD_PIPE)
        control._control_channel = True
        control._bound_target = dict(self.client._bound_target)
        with self.assertRaises(ConnectionError):
            control.send_command("", cmd_type="native:capture_screen")
        self.assertNotIn(NEW_PIPE, self.opened)

    def test_get_selected_reports_an_exited_default_without_switching(self):
        self.send()
        self.max_exits()
        selected = self.client.get_selected_max_instance()
        self.assertTrue(selected["exited"])
        self.assertFalse(selected["available"])
        self.assertEqual(self.client._bound_target["target_pid"], OLD)

    def test_warning_reaches_the_envelope(self):
        transport = {"target_pid": NEW, "retargeted_from": {"pid": OLD, "pipe": OLD_PIPE, "source": "default"}}
        tool = tool_response.make_structured_tool(lambda: "ok", transport_provider=lambda: transport)
        warnings = tool()["warnings"]
        self.assertTrue(any(f"(PID {OLD}) has exited" in w and f"PID {NEW}" in w for w in warnings), warnings)


class SharedSelectionTests(_Case):
    """#19: the selection is per server process; make moves visible and let calls pin their Max."""

    def test_generation_changes_on_select_and_release(self):
        g0 = self.client.get_selected_max_instance()["selection_generation"]
        g1 = self.client.select_max_instance(OLD)["selection_generation"]
        g2 = self.client.select_max_instance(NEW)["selection_generation"]
        g3 = self.client.release_max_instance()["selection_generation"]
        self.assertEqual(len({g0, g1, g2, g3}), 4)
        self.assertLess(g0, g1)
        self.assertLess(g2, g3)

    def test_generation_rides_on_every_transport(self):
        gen = self.client.select_max_instance(NEW)["selection_generation"]
        self.assertEqual(self.send()["selection_generation"], gen)

    def test_replacing_an_explicit_selection_warns(self):
        self.assertNotIn("warning", self.client.select_max_instance(OLD))
        result = self.client.select_max_instance(NEW)
        self.assertEqual(result["previous"], {"target_pid": OLD, "target_source": "explicit"})
        self.assertIn("expected_pid", result["warning"])

    def test_expected_pid_mismatch_sends_nothing(self):
        self.client.select_max_instance(NEW)  # another session moved the shared selection
        with expect_pid(OLD), self.assertRaises(MaxTargetMismatchError) as ctx:
            self.send()
        self.assertEqual(self.kernel.writes, 0)
        self.assertEqual(self.opened, [])
        err = ctx.exception
        self.assertEqual((err.code, err.retryable), ("TARGET_MISMATCH", False))
        self.assertEqual(err.details["expected_pid"], OLD)
        self.assertEqual(err.details["target_pid"], NEW)
        self.assertFalse(err.details["request_sent"])
        self.assertIn(f"select_max_instance({OLD})", str(err))

    def test_expected_pid_match_sends(self):
        self.client.select_max_instance(OLD)
        with expect_pid(OLD):
            self.assertEqual(self.send()["target_pid"], OLD)
        self.assertEqual(self.kernel.writes, 1)

    def test_mismatch_never_binds_a_default(self):
        with expect_pid(NEW), self.assertRaises(MaxTargetMismatchError):
            self.send()
        self.assertIsNone(self.client._bound_target)

    def test_auto_transport_reraises_the_mismatch(self):
        client = MaxClient()  # auto: pipe then TCP
        client.select_max_instance(NEW)
        with mock.patch.object(MaxClient, "_send_via_tcp") as tcp, expect_pid(OLD):
            with self.assertRaises(MaxTargetMismatchError):
                client.send_command("1")
        tcp.assert_not_called()

    def test_tcp_cannot_verify_a_pid(self):
        client = MaxClient(transport="tcp")
        with mock.patch.object(MaxClient, "_send_via_tcp") as tcp, expect_pid(OLD):
            with self.assertRaisesRegex(MaxTargetMismatchError, "TCP"):
                client.send_command("1")
        tcp.assert_not_called()

    def test_guard_is_scoped_to_the_call(self):
        with expect_pid(OLD):
            pass
        self.client.select_max_instance(NEW)
        self.assertEqual(self.send()["target_pid"], NEW)
        with self.assertRaises(ValueError):
            with expect_pid(True):
                pass


class WrapperTests(unittest.TestCase):
    def test_guarded_tool_advertises_and_applies_expected_pid(self):
        seen = []

        def tool(name: str, *, quiet: bool = True, **extra) -> str:
            seen.append(getattr(max_client._call_guard, "pid", None))
            return "ok"

        wrapped = tool_response.make_structured_tool(tool, pid_guard=True)
        params = inspect.signature(wrapped).parameters
        self.assertEqual(list(params), ["name", "quiet", "expected_pid", "extra"])
        self.assertIsNone(params["expected_pid"].default)
        self.assertTrue(wrapped("a", expected_pid=OLD)["ok"])
        self.assertTrue(wrapped("a")["ok"])
        self.assertEqual(seen, [OLD, None])
        self.assertIsNone(getattr(max_client._call_guard, "pid", None))

    def test_mismatch_becomes_a_target_mismatch_envelope(self):
        def tool() -> str:
            raise MaxTargetMismatchError("moved", {"expected_pid": OLD, "target_pid": NEW})

        envelope = tool_response.make_structured_tool(tool, pid_guard=True)(expected_pid=OLD)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "TARGET_MISMATCH")
        self.assertFalse(envelope["error"]["retryable"])

    def test_unguarded_and_existing_params_untouched(self):
        def tool(expected_pid: int) -> int:
            return expected_pid

        self.assertNotIn("expected_pid", inspect.signature(
            tool_response.make_structured_tool(lambda: 1)).parameters)
        own = tool_response.make_structured_tool(tool, pid_guard=True)
        self.assertEqual(list(inspect.signature(own).parameters), ["expected_pid"])
        self.assertEqual(own(expected_pid=3)["result"], 3)

    def test_server_guards_every_tool_but_routing(self):
        from maxmcp import server
        self.assertFalse(server._pid_guarded(type("F", (), {"__module__": "maxmcp.tools.routing"})))
        self.assertTrue(server._pid_guarded(type("F", (), {"__module__": "maxmcp.tools.execute"})))


class PipeGapTests(unittest.TestCase):
    """The bridge has no pipe instance for a moment when several connections close at once."""

    def _open(self, results):
        kernel = mock.Mock()
        kernel.CreateFileW.side_effect = [max_client._INVALID_HANDLE if r is None else r for r in results]
        errors = iter([max_client._ERROR_FILE_NOT_FOUND] * len(results))
        client = MaxClient(transport="pipe")
        with mock.patch.object(max_client, "_kernel32", kernel), \
                mock.patch.object(max_client.ctypes, "get_last_error", lambda: next(errors)), \
                mock.patch.object(max_client.time, "sleep", lambda s: None):
            return client._ensure_pipe_handle(max_client.time.perf_counter() + 5, r"\\.\pipe\x"), kernel

    def test_short_gap_is_retried(self):
        handle, kernel = self._open([None, None, 1234])
        self.assertEqual(handle, 1234)
        self.assertEqual(kernel.CreateFileW.call_count, 3)

    def test_pipe_gone_for_good_still_fails(self):
        clock = iter([0.0, 0.1, 0.3, 0.6])
        with mock.patch.object(max_client.time, "perf_counter", lambda: next(clock, 1.0)):
            with self.assertRaises(ConnectionError):
                self._open([None] * 10)


if __name__ == "__main__":
    unittest.main()
