"""Bridge/transport status tools for the live 3ds Max connection."""

from __future__ import annotations

import json

from ..max_client import BLOCKED_CAUSE, HANG_ADVICE, MaxBusyError, MaxNotRespondingError
from ..process_health import describe as describe_process, diagnose_process
from ..server import mcp, client

_HEALTH_TIMEOUT_S = 3.0
_CPU_SAMPLE_S = 1.0
# A main thread idle in a wait for less than this is reported busy, not blocked
# (a short non-pumping call looks the same as a deadlock from outside).
_BLOCKED_CONFIRM_S = 20.0


def _legacy_bridge_status() -> str:
    maxscript = r"""(
        local esc = MCP_Server.escapeJsonString
        local maxYear = ((1998 + ((maxVersion())[1] / 1000)) as integer)
        local rendererName = try ((classOf renderers.current) as string) catch "unknown"
        "{\"pong\":true" + \
        ",\"server\":\"3dsmax-mcp\"" + \
        ",\"protocolVersion\":1" + \
        ",\"maxVersion\":" + (maxYear as string) + \
        ",\"renderer\":\"" + (esc rendererName) + "\"" + \
        ",\"objectCount\":" + (objects.count as string) + \
        ",\"selectionCount\":" + (selection.count as string) + \
        ",\"safeMode\":" + (if MCP_Server.safeMode then "true" else "false") + \
        ",\"port\":" + (MCP_Server.port as string) + \
        "}"
    )"""
    response = client.send_command(maxscript, timeout=5.0)
    payload = json.loads(response.get("result", "{}"))
    payload["requestId"] = response.get("requestId")
    payload["meta"] = response.get("meta", {})
    payload["connected"] = True
    payload["legacyTransport"] = True
    return json.dumps(payload)


def _native_health() -> dict | MaxBusyError | MaxNotRespondingError | None:
    """Executor/client state from a bridge pipe thread; never touches Max's main thread.

    Returns the health payload; the busy/hung verdict if even the bridge's pipe
    threads did not answer; or None when the bridge predates the health command
    (it answers "Unknown command type" at once), only TCP is available, or no
    pipe exists. The caller falls back to ping on None.
    """
    try:
        response = client.send_command("", cmd_type="health", timeout=_HEALTH_TIMEOUT_S)
        payload = json.loads(response.get("result") or "{}")
    except (MaxBusyError, MaxNotRespondingError) as exc:
        return exc
    except Exception:  # best-effort probe: old bridge, no pipe, TCP-only
        return None
    return payload if isinstance(payload, dict) and isinstance(payload.get("mainThread"), dict) else None


def _compact_health(health: dict) -> dict:
    main = health.get("mainThread") or {}
    executor = health.get("executor") or {}
    clients = health.get("clients") or {}
    return {
        "main_thread": main.get("state"),
        "heartbeat_age_ms": main.get("heartbeatAgeMs"),
        "queued": executor.get("queued"),
        "completed": executor.get("completed"),
        # Includes this status probe's own pipe connection.
        "clients_connected": clients.get("connected"),
        "bridge_version": health.get("bridgeVersion"),
    }


def _seconds(ms) -> float | None:
    return round(ms / 1000.0, 1) if isinstance(ms, (int, float)) else None


