"""get_bridge_status driven by the native pipe-thread health command (MCP_ISSUES #4, #6)."""

import importlib
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp import max_client  # noqa: E402
from maxmcp.max_client import (MaxBridgeError, MaxBusyError, MaxClient, MaxImportSettlingError,  # noqa: E402
                               MaxNotRespondingError)

PING = {"result": json.dumps({"pong": True, "maxVersion": 2026}), "requestId": "r", "meta": {}}


def _load_bridge(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.bridge", None)
        return importlib.import_module("maxmcp.tools.bridge")


def _health(state="responsive", pumping=True, heartbeat_ms=400, running=None, inflight=(), queued=0,
            version=1, window_hung=None):
    return {
        "server": "3dsmax-mcp-native", "bridgeVersion": "1.7.3", "healthVersion": version, "pid": 4242,
        "maxVersion": 2026,
        "mainThread": {"state": state, "pumping": pumping, "heartbeatAgeMs": heartbeat_ms,
                       "heartbeatPeriodMs": 1000,
                       "windowHung": (not pumping) if window_hung is None else window_hung},
        "executor": {"running": running, "queued": queued, "oldestQueuedMs": 900 if queued else None,
                     "oldestQueuedCmd": "ping" if queued else None, "completed": 17, "shuttingDown": False},
        "clients": {"connected": 3, "totalConnections": 9, "requestingClient": "pipe-9",
                    "inflight": list(inflight)},
    }


def _process(state, cpu=0.01):
    return {"pid": 4242, "alive": state != "exited", "exit_code": None, "main_window_found": True,
            "window_hung": state in ("blocked", "busy"), "cpu_seconds_per_second": cpu,
            "cpu_sample_s": 1.0, "state": state}


class _StatusCase(unittest.TestCase):
    def status(self, health, *, mine=None, process=None, ping=PING, verdict=None, transport="auto"):
        client = mock.Mock()
        client.transport = transport
        client.inflight.return_value = mine
        client.hung_verdict.return_value = verdict
        sent = []

        def send(command, cmd_type="maxscript", timeout=None):
            sent.append(cmd_type)
            if cmd_type == "health":
                if isinstance(health, Exception):
                    raise health
                return {"result": json.dumps(health), "requestId": "h", "meta": {}}
            if isinstance(ping, Exception):
                raise ping
            return ping

        client.send_command.side_effect = send
        bridge = _load_bridge(client)
        diagnose = mock.Mock(return_value=process or _process("responsive"))
        with mock.patch.object(bridge, "diagnose_process", diagnose):
            payload = json.loads(bridge.get_bridge_status())
        return payload, sent, diagnose


class HealthyTests(_StatusCase):
    def test_responsive_bridge_pings_and_attaches_health(self):
        payload, sent, diagnose = self.status(_health())
        self.assertEqual(sent, ["health", "ping"])
        self.assertTrue(payload["pong"])
        self.assertEqual(payload["health"]["main_thread"], "responsive")
        self.assertEqual(payload["health"]["clients_connected"], 3)
        self.assertEqual(payload["health"]["bridge_version"], "1.7.3")
        diagnose.assert_not_called()

    def test_bridge_without_health_falls_back_to_ping(self):
        old = MaxBridgeError("Unknown command type: health", {"success": False})
        payload, sent, _ = self.status(old)
        self.assertEqual(sent, ["health", "ping"])
        self.assertTrue(payload["pong"])
        self.assertNotIn("health", payload)

    def test_silent_bridge_does_not_ping_again(self):
        hung = MaxNotRespondingError("3ds Max (PID 4242) is not responding",
                                     {"process": _process("blocked"), "request_sent": True})
        payload, sent, _ = self.status(hung)
        self.assertEqual(sent, ["health"])
        self.assertEqual(payload["bridge_state"], "not_responding")
        self.assertFalse(payload["pong"])


class BusyTests(_StatusCase):
    def test_own_request_running_is_busy_and_never_queues_a_ping(self):
        mine = {"cmd_type": "maxscript", "request_id": "abc", "running_s": 6.0}
        health = _health("busy_mcp", pumping=False, heartbeat_ms=6000,
                         running={"cmdType": "maxscript", "runningMs": 6000},
                         inflight=[{"clientId": "pipe-1", "requestId": "abc", "cmdType": "maxscript",
                                    "elapsedMs": 6100, "nested": 0}])
        payload, sent, diagnose = self.status(health, mine=mine, process=_process("blocked"))
        self.assertEqual(sent, ["health"])
        self.assertFalse(payload["pong"])
        # Idle in a wait for only 6 s: busy, not a deadlock verdict yet.
        self.assertEqual(payload["bridge_state"], "busy")
        self.assertEqual(payload["bridge_code"], "MAX_BUSY")
        self.assertEqual(payload["main_thread"]["running"]["owner"], "this_server")
        self.assertEqual(payload["other_clients"], [])
        self.assertIn("this server's 'maxscript' request", payload["message"])
        diagnose.assert_called_once()

    def test_other_client_blocking_for_long_is_not_responding(self):
        health = _health("busy_mcp", pumping=False, heartbeat_ms=45000,
                         running={"cmdType": "native:assign_material", "runningMs": 45000},
                         inflight=[{"clientId": "pipe-2", "requestId": "zzz", "cmdType": "native:assign_material",
                                    "elapsedMs": 45200, "nested": 0}])
        payload, sent, _ = self.status(health, process=_process("blocked"))
        self.assertEqual(sent, ["health"])
        self.assertEqual(payload["bridge_state"], "not_responding")
        self.assertEqual(payload["bridge_code"], "MAX_NOT_RESPONDING")
        self.assertFalse(payload["retryable"])
        self.assertEqual(payload["main_thread"]["running"]["owner"], "other_client")
        self.assertEqual(payload["other_clients"][0]["client_id"], "pipe-2")
        self.assertIn("another MCP client's", payload["message"])
        self.assertIn("capture_hang_diagnostics", payload["message"])

    def test_stall_outside_the_bridge(self):
        health = _health("busy_other", pumping=False, heartbeat_ms=12000)
        payload, sent, _ = self.status(health, process=_process("busy", cpu=0.9))
        self.assertEqual(sent, ["health"])
        self.assertEqual(payload["bridge_state"], "busy")
        self.assertIsNone(payload["main_thread"]["running"])
        self.assertIn("outside the bridge", payload["message"])

    def test_blocked_outside_the_bridge(self):
        health = _health("busy_other", pumping=False, heartbeat_ms=300000, queued=2)
        payload, _, _ = self.status(health, process=_process("blocked"))
        self.assertEqual(payload["bridge_state"], "not_responding")
        self.assertEqual(payload["main_thread"]["queued"], 2)
        self.assertEqual(payload["main_thread"]["oldest_queued_s"], 0.9)
        self.assertEqual(payload["main_thread"]["heartbeat_age_s"], 300.0)

    def test_pumping_request_is_busy_without_cpu_sampling(self):
        health = _health("busy_mcp", pumping=True, heartbeat_ms=500,
                         running={"cmdType": "native:render_scene", "runningMs": 90000})
        payload, _, diagnose = self.status(health)
        self.assertEqual(payload["bridge_state"], "busy")
        self.assertEqual(payload["main_thread"]["running"]["owner"], "unknown")
        diagnose.assert_not_called()


def _entry(client_id, request_id, cmd_type, elapsed_ms, **extra):
    return {"clientId": client_id, "requestId": request_id, "cmdType": cmd_type, "elapsedMs": elapsed_ms,
            "nested": 0, **extra}


class AttributionTests(_StatusCase):
    def test_queued_own_request_behind_another_clients_same_type_request(self):
        # cmd-type-only matching used to call the other client's running request ours.
        mine = {"cmd_type": "maxscript", "request_id": "MINE", "running_s": 2.0, "timeout_s": 120.0}
        health = _health("busy_mcp", pumping=False, heartbeat_ms=8000, queued=1, version=2,
                         running={"cmdType": "maxscript", "runningMs": 8000, "clientId": "pipe-1",
                                  "requestId": "OTHER"},
                         inflight=[_entry("pipe-1", "OTHER", "maxscript", 8000),
                                   _entry("pipe-7", "MINE", "maxscript", 2000)])
        payload, sent, _ = self.status(health, mine=mine, process=_process("busy", cpu=0.9))
        self.assertEqual(sent, ["health"])
        self.assertEqual(payload["main_thread"]["running"]["owner"], "other_client")
        self.assertIn("another MCP client's", payload["message"])
        self.assertEqual([c["client_id"] for c in payload["other_clients"]], ["pipe-1"])

    def test_old_bridge_attribution_uses_request_age(self):
        # healthVersion 1 has no requestId on the running item; ours is too young to be it.
        mine = {"cmd_type": "maxscript", "request_id": "MINE", "running_s": 2.0, "timeout_s": 120.0}
        health = _health("busy_mcp", pumping=False, heartbeat_ms=8000, queued=1,
                         running={"cmdType": "maxscript", "runningMs": 8000},
                         inflight=[_entry("pipe-1", "OTHER", "maxscript", 8000),
                                   _entry("pipe-7", "MINE", "maxscript", 2000)])
        payload, _, _ = self.status(health, mine=mine, process=_process("busy", cpu=0.9))
        self.assertEqual(payload["main_thread"]["running"]["owner"], "other_client")

    def test_old_bridge_ambiguous_attribution_is_unknown(self):
        health = _health("busy_mcp", pumping=False, heartbeat_ms=8000,
                         running={"cmdType": "maxscript", "runningMs": 8000},
                         inflight=[_entry("pipe-1", "A", "maxscript", 9000),
                                   _entry("pipe-2", "B", "maxscript", 8500)])
        payload, _, _ = self.status(health, process=_process("busy", cpu=0.9))
        self.assertEqual(payload["main_thread"]["running"]["owner"], "unknown")

    def test_abandoned_own_request_is_ours_not_another_clients(self):
        abandoned = {"cmd_type": "native:cosmos_import", "request_id": "LOST", "running_s": 75.0}
        verdict = {"state": "blocked", "process": _process("blocked"), "inflight": abandoned,
                   "source": "abandoned"}
        health = _health("busy_mcp", pumping=False, heartbeat_ms=60000, version=2,
                         running={"cmdType": "native:cosmos_import", "runningMs": 60000,
                                  "clientId": "pipe-1", "requestId": "LOST"},
                         inflight=[_entry("pipe-1", "LOST", "native:cosmos_import", 60000)])
        payload, _, _ = self.status(health, verdict=verdict, process=_process("blocked"))
        running = payload["main_thread"]["running"]
        self.assertEqual(running["owner"], "this_server")
        self.assertTrue(running["abandoned"])
        self.assertEqual(payload["other_clients"], [])
        self.assertEqual(payload["previous"]["request_id"], "LOST")
        self.assertIn("abandoned", payload["message"])
        self.assertNotIn("another MCP client's", payload["message"])

    def test_internal_probe_is_not_another_client(self):
        health = _health("busy_mcp", pumping=True, heartbeat_ms=300, version=2,
                         running={"cmdType": "native:invoke_tool", "runningMs": 5000, "clientId": "pipe-1",
                                  "requestId": "OUTER"},
                         inflight=[_entry("pipe-1", "OUTER", "native:invoke_tool", 5000),
                                   _entry("native-tool-probe", "invoke-x", "maxscript", 4000, internal=True)])
        payload, _, _ = self.status(health)
        self.assertEqual([c["client_id"] for c in payload["other_clients"]], ["pipe-1"])


class OwnRequestTests(_StatusCase):
    def _own(self, running_s, timeout_s=30.0):
        ms = int(running_s * 1000)
        mine = {"cmd_type": "maxscript", "request_id": "abc", "running_s": running_s, "timeout_s": timeout_s}
        health = _health("busy_mcp", pumping=False, heartbeat_ms=ms, version=2,
                         running={"cmdType": "maxscript", "runningMs": ms, "clientId": "pipe-1", "requestId": "abc"},
                         inflight=[_entry("pipe-1", "abc", "maxscript", ms)])
        return self.status(health, mine=mine, process=_process("blocked"))[0]

    def test_own_request_within_its_timeout_is_busy_even_if_blocked(self):
        payload = self._own(25.0)  # past the 20 s stale rule, inside timeout + grace
        self.assertEqual(payload["bridge_state"], "busy")
        self.assertEqual(payload["main_thread"]["running"]["owner"], "this_server")

    def test_own_request_overdue_and_blocked_is_not_responding(self):
        payload = self._own(45.0)  # 30 s timeout + 10 s grace
        self.assertEqual(payload["bridge_state"], "not_responding")


class PingFallbackTests(_StatusCase):
    def test_short_running_request_is_pinged_through(self):
        health = _health("busy_mcp", pumping=True, heartbeat_ms=200, version=2,
                         running={"cmdType": "maxscript", "runningMs": 4, "clientId": "pipe-7", "requestId": "q"},
                         inflight=[_entry("pipe-7", "q", "maxscript", 5)])
        payload, sent, _ = self.status(health)
        self.assertEqual(sent, ["health", "ping"])
        self.assertTrue(payload["pong"])

    def test_v1_stale_timer_without_hung_window_is_pinged(self):
        # WM_TIMER heartbeats starve under posted messages/paints while Max still pumps.
        health = _health("busy_other", pumping=False, heartbeat_ms=4990, window_hung=False)
        payload, sent, diagnose = self.status(health)
        self.assertEqual(sent, ["health", "ping"])
        self.assertTrue(payload["pong"])
        diagnose.assert_not_called()

    def test_v2_stale_heartbeat_with_responding_window_is_pinged(self):
        health = _health("busy_other", pumping=False, heartbeat_ms=3500, version=2, window_hung=False)
        payload, sent, _ = self.status(health, process=_process("responsive"))
        self.assertEqual(sent, ["health", "ping"])
        self.assertTrue(payload["pong"])

    def test_v2_stale_heartbeat_with_hung_window_is_busy(self):
        health = _health("busy_other", pumping=False, heartbeat_ms=8000, version=2)
        payload, sent, _ = self.status(health, process=_process("busy", cpu=0.9))
        self.assertEqual(sent, ["health"])
        self.assertEqual(payload["bridge_state"], "busy")

    def test_unknown_heartbeat_is_pinged(self):
        health = _health("unknown", pumping=None, heartbeat_ms=None, version=2, window_hung=False)
        payload, sent, _ = self.status(health)
        self.assertEqual(sent, ["health", "ping"])
        self.assertTrue(payload["pong"])

    def test_active_settling_guard_reports_import_settling(self):
        settling = {"reason": "a Cosmos import", "since_s": 30.0, "expires_in_s": 270.0, "in_progress": False,
                    "evidence": None, "hung_windows": ["main window"]}
        refused = MaxImportSettlingError("3ds Max (PID 4242) is still settling after a Cosmos import. "
                                         "Do not open or close the Material Editor.",
                                         {"inflight": None, "process": None, "request_sent": False,
                                          "settling": settling})
        health = _health("busy_other", pumping=False, heartbeat_ms=30000, version=2)
        max_client._settling[4242] = {"windows": {}, "reason": "a Cosmos import", "since": 0.0,
                                      "until": float("inf"), "started": None, "owner": None}
        try:
            payload, sent, _ = self.status(health, ping=refused, process=_process("blocked"))
        finally:
            max_client._settling.pop(4242, None)
        self.assertEqual(sent, ["health", "ping"])
        self.assertEqual(payload["bridge_code"], "IMPORT_SETTLING")
        self.assertEqual(payload["settling"]["reason"], "a Cosmos import")
        self.assertIn("Material Editor", payload["message"])

    def test_silent_health_probe_reports_this_servers_request(self):
        probe = {"cmd_type": "health", "request_id": "h1", "running_s": 3.0}
        silent = MaxBusyError("did not answer 'health' within 3 s: its main thread is busy",
                              {"inflight": probe, "process": _process("busy", cpu=0.5), "request_sent": True})
        mine = {"cmd_type": "maxscript", "request_id": "abc", "running_s": 40.0}
        payload, sent, _ = self.status(silent, mine=mine)
        self.assertEqual(sent, ["health"])
        self.assertEqual(payload["inflight"]["request_id"], "abc")
        self.assertIn("whole process", payload["message"])
        self.assertNotIn("main thread is busy", payload["message"])

    def test_tcp_transport_skips_health(self):
        payload, sent, _ = self.status(_health(), transport="tcp")
        self.assertEqual(sent, ["ping"])
        self.assertTrue(payload["pong"])


class RoutingTests(unittest.TestCase):
    def test_health_bypasses_a_held_pipe_lock(self):
        pipe = r"\\.\pipe\3dsmax-mcp-pid-4242"
        client = MaxClient(transport="pipe", pipe_name=pipe)
        client._bound_target = client._target(pipe, "explicit")
        channels = []

        def reply(self, request, timeout, **_kwargs):
            channels.append(self._control_channel)
            rid = json.loads(request)["requestId"]
            return json.dumps({"success": True, "result": "{}", "requestId": rid}).encode() + b"\n"

        holder_ready, release = threading.Event(), threading.Event()

        def hold():  # a stuck request owns the pipe lock
            with client._pipe_lock:
                holder_ready.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        holder_ready.wait(5)
        try:
            with mock.patch.object(MaxClient, "_send_via_pipe", reply):
                started = time.perf_counter()
                client.send_command("", cmd_type="health", timeout=3.0)
                elapsed = time.perf_counter() - started
        finally:
            release.set()
            holder.join()
        self.assertLess(elapsed, 1.0)
        self.assertEqual(channels, [True])

    def test_hung_verdict_is_a_copy(self):
        client = MaxClient(transport="pipe", pipe_name=r"\\.\pipe\custom")
        self.assertIsNone(client.hung_verdict(4242))
        client._hung_pids[4242] = {"inflight": {"request_id": "x"}, "source": "abandoned"}
        verdict = client.hung_verdict(4242)
        verdict["source"] = "changed"
        self.assertEqual(client._hung_pids[4242]["source"], "abandoned")

    def test_health_uses_the_control_channel(self):
        client = MaxClient(transport="pipe", pipe_name=r"\\.\pipe\custom")
        with mock.patch.object(MaxClient, "_send_control_command",
                               return_value={"result": "{}"}) as control:
            client.send_command("", cmd_type="health", timeout=3.0)
        control.assert_called_once_with("", "health", 3.0)

    def test_health_is_a_probe_and_main_thread_free(self):
        self.assertIn("health", max_client._PROBE_CMD_TYPES)
        self.assertIn("health", max_client._MAIN_THREAD_FREE_CMD_TYPES)
        self.assertNotIn("ping", max_client._MAIN_THREAD_FREE_CMD_TYPES)

    def test_tcp_never_routes_health_to_the_control_channel(self):
        client = MaxClient(transport="tcp")
        with mock.patch.object(MaxClient, "_send_control_command") as control, \
                mock.patch.object(MaxClient, "_send_via_tcp", side_effect=ConnectionError("no listener")):
            with self.assertRaises(ConnectionError):
                client.send_command("", cmd_type="health", timeout=1.0)
        control.assert_not_called()

    def test_inflight_is_public(self):
        client = MaxClient(transport="pipe", pipe_name=r"\\.\pipe\custom")
        self.assertIsNone(client.inflight())


if __name__ == "__main__":
    unittest.main()
