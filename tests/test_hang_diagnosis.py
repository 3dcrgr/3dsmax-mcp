import ctypes
import importlib
import json
import os
import subprocess
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from maxmcp import max_client, process_health, tool_response  # noqa: E402
from maxmcp.max_client import (  # noqa: E402
    MaxBusyError,
    MaxClient,
    MaxNotRespondingAfterDispatch,
    MaxNotRespondingError,
    RequestOutcomeUnknown,
)

assert Path(max_client.__file__).resolve().parent.parent == REPO_ROOT, max_client.__file__

PID = 4242
PIPE = rf"\\.\pipe\3dsmax-mcp-pid-{PID}"
HANDLE = 777
REPLY = b'{"success": true, "result": "late", "requestId": ""}\n'
QUEUE_TIMEOUT = (b'{"success": false, "error": "Internal bridge error: Main thread execution timed out", '
                 b'"requestId": ""}\n')


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps = 0

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds

    def time(self) -> float:
        return self.now


class FakeKernel:
    """Stands in for kernel32: reply chunks become readable at given clock times."""

    def __init__(self, clock: FakeClock, chunks: list[tuple[float, bytes]] | None = None,
                 empty_polls: int = 0) -> None:
        self.clock = clock
        self.chunks = list(chunks or [])
        self.empty_polls = empty_polls
        self.polls = 0
        self.closed: list[int] = []
        self.writes = 0
        self.broken = False

    def _ready(self) -> bytes | None:
        if self.polls <= self.empty_polls or not self.chunks:
            return None
        at, data = self.chunks[0]
        return data if self.clock.now >= at else None

    def PeekNamedPipe(self, handle, buf, size, read, avail, left):
        self.polls += 1
        if self.broken:
            ctypes.set_last_error(max_client._ERROR_BROKEN_PIPE)
            return 0
        data = self._ready()
        avail._obj.value = len(data) if data else 0
        return 1

    def ReadFile(self, handle, buf, size, read, overlapped):
        _, data = self.chunks.pop(0)
        ctypes.memmove(buf, data, len(data))
        read._obj.value = len(data)
        return 1

    def WriteFile(self, handle, data, size, written, overlapped):
        self.writes += 1
        written._obj.value = size
        return 1

    def CreateFileW(self, *args):
        return HANDLE

    def WaitNamedPipeW(self, *args):
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def GetNamedPipeServerProcessId(self, handle, pid):
        return 0


def diag(state: str, **extra) -> dict:
    base = {"pid": PID, "alive": state != "exited", "exit_code": 1 if state == "exited" else None,
            "main_window_found": True, "window_hung": state in ("blocked", "busy"),
            "cpu_seconds_per_second": 0.01 if state == "blocked" else 0.9, "cpu_sample_s": 1.0,
            "state": state}
    base.update(extra)
    return base


class _ClientCase(unittest.TestCase):
    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("MCP_MAX_PIPE", "MCP_MAX_PID")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = FakeClock()
        self.diagnoses: list[str] = []
        self.states: list[str] = []
        for target, value in (
            ("time", self.clock),
            ("diagnose_process", self._diagnose),
            ("quick_hung_check", mock.Mock(return_value=None)),
            ("process_start_time", mock.Mock(return_value=1)),
        ):
            p = mock.patch.object(max_client, target, value)
            p.start()
            self.addCleanup(p.stop)

    def _diagnose(self, pid, cpu_sample_s=1.0):
        state = self.states.pop(0) if len(self.states) > 1 else (self.states[0] if self.states else "unknown")
        self.diagnoses.append(state)
        self.clock.now += cpu_sample_s
        return diag(state)

    def use_kernel(self, kernel: FakeKernel) -> FakeKernel:
        p = mock.patch.object(max_client, "_kernel32", kernel)
        p.start()
        self.addCleanup(p.stop)
        return kernel

    def pinned_client(self) -> MaxClient:
        return MaxClient(transport="pipe", pipe_name=PIPE)


