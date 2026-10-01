"""Never let this process exit while it holds a 3ds Max thread suspended.

capture_hang_diagnostics suspends Max threads for milliseconds to read their
stacks. A thread suspended by a process that then dies stays suspended for
good, which would freeze Max. So every suspension goes through `suspended()`,
which suspends and registers in one step under the guard lock, and every exit
path (the parent watchdog's os._exit, server.main's os._exit, and atexit)
calls `release_all()` first: it stops new suspensions, waits briefly for the
active ones, then resumes whatever is still registered itself.

Only a hard kill (TerminateProcess) can still skip this.
"""

from __future__ import annotations

import atexit
import itertools
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

RELEASE_WAIT_S = 0.5

_cond = threading.Condition()
_active: dict[int, Callable[[], None]] = {}
_ids = itertools.count(1)
_closing = False


class ExitingError(RuntimeError):
    """The process is exiting; no new suspension may start."""


def _once(resume: Callable[[], object]) -> Callable[[], None]:
    lock = threading.Lock()
    done = False

    def call() -> None:
        nonlocal done
        with lock:
            if done:
                return
            done = True
        resume()

    return call


@contextmanager
def suspended(suspend: Callable[[], bool], resume: Callable[[], object]) -> Iterator[bool]:
    """Run `suspend()` (True on success) and guarantee exactly one `resume()` after it.

    Yields whether the suspension took effect. Raises ExitingError, without
    calling `suspend`, once release_all() has started.
    """
    resume_once = _once(resume)
    token = None
    with _cond:
        if _closing:
            raise ExitingError("this process is exiting; no thread was suspended")
        # Under the lock, so an exit cannot begin between suspending and registering.
        if suspend():
            token = next(_ids)
            _active[token] = resume_once
    try:
        yield token is not None
    finally:
        if token is not None:
            try:
                resume_once()
            finally:
                with _cond:
                    _active.pop(token, None)
                    _cond.notify_all()


def release_all(wait_s: float = RELEASE_WAIT_S) -> int:
    """Block new suspensions, wait up to `wait_s` for active ones, resume the rest.

    Returns how many suspensions had to be resumed here. Never raises.
    """
    global _closing
    try:
        deadline = time.monotonic() + max(0.0, wait_s)
        with _cond:
            _closing = True
            while _active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _cond.wait(remaining)
            leftovers = list(_active.values())
        for resume in leftovers:
            try:
                resume()
            except Exception:
                pass
        return len(leftovers)
    except Exception:
        return 0


def active_count() -> int:
    with _cond:
        return len(_active)


def _reset_for_tests() -> None:
    global _closing
    with _cond:
        _closing = False
        _active.clear()


atexit.register(release_all)
