"""Read-only health probes for a 3ds Max process (hung window, CPU, exit).

Only query rights are requested; nothing here signals or injects into the
target. Every public function returns a verdict instead of raising.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Any

BLOCKED_CPU_THRESHOLD = 0.05  # CPU-seconds per wall second

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_ERROR_INVALID_PARAMETER = 87
_GW_OWNER = 4

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.IsHungAppWindow.restype = wintypes.BOOL
    _user32.IsHungAppWindow.argtypes = [wintypes.HWND]


def _valid_pid(pid: Any) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid <= 0xFFFFFFFF


def _open_process(pid: int) -> tuple[Any, int]:
    """Return (handle or None, last error). Query rights only."""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not handle:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return (handle or None), (0 if handle else ctypes.get_last_error())


def _exit_status(handle: Any) -> tuple[bool | None, int | None]:
    """Return (alive, exit_code) for an open process handle."""
    code = wintypes.DWORD()
    if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
        return None, None
    if code.value != _STILL_ACTIVE:
        return False, int(code.value)
    # STILL_ACTIVE can be a real exit code; SYNCHRONIZE (if granted) settles it.
    if _kernel32.WaitForSingleObject(handle, 0) == _WAIT_OBJECT_0:
        return False, int(code.value)
    return True, None


def _cpu_seconds(handle: Any) -> float | None:
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        return None
    kernel, user = times[2], times[3]
    total = 0
    for ft in (kernel, user):
        total += (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return total / 1e7


def process_start_time(pid: int) -> int | None:
    """Creation FILETIME of a live process (guards verdicts against PID reuse); None if unknown/gone."""
    if not _IS_WINDOWS or not _valid_pid(pid):
        return None
    handle = None
    try:
        handle, _err = _open_process(pid)
        if handle is None or _exit_status(handle)[0] is False:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    except Exception:
        return None
    finally:
        if handle is not None:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass


def _top_level_windows(pid: int) -> list[int]:
    """Visible, unowned top-level windows of a process."""
    found: list[int] = []

    def _callback(hwnd: Any, _lparam: Any) -> bool:
        try:
            owner = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if (owner.value == pid and _user32.IsWindowVisible(hwnd)
                    and not _user32.GetWindow(hwnd, _GW_OWNER)):
                found.append(hwnd)
        except Exception:
            pass
        return True

    _user32.EnumWindows(_WNDENUMPROC(_callback), 0)
    return found


def _windows_hung(pid: int) -> tuple[bool, bool | None]:
    """Return (main_window_found, any_window_hung)."""
    windows = _top_level_windows(pid)
    if not windows:
        return False, None
    return True, any(bool(_user32.IsHungAppWindow(hwnd)) for hwnd in windows)


def quick_hung_check(pid: int) -> bool | None:
    """Cheap IsHungAppWindow check; None when unknown (no window, not Windows, error)."""
    if not _IS_WINDOWS or not _valid_pid(pid):
        return None
    try:
        return _windows_hung(pid)[1]
    except Exception:
        return None


def diagnose_process(pid: int, cpu_sample_s: float = 1.0) -> dict[str, Any]:
    """Classify a process as exited / blocked / busy / responsive / unknown.

    blocked: main window hung and CPU below BLOCKED_CPU_THRESHOLD (deadlock or
    stalled I/O). busy: hung but burning CPU. Never raises.
    """
    result: dict[str, Any] = {
        "pid": pid,
        "alive": True,
        "exit_code": None,
        "main_window_found": False,
        "window_hung": None,
        "cpu_seconds_per_second": None,
        "cpu_sample_s": cpu_sample_s,
        "state": "unknown",
    }
    if not _IS_WINDOWS or not _valid_pid(pid):
        if not _valid_pid(pid):
            result["alive"] = False
            result["error"] = "invalid pid"
        return result
    handle = None
    try:
        handle, err = _open_process(pid)
        if handle is None:
            if err == _ERROR_INVALID_PARAMETER:
                result["alive"] = False
                result["state"] = "exited"
            else:
                result["error"] = f"OpenProcess failed: Win32 error {err}"
            return result
        alive, exit_code = _exit_status(handle)
        if alive is False:
            result.update(alive=False, exit_code=exit_code, state="exited")
            return result
        found, hung = _windows_hung(pid)
        result["main_window_found"] = found
        result["window_hung"] = hung
        cpu_before = _cpu_seconds(handle)
        started = time.perf_counter()
        time.sleep(max(0.0, cpu_sample_s))
        cpu_after = _cpu_seconds(handle)
        elapsed = time.perf_counter() - started
        if cpu_before is not None and cpu_after is not None and elapsed > 0:
            result["cpu_seconds_per_second"] = round(max(0.0, cpu_after - cpu_before) / elapsed, 4)
        alive, exit_code = _exit_status(handle)
        if alive is False:
            result.update(alive=False, exit_code=exit_code, state="exited")
            return result
        if hung is None:
            result["state"] = "unknown"
        elif not hung:
            result["state"] = "responsive"
        elif result["cpu_seconds_per_second"] is None:
            result["state"] = "unknown"
        elif result["cpu_seconds_per_second"] < BLOCKED_CPU_THRESHOLD:
            result["state"] = "blocked"
        else:
            result["state"] = "busy"
        return result
    except Exception as exc:  # diagnosis must never take the caller down
        result["state"] = "unknown"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if handle is not None:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass


def describe(diagnosis: dict[str, Any] | None) -> str:
    """Short evidence string, e.g. 'main window hung, 0.01 CPU-s/s over 1.0 s'."""
    if not diagnosis:
        return "process state unknown"
    if diagnosis.get("state") == "exited":
        code = diagnosis.get("exit_code")
        return "process exited" + (f" (exit code {code})" if code is not None else "")
    parts = []
    hung = diagnosis.get("window_hung")
    if hung is None:
        parts.append("main window not found" if not diagnosis.get("main_window_found") else "window state unknown")
    else:
        parts.append("main window hung" if hung else "main window responding")
    cpu = diagnosis.get("cpu_seconds_per_second")
    if cpu is not None:
        parts.append(f"{cpu:.2f} CPU-s/s over {diagnosis.get('cpu_sample_s', 0):.1f} s")
    return ", ".join(parts)