class ReadLoopTests(_ClientCase):
    def test_returns_data_after_several_empty_polls(self):
        kernel = self.use_kernel(FakeKernel(self.clock, [(0, b'{"a":'), (0, b' 1}\n')], empty_polls=5))
        client = self.pinned_client()
        deadline = self.clock.now + 5
        self.assertEqual(client._read_pipe_response(HANDLE, deadline, 5, PID), b'{"a": 1}\n')
        self.assertGreaterEqual(kernel.polls, 7)
        self.assertEqual(self.diagnoses, [])
        self.assertLessEqual(self.clock.sleeps, 6)

    def test_poll_backoff_is_bounded(self):
        start = self.clock.now
        self.use_kernel(FakeKernel(self.clock, [(start + 3.0, REPLY)]))
        client = self.pinned_client()
        client._read_pipe_response(HANDLE, start + 60, 60, PID)
        # Reply visible within one max poll interval of becoming available.
        self.assertLess(self.clock.now - (start + 3.0), max_client._POLL_MAX_S + 1e-9)

    def test_broken_pipe_while_reading(self):
        kernel = self.use_kernel(FakeKernel(self.clock))
        kernel.broken = True
        with self.assertRaises(BrokenPipeError):
            self.pinned_client()._read_pipe_response(HANDLE, self.clock.now + 5, 5, PID)

    def test_broken_pipe_after_dispatch_is_outcome_unknown(self):
        kernel = self.use_kernel(FakeKernel(self.clock))
        kernel.broken = True
        client = self.pinned_client()
        with self.assertRaises(RequestOutcomeUnknown) as ctx:
            client.send_command("x", timeout=5)
        self.assertNotIsInstance(ctx.exception, MaxNotRespondingError)
        self.assertIn(HANDLE, kernel.closed)


class DeadlineTests(_ClientCase):
    def test_busy_diagnosis_keeps_waiting_for_late_result(self):
        start = self.clock.now
        kernel = self.use_kernel(FakeKernel(self.clock, [(start + 70.0, REPLY)]))
        self.states = ["busy"]
        client = self.pinned_client()
        response = client.send_command("render()", timeout=1.0)
        self.assertEqual(response["result"], "late")
        self.assertGreaterEqual(len(self.diagnoses), 2)
        self.assertEqual(kernel.closed, [])
        self.assertIsNone(client._inflight)

    def test_blocked_twice_aborts_and_closes_handle(self):
        kernel = self.use_kernel(FakeKernel(self.clock))
        self.states = ["blocked"]
        client = self.pinned_client()
        with self.assertRaises(MaxNotRespondingAfterDispatch) as ctx:
            client.send_command("bad()", timeout=1.0)
        exc = ctx.exception
        self.assertIsInstance(exc, RequestOutcomeUnknown)
        self.assertEqual(exc.code, "MAX_NOT_RESPONDING")
        self.assertFalse(exc.retryable)
        self.assertEqual(self.diagnoses, ["blocked", "blocked"])
        self.assertIn(HANDLE, kernel.closed)
        self.assertTrue(exc.details["request_sent"])
        self.assertEqual(exc.details["inflight"]["cmd_type"], "maxscript")
        self.assertGreaterEqual(exc.details["blocked_for_s"], max_client._BLOCKED_CONFIRM_S)
        self.assertIn(f"PID {PID}", str(exc))
        self.assertIn("Do not replay", str(exc))
        self.assertIn("main window hung", str(exc))
        self.assertIn(PID, client._hung_pids)
        self.assertIsNone(client._inflight)
        self.assertFalse(client._pipe_lock.locked())

    def test_blocked_then_busy_resets_confirmation(self):
        start = self.clock.now
        self.use_kernel(FakeKernel(self.clock, [(start + 120.0, REPLY)]))
        self.states = ["blocked", "busy", "blocked", "busy", "busy"]
        response = self.pinned_client().send_command("x", timeout=1.0)
        self.assertEqual(response["result"], "late")

    def test_exited_aborts_immediately(self):
        kernel = self.use_kernel(FakeKernel(self.clock))
        self.states = ["exited"]
        client = self.pinned_client()
        with self.assertRaises(MaxNotRespondingAfterDispatch) as ctx:
            client.send_command("x", timeout=1.0)
        self.assertEqual(self.diagnoses, ["exited"])
        self.assertIn(HANDLE, kernel.closed)
        self.assertIn("exited", str(ctx.exception))
        self.assertNotIn(PID, client._hung_pids)

    def test_unknown_pid_keeps_waiting_without_diagnosis(self):
        start = self.clock.now
        self.use_kernel(FakeKernel(self.clock, [(start + 40.0, REPLY)]))
        client = MaxClient(transport="pipe", pipe_name=r"\\.\pipe\custom")
        self.assertEqual(client.send_command("x", timeout=1.0)["result"], "late")
        self.assertEqual(self.diagnoses, [])

    def test_fail_fast_on_remembered_hung_pid_then_recovery(self):
        kernel = self.use_kernel(FakeKernel(self.clock))
        self.states = ["blocked"]
        client = self.pinned_client()
        with self.assertRaises(MaxNotRespondingAfterDispatch):
            client.send_command("x", timeout=1.0)
        writes = kernel.writes

        max_client.quick_hung_check.return_value = True
        with self.assertRaises(MaxNotRespondingError) as ctx:
            client.send_command("y", timeout=1.0)
        self.assertNotIsInstance(ctx.exception, RequestOutcomeUnknown)
        self.assertFalse(ctx.exception.details["request_sent"])
        self.assertIn("Nothing was sent", str(ctx.exception))
        self.assertEqual(kernel.writes, writes)

        self.assertEqual(ctx.exception.details["process"]["state"], "blocked")  # fresh, not the stored one
        self.assertIn("abandoned", str(ctx.exception))

        max_client.quick_hung_check.return_value = False
        kernel.chunks = [(0, REPLY)]
        self.assertEqual(client.send_command("z", timeout=1.0)["result"], "late")
        self.assertNotIn(PID, client._hung_pids)
        self.assertEqual(kernel.writes, writes + 1)

    def remember_blocked(self, client: MaxClient) -> None:
        client._hung_pids[PID] = {"state": "blocked", "process": diag("blocked"), "inflight": None,
                                  "source": "abandoned", "started": 1}

    def test_remembered_pid_now_busy_is_cleared(self):
        kernel = self.use_kernel(FakeKernel(self.clock, [(0, REPLY)]))
        client = self.pinned_client()
        self.remember_blocked(client)
        max_client.quick_hung_check.return_value = True
        self.states = ["busy"]
        self.assertEqual(client.send_command("x", timeout=1.0)["result"], "late")
        self.assertNotIn(PID, client._hung_pids)
        self.assertEqual(kernel.writes, 1)

    def test_remembered_pid_reused_by_new_process_is_cleared(self):
        kernel = self.use_kernel(FakeKernel(self.clock, [(0, REPLY)]))
        client = self.pinned_client()
        self.remember_blocked(client)
        max_client.quick_hung_check.return_value = True
        max_client.process_start_time.return_value = 2  # different creation time: PID was reused
        self.states = ["blocked"]
        self.assertEqual(client.send_command("x", timeout=1.0)["result"], "late")
        self.assertNotIn(PID, client._hung_pids)
        self.assertEqual(self.diagnoses, [])
        self.assertEqual(kernel.writes, 1)

    def test_reply_clears_stale_verdict(self):
        self.use_kernel(FakeKernel(self.clock, [(0, REPLY)]))
        client = self.pinned_client()
        self.remember_blocked(client)
        max_client.quick_hung_check.return_value = False
        client.send_command("x", timeout=1.0)
        self.assertEqual(client._hung_pids, {})


