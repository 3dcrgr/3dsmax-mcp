import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 120.0
DEFAULT_PIPE_NAME = r"\\.\pipe\3dsmax-mcp"
MCP_PIPE_ENV = "MCP_MAX_PIPE"
# A call still unanswered after this long is checked for a blocking dialog.
DIALOG_CHECK_AFTER = 1.0
DIALOG_POLL_INTERVAL = 1.0
MAX_BLOCKED_CALLS = 16
# native:core control (hot-reloadable core). Only CORE_BUSY is retried: the
# shell kept the old core and changed nothing. Waits above CORE_RETRY_MAX_WAIT_MS
# get one attempt, so a long drain is never repeated.
CORE_RELOAD_ATTEMPTS = 3
CORE_RETRY_MAX_WAIT_MS = 20_000
CORE_DEFAULT_WAIT_MS = 5_000  # the shell's default when wait_ms is omitted
CORE_BUSY_BACKOFF = 0.5  # seconds before the first retry; doubles after each
CORE_MAX_WAIT_MS = 600_000  # kMcpCoreMaxWaitMs, native/include/mcp_bridge/shell_control.h
CORE_STATUS_TIMEOUT = 5.0


class RequestOutcomeUnknown(Exception):
    """The request reached Max but its response was lost; never replay it."""


class DialogBlocked(Exception):
    """A call is waiting on a modal dialog in Max and keeps running there.

    The client keeps its connection and reports the eventual result through
    blocked_calls(); the call must not be repeated.
    """

    code = "BLOCKED_BY_DIALOG"
    retryable = False

    def __init__(self, request_id: str, command: str, dialogs: list[dict[str, Any]]) -> None:
        self.request_id = request_id
        self.dialogs = dialogs
        titles = ", ".join(repr(d.get("title", "")) for d in dialogs) or "a dialog"
        message = (f"3ds Max is waiting on {titles}. The call is still running inside Max "
                   "and finishes after the dialog is answered.")
        self.bridge_message = json.dumps({
            "type": "DialogBlocked", "code": self.code, "retryable": False, "message": message,
            "hint": ("Read the dialog. If the user has authorized you to proceed unattended, answer it with "
                     "max_dialogs(action='respond', dialog_id, expected_dialog, button) as the task intends; "
                     "otherwise ask the user which button to press. Do not repeat the call: max_dialogs "
                     "reports its result after the dialog closes."),
            "details": {"request_id": request_id, "command": command, "dialogs": dialogs},
        })
        super().__init__(message)


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
_INVALID_HANDLE = wintypes.HANDLE(-1).value


class AmbiguousMaxInstanceError(ConnectionError):
    """Raised when multiple live Max native bridges exist and none is claimed."""


class MaxBridgeError(Exception):
    """Raised when the native/TCP bridge returns a structured error response."""

    def __init__(self, message: str, response: dict[str, Any]) -> None:
        self.bridge_message = message
        self.bridge_response = response
        super().__init__(f"MAXScript error: {message}")


def _bridge_error_code(exc: MaxBridgeError) -> str:
    """Return the structured NativeError code carried by a bridge error, if any."""
    try:
        payload = json.loads(exc.bridge_message)
    except (TypeError, ValueError):
        return ""
    return str(payload.get("code") or "") if isinstance(payload, dict) else ""


