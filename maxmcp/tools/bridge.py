"""Bridge/transport status tools for the live 3ds Max connection."""

from __future__ import annotations

import json

from ..max_client import (BLOCKED_CAUSE, HANG_ADVICE, MaxBusyError, MaxNotRespondingError, _grace,
                          settling_state)
from ..process_health import describe as describe_process, diagnose_process
from ..server import mcp, client

_HEALTH_TIMEOUT_S = 3.0
_CPU_SAMPLE_S = 1.0
# A main thread idle in a wait for less than this is reported busy, not blocked
# (a short non-pumping call looks the same as a deadlock from outside).
_BLOCKED_CONFIRM_S = 20.0
# A bridge request younger than this does not make Max "busy": the ping queues behind it briefly.
_BUSY_RUNNING_MS = 2000
# Bridge-internal dispatchers (invoke_tool/tool_smoke probes); never another MCP client.
_INTERNAL_CLIENTS = frozenset({"native-tool-probe"})
# Clock skew allowed between a request's registry age and its executor run time.
_ATTRIBUTION_SLACK_MS = 250


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
    if getattr(client, "transport", None) == "tcp":
        return None  # the legacy TCP listener has no health and runs on the main thread
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


def _skip_ping(health: dict) -> bool:
    """True when the health reply alone shows a ping would only queue behind a busy main thread."""
    main = health.get("mainThread") or {}
    pid = health.get("pid")
    if pid and settling_state(int(pid)):
        return False  # the ping path refuses with IMPORT_SETTLING and its advice
    state = main.get("state")
    if state == "busy_mcp":
        # A request that just started (another client's quick call) is not "busy": ping queues briefly.
        running = (health.get("executor") or {}).get("running") or {}
        return (running.get("runningMs") or 0) >= _BUSY_RUNNING_MS
    if state == "busy_other":
        # Version 1 bridges timed the heartbeat with WM_TIMER, which paints and posted
        # messages starve while the main thread still pumps; only trust it if the window is hung.
        return (health.get("healthVersion") or 1) >= 2 or main.get("windowHung") is True
    return False


def _internal(entry: dict) -> bool:
    return bool(entry.get("internal")) or entry.get("clientId") in _INTERNAL_CLIENTS


def _running_owner(running: dict, inflight: list[dict], mine_id, abandoned_id) -> str:
    """Who submitted the running item: this_server, abandoned (ours), other_client or unknown."""
    request_id = running.get("requestId")
    if request_id:  # healthVersion >= 2 names the running request
        entry = next((r for r in inflight if r.get("requestId") == request_id), None)
    else:
        # Older bridges only report the cmd type. A candidate must have existed at
        # least as long as the item has run (a queued request is younger).
        run_ms = running.get("runningMs") or 0
        candidates = [r for r in inflight if r.get("cmdType") == running.get("cmdType")
                      and (r.get("elapsedMs") or 0) + _ATTRIBUTION_SLACK_MS >= run_ms]
        entry = candidates[0] if len(candidates) == 1 else None
        request_id = entry.get("requestId") if entry else None
    if request_id and request_id == mine_id:
        return "this_server"
    if request_id and request_id == abandoned_id:
        return "abandoned"
    if entry is not None and not _internal(entry):
        return "other_client"
    return "unknown"