class QueueTimeoutTests(_ClientCase):
    """Native cancels work its main thread never started after 120 s (before Python's grace)."""

    def test_blocked_main_thread_is_not_responding_and_remembered(self):
        kernel = self.use_kernel(FakeKernel(self.clock, [(self.clock.now + 120.0, QUEUE_TIMEOUT)]))
        self.states = ["blocked"]
        client = self.pinned_client()
        with self.assertRaises(MaxNotRespondingError) as ctx:
            client.send_command("x", timeout=120.0)
        exc = ctx.exception
        self.assertNotIsInstance(exc, RequestOutcomeUnknown)
        self.assertFalse(exc.retryable)
        self.assertFalse(exc.details["executed"])
        self.assertIn("never picked up", str(exc))
        self.assertEqual(self.diagnoses, ["blocked"])
        self.assertEqual(client._hung_pids[PID]["source"], "queue_timeout")
        self.assertNotIn(HANDLE, kernel.closed)  # the reply arrived, so the stream is still in sync

        max_client.quick_hung_check.return_value = True
        writes = kernel.writes
        with self.assertRaises(MaxNotRespondingError) as ctx:
            client.send_command("y", timeout=120.0)
        self.assertIn("never picked up", str(ctx.exception))
        self.assertEqual(kernel.writes, writes)

    def test_busy_main_thread_is_retryable_busy_without_tcp_fallback(self):
        self.use_kernel(FakeKernel(self.clock, [(0, QUEUE_TIMEOUT)]))
        self.states = ["busy"]
        client = MaxClient()  # auto transport, nothing pinned
        with mock.patch.object(client, "_resolve_pipe_name", return_value=PIPE), \
                mock.patch.object(client, "_send_via_tcp") as tcp:
            with self.assertRaises(MaxBusyError) as ctx:
                client.send_command("x", timeout=120.0)
        tcp.assert_not_called()
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(client._hung_pids, {})

    def test_responsive_keeps_bridge_error(self):
        self.use_kernel(FakeKernel(self.clock, [(0, QUEUE_TIMEOUT)]))
        self.states = ["responsive"]
        with self.assertRaises(max_client.MaxBridgeError):
            self.pinned_client().send_command("x", timeout=120.0)

    def test_ordinary_error_reply_is_not_diagnosed(self):
        self.use_kernel(FakeKernel(self.clock, [(0, b'{"success": false, "error": "boom"}\n')]))
        with self.assertRaises(max_client.MaxBridgeError):
            self.pinned_client().send_command("x", timeout=120.0)
        self.assertEqual(self.diagnoses, [])


