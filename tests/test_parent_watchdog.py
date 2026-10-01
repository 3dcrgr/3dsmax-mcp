import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from maxmcp import parent_watchdog  # noqa: E402

PYTHON = r"C:/Program Files/3dsmax-mcp/runtime/python.exe"
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

# The embedded runtime ignores PYTHONPATH (python312._pth), so the child adds
# the repo root itself to avoid importing an installed maxmcp.
# argv: ready_file mode, mode in {"sleep", "block_stdout", "block_both"}.
CHILD_SCRIPT = """
import os, sys, threading, time
sys.path.insert(0, os.environ["PYTHONPATH"])
from maxmcp.parent_watchdog import start_parent_watchdog
import maxmcp.parent_watchdog as pw
assert os.path.dirname(os.path.dirname(pw.__file__)) == os.path.normpath(os.environ["PYTHONPATH"]), pw.__file__
thread = start_parent_watchdog()
with open(sys.argv[1], "w") as f:
    f.write(("started" if thread else "none") + " " + str(os.getpid()))
mode = sys.argv[2]
if mode == "block_both":
    threading.Thread(target=lambda: (sys.stderr.write("e" * 4_000_000), sys.stderr.flush()), daemon=True).start()
if mode in ("block_stdout", "block_both"):
    sys.stdout.write("o" * 4_000_000)
    sys.stdout.flush()
time.sleep(60)
"""

# Plays the MCP client. argv[1] is a JSON spec: cmd (child command line),
# stderr (log path or null), hold ("none" | "stdout" | "both"). With hold, the
# child's pipes go to a holder process that never reads them.
LAUNCHER_SCRIPT = """
import json, subprocess, sys, time
spec = json.loads(sys.argv[1])
hold = spec["hold"]
child = subprocess.Popen(
    spec["cmd"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE if hold != "none" else subprocess.DEVNULL,
    stderr=subprocess.PIPE if hold == "both" else open(spec["stderr"], "w"),
)
holder_pid = 0
if hold != "none":
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=child.stdout,
        stdout=child.stderr if hold == "both" else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    holder_pid = holder.pid
print(child.pid, holder_pid, flush=True)
time.sleep(60)
"""


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop(parent_watchdog._ENV_VAR, None)
    return env


def _python_with_venv() -> str | None:
    for candidate in (sys.executable, PYTHON, r"C:/Program Files/Python312/python.exe", shutil.which("python")):
        if not candidate or not os.path.exists(candidate):
            continue
        probe = subprocess.run([candidate, "-c", "import venv"], capture_output=True)
        if probe.returncode == 0:
            return candidate
    return None


