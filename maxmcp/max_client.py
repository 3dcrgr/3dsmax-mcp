import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from uuid import uuid4

from .process_health import describe as describe_process
from .process_health import diagnose_process, process_start_time, quick_hung_check, window_hung

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 120.0
DEFAULT_PIPE_NAME = r"\\.\pipe\3dsmax-mcp"
MCP_PIPE_ENV = "MCP_MAX_PIPE"


class RequestOutcomeUnknown(Exception):
    """The request reached Max but its response was lost; never replay it."""


class MaxHealthError(Exception):
    """Max cannot take a request right now; carries code/retryable/details."""

    code = "MAX_BUSY"
    retryable = True

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class MaxBusyError(MaxHealthError):
    """Max is occupied (pipe held or main thread busy); the request did not run, so it may be retried."""


class MaxNotRespondingError(MaxHealthError):
    """Max is hung or gone. Raised before sending unless it is also RequestOutcomeUnknown."""

    code = "MAX_NOT_RESPONDING"
    retryable = False


class MaxNotRespondingAfterDispatch(MaxNotRespondingError, RequestOutcomeUnknown):
    """Max stopped responding after the request was written; never replay it."""


class MaxImportSettlingError(MaxBusyError):
    """A native import left Max (or its Cosmos browser window) not responding; nothing was sent."""

    code = "IMPORT_SETTLING"


BLOCKED_CAUSE = "deadlock, stalled I/O, or a long blocking call (possibly from another MCP client)"
HANG_ADVICE = ("Some stalls (V-Ray Material Editor preview) cleared in 5-8 min, but the cross-thread deadlock "
               "after a Cosmos import never did: wait up to ~10 min, re-checking with get_bridge_status. If Max "
               "is still blocked after that, treat it as a deadlock: the user must end the process (unsaved "
               "work is lost).")

# Import settling guard, shared by every MaxClient (Cosmos imports use their own client).
SETTLING_MAX_S = 900.0  # hard expiry: the guard can never lock the user out for good
_settling_lock = threading.Lock()
_settling: dict[int, dict[str, Any]] = {}


def mark_settling(pid: int, windows: dict[int, str], reason: str, evidence: Any = None,
                  max_seconds: float = SETTLING_MAX_S, owner: Any = None) -> dict[str, Any]:
    """Refuse requests to `pid` (nothing sent) while any of `windows` {hwnd: title} is hung.

    With `owner` (an import in progress) every other client is refused outright;
    only the owner client may send. Returns the entry for release_settling.
    """
    now = time.perf_counter()
    entry = {"windows": {int(h): str(t) for h, t in windows.items()}, "reason": reason, "evidence": evidence,
             "since": now, "until": now + max(1.0, max_seconds), "started": process_start_time(pid),
             "owner": owner}
    with _settling_lock:
        _settling[pid] = entry
    return entry


def release_settling(pid: int, entry: dict[str, Any] | None) -> None:
    """Drop `entry` only if it is still the PID's guard (a newer one is kept)."""
    if entry is not None:
        _drop_settling(pid, entry)


def clear_settling(pid: int) -> None:
    with _settling_lock:
        _settling.pop(pid, None)


def settling_state(pid: int) -> dict[str, Any] | None:
    with _settling_lock:
        entry = _settling.get(pid)
        return dict(entry) if entry else None


def _drop_settling(pid: int, entry: dict[str, Any]) -> None:
    with _settling_lock:
        if _settling.get(pid) is entry:
            _settling.pop(pid, None)


# Win32 constants for named pipe
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_BROKEN_PIPE = 109
_ERROR_SEM_TIMEOUT = 121
_ERROR_PIPE_BUSY = 231

# CreateFileW returns HANDLE; set proper return type for correct comparison
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.WaitNamedPipeW.restype = wintypes.BOOL
_kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_kernel32.WriteFile.restype = wintypes.BOOL
_kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_kernel32.ReadFile.restype = wintypes.BOOL
_kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_kernel32.PeekNamedPipe.restype = wintypes.BOOL
_kernel32.PeekNamedPipe.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
_kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
_INVALID_HANDLE = wintypes.HANDLE(-1).value

INSTANCE_LOCK_TIMEOUT = 10.0
_POLL_MIN_S = 0.002
_POLL_MAX_S = 0.05
_REDIAGNOSE_S = 15.0
_BLOCKED_CONFIRM_S = 20.0
_CPU_SAMPLE_S = 1.0
_RECHECK_SAMPLE_S = 0.25
# Read-only probes: safe to drop at their deadline instead of waiting out a hung Max.
_PROBE_CMD_TYPES = frozenset({"ping", "health"})
# Answered on a bridge pipe thread without touching Max's main thread. Sent on the
# control channel so a stuck request holding the pipe lock cannot delay them.
_MAIN_THREAD_FREE_CMD_TYPES = frozenset({"health"})
_CONTROL_CMD_TYPES = frozenset({"native:render_cancel", "native:render_cancel_capture",
                                "native:capture_screen"}) | _MAIN_THREAD_FREE_CMD_TYPES