class ProbeTests(_ClientCase):
    """A status ping is read-only, so it is dropped at its own deadline."""

    def test_blocked_ping_returns_within_its_timeout(self):
        start = self.clock.now
        kernel = self.use_kernel(FakeKernel(self.clock))
        self.states = ["blocked"]
        client = self.pinned_client()
        with self.assertRaises(MaxNotRespondingError) as ctx:
            client.send_command("", cmd_type="ping", timeout=5.0)
        self.assertNotIsInstance(ctx.exception, RequestOutcomeUnknown)
        self.assertLess(self.clock.now - start, 5.0 + max_client._CPU_SAMPLE_S + 0.5)
        self.assertEqual(self.diagnoses, ["blocked"])
        self.assertIn(HANDLE, kernel.closed)
        self.assertEqual(client._hung_pids, {})  # one sample is not a remembered verdict
        self.assertFalse(client._pipe_lock.locked())

    def test_busy_ping_is_busy(self):
        self.use_kernel(FakeKernel(self.clock))
        self.states = ["busy"]
        with self.assertRaises(MaxBusyError) as ctx:
            self.pinned_client().send_command("", cmd_type="ping", timeout=5.0)
        self.assertTrue(ctx.exception.details["request_sent"])

    def test_unknown_pid_ping_is_busy_without_diagnosis(self):
        self.use_kernel(FakeKernel(self.clock))
        client = MaxClient(transport="pipe", pipe_name=r"\\.\pipe\custom")
        with self.assertRaises(MaxBusyError):
            client.send_command("", cmd_type="ping", timeout=5.0)
        self.assertEqual(self.diagnoses, [])

    def test_get_bridge_status_on_hung_max_is_structured_and_fast(self):
        start = self.clock.now
        self.use_kernel(FakeKernel(self.clock))
        self.states = ["blocked"]
        payload = json.loads(_load_bridge(self.pinned_client()).get_bridge_status())
        self.assertFalse(payload["pong"])
        self.assertEqual(payload["bridge_state"], "not_responding")
        self.assertEqual(payload["process"]["state"], "blocked")
        self.assertLess(self.clock.now - start, 10.0)

    def test_maxscript_is_not_a_probe(self):
        start = self.clock.now
        self.use_kernel(FakeKernel(self.clock, [(start + 8.0, REPLY)]))
        self.states = ["blocked"]
        self.assertEqual(self.pinned_client().send_command("x", timeout=5.0)["result"], "late")
        self.assertEqual(self.diagnoses, [])


