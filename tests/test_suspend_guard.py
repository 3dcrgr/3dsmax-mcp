"""A Max thread suspended by capture_hang_diagnostics must never outlive this process."""

import os
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

    def test_second_caller_waits_until_the_first_resume_has_run(self):
        # A walker must not close its thread handle (or the process exit) while
        # release_all's forced resume is still on its way to the OS.
        events = []
        entered = threading.Event()

        def resume():
            events.append("resume-start")
            entered.set()
            time.sleep(0.2)
            events.append("resume-end")

        once = suspend_guard._once(resume)
        first = threading.Thread(target=once)
        first.start()
        self.assertTrue(entered.wait(5))
        once()  # the second caller
        events.append("second-returned")
        first.join(5)
        self.assertEqual(events, ["resume-start", "resume-end", "second-returned"])

    def test_failed_resume_can_be_retried(self):
        resume = mock.Mock(side_effect=[OSError("busy"), None])
        once = suspend_guard._once(resume)
        with self.assertRaises(OSError):
            once()
        once()
        once()
        self.assertEqual(resume.call_count, 2)

    def test_interrupt_right_after_suspending_still_resumes(self):
        # KeyboardInterrupt delivered after the OS suspended the thread but before
        # suspend() returned: the thread is resumed before the exception propagates.
        state = {"suspended": 0}

        def suspend():
            state["suspended"] += 1
            raise KeyboardInterrupt

        def resume():
            state["suspended"] -= 1

        with self.assertRaises(KeyboardInterrupt):
            with suspend_guard.suspended(suspend, resume):
                self.fail("body must not run")
        self.assertEqual(state["suspended"], 0)
        self.assertEqual(suspend_guard.active_count(), 0)

    def test_release_does_not_hang_on_a_held_lock(self):
        resume = mock.Mock()
        with suspend_guard._cond:
            suspend_guard._active[7] = suspend_guard._once(resume)
        held, done = threading.Event(), threading.Event()

        def hold():
            with suspend_guard._cond:
                held.set()
                done.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(held.wait(5))
        try:
            started = time.monotonic()
            self.assertEqual(suspend_guard.release_all(wait_s=0.1), 1)
            self.assertLess(time.monotonic() - started, 2.0)
            resume.assert_called_once()
            self.assertTrue(suspend_guard._closing)
        finally:
            done.set()
            holder.join(5)


class ExitPathTests(_GuardCase):
    def test_watchdog_exit_releases_before_exiting(self):
        events = []
        with mock.patch.object(suspend_guard, "release_all", side_effect=lambda *a, **k: events.append("release")), \
                mock.patch.object(parent_watchdog.os, "_exit", side_effect=lambda code: events.append(("exit", code))):
            parent_watchdog._exit_process()
        self.assertEqual(events, ["release", ("exit", 0)])

    def test_watchdog_exit_survives_a_failing_release(self):
        exits = []
        with mock.patch.dict(sys.modules, {"maxmcp.suspend_guard": None}), \
                mock.patch.object(parent_watchdog.os, "_exit", side_effect=exits.append):
            with self.assertRaises(ImportError):
                parent_watchdog._exit_process()
        self.assertEqual(exits, [0])

    def test_watchdog_shutdown_exits_while_the_guard_lock_is_held(self):
        exits = []
        held, done = threading.Event(), threading.Event()

        def hold():
            with suspend_guard._cond:
                held.set()
                done.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(held.wait(5))
        try:
            with mock.patch.object(suspend_guard.release_all, "__defaults__", (0.1,)), \
                    mock.patch.object(parent_watchdog, "_log"), \
                    mock.patch.object(parent_watchdog, "_EXIT_GRACE_SECONDS", 30), \
                    mock.patch.object(parent_watchdog.os, "_exit", side_effect=exits.append):
                started = time.monotonic()
                parent_watchdog._shutdown(1234)
                self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(exits[:1], [0])
        finally:
            done.set()
            holder.join(5)

    def test_server_main_releases_before_exiting(self):
        source = (Path(__file__).resolve().parent.parent / "maxmcp" / "server.py").read_text(encoding="utf-8")
        main = source[source.index("def main():"):]
        self.assertLess(main.index("release_all()"), main.index("os._exit(0)"))
        self.assertLess(main.index("finally:"), main.index("os._exit(0)"))  # exits even if release fails


PID = 4242


class _FakeApi:
    """Just enough of the kernel32/dbghelp surface for _walk."""

    def __init__(self, suspend_ok=True, walk_steps=3, step_delay=0.0, owner=PID, resume_ok=True):
        self.calls = []
        self.suspend_ok = suspend_ok
        self.resume_ok = resume_ok
        self.walk_steps = walk_steps
        self.step_delay = step_delay
        self.owner = owner
        self.function_table_access = None
        self.get_module_base = None
        self.on_walk = None
        self.on_resume = None

    def OpenThread(self, *_):
        return 1234

    def GetProcessIdOfThread(self, _):
        return self.owner

    def SuspendThread(self, _):
        self.calls.append("suspend")
        return 0 if self.suspend_ok else 0xFFFFFFFF

    def ResumeThread(self, _):
        if self.on_resume:
            self.on_resume()
        self.calls.append("resume")
        return 1 if self.resume_ok else 0xFFFFFFFF

    def GetThreadContext(self, *_):
        return True

    def StackWalk64(self, _machine, _proc, _thread, frame_ref, *_):
        if self.on_walk:
            self.on_walk()
        if self.step_delay:
            time.sleep(self.step_delay)
        if self.walk_steps <= 0:
            return False
        self.walk_steps -= 1
        frame = frame_ref._obj
        frame.AddrPC.Offset = 0x1000 + self.walk_steps
        frame.AddrStack.Offset = 0x2000 + self.walk_steps
        return True

    def CloseHandle(self, handle):
        self.calls.append("close" if handle == 1234 else f"close-{handle}")


