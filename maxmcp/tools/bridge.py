"""Bridge/transport status tools for the live 3ds Max connection."""

from __future__ import annotations

import json

from ..max_client import MaxBusyError, MaxNotRespondingError
from ..server import mcp, client


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


def _unhealthy_status(exc: MaxBusyError | MaxNotRespondingError) -> str:
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
    })


@mcp.tool()
def get_bridge_status() -> str:
    """Ping the MCP bridge for protocol/transport metadata.

    Use when: a tool failed with a connection/transport/claim error and you need to diagnose.
    If Max is busy or hung, returns pong=false with bridge_state, the in-flight request and process health.
    Not when: starting a session or before every task — prefer query_scene for scene work.
    """
    try:
        response = client.send_command("", cmd_type="ping", timeout=5.0)
    except (MaxBusyError, MaxNotRespondingError) as exc:
        return _unhealthy_status(exc)
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
    return json.dumps(payload)
