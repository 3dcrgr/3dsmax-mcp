"""A Max thread suspended by capture_hang_diagnostics must never outlive this process."""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp import parent_watchdog, suspend_guard  # noqa: E402
from maxmcp.diagnostics import stackdump  # noqa: E402


class _GuardCase(unittest.TestCase):
    def setUp(self):
        suspend_guard._reset_for_tests()
        self.addCleanup(suspend_guard._reset_for_tests)


class GuardTests(_GuardCase):
    def test_resumes_exactly_once(self):
        resume = mock.Mock()
        with suspend_guard.suspended(lambda: True, resume) as ok:
            self.assertTrue(ok)
            self.assertEqual(suspend_guard.active_count(), 1)
        resume.assert_called_once()
        self.assertEqual(suspend_guard.active_count(), 0)

    def test_failed_suspend_is_never_resumed(self):
        resume = mock.Mock()
        with suspend_guard.suspended(lambda: False, resume) as ok:
            self.assertFalse(ok)
            self.assertEqual(suspend_guard.active_count(), 0)
        resume.assert_not_called()

    def test_resumes_when_the_walk_raises(self):
        resume = mock.Mock()
        with self.assertRaises(ValueError):
            with suspend_guard.suspended(lambda: True, resume):
                raise ValueError("walk failed")
        resume.assert_called_once()

    def test_release_waits_for_an_active_walk(self):
        resume = mock.Mock()
        inside, finish = threading.Event(), threading.Event()

        def walk():
            with suspend_guard.suspended(lambda: True, resume):
                inside.set()
                finish.wait(5)

        worker = threading.Thread(target=walk)
        worker.start()
        self.assertTrue(inside.wait(5))
        threading.Timer(0.05, finish.set).start()
        self.assertEqual(suspend_guard.release_all(wait_s=5), 0)  # the walk resumed its own thread
        worker.join(5)
        resume.assert_called_once()

    def test_release_resumes_a_stuck_walk_itself_and_only_once(self):
        resume = mock.Mock()
        inside, finish = threading.Event(), threading.Event()

        def walk():
            with suspend_guard.suspended(lambda: True, resume):
                inside.set()
                finish.wait(5)

        worker = threading.Thread(target=walk)
        worker.start()
        self.assertTrue(inside.wait(5))
        started = time.monotonic()
        self.assertEqual(suspend_guard.release_all(wait_s=0.05), 1)
        self.assertLess(time.monotonic() - started, 2.0)
        resume.assert_called_once()  # forced by release_all
        finish.set()
        worker.join(5)
        resume.assert_called_once()  # the walk's own resume is a no-op now

    def test_no_suspension_after_release(self):
        suspend_guard.release_all(wait_s=0)
        suspend = mock.Mock(return_value=True)
        with self.assertRaises(suspend_guard.ExitingError):
            with suspend_guard.suspended(suspend, mock.Mock()):
                pass
        suspend.assert_not_called()

    def test_release_never_raises(self):
        with suspend_guard._cond:
            suspend_guard._active[99] = mock.Mock(side_effect=OSError("gone"))
        self.assertEqual(suspend_guard.release_all(wait_s=0), 1)


class ExitPathTests(_GuardCase):
    def test_watchdog_exit_releases_before_exiting(self):
        events = []
        with mock.patch.object(suspend_guard, "release_all", side_effect=lambda *a, **k: events.append("release")), \
                mock.patch.object(parent_watchdog.os, "_exit", side_effect=lambda code: events.append(("exit", code))):
            parent_watchdog._exit_process()
        self.assertEqual(events, ["release", ("exit", 0)])

    def test_server_main_releases_before_exiting(self):
        source = (Path(__file__).resolve().parent.parent / "maxmcp" / "server.py").read_text(encoding="utf-8")
        main = source[source.index("def main():"):]
        self.assertLess(main.index("release_all()"), main.index("os._exit(0)"))


class _FakeApi:
    """Just enough of the kernel32/dbghelp surface for _walk."""

    def __init__(self, suspend_ok=True, walk_steps=3, step_delay=0.0):
        self.calls = []
        self.suspend_ok = suspend_ok
        self.walk_steps = walk_steps
        self.step_delay = step_delay
        self.function_table_access = None
        self.get_module_base = None

    def OpenThread(self, *_):
        return 1234

    def SuspendThread(self, _):
        self.calls.append("suspend")
        return 0 if self.suspend_ok else 0xFFFFFFFF

    def ResumeThread(self, _):
        self.calls.append("resume")
        return 1

    def GetThreadContext(self, *_):
        return True

    def StackWalk64(self, _machine, _proc, _thread, frame_ref, *_):
        if self.step_delay:
            time.sleep(self.step_delay)
        if self.walk_steps <= 0:
            return False
        self.walk_steps -= 1
        frame = frame_ref._obj
        frame.AddrPC.Offset = 0x1000 + self.walk_steps
        frame.AddrStack.Offset = 0x2000 + self.walk_steps
        return True

    def CloseHandle(self, _):
        self.calls.append("close")


@unittest.skipUnless(sys.platform == "win32", "ctypes CONTEXT layout")
class WalkTests(_GuardCase):
    def test_walk_suspends_and_resumes_through_the_guard(self):
        api = _FakeApi()
        pcs, suspended_ms, error = stackdump._walk(api, 1, 42, depth=10)
        self.assertEqual(api.calls, ["suspend", "resume", "close"])
        self.assertEqual(len(pcs), 3)
        self.assertIsNotNone(suspended_ms)
        self.assertIsNone(error)
        self.assertEqual(suspend_guard.active_count(), 0)

    def test_walk_is_cut_short_to_bound_the_pause(self):
        api = _FakeApi(walk_steps=1000, step_delay=0.02)
        with mock.patch.object(stackdump, "_MAX_SUSPEND_S", 0.05):
            pcs, suspended_ms, error = stackdump._walk(api, 1, 42, depth=1000)
        self.assertLess(len(pcs), 1000)
        self.assertIn("truncated", error["message"])
        self.assertEqual(api.calls[-2:], ["resume", "close"])
        self.assertLess(suspended_ms, 1000)

    def test_walk_refuses_to_suspend_while_exiting(self):
        suspend_guard.release_all(wait_s=0)
        api = _FakeApi()
        pcs, suspended_ms, error = stackdump._walk(api, 1, 42, depth=10)
        self.assertEqual(api.calls, ["close"])
        self.assertEqual(pcs, [])
        self.assertIsNone(suspended_ms)
        self.assertIn("exiting", error["message"])

    def test_failed_suspend_is_reported_and_not_resumed(self):
        api = _FakeApi(suspend_ok=False)
        _, suspended_ms, error = stackdump._walk(api, 1, 42, depth=10)
        self.assertEqual(api.calls, ["suspend", "close"])
        self.assertIsNone(suspended_ms)
        self.assertEqual(error["stage"], "SuspendThread")


if __name__ == "__main__":
    unittest.main()