def _busy_from_health(health: dict) -> str:
    """pong=false status built from the health reply alone; no request is queued behind Max."""
    main = health.get("mainThread") or {}
    executor = health.get("executor") or {}
    running = executor.get("running") or None
    mine = client.inflight()
    mine = mine if isinstance(mine, dict) else None
    inflight = (health.get("clients") or {}).get("inflight") or []
    # The bridge echoes each request's requestId, so this server's request is identifiable.
    mine_native = next((r for r in inflight if mine and r.get("requestId") == mine.get("request_id")), None)
    others = [r for r in inflight if r is not mine_native]
    pid = health.get("pid")

    if running:
        if mine_native and mine_native.get("cmdType") == running.get("cmdType"):
            owner = "this_server"
        elif any(r.get("cmdType") == running.get("cmdType") for r in others):
            owner = "other_client"
        else:
            owner = "unknown"  # e.g. a nested or internal bridge request
        holder = {"this_server": "this server's", "other_client": "another MCP client's",
                  "unknown": "a bridge"}[owner]
        what = (f"{holder} '{running.get('cmdType') or 'unlabelled'}' request has been running on Max's "
                f"main thread for {_seconds(running.get('runningMs'))} s")
    else:
        owner = None
        what = ("Max's main thread stopped pumping messages outside the bridge (rendering, loading, a "
                "script or dialog started from the UI, or a plugin)")

    # Only sample CPU when the main thread is not pumping: a pumping request is merely long.
    process = diagnose_process(int(pid), _CPU_SAMPLE_S) if pid and not main.get("pumping") else None
    stuck_s = _seconds(main.get("heartbeatAgeMs")) or 0.0
    blocked = bool(process) and (process.get("state") == "exited" or (
        process.get("state") == "blocked" and stuck_s >= _BLOCKED_CONFIRM_S))
    if blocked:
        state, code = "not_responding", MaxNotRespondingError.code
        message = (f"3ds Max (PID {pid}) is not responding: {what}; {describe_process(process)}. "
                   f"Max is blocked: {BLOCKED_CAUSE}. Nothing was sent. {HANG_ADVICE} "
                   "capture_hang_diagnostics shows where it is stuck without touching Max.")
    else:
        state, code = "busy", MaxBusyError.code
        evidence = f" ({describe_process(process)})" if process else ""
        message = (f"3ds Max (PID {pid}) is busy: {what}{evidence}. Nothing was sent; "
                   "new requests queue behind it. Wait and re-check with get_bridge_status.")
    return json.dumps({
        "pong": False,
        "connected": not (process and process.get("state") == "exited"),
        "bridge_state": state,
        "bridge_code": code,
        "retryable": state == "busy",
        "message": message,
        "main_thread": {
            "state": main.get("state"),
            "pumping": main.get("pumping"),
            "heartbeat_age_s": _seconds(main.get("heartbeatAgeMs")),
            "running": ({"cmd_type": running.get("cmdType"), "running_s": _seconds(running.get("runningMs")),
                         "owner": owner} if running else None),
            "queued": executor.get("queued"),
            "oldest_queued_s": _seconds(executor.get("oldestQueuedMs")),
        },
        "inflight": mine,
        "other_clients": [{"client_id": r.get("clientId"), "cmd_type": r.get("cmdType"),
                           "elapsed_s": _seconds(r.get("elapsedMs"))} for r in others],
        "process": process,
        "request_sent": False,
        "health": _compact_health(health),
    })


def _unhealthy_status(exc: MaxBusyError | MaxNotRespondingError, health: dict | None = None) -> str:
    """Status payload for a busy or hung Max (deliberately no error/code keys)."""
    details = getattr(exc, "details", None) or {}
    busy = isinstance(exc, MaxBusyError)
    process = details.get("process") or {}
    return json.dumps({
        "pong": False,
        "connected": busy and process.get("alive", True) is not False,
        "bridge_state": "busy" if busy else "not_responding",
        "bridge_code": exc.code,
        "retryable": exc.retryable,
        "message": str(exc),
        "inflight": details.get("inflight"),
        "process": details.get("process"),
        "request_sent": details.get("request_sent", False),
        **({"health": _compact_health(health)} if health else {}),
    })


@mcp.tool()
def get_bridge_status() -> str:
    """Ping the MCP bridge for protocol/transport metadata.

    Use when: a tool failed with a connection/transport/claim error and you need to diagnose.
    If Max is busy or hung, returns pong=false with bridge_state, what holds the main thread
    (this server's request, another MCP client's, or work outside the bridge) and process health.
    Not when: starting a session or before every task — prefer query_scene for scene work.
    """
    health = _native_health()
    if isinstance(health, (MaxBusyError, MaxNotRespondingError)):
        return _unhealthy_status(health)  # a ping would wait out the same silence
    if health and (health.get("mainThread") or {}).get("state") in ("busy_mcp", "busy_other"):
        return _busy_from_health(health)
    try:
        response = client.send_command("", cmd_type="ping", timeout=5.0)
    except (MaxBusyError, MaxNotRespondingError) as exc:
        return _unhealthy_status(exc, health)
    except RuntimeError as exc:
        error = str(exc)
        if "Empty command" in error or "Unknown command type" in error:
            try:
                return _legacy_bridge_status()
            except (MaxBusyError, MaxNotRespondingError) as legacy_exc:
                return _unhealthy_status(legacy_exc)
        raise

    payload = json.loads(response.get("result", "{}"))
    payload["requestId"] = response.get("requestId")
    payload["meta"] = response.get("meta", {})
    payload["connected"] = True
    payload["legacyTransport"] = False
    if health:
        payload["health"] = _compact_health(health)
    return json.dumps(payload)
