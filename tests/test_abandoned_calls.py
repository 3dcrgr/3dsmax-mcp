"""Issue #48: calls this server abandoned at the reply deadline stay ours.

(1) get_bridge_status names a still-running abandoned request as this server's,
never "other_client". (2) A main-thread call sent while it still runs is refused
at once (nothing sent) instead of queueing behind it with no result.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp import max_client  # noqa: E402
from maxmcp.max_client import MaxBusy, MaxBusyError, MaxNotRespondingAfterDispatch  # noqa: E402
from test_bridge_health import _StatusCase, _entry, _health, _process  # noqa: E402
from test_hang_diagnosis import PID, REPLY, FakeKernel, _ClientCase  # noqa: E402


class AbandonedTrackingTests(_ClientCase):
    def abandon(self, client, state="busy"):
        self.states = [state]
        with self.assertRaises((MaxBusy, MaxNotRespondingAfterDispatch)) as ctx:
            client.send_command("polyop.setVert $Throw ids pts", timeout=1.0)
        return ctx.exception

    def test_busy_deadline_records_the_request_as_abandoned(self):
        self.use_kernel(FakeKernel(self.clock))
        client = self.pinned_client()
        exc = self.abandon(client)
        rid = exc.request_id
        self.assertEqual(client.abandoned_request_ids(), [rid])
        call = client.abandoned_call(rid)
        self.assertEqual(call["cmd_type"], "maxscript")
        self.assertEqual(call["target_pid"], PID)
        self.assertTrue(call["still_running"])
        self.assertGreaterEqual(call["running_s"], max_client.NATIVE_QUEUE_TIMEOUT)

    def test_next_main_thread_call_is_refused_and_nothing_is_sent(self):
        kernel = self.use_kernel(FakeKernel(self.clock))
        client = self.pinned_client()
        rid = self.abandon(client).request_id
        writes = kernel.writes
        before = self.clock.now
        with self.assertRaises(MaxBusyError) as ctx:
            client.send_command('{"max_width": 800}', cmd_type="native:capture_viewport")
        exc = ctx.exception
        self.assertEqual(exc.code, "MAX_BUSY")
        self.assertFalse(exc.details["request_sent"])
        self.assertEqual(exc.details["previous"]["request_id"], rid)
        self.assertIn(rid, str(exc))
        self.assertIn("Nothing was sent", str(exc))
        self.assertIn(f"PID {PID}", str(exc))
        self.assertEqual(kernel.writes, writes)
        self.assertLess(self.clock.now - before, 5.0)  # at once, not after a queue wait
        self.assertFalse(client._pipe_lock.locked())

    def test_hung_abandon_is_tracked_too(self):
        self.use_kernel(FakeKernel(self.clock))
        client = self.pinned_client()
        self.abandon(client, state="blocked")
        self.assertEqual(len(client.abandoned_request_ids()), 1)

    def test_probes_and_pipe_thread_reads_still_go(self):
        kernel = self.use_kernel(FakeKernel(self.clock))
        client = self.pinned_client()
        self.abandon(client)
        writes = kernel.writes
        # The abandoned call's reader stays pending; a fresh reply chunk would be read by it
        # first in this fake, so check the guard itself for the exempt types.
        for cmd_type, probe in (("ping", True), ("native:scene_info", False), ("maxscript", True)):
            client._check_own_abandoned(PID, "pipe", cmd_type, probe)
        with self.assertRaises(MaxBusyError):
            client._check_own_abandoned(PID, "pipe", "maxscript", False)
        client._check_own_abandoned(PID + 1, "pipe", "maxscript", False)  # another Max: not ours to guard
        client._check_own_abandoned(None, "pipe", "maxscript", False)
        self.assertEqual(kernel.writes, writes)  # the guard itself sends nothing

    def test_calls_go_again_once_max_finishes_the_abandoned_one(self):
        kernel = self.use_kernel(FakeKernel(self.clock))
        client = self.pinned_client()
        rid = self.abandon(client).request_id
        kernel.chunks = [(0, REPLY), (0, REPLY)]  # the abandoned call's reply, then the new call's
        self.assertEqual(client.send_command("1+1", timeout=1.0)["result"], "late")
        self.assertIn(rid, client.abandoned_request_ids())  # still known as ours
        self.assertFalse(client.abandoned_call(rid)["still_running"])

    def test_dialog_blocked_calls_are_not_abandoned(self):
        self.use_kernel(FakeKernel(self.clock))
        client = self.pinned_client()
        reader = max_client._PipeReader(1)
        client._detach(reader, "D1", "maxscript", dialog=True, pid=PID)
        client._detach(max_client._PipeReader(2), "P1", "ping", dialog=False, probe=True, pid=PID)
        self.assertEqual(client.abandoned_request_ids(), [])

    def test_abandoned_list_is_bounded(self):
        self.use_kernel(FakeKernel(self.clock))
        client = self.pinned_client()
        for i in range(max_client.MAX_ABANDONED_CALLS + 5):
            client._detach(max_client._PipeReader(i), f"R{i}", "maxscript", dialog=False, pid=PID)
        ids = client.abandoned_request_ids()
        self.assertEqual(len(ids), max_client.MAX_ABANDONED_CALLS)
        self.assertEqual(ids[-1], f"R{max_client.MAX_ABANDONED_CALLS + 4}")


class StatusOwnerTests(_StatusCase):
    def status_with_abandoned(self, health, abandoned_ids, abandoned_call=None, **kwargs):
        return self.status(health, abandoned=abandoned_ids, abandoned_call=abandoned_call, **kwargs)

    def test_abandoned_at_deadline_is_ours_not_another_clients(self):
        # 2026-10-30: the client gave up on a busy polyop.setVert at its deadline; the bridge still
        # ran it under this server's pipe client id, and status said "other_client" (pipe-81).
        call = {"cmd_type": "maxscript", "request_id": "SETVERT", "running_s": 200.0, "abandoned_s": 70.0,
                "still_running": True, "target_pid": 4242, "timeout_s": 120.0}
        health = _health("busy_mcp", pumping=False, heartbeat_ms=200000, version=2,
                         running={"cmdType": "maxscript", "runningMs": 200000, "clientId": "pipe-81",
                                  "requestId": "SETVERT"},
                         inflight=[_entry("pipe-81", "SETVERT", "maxscript", 200000)])
        payload, _, _ = self.status_with_abandoned(health, ["SETVERT"], call, process=_process("busy", cpu=1.0))
        running = payload["main_thread"]["running"]
        self.assertEqual(running["owner"], "this_server")
        self.assertTrue(running["abandoned"])
        self.assertEqual(payload["other_clients"], [])
        self.assertEqual(payload["previous"]["request_id"], "SETVERT")
        self.assertIn("this server's earlier 'maxscript' request", payload["message"])
        self.assertNotIn("another MCP client's", payload["message"])

    def test_unknown_request_is_still_another_clients(self):
        health = _health("busy_mcp", pumping=False, heartbeat_ms=200000, version=2,
                         running={"cmdType": "maxscript", "runningMs": 200000, "clientId": "pipe-5",
                                  "requestId": "THEIRS"},
                         inflight=[_entry("pipe-5", "THEIRS", "maxscript", 200000)])
        payload, _, _ = self.status_with_abandoned(health, ["MINE"], None, process=_process("busy", cpu=1.0))
        self.assertEqual(payload["main_thread"]["running"]["owner"], "other_client")
        self.assertEqual([c["client_id"] for c in payload["other_clients"]], ["pipe-5"])


if __name__ == "__main__":
    unittest.main()