# Native ExecuteSync cancels work the main thread never started after 120 s.
_QUEUE_TIMEOUT_MARKER = "main thread execution timed out"


def _grace(timeout: float) -> float:
    return max(10.0, 0.1 * timeout)


class AmbiguousMaxInstanceError(ConnectionError):
    """Raised when multiple live Max native bridges exist and none is claimed."""


class MaxBridgeError(Exception):
    """Raised when the native/TCP bridge returns a structured error response."""

    def __init__(self, message: str, response: dict[str, Any]) -> None:
        self.bridge_message = message
        self.bridge_response = response
        super().__init__(f"MAXScript error: {message}")


class MaxClient:
    """Client that sends commands to 3ds Max via named pipe or TCP."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        transport: str = "auto",
        pipe_name: str = DEFAULT_PIPE_NAME,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.transport = transport
        self.pipe_name = pipe_name
        self._pipe_handle: Optional[int] = None
        self._selected_pipe_name: Optional[str] = None
        self._pipe_lock = threading.Lock()
        self._local = threading.local()
        self._control_channel = False
        self._pinned_pipe_name: str | None = None
        self._bound_target: dict[str, Any] | None = None
        self._inflight: dict[str, Any] | None = None
        self._hung_pids: dict[int, dict[str, Any]] = {}
        env_pipe = os.environ.get(MCP_PIPE_ENV)
        env_pid = os.environ.get("MCP_MAX_PID")
        if env_pid and (not env_pid.isdecimal() or int(env_pid) <= 0):
            raise ValueError("MCP_MAX_PID must be a positive process ID")
        self._startup_pipe = env_pipe or (fr"\\.\pipe\3dsmax-mcp-pid-{int(env_pid)}" if env_pid else None)
        self._startup_source = "environment" if self._startup_pipe else "explicit"
        if self._startup_pipe is None and pipe_name != DEFAULT_PIPE_NAME:
            self._startup_pipe = pipe_name

    def clear_last_response(self) -> None:
        """Clear thread-local metadata from the previous command."""
        self._local.last_response = None
        self._local.last_error = None

    def get_last_transport(self) -> dict[str, Any] | None:
        """Return compact transport metadata from the last command on this thread."""
        response = getattr(self._local, "last_response", None)
        if isinstance(response, dict):
            meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
            return {
                "transport": meta.get("transport"),
                "requested_transport": meta.get("requestedTransport"),
                "request_id": response.get("requestId"),
                "protocol_version": meta.get("protocolVersion"),
                "client_round_trip_ms": meta.get("clientRoundTripMs"),
                "fallback_error": meta.get("fallbackError"),
                **meta.get("target", {}),
            }
        error = getattr(self._local, "last_error", None)
        if isinstance(error, dict):
            return error
        return None

    @property
    def native_available(self) -> bool:
        """Check whether the native C++ bridge is currently available."""
        if self.transport == "pipe":
            return True
        if self.transport == "tcp":
            return False
        try:
            return self._probe_pipe_available(self._resolve_pipe_name())
        except (ConnectionError, TimeoutError):
            return False

    def _config_dir(self) -> Path:
        root = os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / "3dsmax-mcp"
        return Path.home() / "AppData" / "Local" / "3dsmax-mcp"

    def _load_instance(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("pipe"), str):
            return None
        return data

    def _active_instance(self) -> dict[str, Any] | None:
        return self._load_instance(self._config_dir() / "active_instance.json")

    @staticmethod
    def _target(pipe: str, source: str) -> dict[str, Any]:
        match = re.search(r"-pid-(\d+)$", pipe)
        return {"target_pid": int(match[1]) if match else None,
                "target_pipe": pipe, "target_source": source, "pinned": True}

    def _live_instances(self) -> list[dict[str, Any]]:
        live = []
        for path in (self._config_dir() / "instances").glob("*.json"):
            data = self._load_instance(path)
            try:
                updated = path.stat().st_mtime_ns
            except OSError:
                continue
            if data and self._probe_pipe_available(data["pipe"]):
                live.append({**data, "updated": updated})
        return sorted(live, key=lambda item: item["updated"], reverse=True)

    def _default_target(self) -> dict[str, Any]:
        live = self._live_instances()
        active = self._active_instance()
        try:
            claimed = (self._config_dir() / "active_instance.json").stat().st_mtime_ns
        except OSError:
            claimed = 0
        if active and self._probe_pipe_available(active["pipe"]) and (not live or claimed >= live[0]["updated"]):
            return self._target(active["pipe"], "claim")
        return self._target(live[0]["pipe"] if live else DEFAULT_PIPE_NAME, "default")

    def _resolve_pipe_name(self) -> str:
        target = self._bound_target
        if self._pinned_pipe_name is not None:
            target = self._target(self._pinned_pipe_name, "explicit")
        if target is None and self._startup_pipe:
            target = self._target(self._startup_pipe, self._startup_source)
        if target is None:
            target = self._default_target()
        self._local.route_candidate = dict(target)
        return target["target_pipe"]

    def resolve_target(self) -> dict[str, Any]:
        """The Max the next command would reach. Never binds, takes the lock or sends."""
        target = self._bound_target
        if self._pinned_pipe_name is not None:
            target = self._target(self._pinned_pipe_name, "explicit")
        if target is None and self._startup_pipe:
            target = self._target(self._startup_pipe, self._startup_source)
        if target is None:
            target = self._default_target()
        return {**target, "available": self._probe_pipe_available(target["target_pipe"])}

    def list_max_instances(self) -> dict[str, Any]:
        with self._instance_lock():
            default = self._default_target()
            instances = self._live_instances()
            instances.sort(key=lambda item: item["pipe"] != default["target_pipe"])
            return {"instances": [{**item, "default": item["pipe"] == default["target_pipe"],
                                   "selected": bool(self._bound_target and item["pipe"] == self._bound_target["target_pipe"])}
                                  for item in instances]}

    def get_selected_max_instance(self) -> dict[str, Any]:
        with self._instance_lock():
            target = self._bound_target or (self._target(self._startup_pipe, self._startup_source) if self._startup_pipe else None)
            if target is None:
                return {"target_pid": None, "target_pipe": None, "target_source": None, "pinned": False, "available": False}
            return {**target, "available": self._probe_pipe_available(target["target_pipe"])}

    def select_max_instance(self, pid: int) -> dict[str, Any]:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("pid must be a positive process ID")
        pipe = fr"\\.\pipe\3dsmax-mcp-pid-{pid}"
        with self._instance_lock():
            if not self._probe_pipe_available(pipe):
                raise ConnectionError(f"3ds Max PID {pid} is unavailable; selection was not changed")
            self._close_pipe_handle()
            self._selected_pipe_name = None
            self._bound_target = self._target(pipe, "explicit")
            return {**self._bound_target, "available": True}

    def release_max_instance(self) -> dict[str, Any]:
        with self._instance_lock():
            self._close_pipe_handle()
            self._selected_pipe_name = None
            self._bound_target = None
            self._startup_pipe = None
            return {"target_pid": None, "target_pipe": None, "target_source": None, "pinned": False}

    # ── Hang diagnosis ───────────────────────────────────────────
    @contextmanager
    def _instance_lock(self, timeout: float | None = None) -> Iterator[None]:
        timeout = INSTANCE_LOCK_TIMEOUT if timeout is None else timeout
        if not self._pipe_lock.acquire(timeout=max(0.001, timeout)):
            raise self._busy_error(timeout)
        try:
            yield
        finally:
            self._pipe_lock.release()

    def inflight(self) -> dict[str, Any] | None:
        """This client's in-flight request (cmd_type, request_id, target, running_s), if any."""
        return self._inflight_snapshot()

    def hung_verdict(self, pid: int | None) -> dict[str, Any] | None:
        """The remembered not-responding verdict for `pid` (incl. the abandoned inflight), if any."""
        verdict = self._hung_pids.get(pid) if pid else None
        return dict(verdict) if verdict else None

    def _inflight_snapshot(self) -> dict[str, Any] | None:
        inflight = self._inflight
        if not inflight:
            return None
        snapshot = {k: v for k, v in inflight.items() if k != "started"}
        snapshot["running_s"] = round(time.perf_counter() - inflight["started"], 1)
        return snapshot

    @staticmethod
    def _pid_for_pipe(pipe_name: str | None, handle: Any = None) -> int | None:
        match = re.search(r"-pid-(\d+)$", pipe_name or "")
        if match:
            return int(match[1])
        if handle in (None, 0, _INVALID_HANDLE):
            return None
        pid = wintypes.ULONG()
        try:
            if _kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)) and pid.value:
                return int(pid.value)
        except Exception:
            pass
        return None

    @staticmethod
    def _label(pid: int | None, pipe: str | None = None) -> str:
        return f"3ds Max (PID {pid})" if pid else f"3ds Max ({pipe or 'unknown pipe'})"

    def _busy_error(self, waited: float) -> MaxHealthError:
        """Build the error for a pipe lock that stayed held for ``waited`` seconds."""
        inflight = self._inflight_snapshot()
        pid = (inflight or {}).get("target_pid") or (self._bound_target or {}).get("target_pid")
        process = diagnose_process(pid, _CPU_SAMPLE_S) if pid else None
        state = (process or {}).get("state")
        details = {"inflight": inflight, "process": process, "waited_s": round(waited, 1), "request_sent": False}
        label = self._label(pid, (inflight or {}).get("target_pipe"))
        running = (f"request '{inflight['cmd_type']}' has been running for {inflight['running_s']:.0f} s"
                   if inflight else "another call holds the connection")
        limit = (inflight or {}).get("timeout_s")
        overdue = limit is not None and inflight["running_s"] >= limit + _grace(limit)
        # One sample is not enough to call a healthy-but-stalled op hung; only an overdue request is.
        if state == "exited" or (state == "blocked" and overdue):
            what = ("Max has exited." if state == "exited"
                    else "Max is blocked: " + BLOCKED_CAUSE + ".")
            return MaxNotRespondingError(
                f"{label} is not responding: {describe_process(process)}, {running}. {what} "
                "Nothing was sent. Do not retry yet. " + HANG_ADVICE,
                details,
            )
        evidence = f" ({describe_process(process)})" if process else ""
        advice = ("Max may be blocked; call get_bridge_status again shortly to confirm." if state == "blocked"
                  else "Retry later, or call get_bridge_status to check whether Max is responding.")
        return MaxBusyError(
            f"{label} is still busy with another request{evidence}: {running}; waited {waited:.0f} s. "
            f"Nothing was sent. {advice}",
            details,
        )

    def _remember_hung(self, pid: int, process: dict[str, Any], inflight: dict[str, Any] | None,
                       source: str) -> None:
        self._hung_pids[pid] = {"state": process.get("state"), "process": process, "inflight": inflight,
                                "source": source, "started": process_start_time(pid)}

    def _check_known_hung(self, pid: int | None, pipe_name: str) -> None:
        """Fail fast (nothing sent) while a PID previously judged not responding is still blocked."""
        verdict = self._hung_pids.get(pid) if pid else None
        if verdict is None:
            return
        if verdict.get("started") != process_start_time(pid) or quick_hung_check(pid) is not True:
            self._hung_pids.pop(pid, None)  # PID reused/gone, or the window pumps messages again
            return
        process = diagnose_process(pid, _RECHECK_SAMPLE_S)
        if process.get("state") != "blocked":
            self._hung_pids.pop(pid, None)
            return
        why = ("an earlier request was abandoned" if verdict.get("source") == "abandoned"
               else "the bridge cancelled an earlier request its main thread never picked up")
        raise MaxNotRespondingError(
            f"{self._label(pid, pipe_name)} is still not responding ({describe_process(process)}; {why}). "
            "Nothing was sent. " + HANG_ADVICE,
            {"inflight": None, "process": process, "previous": verdict.get("inflight"),
             "previous_process": verdict.get("process"), "request_sent": False},
        )

    def _check_settling(self, pid: int | None, pipe_name: str) -> None:
        """Refuse (nothing sent) while a native import left a remembered window hung.

        Fast path: IsHungAppWindow on the remembered hwnds only. Clears itself once
        they pump again, the process changed, or the guard expired.
        """
        if not pid or self._control_channel or not _settling:
            return
        with _settling_lock:
            entry = _settling.get(pid)
        if entry is None:
            return
        now = time.perf_counter()
        if now >= entry["until"] or entry.get("started") != process_start_time(pid):
            _drop_settling(pid, entry)
            return
        owner = entry.get("owner")
        if owner is self:
            return
        expires = entry["until"] - now
        details = {"inflight": None, "process": None, "request_sent": False,
                   "settling": {"reason": entry["reason"], "since_s": round(now - entry["since"], 1),
                                "expires_in_s": round(expires, 1), "in_progress": owner is not None,
                                "evidence": entry.get("evidence")}}
        if owner is not None:
            raise MaxImportSettlingError(
                f"{self._label(pid, pipe_name)} is busy with {entry['reason']} ({now - entry['since']:.0f} s so "
                f"far). Nothing was sent. Retry once it returns (this guard lifts within {expires:.0f} s at the "
                "latest). Do not open or close the Material Editor meanwhile.",
                details,
            )
        hung = {h: t for h, t in entry["windows"].items() if window_hung(h) is True}
        if not hung:
            _drop_settling(pid, entry)
            return
        names = ", ".join(sorted({f"'{t}'" if t else "main window" for t in hung.values()}))
        details["settling"]["hung_windows"] = sorted({t or "main window" for t in hung.values()})
        raise MaxImportSettlingError(
            f"{self._label(pid, pipe_name)} is still settling after {entry['reason']}: {names} not responding "
            f"({now - entry['since']:.0f} s so far; this guard lifts within {expires:.0f} s at the latest). "
            "Nothing was sent. Do not open or close the Material Editor. " + HANG_ADVICE,
            details,
        )

    def _probe_pipe_available(self, pipe_name: str | None = None) -> bool:
        """Best-effort probe that treats a busy pipe as available."""
        pipe_name = pipe_name or self.pipe_name
        handle = _kernel32.CreateFileW(
            pipe_name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle != _INVALID_HANDLE:
            _kernel32.CloseHandle(handle)
            return True

        err = ctypes.get_last_error()
        if err in (_ERROR_PIPE_BUSY, _ERROR_ACCESS_DENIED):
            return True
        if err in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
            return False

        if _kernel32.WaitNamedPipeW(pipe_name, 0):
            return True
        wait_err = ctypes.get_last_error()
        if wait_err in (_ERROR_SEM_TIMEOUT, _ERROR_PIPE_BUSY, _ERROR_ACCESS_DENIED):
            return True
        return False

    def _close_pipe_handle(self) -> None:
        handle = self._pipe_handle
        if handle not in (None, 0, _INVALID_HANDLE):
            _kernel32.CloseHandle(handle)
        self._pipe_handle = None

    def _ensure_pipe_handle(self, deadline: float, pipe_name: str) -> int:
        handle = self._pipe_handle
        if handle not in (None, 0, _INVALID_HANDLE):
            return handle

        while True:
            handle = _kernel32.CreateFileW(
                pipe_name,
                _GENERIC_READ | _GENERIC_WRITE,
                0,
                None,
                _OPEN_EXISTING,
                0,
                None,
            )
            if handle != _INVALID_HANDLE:
                self._pipe_handle = handle
                return handle

            err = ctypes.get_last_error()
            if err in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
                raise ConnectionError(
                    f"Named pipe {pipe_name} not found. "
                    "Is the MCP Bridge plugin loaded in 3ds Max?"
                )
            if err != _ERROR_PIPE_BUSY:
                raise ConnectionError(f"Failed to open pipe: Win32 error {err}")

            remaining_ms = int((deadline - time.perf_counter()) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError(
                    f"Timed out waiting for named pipe {pipe_name} after "
                    f"{self.timeout}s."
                )

            wait_ms = min(remaining_ms, 250)
            if _kernel32.WaitNamedPipeW(pipe_name, wait_ms):
                continue
            wait_err = ctypes.get_last_error()
            if wait_err in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
                raise ConnectionError(
                    f"Named pipe {pipe_name} disappeared while waiting."
                )
            if wait_err in (_ERROR_SEM_TIMEOUT, _ERROR_PIPE_BUSY):
                continue
            raise ConnectionError(
                f"Failed waiting for named pipe {self.pipe_name}: "
                f"Win32 error {wait_err}"
            )

    def _send_control_command(self, command: str, cmd_type: str, timeout: Optional[float]) -> dict[str, Any]:
        """Bypass an in-flight request's pipe lock, without changing Max targets.

        Only cancellation, pure desktop capture and the main-thread-free health
        probe use this channel. It cannot fall back to TCP or re-resolve another
        Max after a claim/environment change.
        """
        acquired = self._pipe_lock.acquire(blocking=False)
        try:
            if not acquired and self._bound_target is None:
                raise ConnectionError("Max target selection is in progress; retry the control request")
            pipe = self._bound_target["target_pipe"] if self._bound_target else self._resolve_pipe_name()
            target = dict(self._bound_target or getattr(self._local, "route_candidate", self._target(pipe, "default")))
            control = MaxClient(host=self.host, port=self.port, timeout=timeout or min(self.timeout, 15.0),
                                transport="pipe", pipe_name=pipe)
            control._control_channel = True
            control._bound_target = target
            self.clear_last_response()
            try:
                response = control.send_command(command, cmd_type=cmd_type, timeout=timeout)
                if acquired and self._bound_target is None:
                    self._bound_target = target
                return response
            finally:
                if acquired and self._bound_target is None and getattr(control._local, "request_target", None):
                    self._bound_target = target
                self._local.last_response = getattr(control._local, "last_response", None)
                self._local.last_error = getattr(control._local, "last_error", None)
                control._close_pipe_handle()
        finally:
            if acquired:
                self._pipe_lock.release()

    def send_command(
        self,
        command: str,
        cmd_type: str = "maxscript",
        timeout: Optional[float] = None,
        *,
        probe: bool = False,
    ) -> dict[str, Any]:
        """Send a command to 3ds Max and return the parsed JSON response.

        probe=True marks a read-only command that may be dropped at its deadline.
        """
        if cmd_type in _CONTROL_CMD_TYPES and self.transport != "tcp" and not self._control_channel:
            return self._send_control_command(command, cmd_type, timeout)
        effective_timeout = timeout or self.timeout
        request_id = uuid4().hex
        started_at = time.perf_counter()
        transport_used = self.transport
        fallback_error: str | None = None
        self.clear_last_response()

        request = json.dumps({
            "command": command,
            "type": cmd_type,
            "requestId": request_id,
            "protocolVersion": 2,
        }, ensure_ascii=True)

        if self.transport == "pipe":
            transport_used = "namedpipe"
            response_data = self._send_via_pipe(request, effective_timeout, cmd_type=cmd_type, request_id=request_id,
                                                probe=probe)
        elif self.transport == "tcp":
            transport_used = "tcp"
            response_data = self._send_via_tcp(request, effective_timeout)
        else:
            try:
                transport_used = "namedpipe"
                response_data = self._send_via_pipe(request, effective_timeout, cmd_type=cmd_type,
                                                    request_id=request_id, probe=probe)
            except (AmbiguousMaxInstanceError, MaxBusyError, MaxNotRespondingError):
                raise
            except (ConnectionError, TimeoutError) as exc:
                if self._bound_target or self._startup_pipe or self._pinned_pipe_name:
                    target = self._bound_target or self._target(self._startup_pipe or self._pinned_pipe_name, self._startup_source)
                    self._local.last_error = {**target, "transport": "namedpipe", "error": str(exc)}
                    raise ConnectionError(f"Selected 3ds Max target {target['target_pipe']} is unavailable. Select another instance or release it explicitly. {exc}") from exc
                fallback_error = str(exc)
                transport_used = "tcp"
                response_data = self._send_via_tcp(request, effective_timeout)

        try:
            response = self._parse_response(response_data, request_id, started_at)
        except Exception as exc:
            self._local.last_error = {
                "transport": transport_used,
                "requested_transport": self.transport,
                "request_id": request_id,
                "error": str(exc),
                "fallback_error": fallback_error,
            }
            raise

        meta = response.setdefault("meta", {})
        meta.setdefault("transport", transport_used)
        meta.setdefault("requestedTransport", self.transport)
        if transport_used == "namedpipe":
            meta["target"] = getattr(self._local, "request_target", None) or self._bound_target or {}
        if fallback_error:
            meta.setdefault("fallbackError", fallback_error)
        self._local.last_response = response
        return response

    # ── Named Pipe transport ─────────────────────────────────────
    def _send_via_pipe(
        self,
        request: str,
        timeout: float,
        *,
        cmd_type: str | None = None,
        request_id: str | None = None,
        probe: bool = False,
    ) -> bytes:
        deadline = time.perf_counter() + timeout
        data = (request + "\n").encode("utf-8")
        if not self._pipe_lock.acquire(timeout=max(0.001, timeout)):
            raise self._busy_error(timeout)
        try:
            pipe_name = self._resolve_pipe_name()
            if self._selected_pipe_name != pipe_name:
                self._close_pipe_handle()
                self._selected_pipe_name = pipe_name
            self._check_known_hung(self._pid_for_pipe(pipe_name), pipe_name)
            self._check_settling(self._pid_for_pipe(pipe_name), pipe_name)

            for attempt in range(2):
                handle = self._ensure_pipe_handle(deadline, pipe_name)
                pid = self._pid_for_pipe(pipe_name, handle)
                self._check_known_hung(pid, pipe_name)
                self._check_settling(pid, pipe_name)
                if self._bound_target is None:
                    self._bound_target = getattr(self._local, "route_candidate", None) or self._target(pipe_name, "default")
                self._local.request_target = dict(self._bound_target)
                self._inflight = {"cmd_type": cmd_type, "request_id": request_id, "target_pipe": pipe_name,
                                  "target_pid": pid, "timeout_s": timeout, "started": time.perf_counter()}
                try:
                    total_written = 0
                    while total_written < len(data):
                        written = wintypes.DWORD()
                        ok = _kernel32.WriteFile(
                            handle,
                            data[total_written:],
                            len(data) - total_written,
                            ctypes.byref(written),
                            None,
                        )
                        total_written += written.value
                        if not ok:
                            err = ctypes.get_last_error()
                            if err == _ERROR_BROKEN_PIPE:
                                raise BrokenPipeError("Pipe closed while writing request.")
                            raise ConnectionError(
                                f"Failed writing to pipe: Win32 error {err}"
                            )
                        if written.value == 0:
                            raise ConnectionError(
                                "Pipe write returned 0 bytes written."
                            )

                    reply = self._read_pipe_response(handle, deadline, timeout, pid,
                                                     probe=probe or cmd_type in _PROBE_CMD_TYPES)
                    if pid:
                        self._hung_pids.pop(pid, None)  # Max answered, so any old verdict is stale
                    self._check_queue_timeout(reply, pid)
                    return reply
                except BrokenPipeError:
                    self._close_pipe_handle()
                    if total_written:
                        raise RequestOutcomeUnknown("Pipe closed after dispatch. The request may have committed; inspect before retrying.") from None
                    if attempt == 0 and time.perf_counter() < deadline:
                        continue
                    raise ConnectionError("Named pipe connection closed during request.")
                except ConnectionError:
                    self._close_pipe_handle()
                    if total_written:
                        raise RequestOutcomeUnknown("Connection lost after dispatch. The request may have committed; inspect before retrying.") from None
                    if attempt == 0 and time.perf_counter() < deadline:
                        continue
                    raise
                finally:
                    self._inflight = None
        finally:
            self._pipe_lock.release()

    def _read_pipe_response(self, handle: Any, deadline: float, timeout: float, pid: int | None,
                            probe: bool = False) -> bytes:
        """Poll for the newline-terminated reply without ever blocking in ReadFile.

        Past the deadline (+ grace) the target is diagnosed: healthy long work keeps
        waiting so its real result still arrives; an exited or confirmed-blocked Max
        is abandoned with MaxNotRespondingAfterDispatch. A read-only probe is dropped
        at its deadline.
        """
        response_data = bytearray()
        buf = ctypes.create_string_buffer(65536)
        delay = _POLL_MIN_S
        next_check = deadline if probe else deadline + _grace(timeout)
        blocked_since: float | None = None
        while True:
            available = wintypes.DWORD()
            if not _kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                err = ctypes.get_last_error()
                if err == _ERROR_BROKEN_PIPE:
                    raise BrokenPipeError(
                        "Pipe closed before response terminator."
                        if response_data else "Pipe closed while reading response."
                    )
                raise ConnectionError(f"Failed reading from pipe: Win32 error {err}")

            if available.value:
                bytes_read = wintypes.DWORD()
                ok = _kernel32.ReadFile(
                    handle, buf, min(available.value, len(buf)), ctypes.byref(bytes_read), None
                )
                if bytes_read.value > 0:
                    response_data.extend(buf.raw[:bytes_read.value])
                    if b"\n" in response_data:
                        return bytes(response_data)
                if not ok:
                    err = ctypes.get_last_error()
                    if err == _ERROR_BROKEN_PIPE:
                        raise BrokenPipeError("Pipe closed while reading response.")
                    raise ConnectionError(f"Failed reading from pipe: Win32 error {err}")
                if bytes_read.value == 0:
                    raise BrokenPipeError("Pipe closed before response terminator.")
                delay = _POLL_MIN_S
                continue

            now = time.perf_counter()
            if now >= next_check:
                if probe:
                    self._abandon_probe(pid, timeout)
                if not pid:
                    next_check = now + _REDIAGNOSE_S
                    continue
                process = diagnose_process(pid, _CPU_SAMPLE_S)
                state = process.get("state")
                now = time.perf_counter()
                if state == "exited" or (state == "blocked" and blocked_since is not None
                                         and now - blocked_since >= _BLOCKED_CONFIRM_S):
                    self._abandon_request(pid, process, blocked_since)
                if state == "blocked":
                    if blocked_since is None:
                        blocked_since = now
                    next_check = now + _BLOCKED_CONFIRM_S
                else:
                    blocked_since = None
                    next_check = now + _REDIAGNOSE_S
                continue

            time.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_S)

    def _abandon_request(self, pid: int, process: dict[str, Any], blocked_since: float | None) -> None:
        """Close the pipe and raise MaxNotRespondingAfterDispatch (never replay)."""
        inflight = self._inflight_snapshot()
        self._close_pipe_handle()
        details = {"inflight": inflight, "process": process, "request_sent": True}
        running = (f"request '{inflight['cmd_type']}' running for {inflight['running_s']:.0f} s"
                   if inflight else "request in flight")
        if process.get("state") == "exited":
            raise MaxNotRespondingAfterDispatch(
                f"{self._label(pid)} is not responding: {describe_process(process)} while {running}. "
                "The request may have partly committed before Max exited; do not replay it blindly. "
                "Ask the user to restart Max, then inspect the scene.",
                details,
            )
        details["blocked_for_s"] = round(time.perf_counter() - blocked_since, 1) if blocked_since else None
        self._remember_hung(pid, process, inflight, "abandoned")
        raise MaxNotRespondingAfterDispatch(
            f"{self._label(pid)} is not responding: {describe_process(process)}, {running}. "
            "Max is blocked: " + BLOCKED_CAUSE + ". Do not replay this request. " + HANG_ADVICE,
            details,
        )

    def _abandon_probe(self, pid: int | None, timeout: float) -> None:
        """Drop a read-only probe that missed its deadline (safe: it changes nothing)."""
        process = diagnose_process(pid, _CPU_SAMPLE_S) if pid else None
        inflight = self._inflight_snapshot()
        self._close_pipe_handle()
        state = (process or {}).get("state")
        details = {"inflight": inflight, "process": process, "request_sent": True}
        label = self._label(pid, (inflight or {}).get("target_pipe"))
        probe = f"'{(inflight or {}).get('cmd_type') or 'probe'}'"
        if state in ("blocked", "exited"):
            what = "Max has exited." if state == "exited" else "Max is blocked: " + BLOCKED_CAUSE + "."
            raise MaxNotRespondingError(
                f"{label} is not responding: {describe_process(process)}; {probe} got no answer within "
                f"{timeout:g} s. {what} The probe was dropped (it changes nothing). " + HANG_ADVICE,
                details,
            )
        evidence = f" ({describe_process(process)})" if process else ""
        raise MaxBusyError(
            f"{label} did not answer {probe} within {timeout:g} s{evidence}: its main thread is busy. "
            "The probe was dropped (it changes nothing). Retry later.",
            details,
        )

    def _check_queue_timeout(self, reply: bytes, pid: int | None) -> None:
        """Explain a native queue timeout: the main thread never started the request in 120 s."""
        if not pid or _QUEUE_TIMEOUT_MARKER.encode() not in reply.lower():
            return
        try:
            response = json.loads(reply.decode("utf-8-sig", errors="replace"))
        except ValueError:
            return
        if (not isinstance(response, dict) or response.get("success", False)
                or _QUEUE_TIMEOUT_MARKER not in str(response.get("error", "")).lower()):
            return
        process = diagnose_process(pid, _CPU_SAMPLE_S)
        state = process.get("state")
        if state not in ("blocked", "busy", "exited"):
            return
        inflight = self._inflight_snapshot()
        details = {"inflight": inflight, "process": process, "request_sent": True, "executed": False,
                   "bridge_error": str(response.get("error"))}
        what = (f"Its main thread never picked up request '{(inflight or {}).get('cmd_type')}', "
                "so the bridge cancelled it before it ran.")
        if state == "busy":
            raise MaxBusyError(
                f"{self._label(pid)} is busy: {describe_process(process)}. {what} "
                "Retry later, or call get_bridge_status to check whether Max is responding.",
                details,
            )
        if state == "blocked":
            self._remember_hung(pid, process, inflight, "queue_timeout")
        cause = "Max has exited." if state == "exited" else "Max is blocked: " + BLOCKED_CAUSE + "."
        raise MaxNotRespondingError(
            f"{self._label(pid)} is not responding: {describe_process(process)}. {what} {cause} "
            "Do not retry yet. " + HANG_ADVICE,
            details,
        )

    # ── TCP transport (legacy) ───────────────────────────────────
    def _send_via_tcp(self, request: str, timeout: float) -> bytes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)

        try:
            sock.connect((self.host, self.port))
            sock.sendall((request + "\n").encode("utf-8"))

            response_data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                if b"\n" in response_data:
                    break

            return response_data

        except socket.timeout:
            raise TimeoutError(
                f"3ds Max did not respond within {timeout}s. "
                "Is the MCP TCP listener running in 3ds Max?"
            )
        except ConnectionRefusedError:
            raise ConnectionError(
                f"Could not connect to 3ds Max on {self.host}:{self.port}. "
                "Is the MCP TCP listener running in 3ds Max?"
            )
        finally:
            sock.close()

    # ── Response parsing (shared) ────────────────────────────────
    def _parse_response(
        self, response_data: bytes, request_id: str, started_at: float
    ) -> dict[str, Any]:
        # Strip UTF-8 BOM if present
        if response_data.startswith(b'\xef\xbb\xbf'):
            response_data = response_data[3:]
        response_str = response_data.decode("utf-8", errors="replace").strip()

        if not response_str:
            raise RuntimeError("Empty response from 3ds Max")

        response = json.loads(response_str)
        response_request_id = response.get("requestId")
        if response_request_id not in (None, "", request_id):
            raise RuntimeError(
                f"Mismatched response requestId: expected {request_id}, got {response_request_id}"
            )

        response["requestId"] = request_id
        meta = response.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            response["meta"] = meta
        meta.setdefault(
            "clientRoundTripMs",
            round((time.perf_counter() - started_at) * 1000.0, 3),
        )

        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            raise MaxBridgeError(str(error_msg), response)

        return response