class LockTimeoutTests(_ClientCase):
    def hold_lock(self, client: MaxClient, cmd_type: str = "maxscript", timeout_s: float = 120.0) -> None:
        client._pipe_lock.acquire()
        self.addCleanup(client._pipe_lock.release)
        client._inflight = {"cmd_type": cmd_type, "request_id": "abc", "target_pipe": PIPE,
                            "target_pid": PID, "timeout_s": timeout_s, "started": self.clock.now - 312}

    def test_lock_timeout_raises_busy_without_tcp_fallback(self):
        client = MaxClient()  # auto transport, nothing pinned
        self.hold_lock(client)
        self.states = ["busy"]
        with mock.patch.object(client, "_send_via_tcp") as tcp:
            with self.assertRaises(MaxBusyError) as ctx:
                client.send_command("x", timeout=0.05)
        tcp.assert_not_called()
        exc = ctx.exception
        self.assertEqual(exc.code, "MAX_BUSY")
        self.assertTrue(exc.retryable)
        self.assertEqual(exc.details["inflight"]["cmd_type"], "maxscript")
        self.assertEqual(exc.details["inflight"]["request_id"], "abc")
        self.assertGreaterEqual(exc.details["inflight"]["running_s"], 312)
        self.assertEqual(exc.details["process"]["state"], "busy")
        self.assertFalse(exc.details["request_sent"])
        self.assertIn("Nothing was sent", str(exc))

    def test_lock_timeout_on_blocked_max_is_not_responding(self):
        client = MaxClient()
        self.hold_lock(client)
        self.states = ["blocked"]
        with mock.patch.object(client, "_send_via_tcp") as tcp:
            with self.assertRaises(MaxNotRespondingError) as ctx:
                client.send_command("x", timeout=0.05)
        tcp.assert_not_called()
        self.assertNotIsInstance(ctx.exception, RequestOutcomeUnknown)
        self.assertIn("not responding", str(ctx.exception))
        self.assertIn("312 s", str(ctx.exception))
        self.assertEqual(client._hung_pids, {})  # a waiter's single sample is never remembered

    def test_lock_timeout_on_blocked_but_not_overdue_is_busy(self):
        client = MaxClient()
        self.hold_lock(client, timeout_s=600.0)  # long render: 312 s is within its budget
        self.states = ["blocked"]
        with self.assertRaises(MaxBusyError) as ctx:
            client.send_command("x", timeout=0.05)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.details["process"]["state"], "blocked")
        self.assertIn("may be blocked", str(ctx.exception))
        self.assertEqual(client._hung_pids, {})

    def test_lock_timeout_on_exited_max_is_not_responding(self):
        client = MaxClient()
        self.hold_lock(client, timeout_s=600.0)
        self.states = ["exited"]
        with self.assertRaises(MaxNotRespondingError):
            client.send_command("x", timeout=0.05)

    def test_instance_management_does_not_block_forever(self):
        client = MaxClient()
        self.hold_lock(client)
        self.states = ["busy"]
        with mock.patch.object(max_client, "INSTANCE_LOCK_TIMEOUT", 0.05):
            for call in (client.list_max_instances, client.get_selected_max_instance,
                         client.release_max_instance, lambda: client.select_max_instance(PID)):
                with self.assertRaises(MaxBusyError):
                    call()

    def test_control_channel_acquire_stays_non_blocking(self):
        client = MaxClient()
        self.hold_lock(client)
        with self.assertRaises(ConnectionError):
            client.send_command("", cmd_type="native:render_cancel", timeout=1.0)
        self.assertEqual(self.diagnoses, [])


class ToolResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        p = mock.patch.dict(os.environ, {"MCP_TRIPBACK_MODE": "minimal"})
        p.start()
        self.addCleanup(p.stop)

    def test_busy_code_beats_timed_out_heuristic(self):
        exc = MaxBusyError("request timed out; busy", {"inflight": {"cmd_type": "maxscript"}, "obj": object()})
        error = tool_response.envelope_exception(exc, elapsed_ms=1.0)["error"]
        self.assertEqual(error["code"], "MAX_BUSY")
        self.assertTrue(error["retryable"])
        self.assertEqual(error["details"]["inflight"]["cmd_type"], "maxscript")
        json.dumps(error)

    def test_not_responding_after_dispatch(self):
        exc = MaxNotRespondingAfterDispatch("3ds Max (PID 1) is not responding", {"request_sent": True})
        error = tool_response._error_from_exception(exc)
        self.assertEqual(error["code"], tool_response.ErrorCode.MAX_NOT_RESPONDING.value)
        self.assertFalse(error["retryable"])
        self.assertTrue(error["details"]["request_sent"])

    def test_plain_exception_has_no_details(self):
        error = tool_response._error_from_exception(TimeoutError("named pipe timed out"))
        self.assertEqual(error["code"], "BRIDGE_DOWN")
        self.assertNotIn("details", error)

    def test_message_heuristics(self):
        self.assertEqual(tool_response._classify_error_code("3ds Max (PID 2) is not responding: timed out"),
                         tool_response.ErrorCode.MAX_NOT_RESPONDING)
        self.assertEqual(tool_response._classify_error_code("still busy with another request; waited 5s"),
                         tool_response.ErrorCode.MAX_BUSY)
        self.assertIn(tool_response.ErrorCode.MAX_BUSY, tool_response._RETRYABLE_CODES)
        self.assertEqual(tool_response._classify_error_code("Main thread execution timed out"),
                         tool_response.ErrorCode.BRIDGE_DOWN)
        self.assertNotIn(tool_response.ErrorCode.MAX_NOT_RESPONDING, tool_response._RETRYABLE_CODES)