class _StateChangeApi(_FakeApi):
    """Windows 11: suspension through a thread state-change object."""

    def NtCreateThreadStateChange(self, handle_ref, _access, _attrs, _thread, _reserved):
        self.calls.append("sc-create")
        handle_ref._obj.value = 777
        return 0

    def NtChangeThreadState(self, handle, _thread, change, *_):
        if change == 1 and self.on_resume:
            self.on_resume()
        self.calls.append(("sc-suspend", "sc-resume")[change] + f"@{handle}")
        return 0 if self.suspend_ok else -0x3FFFFFFF  # an NTSTATUS error


@unittest.skipUnless(sys.platform == "win32", "ctypes CONTEXT layout")
class WalkTests(_GuardCase):
    def test_walk_suspends_and_resumes_through_the_guard(self):
        api = _FakeApi()
        methods = {}
        pcs, suspended_ms, error = stackdump._walk(api, 1, PID, 42, depth=10, methods=methods)
        self.assertEqual(api.calls, ["suspend", "resume", "close"])
        self.assertEqual(methods, {"suspend_thread": 1})
        self.assertEqual(len(pcs), 3)
        self.assertIsNotNone(suspended_ms)
        self.assertIsNone(error)
        self.assertEqual(suspend_guard.active_count(), 0)

    def test_walk_is_cut_short_to_bound_the_pause(self):
        api = _FakeApi(walk_steps=1000, step_delay=0.02)
        with mock.patch.object(stackdump, "_MAX_SUSPEND_S", 0.05):
            pcs, suspended_ms, error = stackdump._walk(api, 1, PID, 42, depth=1000)
        self.assertLess(len(pcs), 1000)
        self.assertIn("truncated", error["message"])
        self.assertEqual(api.calls[-2:], ["resume", "close"])
        self.assertLess(suspended_ms, 1000)

    def test_walk_refuses_to_suspend_while_exiting(self):
        suspend_guard.release_all(wait_s=0)
        api = _FakeApi()
        pcs, suspended_ms, error = stackdump._walk(api, 1, PID, 42, depth=10)
        self.assertEqual(api.calls, ["close"])
        self.assertEqual(pcs, [])
        self.assertIsNone(suspended_ms)
        self.assertIn("exiting", error["message"])

    def test_state_change_object_is_preferred_and_closed_last(self):
        api = _StateChangeApi()
        methods = {}
        pcs, suspended_ms, error = stackdump._walk(api, 1, PID, 42, depth=10, methods=methods)
        self.assertEqual(api.calls, ["sc-create", "sc-suspend@777", "sc-resume@777", "close-777", "close"])
        self.assertEqual(methods, {"state_change": 1})
        self.assertEqual(len(pcs), 3)
        self.assertIsNone(error)

    def test_failed_state_change_suspend_is_reported(self):
        api = _StateChangeApi(suspend_ok=False)
        _, suspended_ms, error = stackdump._walk(api, 1, PID, 42, depth=10)
        self.assertEqual(api.calls, ["sc-create", "sc-suspend@777", "close-777", "close"])
        self.assertIsNone(suspended_ms)
        self.assertIn("NTSTATUS", error["message"])

    def test_recycled_tid_of_another_process_is_never_suspended(self):
        for owner in (PID + 1, 0, os.getpid()):
            api = _FakeApi(owner=owner)
            pcs, suspended_ms, error = stackdump._walk(api, 1, PID, 42, depth=10)
            self.assertEqual(api.calls, ["close"], owner)
            self.assertEqual((pcs, suspended_ms), ([], None))
            self.assertIn("no longer belongs", error["message"])

    def test_failed_resume_is_reported(self):
        api = _FakeApi(resume_ok=False)
        _, _, error = stackdump._walk(api, 1, PID, 42, depth=10)
        self.assertEqual(error["stage"], "ResumeThread")
        self.assertIn("may still be suspended", error["message"])

    def test_forced_resume_completes_before_the_handle_is_closed(self):
        # release_all force-resumes a stuck walk; the walk then finishes. The thread
        # handle must not be closed until the forced resume has really run.
        for api in (_FakeApi(walk_steps=1), _StateChangeApi(walk_steps=1)):
            with self.subTest(api=type(api).__name__):
                suspend_guard._reset_for_tests()
                inside, finish = threading.Event(), threading.Event()
                api.on_walk = lambda inside=inside, finish=finish: (inside.set(), finish.wait(5))

                def slow_resume(api=api, finish=finish):
                    api.calls.append("resume-start")
                    finish.set()  # the walk now ends and tries to resume and close
                    time.sleep(0.2)

                api.on_resume = slow_resume
                walker = threading.Thread(target=stackdump._walk, args=(api, 1, PID, 42, 10))
                walker.start()
                self.assertTrue(inside.wait(5))
                self.assertEqual(suspend_guard.release_all(wait_s=0.05), 1)
                walker.join(5)
                resumes = [c for c in api.calls if c == "resume" or str(c).startswith("sc-resume")]
                self.assertEqual(len(resumes), 1, api.calls)
                self.assertEqual(api.calls.count("resume-start"), 1, api.calls)
                self.assertLess(api.calls.index(resumes[0]), api.calls.index("close"), api.calls)

    def test_failed_suspend_is_reported_and_not_resumed(self):
        api = _FakeApi(suspend_ok=False)
        _, suspended_ms, error = stackdump._walk(api, 1, PID, 42, depth=10)
        self.assertEqual(api.calls, ["suspend", "close"])
        self.assertIsNone(suspended_ms)
        self.assertEqual(error["stage"], "SuspendThread")


if __name__ == "__main__":
    unittest.main()