@unittest.skipUnless(sys.platform == "win32", "Windows-only watchdog")
class ParentWatchdogProcessTest(unittest.TestCase):
    def setUp(self):
        import ctypes
        from ctypes import wintypes

        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.k32.WaitForSingleObject.restype = wintypes.DWORD
        self.k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.k32.GetExitCodeProcess.restype = wintypes.BOOL
        self.k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.k32.TerminateProcess.restype = wintypes.BOOL
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32.CloseHandle.restype = wintypes.BOOL
        self.tmp = tempfile.TemporaryDirectory()
        self.procs: list[subprocess.Popen] = []
        self.handles: list = []

    def tearDown(self):
        for handle in self.handles:
            if self.k32.WaitForSingleObject(handle, 0) != 0:
                self.k32.TerminateProcess(handle, 1)
                self.k32.WaitForSingleObject(handle, 5000)
            self.k32.CloseHandle(handle)
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()
        self.tmp.cleanup()

    def _open(self, pid: int):
        # Holding a handle keeps the pid from being reused under us.
        handle = self.k32.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, pid)
        self.assertTrue(handle, f"could not open process {pid}")
        self.handles.append(handle)
        return handle

    def _wait_for_file(self, path: Path, timeout: float = 15.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                text = path.read_text()
                if text:
                    return text
            time.sleep(0.05)
        self.fail(f"child did not report readiness via {path}")

    def _start_client(self, child_prefix: list[str], mode: str = "sleep", hold: str = "none"):
        """Spawn client -> child_prefix... -> watchdog child; return (client, child_handle, log)."""
        ready = Path(self.tmp.name) / "ready.txt"
        log = Path(self.tmp.name) / "child_stderr.txt"
        spec = {
            "cmd": child_prefix + ["-c", CHILD_SCRIPT, str(ready), mode],
            "stderr": str(log),
            "hold": hold,
        }
        client = subprocess.Popen(
            [PYTHON, "-c", LAUNCHER_SCRIPT, json.dumps(spec)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            env=_env(),
            text=True,
        )
        self.procs.append(client)
        direct_pid, holder_pid = (int(x) for x in client.stdout.readline().split())
        self._open(direct_pid)
        if holder_pid:
            self._open(holder_pid)
        status, child_pid = self._wait_for_file(ready).split()
        self.assertEqual(status, "started")
        child = self._open(int(child_pid)) if int(child_pid) != direct_pid else self.handles[0]
        self.assertEqual(self.k32.WaitForSingleObject(child, 0), 0x102, "child exited early")
        return client, child, log

    def _assert_exits_cleanly(self, handle, timeout_ms: int = 5000):
        import ctypes
        from ctypes import wintypes

        self.assertEqual(self.k32.WaitForSingleObject(handle, timeout_ms), 0, "child did not exit")
        code = wintypes.DWORD()
        self.assertTrue(self.k32.GetExitCodeProcess(handle, ctypes.byref(code)))
        self.assertEqual(code.value, 0)

    def _kill(self, proc: subprocess.Popen):
        proc.kill()
        proc.wait(timeout=5)

    def test_child_exits_when_parent_is_killed(self):
        client, child, log = self._start_client([PYTHON])
        self._kill(client)
        self._assert_exits_cleanly(child)
        self.assertIn(f"parent process {client.pid} exited; shutting down", log.read_text())

    def test_exit_not_blocked_by_full_stdout_pipe(self):
        client, child, log = self._start_client([PYTHON], mode="block_stdout", hold="stdout")
        time.sleep(0.5)  # let the main thread block inside its stdout write
        self._kill(client)
        self._assert_exits_cleanly(child)
        self.assertIn("exited; shutting down", log.read_text())

    def test_exit_not_blocked_by_full_stdout_and_stderr_pipes(self):
        client, child, _ = self._start_client([PYTHON], mode="block_both", hold="both")
        time.sleep(0.5)
        self._kill(client)
        grace_ms = int(parent_watchdog._EXIT_GRACE_SECONDS * 1000)
        self._assert_exits_cleanly(child, timeout_ms=grace_ms + 5000)

    def test_exits_when_client_dies_behind_venv_stub(self):
        base = _python_with_venv()
        if base is None:
            self.skipTest("no python with the venv module")
        venv_dir = Path(self.tmp.name) / "venv"
        subprocess.run([base, "-m", "venv", "--without-pip", str(venv_dir)], check=True, capture_output=True)
        stub = venv_dir / "Scripts" / "python.exe"
        client, child, log = self._start_client([str(stub)])
        stub_handle = self.handles[0]
        self.assertNotEqual(child, stub_handle, "venv python.exe did not spawn a separate interpreter")
        self._kill(client)
        self._assert_exits_cleanly(child)
        self.assertIn(f"parent process {client.pid} exited; shutting down", log.read_text())

    def test_exits_when_client_dies_behind_py_launcher(self):
        py = shutil.which("py")
        if py is None:
            self.skipTest("py.exe launcher not installed")
        client, child, log = self._start_client([py])
        self.assertNotEqual(child, self.handles[0], "py.exe did not spawn a separate interpreter")
        self._kill(client)
        self._assert_exits_cleanly(child)
        self.assertIn(f"parent process {client.pid} exited; shutting down", log.read_text())

    def test_younger_pid_is_not_watched(self):
        younger = subprocess.Popen(
            [PYTHON, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(younger)
        with mock.patch.object(parent_watchdog, "_exit_process") as exit_mock, \
                mock.patch.object(parent_watchdog, "_log") as log_mock:
            thread = parent_watchdog.start_parent_watchdog(younger.pid)
            younger.kill()
            younger.wait(timeout=5)
            if thread is not None:
                thread.join(timeout=5)
        self.assertIsNone(thread)
        exit_mock.assert_not_called()
        self.assertTrue(any("younger" in str(c) for c in log_mock.call_args_list))

    def test_younger_ancestor_is_not_opened(self):
        younger = subprocess.Popen(
            [PYTHON, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(younger)
        k32 = parent_watchdog._load_kernel32()
        own_created = parent_watchdog._creation_time(k32, k32.GetCurrentProcess())
        self.assertIsNone(parent_watchdog._open_older_process(k32, younger.pid, own_created))
        opened = parent_watchdog._open_older_process(k32, os.getpid(), 2**63)
        self.assertIsNotNone(opened)
        k32.CloseHandle(opened[0])


class ParentWatchdogUnitTest(unittest.TestCase):
    def test_env_opt_out_starts_nothing(self):
        with mock.patch.dict(os.environ, {parent_watchdog._ENV_VAR: "0"}), \
                mock.patch.object(parent_watchdog.threading, "Thread") as thread_cls:
            self.assertIsNone(parent_watchdog.start_parent_watchdog())
        thread_cls.assert_not_called()

    def test_non_windows_is_noop(self):
        with mock.patch.object(parent_watchdog.sys, "platform", "linux"), \
                mock.patch.object(parent_watchdog.threading, "Thread") as thread_cls:
            self.assertIsNone(parent_watchdog.start_parent_watchdog())
        thread_cls.assert_not_called()

    def test_bogus_pids_never_raise(self):
        with mock.patch.object(parent_watchdog, "_exit_process") as exit_mock, \
                mock.patch.object(parent_watchdog, "_log"):
            for pid in (0xFFFFFFF0, 0, -5, 2**40, "not-a-pid", object()):
                self.assertIsNone(parent_watchdog.start_parent_watchdog(pid))
        exit_mock.assert_not_called()

    def test_kernel32_failure_never_raises(self):
        with mock.patch.object(parent_watchdog.sys, "platform", "win32"), \
                mock.patch.object(parent_watchdog, "_load_kernel32", side_effect=OSError("boom")), \
                mock.patch.object(parent_watchdog, "_log"):
            self.assertIsNone(parent_watchdog.start_parent_watchdog(1234))

    def test_launcher_detection(self):
        is_launcher = parent_watchdog._is_launcher
        for image in (r"C:\x\uv.exe", r"C:\x\UVX.EXE", r"C:\Windows\py.exe", r"C:\Windows\System32\cmd.exe",
                      r"C:\venv\Scripts\3dsmax-mcp.exe"):
            self.assertTrue(is_launcher(image), image)
        for image in (None, "", r"C:\x\claude.exe", r"C:\x\node.exe"):
            self.assertFalse(is_launcher(image), image)
        stub = r"C:\venv\Scripts\python.exe"
        with mock.patch.object(parent_watchdog.sys, "executable", stub), \
                mock.patch.object(parent_watchdog.sys, "prefix", r"C:\venv"), \
                mock.patch.object(parent_watchdog.sys, "base_prefix", r"C:\Python312"):
            self.assertTrue(is_launcher(stub.upper()))
            self.assertFalse(is_launcher(r"C:\other\python.exe"))
        with mock.patch.object(parent_watchdog.sys, "executable", stub), \
                mock.patch.object(parent_watchdog.sys, "prefix", r"C:\Python312"), \
                mock.patch.object(parent_watchdog.sys, "base_prefix", r"C:\Python312"):
            self.assertFalse(is_launcher(stub))

    def test_shutdown_arms_fallback_before_logging(self):
        events = []

        class FakeTimer:
            def __init__(self, interval, fn):
                events.append(("timer", interval, fn))

            def start(self):
                events.append(("start",))

        with mock.patch.object(parent_watchdog.threading, "Timer", FakeTimer), \
                mock.patch.object(parent_watchdog, "_log", side_effect=lambda m: events.append(("log", m))), \
                mock.patch.object(parent_watchdog, "_exit_process", side_effect=lambda: events.append(("exit",))):
            parent_watchdog._shutdown(42)
        self.assertEqual([e[0] for e in events], ["timer", "start", "log", "exit"])
        self.assertEqual(events[0][1], parent_watchdog._EXIT_GRACE_SECONDS)


if __name__ == "__main__":
    unittest.main()
