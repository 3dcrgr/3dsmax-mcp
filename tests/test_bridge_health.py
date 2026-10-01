"""get_bridge_status driven by the native pipe-thread health command (MCP_ISSUES #4, #6)."""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp import max_client  # noqa: E402
from maxmcp.max_client import MaxBridgeError, MaxClient, MaxNotRespondingError  # noqa: E402

PING = {"result": json.dumps({"pong": True, "maxVersion": 2026}), "requestId": "r", "meta": {}}


def _load_bridge(client):
    fake_server = types.ModuleType("maxmcp.server")
    fake_server.mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))
    fake_server.client = client
    with mock.patch.dict(sys.modules, {"maxmcp.server": fake_server}):
        sys.modules.pop("maxmcp.tools.bridge", None)
        return importlib.import_module("maxmcp.tools.bridge")


def _health(state="responsive", pumping=True, heartbeat_ms=400, running=None, inflight=(), queued=0):
    return {
        "server": "3dsmax-mcp-native", "bridgeVersion": "1.7.3", "healthVersion": 1, "pid": 4242,
        "maxVersion": 2026,
        "mainThread": {"state": state, "pumping": pumping, "heartbeatAgeMs": heartbeat_ms,
                       "heartbeatPeriodMs": 1000, "windowHung": not pumping},
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
    def status(self, health, *, mine=None, process=None, ping=PING):
        client = mock.Mock()
        client.inflight.return_value = mine
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


class RoutingTests(unittest.TestCase):
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