def _busy_from_health(health: dict) -> str | None:
    """pong=false status built from the health reply alone; no request is queued behind Max.

    None means the evidence is inconclusive (the main window still responds while no
    bridge work runs) and the caller should ping instead.
    """
    main = health.get("mainThread") or {}
    executor = health.get("executor") or {}
    running = executor.get("running") or None
    pid = health.get("pid")
    mine = client.inflight()
    mine = mine if isinstance(mine, dict) else None
    verdict = client.hung_verdict(int(pid)) if pid else None
    verdict = verdict if isinstance(verdict, dict) else None
    # This server's request abandoned after Max stopped responding: the bridge may still run it.
    abandoned = verdict.get("inflight") if verdict and isinstance(verdict.get("inflight"), dict) else None
    mine_id = (mine or {}).get("request_id")
    abandoned_id = (abandoned or {}).get("request_id")
    inflight = (health.get("clients") or {}).get("inflight") or []
    ours = {i for i in (mine_id, abandoned_id) if i}
    others = [r for r in inflight if r.get("requestId") not in ours and not _internal(r)]

    if running:
        owner = _running_owner(running, inflight, mine_id, abandoned_id)
        cmd = running.get("cmdType") or "unlabelled"
        elapsed = _seconds(running.get("runningMs"))
        if owner == "abandoned":
            what = (f"this server's earlier '{cmd}' request (abandoned after Max stopped responding) is still "
                    f"running on Max's main thread after {elapsed} s")
        else:
            holder = {"this_server": "this server's", "other_client": "another MCP client's",
                      "unknown": "a bridge"}[owner]
            what = f"{holder} '{cmd}' request has been running on Max's main thread for {elapsed} s"
    else:
        owner = None
        what = ("Max's main thread stopped pumping messages outside the bridge (rendering, loading, a "
                "script or dialog started from the UI, or a plugin)")

    # Only sample CPU when the main thread is not pumping: a pumping request is merely long.
    process = diagnose_process(int(pid), _CPU_SAMPLE_S) if pid and not main.get("pumping") else None
    if not running and (process or {}).get("state") == "responsive":
        return None  # main window still answers: let the ping (5 s deadline) decide
    stuck_s = _seconds(main.get("heartbeatAgeMs")) or 0.0
    blocked = bool(process) and (process.get("state") == "exited" or (
        process.get("state") == "blocked" and stuck_s >= _BLOCKED_CONFIRM_S))
    if blocked and owner == "this_server" and process.get("state") != "exited":
        # Same rule as a held pipe lock: this server's own request is hung only once overdue.
        limit = mine.get("timeout_s")
        blocked = limit is not None and (mine.get("running_s") or 0) >= limit + _grace(limit)
    if blocked:
        state, code = "not_responding", MaxNotRespondingError.code
        message = (f"3ds Max (PID {pid}) is not responding: {what}; {describe_process(process)}. "
                   f"Max is blocked: {BLOCKED_CAUSE}. Nothing was sent. {HANG_ADVICE} "
                   "capture_hang_diagnostics shows where it is stuck (OS-only; pauses each Max thread for milliseconds).")
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
                         "owner": "this_server" if owner == "abandoned" else owner,
                         "abandoned": owner == "abandoned"} if running else None),
            "queued": executor.get("queued"),
            "oldest_queued_s": _seconds(executor.get("oldestQueuedMs")),
        },
        "inflight": mine,
        **({"previous": abandoned, "previous_process": verdict.get("process")} if abandoned else {}),
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
        **{key: details[key] for key in ("settling", "previous") if details.get(key)},
        **({"health": _compact_health(health)} if health else {}),
    })


def _silent_bridge_status(exc: MaxBusyError | MaxNotRespondingError) -> str:
    """Status when even the bridge's pipe threads did not answer health (the probe was dropped)."""
    details = getattr(exc, "details", None) or {}
    process = details.get("process") or None
    pid = (process or {}).get("pid")
    label = f"3ds Max (PID {pid})" if pid else "3ds Max"
    evidence = f" ({describe_process(process)})" if process else ""
    advice = HANG_ADVICE if isinstance(exc, MaxNotRespondingError) else "Retry later."
    payload = json.loads(_unhealthy_status(exc))
    payload["message"] = (
        f"{label} did not answer the bridge health probe within {_HEALTH_TIMEOUT_S:g} s{evidence}. "
        "The bridge answers health off Max's main thread, so the whole process (not just its main thread) "
        f"appears frozen or suspended. The probe was dropped (it changes nothing). {advice}")
    # This server's request, not the dropped health probe.
    mine = client.inflight()
    probe = details.get("inflight") or {}
    payload["inflight"] = mine if isinstance(mine, dict) else (None if probe.get("cmd_type") == "health"
                                                              else details.get("inflight"))
    return json.dumps(payload)


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
        return _silent_bridge_status(health)  # a ping would wait out the same silence
    if health and _skip_ping(health):
        status = _busy_from_health(health)
        if status is not None:
            return status
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
