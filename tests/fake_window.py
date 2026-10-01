"""Child process with real top-level windows, for process_health window tests.

usage: fake_window.py --title TITLE [--hidden] [--mode pump|hang] [--main] [--on-main]

--title   title of the probed window, created on the main thread
--hidden  never show the probed window (EnumWindows must still list it)
--mode    pump: keep pumping messages; hang: pump 0.5 s, then stop pumping
--main    also create a visible, off-screen, non-activating "Fake 3ds Max"
          window on a second thread that keeps pumping (Max's main window)
--on-main implies --main; the probed window is created on that main-window
          thread instead, and --mode applies to that thread

Prints "ready <probed hwnd> <main hwnd or 0>" once the windows exist.
Windows are off-screen and never activated, so nothing takes focus.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time

parser = argparse.ArgumentParser()
parser.add_argument("--title", required=True)
parser.add_argument("--hidden", action="store_true")
parser.add_argument("--mode", choices=("pump", "hang"), default="pump")
parser.add_argument("--main", action="store_true")
parser.add_argument("--on-main", action="store_true")
args = parser.parse_args()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
kernel32.GetModuleHandleW.restype = wt.HMODULE

WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
PM_REMOVE = 1


def _proc(hwnd, msg, wparam, lparam):
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_callback = WNDPROC(_proc)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


_instance = kernel32.GetModuleHandleW(None)
_class = WNDCLASS(lpfnWndProc=_callback, lpszClassName="McpFakeWindow", hInstance=_instance)
if not user32.RegisterClassW(ctypes.byref(_class)):
    sys.exit("RegisterClassW failed")


def _create(title, visible):
    hwnd = user32.CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, "McpFakeWindow", title, WS_POPUP,
                                  -32000, -32000, 10, 10, None, None, _instance, None)
    if visible:
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    return hwnd


def _pump(seconds):
    msg = wt.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def _run_mode():
    if args.mode == "pump":
        _pump(3600)
    else:
        _pump(0.5)
        time.sleep(3600)  # stop pumping: the probed window's thread looks hung


main_hwnd = [0]
probed = [0]
main_ready = threading.Event()


def _main_window_thread():
    main_hwnd[0] = _create("Fake 3ds Max", True) or 0
    if args.on_main:
        probed[0] = _create(args.title, not args.hidden) or 0
    main_ready.set()
    _run_mode() if args.on_main else _pump(3600)


if args.main or args.on_main:
    threading.Thread(target=_main_window_thread, daemon=True).start()
    main_ready.wait(10)

if not args.on_main:
    probed[0] = _create(args.title, not args.hidden) or 0
print("ready %d %d" % (probed[0], main_hwnd[0]), flush=True)
if args.on_main:
    time.sleep(3600)
else:
    _run_mode()
