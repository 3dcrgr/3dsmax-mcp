"""Read-only health probes for a 3ds Max process (hung window, CPU, exit).

Only query rights are requested; nothing here signals or injects into the
target, except window_responsive's no-op WM_NULL (settle checks only) and
minimize_window's posted ShowWindowAsync (cosmos_import only).
Every public function returns a verdict instead of raising.
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
_TH32CS_SNAPPROCESS = 0x2

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
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]
    # Never sends WM_GETTEXT (GetWindowTextW would, to a possibly hung thread).
    _user32.InternalGetWindowText.restype = ctypes.c_int
    _user32.InternalGetWindowText.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    _user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    # Neither sends a message to the window's thread.
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    # IsIconic reads window state; ShowWindowAsync posts and never waits for the window's thread.
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.ShowWindowAsync.restype = wintypes.BOOL
    _user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]

_WM_NULL = 0x0000
_SW_SHOWMINNOACTIVE = 7  # minimized; the active window stays active (no activation message)
_SMTO_BLOCK = 0x0001
_SMTO_ABORTIFHUNG = 0x0002
COSMOS_BROWSER_TITLE = "Chaos Cosmos Browser"


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

    blocked: main window hung and CPU below BLOCKED_CPU_THRESHOLD (deadlock,
    stalled I/O, or a long blocking call). busy: hung but burning CPU. Never raises.
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


def _window_title(hwnd: Any) -> str:
    buf = ctypes.create_unicode_buffer(512)
    _user32.InternalGetWindowText(hwnd, buf, 512)
    return buf.value


def find_windows(pid: int, title: str) -> list[int]:
    """Top-level windows of `pid` titled `title` (case-insensitive), hidden ones included."""
    if not _IS_WINDOWS or not _valid_pid(pid):
        return []
    wanted = title.casefold()
    found: list[int] = []

    def _callback(hwnd: Any, _lparam: Any) -> bool:
        try:
            owner = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            # EnumWindows does not filter visibility: hidden Qt windows are listed too.
            if owner.value == pid and _window_title(hwnd).casefold() == wanted:
                found.append(int(hwnd))
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_WNDENUMPROC(_callback), 0)
    except Exception:
        return []
    return found


def main_windows(pid: int) -> list[int]:
    """Visible, unowned top-level windows of `pid` (Max's main window)."""
    if not _IS_WINDOWS or not _valid_pid(pid):
        return []
    try:
        return [int(h) for h in _top_level_windows(pid)]
    except Exception:
        return []


def window_thread(hwnd: int) -> int | None:
    """Thread id that owns `hwnd` (GetWindowThreadProcessId); None if gone or unknown."""
    if not _IS_WINDOWS or not hwnd:
        return None
    try:
        if not _user32.IsWindow(hwnd):
            return None
        return int(_user32.GetWindowThreadProcessId(hwnd, None)) or None
    except Exception:
        return None


def main_threads(pid: int, exclude_title: str = COSMOS_BROWSER_TITLE) -> list[int]:
    """Thread ids of Max's main window(s): visible unowned windows not titled
    `exclude_title`, preferring those titled "...3ds Max...". Never raises."""
    try:
        excluded = set(find_windows(pid, exclude_title)) if exclude_title else set()
        mains = [h for h in main_windows(pid) if h not in excluded]
        named = [h for h in mains if "3ds max" in _window_title(h).casefold()]
        threads = [window_thread(h) for h in (named or mains)]
        return sorted({t for t in threads if t})
    except Exception:
        return []


def thread_windows(pid: int, title: str, timeout_ms: int = 500) -> dict[str, Any]:
    """Windows of `pid` titled `title` (hidden too) with their owning thread.

    Per window: thread, main_thread (owned by Max's main-window thread),
    visible, and hung (IsHungAppWindow, or no answer to a WM_NULL within
    `timeout_ms`). Query-only apart from that WM_NULL; never raises.
    """
    result: dict[str, Any] = {"main_threads": [], "windows": []}
    try:
        result["main_threads"] = mains = main_threads(pid, title)
        for hwnd in find_windows(pid, title):
            state = window_responsive(hwnd, timeout_ms)
            if not state.get("exists"):
                continue
            thread = window_thread(hwnd)
            result["windows"].append({
                "hwnd": hwnd, "thread": thread, "main_thread": bool(thread) and thread in mains,
                "visible": state.get("visible"),
                "hung": bool(state.get("hung")) or state.get("responds") is False})
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def list_windows(pid: int) -> list[dict[str, Any]]:
    """Every top-level window of `pid` (hidden ones too) with the thread that owns it.

    Sends no message, so it is safe against a hung process: title via
    InternalGetWindowText, class via GetClassNameW, hung via IsHungAppWindow.
    Each entry: hwnd, tid, title, class, visible, owned, hung, area. [] on error.
    """
    if not _IS_WINDOWS or not _valid_pid(pid):
        return []
    found: list[dict[str, Any]] = []

    def _callback(hwnd: Any, _lparam: Any) -> bool:
        try:
            owner = wintypes.DWORD()
            tid = _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            cls = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, cls, 256)
            rect = wintypes.RECT()
            area = 0
            if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            found.append({"hwnd": int(hwnd), "tid": int(tid), "title": _window_title(hwnd), "class": cls.value,
                          "visible": bool(_user32.IsWindowVisible(hwnd)),
                          "owned": bool(_user32.GetWindow(hwnd, _GW_OWNER)),
                          "hung": bool(_user32.IsHungAppWindow(hwnd)), "area": area})
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_WNDENUMPROC(_callback), 0)
    except Exception:
        return []
    return found


