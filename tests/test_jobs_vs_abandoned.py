"""Merge of upstream 1.7.7: job handles (render_scene, capture_multi_view) beside the fork's #48
abandoned-call bookkeeping and #50 command log."""
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from maxmcp import max_client  # noqa: E402
from maxmcp.max_client import JobPending, MaxClient  # noqa: E402


class _Reader:
    def __init__(self, data: bytes = b"") -> None:
        self.done = threading.Event()
        self.data = data
        self.detached = False

    def detach(self) -> None:
        self.detached = True

    def result(self) -> bytes:
        return self.data


class JobDetachTests(unittest.TestCase):
    def test_only_unfinished_jobs_are_reported_as_owned(self):
        client = MaxClient(transport="pipe")
        active, finished = _Reader(), _Reader()
        finished.done.set()
        client._detach(active, "active", "native:capture_multi_view", dialog=False,
                       sent_at=0.0, timeout=300, job=True)
        client._detach(finished, "finished", "native:capture_multi_view", dialog=False,
                       sent_at=0.0, timeout=300, job=True)
        client._detach(_Reader(), "dialog", "maxscript", dialog=True, sent_at=0.0, timeout=130)
        self.assertEqual(client.job_request_ids(), ["active"])
        self.assertEqual(client.blocked_request_ids(), ["dialog"])
        self.assertNotIn("active", client.abandoned_request_ids())
        self.assertIn("active", client._blocked)  # reporting must not collect the reply

    def test_job_is_not_an_abandoned_call(self):
        client = MaxClient(transport="pipe")
        client._detach(_Reader(), "job1", "native:render_scene", dialog=False, sent_at=0.0, timeout=300,
                       job=True, pid=4242)
        self.assertTrue(client._blocked["job1"]["job"])
        self.assertNotIn("job1", client._abandoned)
        self.assertIsNone(client._running_abandoned(4242))  # later calls are not refused by #48

    def test_jobs_outlive_other_released_calls_at_the_tracking_limit(self):
        client = MaxClient(transport="pipe")
        client._detach(_Reader(), "job0", "native:render_scene", dialog=False, sent_at=0.0, timeout=300, job=True)
        for i in range(max_client.MAX_BLOCKED_CALLS + 4):
            client._detach(_Reader(), f"busy{i}", "maxscript", dialog=False, sent_at=0.0, timeout=130, pid=4242)
        self.assertIn("job0", client._blocked)  # still collectable with wait_job
        self.assertLessEqual(len(client._blocked), max_client.MAX_BLOCKED_CALLS)

    def test_max_busy_detach_is_still_abandoned(self):
        client = MaxClient(transport="pipe")
        client._detach(_Reader(), "busy1", "maxscript", dialog=False, sent_at=0.0, timeout=130, pid=4242)
        self.assertIn("busy1", client._abandoned)
        self.assertEqual(client._running_abandoned(4242)["request_id"], "busy1")


class JobWaitPassthroughTests(unittest.TestCase):
    def test_send_command_forwards_job_wait_and_leaves_the_log_entry_open(self):
        client = MaxClient(transport="pipe")
        seen = {}

        def fake_pipe(request, timeout, **kwargs):
            seen.update(kwargs)
            raise JobPending(kwargs["request_id"], kwargs["cmd_type"], 50.0)

        with mock.patch.object(MaxClient, "_send_via_pipe", side_effect=fake_pipe), \
                mock.patch.object(max_client.command_log, "was_sent", return_value=True), \
                mock.patch.object(max_client.command_log, "record_done") as done:
            with self.assertRaises(JobPending):
                client.send_command("{}", cmd_type="native:render_scene", timeout=300, job_wait=50.0)
        self.assertEqual(seen["job_wait"], 50.0)
        done.assert_not_called()  # the job still runs; wait_job logs its end

    def test_wait_job_logs_the_end(self):
        client = MaxClient(transport="pipe")
        reader = _Reader(b'{"success": true, "result": "done", "requestId": "j2"}\n')
        reader.done.set()
        client._blocked["j2"] = {"command": "native:render_scene", "reader": reader, "blocked_at": 0.0,
                                 "sent_at": 0.0, "timeout_s": 300, "probe": False, "dialog": False, "job": True}
        with mock.patch.object(max_client.command_log, "was_sent", return_value=True), \
                mock.patch.object(max_client.command_log, "record_done") as done:
            state = client.wait_job("j2", 0.0)
        self.assertEqual(state["status"], "completed")
        done.assert_called_once()
        self.assertTrue(done.call_args.kwargs["ok"])
        self.assertNotIn("j2", client._blocked)


if __name__ == "__main__":
    unittest.main()