def _load_bridge(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.bridge", None)
        return importlib.import_module("maxmcp.tools.bridge")


class BridgeStatusTests(unittest.TestCase):
    def test_busy_status_is_structured(self):
        client = mock.Mock()
        client.send_command.side_effect = MaxBusyError(
            "busy", {"inflight": {"cmd_type": "maxscript", "running_s": 40}, "process": diag("busy"),
                     "request_sent": False})
        payload = json.loads(_load_bridge(client).get_bridge_status())
        self.assertFalse(payload["pong"])
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["bridge_state"], "busy")
        self.assertEqual(payload["inflight"]["cmd_type"], "maxscript")
        self.assertEqual(payload["process"]["state"], "busy")
        envelope = tool_response.envelope_result(json.dumps(payload), elapsed_ms=1.0)
        self.assertTrue(envelope["ok"])

    def test_not_responding_status(self):
        client = mock.Mock()
        client.send_command.side_effect = MaxNotRespondingError("3ds Max (PID 1) is not responding",
                                                                {"process": diag("blocked")})
        payload = json.loads(_load_bridge(client).get_bridge_status())
        self.assertEqual(payload["bridge_state"], "not_responding")
        self.assertFalse(payload["connected"])
        self.assertEqual(payload["bridge_code"], "MAX_NOT_RESPONDING")

    def test_legacy_ping_path_still_works(self):
        client = mock.Mock()
        legacy = {"result": json.dumps({"pong": True}), "requestId": "r", "meta": {}}

        def send(command, cmd_type="maxscript", timeout=None):
            if cmd_type in ("health", "ping"):
                raise RuntimeError(f"Unknown command type: {cmd_type}")
            return legacy

        client.send_command.side_effect = send
        payload = json.loads(_load_bridge(client).get_bridge_status())
        self.assertTrue(payload["pong"])
        self.assertTrue(payload["legacyTransport"])

    def test_healthy_ping(self):
        client = mock.Mock()
        client.send_command.return_value = {"result": json.dumps({"pong": True}), "requestId": "r", "meta": {}}
        payload = json.loads(_load_bridge(client).get_bridge_status())
        self.assertTrue(payload["pong"])
        self.assertFalse(payload["legacyTransport"])


@unittest.skipUnless(sys.platform == "win32", "Windows named pipes")
class RealPipeTests(unittest.TestCase):
    """Exercise the real kernel32 polling path against a pipe served by this test process."""

    def test_round_trip_with_delayed_chunked_reply(self):
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateNamedPipeW.restype = wintypes.HANDLE
        k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        k32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        name = rf"\\.\pipe\3dsmax-mcp-hangtest-{os.getpid()}-{threading.get_ident()}"
        pipe = k32.CreateNamedPipeW(name, 0x3, 0, 1, 65536, 65536, 0, None)  # duplex, byte mode
        self.assertNotEqual(pipe, max_client._INVALID_HANDLE)
        received = {}

        def serve():
            k32.ConnectNamedPipe(pipe, None)
            buf = ctypes.create_string_buffer(65536)
            got = wintypes.DWORD()
            data = b""
            while b"\n" not in data:
                if not k32.ReadFile(pipe, buf, len(buf), ctypes.byref(got), None):
                    return
                data += buf.raw[:got.value]
            received["request"] = json.loads(data)
            reply = json.dumps({"success": True, "result": "x" * 100000,
                                "requestId": received["request"]["requestId"]}).encode() + b"\n"
            threading.Event().wait(0.2)
            for part in (reply[:70000], reply[70000:]):
                k32.WriteFile(pipe, part, len(part), ctypes.byref(got), None)
                threading.Event().wait(0.05)
            k32.FlushFileBuffers(pipe)

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MCP_MAX_PIPE", None)
                os.environ.pop("MCP_MAX_PID", None)
                client = MaxClient(transport="pipe", pipe_name=name)
                response = client.send_command("ping()", timeout=10)
            self.assertEqual(len(response["result"]), 100000)
            self.assertEqual(received["request"]["command"], "ping()")
            self.assertEqual(client._pid_for_pipe(name, client._pipe_handle), os.getpid())
            client._close_pipe_handle()
        finally:
            server.join(5)
            k32.DisconnectNamedPipe(pipe)
            k32.CloseHandle(pipe)