class _PipeReader:
    """Reads one newline-terminated response on its own thread.

    The caller may stop waiting while Max sits in a modal dialog; the reader
    then owns the pipe handle and closes it once the response arrives.
    """

    def __init__(self, handle: int) -> None:
        self.handle = handle
        self.done = threading.Event()
        self.data = b""
        self.error: BaseException | None = None
        self._detached = False
        self._lock = threading.Lock()
        threading.Thread(target=self._run, name="max-pipe-reader", daemon=True).start()

    def _run(self) -> None:
        response = bytearray()
        buf = ctypes.create_string_buffer(65536)
        try:
            while True:
                bytes_read = wintypes.DWORD()
                ok = _kernel32.ReadFile(self.handle, buf, len(buf), ctypes.byref(bytes_read), None)
                if bytes_read.value > 0:
                    response.extend(buf.raw[:bytes_read.value])
                    if b"\n" in response:
                        self.data = bytes(response)
                        return
                if not ok:
                    err = ctypes.get_last_error()
                    if err == _ERROR_BROKEN_PIPE:
                        raise BrokenPipeError("Pipe closed while reading response.")
                    raise ConnectionError(f"Failed reading from pipe: Win32 error {err}")
                if bytes_read.value == 0:
                    raise BrokenPipeError("Pipe closed before response terminator.")
        except BaseException as exc:
            self.error = exc
        finally:
            with self._lock:
                self.done.set()
                if self._detached:
                    _kernel32.CloseHandle(self.handle)

    def detach(self) -> None:
        with self._lock:
            self._detached = True
            if self.done.is_set():
                _kernel32.CloseHandle(self.handle)

    def result(self) -> bytes:
        if self.error is not None:
            raise self.error
        return self.data


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
        self._blocked: dict[str, dict[str, Any]] = {}
        self._blocked_lock = threading.Lock()
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
            transport = {
                "transport": meta.get("transport"),
                "requested_transport": meta.get("requestedTransport"),
                "request_id": response.get("requestId"),
                "protocol_version": meta.get("protocolVersion"),
                "client_round_trip_ms": meta.get("clientRoundTripMs"),
                "fallback_error": meta.get("fallbackError"),
                **meta.get("target", {}),
            }
            if meta.get("openDialogs"):
                transport["open_dialogs"] = meta["openDialogs"]
            return transport
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

    def list_max_instances(self) -> dict[str, Any]:
        with self._pipe_lock:
            default = self._default_target()
            instances = self._live_instances()
            instances.sort(key=lambda item: item["pipe"] != default["target_pipe"])
            return {"instances": [{**item, "default": item["pipe"] == default["target_pipe"],
                                   "selected": bool(self._bound_target and item["pipe"] == self._bound_target["target_pipe"])}
                                  for item in instances]}

    def get_selected_max_instance(self) -> dict[str, Any]:
        with self._pipe_lock:
            target = self._bound_target or (self._target(self._startup_pipe, self._startup_source) if self._startup_pipe else None)
            if target is None:
                return {"target_pid": None, "target_pipe": None, "target_source": None, "pinned": False, "available": False}
            return {**target, "available": self._probe_pipe_available(target["target_pipe"])}

    def select_max_instance(self, pid: int) -> dict[str, Any]:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("pid must be a positive process ID")
        pipe = fr"\\.\pipe\3dsmax-mcp-pid-{pid}"
        with self._pipe_lock:
            if not self._probe_pipe_available(pipe):
                raise ConnectionError(f"3ds Max PID {pid} is unavailable; selection was not changed")
            self._close_pipe_handle()
            self._selected_pipe_name = None
            self._bound_target = self._target(pipe, "explicit")
            return {**self._bound_target, "available": True}

    def release_max_instance(self) -> dict[str, Any]:
        with self._pipe_lock:
            self._close_pipe_handle()
            self._selected_pipe_name = None
            self._bound_target = None
            self._startup_pipe = None
            return {"target_pid": None, "target_pipe": None, "target_source": None, "pinned": False}

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

        Cancellation, dialog recovery and pure desktop capture use this channel. It cannot
        fall back to TCP or re-resolve another Max after a claim/environment change.
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
        request_fields: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Send a command to 3ds Max and return the parsed JSON response.

        request_fields adds handler options beside the command, e.g. quiet.
        """
        if (cmd_type in {"native:render_cancel", "native:render_cancel_capture", "native:capture_screen", "native:max_dialogs",
                         "native:core"}
                and self.transport != "tcp" and not self._control_channel):
            return self._send_control_command(command, cmd_type, timeout)
        effective_timeout = timeout or self.timeout
        request_id = uuid4().hex
        started_at = time.perf_counter()
        transport_used = self.transport
        fallback_error: str | None = None
        self.clear_last_response()

        request = json.dumps({
            **(request_fields or {}),
            "command": command,
            "type": cmd_type,
            "requestId": request_id,
            "protocolVersion": 2,
        }, ensure_ascii=True)

        if self.transport == "pipe":
            transport_used = "namedpipe"
            response_data = self._send_via_pipe(request, effective_timeout, request_id=request_id, cmd_type=cmd_type)
        elif self.transport == "tcp":
            transport_used = "tcp"
            response_data = self._send_via_tcp(request, effective_timeout)
        else:
            try:
                transport_used = "namedpipe"
                response_data = self._send_via_pipe(request, effective_timeout, request_id=request_id, cmd_type=cmd_type)
            except AmbiguousMaxInstanceError:
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
    def _send_via_pipe(self, request: str, timeout: float, request_id: str = "", cmd_type: str = "") -> bytes:
        deadline = time.perf_counter() + timeout
        data = (request + "\n").encode("utf-8")
        with self._pipe_lock:
            pipe_name = self._resolve_pipe_name()
            if self._selected_pipe_name != pipe_name:
                self._close_pipe_handle()
                self._selected_pipe_name = pipe_name

            for attempt in range(2):
                handle = self._ensure_pipe_handle(deadline, pipe_name)
                if self._bound_target is None:
                    self._bound_target = getattr(self._local, "route_candidate", None) or self._target(pipe_name, "default")
                self._local.request_target = dict(self._bound_target)
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

                    return self._await_response(handle, request_id, cmd_type)
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

    def _await_response(self, handle: int, request_id: str, cmd_type: str) -> bytes:
        reader = _PipeReader(handle)
        watch = bool(request_id) and not self._control_channel
        delay = DIALOG_CHECK_AFTER
        while not reader.done.wait(delay if watch else None):
            delay = DIALOG_POLL_INTERVAL
            dialogs = self._dialogs_blocking(request_id)
            if dialogs:
                # Max keeps running the call; its connection now belongs to the reader.
                self._pipe_handle = None
                reader.detach()
                with self._blocked_lock:
                    while len(self._blocked) >= MAX_BLOCKED_CALLS:
                        self._blocked.pop(next(iter(self._blocked)))
                    self._blocked[request_id] = {"command": cmd_type, "reader": reader,
                                                 "blocked_at": time.perf_counter()}
                raise DialogBlocked(request_id, cmd_type, dialogs)
        return reader.result()

    def _dialog_control(self, action: str) -> dict[str, Any]:
        """Query the dialog monitor over the control channel, keeping this call's metadata."""
        saved = getattr(self._local, "last_response", None), getattr(self._local, "last_error", None)
        try:
            response = self._send_control_command(json.dumps({"action": action}), "native:max_dialogs", 3.0)
            result = json.loads(response.get("result") or "{}")
            return result if isinstance(result, dict) else {}
        finally:
            self._local.last_response, self._local.last_error = saved

    def core_control(
        self,
        action: str = "status",
        *,
        wait_ms: int | None = None,
        path: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Report or reload the hot-reloadable native core through the bridge shell.

        Uses the control channel, so it runs beside an in-flight request and never
        falls back to TCP. reload retries CORE_BUSY up to CORE_RELOAD_ATTEMPTS
        times with backoff when wait_ms (default 5000) is at most
        CORE_RETRY_MAX_WAIT_MS (20000); a longer wait gets a single attempt and
        CORE_BUSY is raised directly. Any other error, and a lost response, is
        raised at once. A raised MaxBridgeError records the attempts made in .attempts.
        """
        if action not in {"status", "reload"}:
            raise ValueError("action must be status or reload")
        if self.transport == "tcp":
            raise ValueError("native core control requires the native bridge control channel")
        if action == "status" and (wait_ms is not None or path is not None):
            raise ValueError("wait_ms and path apply to reload only")
        request: dict[str, Any] = {"action": action}
        if wait_ms is not None:
            if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or not 0 <= wait_ms <= CORE_MAX_WAIT_MS:
                raise ValueError(f"wait_ms must be an integer from 0 to {CORE_MAX_WAIT_MS}")
            request["wait_ms"] = wait_ms
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                raise ValueError("path must be a non-empty core DLL path")
            request["path"] = path
        wait = CORE_DEFAULT_WAIT_MS if wait_ms is None else wait_ms
        if timeout is None:
            timeout = CORE_STATUS_TIMEOUT if action == "status" else wait / 1000.0 + 30.0
        command = json.dumps(request)
        retry = action == "reload" and wait <= CORE_RETRY_MAX_WAIT_MS
        attempts = CORE_RELOAD_ATTEMPTS if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                response = self._send_control_command(command, "native:core", timeout)
                break
            except MaxBridgeError as exc:
                exc.attempts = attempt
                if attempt == attempts or _bridge_error_code(exc) != "CORE_BUSY":
                    raise
                time.sleep(CORE_BUSY_BACKOFF * 2 ** (attempt - 1))
        raw = response.get("result")
        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            result = None
        if not isinstance(result, dict):
            raise RuntimeError(f"native:core {action} returned no status object")
        return result

    def _dialogs_blocking(self, request_id: str) -> list[dict[str, Any]]:
        """Main-thread dialogs holding this request: its own, or any while it is queued."""
        try:
            status = self._dialog_control("status")
            dialogs = [d for d in status.get("dialogs", []) if d.get("main_thread")]
            state = next((r.get("state") for r in status.get("requests", [])
                          if r.get("request_id") == request_id), None)
            if state == "running":
                dialogs = [d for d in dialogs if request_id in d.get("during_requests", [])]
            elif state != "queued":
                return []
            if not dialogs:
                return []
            wanted = {d.get("dialog_id") for d in dialogs}
            inspected = self._dialog_control("inspect").get("dialogs", [])
            return [d for d in inspected if d.get("dialog_id") in wanted] or dialogs
        except Exception:
            return []

    def blocked_calls(self, wait: float = 0.0) -> list[dict[str, Any]]:
        """Report calls that returned BLOCKED_BY_DIALOG; completed ones are reported once.

        wait: seconds to wait for the blocked calls to complete. Waiting stops
        early when one of them is blocked by a dialog again.
        """
        deadline = time.perf_counter() + max(0.0, wait)
        while True:
            with self._blocked_lock:
                pending = list(self._blocked.items())
            waiting = [(rid, entry) for rid, entry in pending if not entry["reader"].done.is_set()]
            if not waiting or time.perf_counter() >= deadline:
                break
            waiting[0][1]["reader"].done.wait(min(0.5, max(0.0, deadline - time.perf_counter())))
            if any(self._dialogs_blocking(rid) for rid, entry in waiting if not entry["reader"].done.is_set()):
                break
        report = []
        for request_id, entry in pending:
            reader = entry["reader"]
            item: dict[str, Any] = {"request_id": request_id, "command": entry["command"]}
            if not reader.done.is_set():
                item.update(status="waiting", waiting_s=round(time.perf_counter() - entry["blocked_at"], 1))
                report.append(item)
                continue
            with self._blocked_lock:
                self._blocked.pop(request_id, None)
            try:
                response = self._parse_response(reader.result(), request_id, entry["blocked_at"])
                raw = response.get("result", "")
                try:
                    value = json.loads(raw) if isinstance(raw, str) and raw[:1] in ("{", "[") else raw
                except ValueError:
                    value = raw
                item.update(status="completed", ok=True, result=value)
            except MaxBridgeError as exc:
                item.update(status="completed", ok=False, error=exc.bridge_message)
            except Exception as exc:
                item.update(status="completed", ok=False, error=str(exc))
            report.append(item)
        return report

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