def list_processes(image_names: tuple[str, ...] = ("3dsmax.exe",)) -> list[dict[str, Any]]:
    """Running processes whose image file name is one of `image_names` (case-insensitive).

    Toolhelp snapshot only; nothing is opened or signalled. [] on error.
    """
    if not _IS_WINDOWS:
        return []
    wanted = {name.casefold() for name in image_names}
    invalid = wintypes.HANDLE(-1).value
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == invalid:
        return []
    found: list[dict[str, Any]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold() in wanted:
                found.append({"pid": int(entry.th32ProcessID), "image": entry.szExeFile})
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    except Exception:
        return []
    finally:
        _kernel32.CloseHandle(snap)
    return sorted(found, key=lambda item: item["pid"])


def window_hung(hwnd: int) -> bool | None:
    """Cheap per-request check: IsHungAppWindow only (no message is sent).

    None when the window no longer exists or on error.
    """
    if not _IS_WINDOWS or not hwnd:
        return None
    try:
        if not _user32.IsWindow(hwnd):
            return None
        return bool(_user32.IsHungAppWindow(hwnd))
    except Exception:
        return None


def window_iconic(hwnd: int) -> bool | None:
    """IsIconic (minimized); no message is sent. None when gone or unknown."""
    if not _IS_WINDOWS or not hwnd:
        return None
    try:
        if not _user32.IsWindow(hwnd):
            return None
        return bool(_user32.IsIconic(hwnd))
    except Exception:
        return None


def minimize_window(hwnd: int, pid: int, title: str) -> bool:
    """Minimize without activating (ShowWindowAsync SW_SHOWMINNOACTIVE: posted, never waits).

    Only when `hwnd` still belongs to `pid` and is titled `title`. Never closes it.
    """
    if not _IS_WINDOWS or not hwnd or not _valid_pid(pid):
        return False
    try:
        owner = wintypes.DWORD()
        if not _user32.IsWindow(hwnd):
            return False
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or _window_title(hwnd).casefold() != title.casefold():
            return False
        return bool(_user32.ShowWindowAsync(hwnd, _SW_SHOWMINNOACTIVE))
    except Exception:
        return False


def window_responsive(hwnd: int, timeout_ms: int = 500) -> dict[str, Any]:
    """IsHungAppWindow plus a WM_NULL round trip (SMTO_ABORTIFHUNG|SMTO_BLOCK).

    IsHungAppWindow only turns true after ~5 s without pumping; the WM_NULL
    round trip catches a thread that stopped more recently. Costs up to
    `timeout_ms`, so use it for settle checks only, never per request.
    """
    state: dict[str, Any] = {"hwnd": hwnd, "exists": False, "hung": None, "responds": None, "title": None}
    if not _IS_WINDOWS or not hwnd:
        return state
    try:
        if not _user32.IsWindow(hwnd):
            return state
        state["exists"] = True
        state["title"] = _window_title(hwnd)
        state["visible"] = bool(_user32.IsWindowVisible(hwnd))
        state["hung"] = bool(_user32.IsHungAppWindow(hwnd))
        result = ctypes.c_size_t()
        state["responds"] = bool(_user32.SendMessageTimeoutW(
            hwnd, _WM_NULL, 0, 0, _SMTO_ABORTIFHUNG | _SMTO_BLOCK, max(1, int(timeout_ms)), ctypes.byref(result)))
        if not state["responds"] and not _user32.IsWindow(hwnd):
            state.update(exists=False, hung=None, responds=None)  # destroyed meanwhile
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
    return state


def cpu_cores(pid: int, seconds: float = 0.5) -> float | None:
    """Average process CPU cores used over `seconds` (None if unknown)."""
    if not _IS_WINDOWS or not _valid_pid(pid):
        return None
    handle = None
    try:
        handle, _err = _open_process(pid)
        if handle is None:
            return None
        before = _cpu_seconds(handle)
        started = time.perf_counter()
        time.sleep(max(0.05, seconds))
        after = _cpu_seconds(handle)
        elapsed = time.perf_counter() - started
        if before is None or after is None or elapsed <= 0:
            return None
        return round(max(0.0, after - before) / elapsed, 3)
    except Exception:
        return None
    finally:
        if handle is not None:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass


def wait_responsive(pid: int, max_seconds: float, titles: tuple[str, ...] = (COSMOS_BROWSER_TITLE,),
                    checks: int = 3, interval: float = 1.0, timeout_ms: int = 500,
                    cpu_threshold: float | None = None, min_span_s: float = 0.0,
                    cpu_grace_s: float | None = None) -> dict[str, Any]:
    """Wait (OS-level only) until Max's main window and every window titled one of
    `titles` respond for `checks` consecutive checks at least `interval` apart,
    the streak spanning at least `min_span_s` (IsHungAppWindow lags ~5 s).

    `cpu_threshold` (cores) is a secondary quiet signal; None disables it.
    windows_quiet means the windows alone met the bar; with `cpu_grace_s` the
    wait stops that long after that even if CPU stays busy. Bounded by
    `max_seconds`; never raises. Sends nothing but WM_NULL.
    """
    started = time.perf_counter()
    result: dict[str, Any] = {"quiet": False, "windows_quiet": False, "waited_s": 0.0, "checks": 0, "streak": 0,
                              "responsive_streak": 0, "required_checks": checks, "cpu_threshold": cpu_threshold,
                              "windows": [], "hwnds": [], "state": "unknown"}
    if not _IS_WINDOWS or not _valid_pid(pid):
        result["error"] = "not available"
        return result
    handle = None
    try:
        handle, _err = _open_process(pid)
        last_cpu = _cpu_seconds(handle) if handle is not None else None
        last_at = time.perf_counter()
        peak = 0.0
        quiet_since = responsive_since = None
        while True:
            if handle is not None and _exit_status(handle)[0] is False:
                result["state"] = "exited"
                break
            mains = main_windows(pid)
            watched = list(mains)
            for title in titles:
                watched += [h for h in find_windows(pid, title) if h not in watched]
            states = [window_responsive(h, timeout_ms) for h in watched]
            for s in states:
                s["role"] = "main" if s["hwnd"] in mains else "titled"
            responsive = bool(mains) and all(
                not s["exists"] or (s["hung"] is False and s["responds"] is True) for s in states)
            now = time.perf_counter()
            cpu = None
            current = _cpu_seconds(handle) if handle is not None else None
            if current is not None and last_cpu is not None and now - last_at > 0.05:
                cpu = round(max(0.0, current - last_cpu) / (now - last_at), 3)
                peak = max(peak, cpu)
            last_cpu, last_at = current, now
            cpu_ok = cpu_threshold is None or cpu is None or cpu < cpu_threshold
            result["checks"] += 1
            result["streak"] = result["streak"] + 1 if (responsive and cpu_ok) else 0
            result["responsive_streak"] = result["responsive_streak"] + 1 if responsive else 0
            if result["streak"] == 1:
                quiet_since = now
            if result["responsive_streak"] == 1:
                responsive_since = now
            windows_span = now - responsive_since if responsive else 0.0
            result.update(windows=states, hwnds=[s["hwnd"] for s in states if s["exists"]],
                          main_window_found=bool(mains), cpu_cores=cpu, peak_cpu_cores=peak,
                          state="responsive" if responsive else "not_responsive",
                          windows_quiet=result["responsive_streak"] >= checks and windows_span >= min_span_s)
            if result["streak"] >= checks and now - quiet_since >= min_span_s:
                result["quiet"] = True
                break
            if (cpu_grace_s is not None and result["windows_quiet"]
                    and windows_span >= min_span_s + cpu_grace_s):
                break  # windows answer; CPU (textures, viewport) is only a secondary signal
            elapsed = now - started
            if elapsed >= max_seconds:
                break
            time.sleep(min(interval, max_seconds - elapsed))
    except Exception as exc:  # settle must never take the caller down
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if handle is not None:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass
    result["waited_s"] = round(time.perf_counter() - started, 1)
    return result


def describe_windows(settle: dict[str, Any] | None) -> str:
    """Short evidence string for wait_responsive's last check."""
    if not settle:
        return "window state unknown"
    if settle.get("state") == "exited":
        return "process exited"
    parts = []
    for s in settle.get("windows") or []:
        if not s.get("exists"):
            continue
        name = "main window" if s.get("role") == "main" else f"'{s.get('title')}'"
        if s.get("visible") is False:
            name += " (hidden)"
        status = "hung" if s.get("hung") else ("responding" if s.get("responds") else "not answering")
        parts.append(f"{name} {status}")
    if not settle.get("main_window_found"):
        parts.insert(0, "main window not found")
    if settle.get("cpu_cores") is not None:
        parts.append(f"{settle['cpu_cores']:.2f} CPU cores")
    return ", ".join(parts) or "window state unknown"


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