@unittest.skipUnless(sys.platform == "win32", "Windows process probes")
class ProcessHealthTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        result = process_health.diagnose_process(os.getpid(), cpu_sample_s=0.05)
        self.assertTrue(result["alive"])
        self.assertNotEqual(result["state"], "exited")
        self.assertIsNotNone(result["cpu_seconds_per_second"])
        self.assertIn(process_health.quick_hung_check(os.getpid()), (None, True, False))

    def test_terminated_child_is_exited(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            running = process_health.diagnose_process(child.pid, cpu_sample_s=0.05)
            self.assertTrue(running["alive"])
            self.assertNotEqual(running["state"], "exited")
            child.terminate()
            child.wait(10)
            # Popen still holds its handle, so the PID cannot be reused yet.
            result = process_health.diagnose_process(child.pid, cpu_sample_s=0.05)
            self.assertFalse(result["alive"])
            self.assertEqual(result["state"], "exited")
            self.assertEqual(result["exit_code"], child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(10)

    def test_process_start_time(self):
        first = process_health.process_start_time(os.getpid())
        self.assertIsInstance(first, int)
        self.assertEqual(first, process_health.process_start_time(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertIsInstance(process_health.process_start_time(child.pid), int)
            child.terminate()
            child.wait(10)
            self.assertIsNone(process_health.process_start_time(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(10)
        for pid in (0xFFFFFFF0, 0, -1, True, "12"):
            self.assertIsNone(process_health.process_start_time(pid))

    def test_impossible_pids_do_not_raise(self):
        for pid in (0xFFFFFFF0, 0, -1, True, "12"):
            result = process_health.diagnose_process(pid, cpu_sample_s=0.0)
            self.assertIn(result["state"], ("exited", "unknown"))
            self.assertIsNone(process_health.quick_hung_check(pid))

    def _classify(self, hung, cpu_before, cpu_after):
        clock = FakeClock()
        with mock.patch.object(process_health, "time", clock), \
                mock.patch.object(process_health, "_windows_hung", return_value=(hung is not None, hung)), \
                mock.patch.object(process_health, "_cpu_seconds", side_effect=[cpu_before, cpu_after]):
            return process_health.diagnose_process(os.getpid(), cpu_sample_s=1.0)

    def test_state_classification(self):
        self.assertEqual(self._classify(True, 10.0, 10.01)["state"], "blocked")
        self.assertEqual(self._classify(True, 10.0, 10.9)["state"], "busy")
        self.assertEqual(self._classify(False, 10.0, 10.0)["state"], "responsive")
        self.assertEqual(self._classify(None, 10.0, 10.0)["state"], "unknown")
        self.assertEqual(self._classify(True, None, None)["state"], "unknown")
        self.assertIn("main window hung, 0.01 CPU-s/s over 1.0 s",
                      process_health.describe(self._classify(True, 10.0, 10.01)))

    def test_failures_become_unknown(self):
        with mock.patch.object(process_health, "_open_process", side_effect=OSError("boom")):
            result = process_health.diagnose_process(os.getpid(), cpu_sample_s=0.0)
        self.assertEqual(result["state"], "unknown")
        self.assertIn("boom", result["error"])

    def test_handles_are_closed(self):
        closed = []
        real_close = process_health._kernel32.CloseHandle
        fake = mock.Mock(wraps=process_health._kernel32)
        fake.CloseHandle.side_effect = lambda h: (closed.append(h), real_close(h))[1]
        with mock.patch.object(process_health, "_kernel32", fake):
            process_health.diagnose_process(os.getpid(), cpu_sample_s=0.0)
        self.assertEqual(len(closed), 1)
        rights = fake.OpenProcess.call_args_list[0].args[0]
        self.assertEqual(rights & ~(process_health._PROCESS_QUERY_LIMITED_INFORMATION | process_health._SYNCHRONIZE), 0)


if __name__ == "__main__":
    unittest.main()
