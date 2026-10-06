"""Chaos Vantage's local command server, read-only.

Vantage 3.4 runs an undocumented HTTP server on localhost (IPv6 loopback only;
port 20702 unless Vantage's own preferences say otherwise). Chaos's 3ds Max
scripts (dcc_scripts/3dsMax/Vantage-UtilityFunctions.ms) use it:
GET /ping answers 200 when Vantage is ready and 503 while it initialises, and
POST /command takes {"command": ...}. Only getStatus and getImage are sent from
here. getImage is costly (Vantage busy-waits on one core, holds its other
commands, and leaks process handles), so only vantage_get_image calls it, once
per request. The interface isn't public and may change between Vantage versions.
"""

from __future__ import annotations

import configparser
import http.client
import json
import os
from pathlib import Path
from typing import Any, Optional

from ..process_health import list_processes

DEFAULT_COMMAND_PORT = 20702
DEFAULT_LINK_PORT = 20703  # Live Link v2, the port V-Ray's DR2 protocol links to
PING_TIMEOUT = 2.0
# Chaos's scripts allow 5 s: "There are cases in which Vantage replies very slow".
COMMAND_TIMEOUT = 5.0
_MAX_BODY = 1 << 20
IMAGE_MAX_BODY = 512 << 20  # base64 of an 8K frame is about 180 MB

# getStatus fields, as Vantage names them, and the names reported here.
_STATUS_FIELDS = (
    ("state", "state"),
    ("liveLinkState", "live_link"),
    ("offlineTaskState", "offline_task"),
    ("offlineStartFrame", "offline_start_frame"),
    ("offlineCurrentFrame", "offline_current_frame"),
    ("offlineEndFrame", "offline_end_frame"),
    ("lastProcessedTaskID", "last_task_id"),
    ("appVersion", "version"),
)
OFFLINE_STATES = ("RenderingOffline", "RenderingOfflinePaused")
OFFLINE_TASKS = ("Rendering", "Queued", "AwaitingUpdate")


class VantageError(RuntimeError):
    """A Vantage request or precondition failed; carries code/retryable/hint for the tool envelope."""

    def __init__(self, message: str, code: str, retryable: bool = False,
                 hint: Optional[str] = None, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        payload: dict[str, Any] = {"type": "VantageError", "code": code, "retryable": retryable, "message": message}
        if hint:
            payload["hint"] = hint
        if details:
            payload["details"] = details
        self.bridge_message = json.dumps(payload)


def vantage_ini_path() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Chaos" / "Vantage" / "vantage.ini" if appdata else None


def read_ports() -> dict[str, Any]:
    """The command and Live Link v2 ports from Vantage's preferences, else the defaults."""
    ports: dict[str, Any] = {"command": DEFAULT_COMMAND_PORT, "link": DEFAULT_LINK_PORT, "source": "default"}
    path = vantage_ini_path()
    # Qt writes this file: "key=value" lines only; keys can hold ":".
    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",), allow_no_value=True)
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace") if path else ""
        parser.read_string(text)
    except configparser.ParsingError:
        pass  # raised after the whole file is read: every good section is kept
    except (OSError, configparser.Error):
        return ports
    if not parser.has_section("Preferences"):
        return ports
    prefs = parser["Preferences"]
    for key, name in (("commandPort", "command"), ("liveLink2Port", "link")):
        value = (prefs.get(key) or "").strip()
        if value.isdigit() and 0 < int(value) < 65536:
            ports[name] = int(value)
            ports["source"] = "vantage.ini"
    with_app = (prefs.get("startLiveLinkServerWithApp") or "").strip().lower()
    if with_app in ("true", "false"):
        ports["link_server_with_app"] = with_app == "true"
    return ports


class NotListening(OSError):
    """Nothing accepted the connection: Vantage isn't listening (yet) on that port."""


class ReplyTooLarge(http.client.HTTPException):
    """The reply is larger than the caller allows."""


def _request(method: str, path: str, port: int, body: Optional[dict] = None,
             timeout: float = COMMAND_TIMEOUT, max_body: int = _MAX_BODY) -> tuple[int, bytes]:
    # http.client never applies system proxies. "localhost" tries ::1 first,
    # where Vantage listens, then 127.0.0.1. On Windows a refused loopback
    # connect takes about 2 s per address, so it usually ends as a timeout.
    conn = http.client.HTTPConnection("localhost", port, timeout=timeout)
    try:
        try:
            conn.connect()
        except OSError as exc:
            raise NotListening("%s: %s" % (type(exc).__name__, exc)) from exc
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        if response.length is not None and response.length > max_body:
            raise ReplyTooLarge("reply of %d bytes exceeds %d" % (response.length, max_body))
        body = response.read(max_body + 1)
        if len(body) > max_body:
            raise ReplyTooLarge("reply exceeds %d bytes" % max_body)
        return response.status, body
    finally:
        conn.close()


def ping(port: int, timeout: float = PING_TIMEOUT) -> dict[str, Any]:
    """{listening, responsive, status} for GET /ping; never raises."""
    try:
        status, _ = _request("GET", "/ping", port, timeout=timeout)
    except NotListening as exc:
        return {"listening": False, "responsive": False, "error": str(exc)}
    except (OSError, http.client.HTTPException) as exc:  # connected, but no usable reply
        return {"listening": True, "responsive": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"listening": True, "responsive": True, "status": status}


def get_status(port: int, timeout: float = COMMAND_TIMEOUT) -> dict[str, Any]:
    """Vantage's getStatus reply, as Vantage sends it. Raises VantageError."""
    try:
        status, raw = _request("POST", "/command", port, body={"command": "getStatus"}, timeout=timeout)
    except (OSError, http.client.HTTPException) as exc:
        raise VantageError("Chaos Vantage didn't answer getStatus on localhost:%d (%s: %s)."
                           % (port, type(exc).__name__, exc), "VANTAGE_UNAVAILABLE", True) from exc
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise VantageError("Chaos Vantage answered getStatus with HTTP %d: %s" % (status, text[:300]),
                           "VANTAGE_UNAVAILABLE", status >= 500)
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise VantageError("Chaos Vantage's getStatus reply isn't a JSON object: %s" % text[:300],
                           "VANTAGE_BAD_REPLY", False)
    return data


def get_image(port: int, timeout_seconds: int = 5, minimum_samples: int = 0) -> dict[str, Any]:
    """Vantage's getImage reply: {"bitmap": base64 BMP or None, "headerSize": n}. Raises VantageError.

    Vantage waits (busy, on one core, holding its other commands) until minimum_samples
    are rendered or timeout_seconds pass; without minimum_samples it returns the next frame.
    """
    request: dict[str, Any] = {"command": "getImage", "timeoutSeconds": int(timeout_seconds)}
    if minimum_samples:
        request["minimumSamples"] = int(minimum_samples)  # numbers only: Vantage ignores strings
    try:
        status, raw = _request("POST", "/command", port, body=request, timeout=timeout_seconds + 15,
                               max_body=IMAGE_MAX_BODY)
    except ReplyTooLarge as exc:
        raise VantageError("Chaos Vantage's frame is too large to fetch (%s)." % exc, "VANTAGE_FRAME_TOO_LARGE",
                           False, "Lower Vantage's viewport resolution.") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise VantageError("Chaos Vantage didn't answer getImage on localhost:%d (%s: %s)."
                           % (port, type(exc).__name__, exc), "VANTAGE_UNAVAILABLE", True) from exc
    if status == 403:
        raise VantageError("Chaos Vantage refused getImage: it found no running 3ds Max or other supported DCC "
                           "it can read (it also can't when 3ds Max runs as administrator and Vantage doesn't).",
                           "VANTAGE_NO_HOST", False)
    if status == 408:
        if minimum_samples:
            raise VantageError("Chaos Vantage didn't reach %d samples within %d s."
                               % (minimum_samples, timeout_seconds), "VANTAGE_TIMEOUT", False,
                               "Use a lower minimum_samples, or 0 for the current frame. Don't retry in a loop.")
        raise VantageError("Chaos Vantage didn't hand over a frame within %d s: its command queue didn't advance "
                           "(it may be paused, or another getImage request is still waiting)." % timeout_seconds,
                           "VANTAGE_TIMEOUT", True, "Check vantage_status; retry at most once, never in a loop.")
    if status != 200:
        detail = raw[:300].decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("errorText") or detail
        except (ValueError, AttributeError):
            pass
        raise VantageError("Chaos Vantage answered getImage with HTTP %d: %s" % (status, detail),
                           "VANTAGE_UNAVAILABLE", status >= 500)
    head = raw[:300].decode("utf-8", "replace")
    try:
        data = json.loads(raw)  # bytes, so the reply isn't held twice
    except ValueError:
        data = None
    del raw
    if not isinstance(data, dict) or "bitmap" not in data:
        raise VantageError("Chaos Vantage's getImage reply isn't the expected JSON: %s" % head,
                           "VANTAGE_BAD_REPLY", False)
    return data


def read_status(timeout: Optional[float] = None) -> dict[str, Any]:
    """Whether Vantage runs and is ready, and its getStatus fields. Never raises.

    timeout caps each HTTP request (ping and getStatus), for callers with a deadline.
    """
    ports = read_ports()
    pids = [p["pid"] for p in list_processes(("vantage.exe",))]
    out: dict[str, Any] = {
        "running": bool(pids),
        "pids": pids,
        "command_port": ports["command"],
        "link_port": ports["link"],
        "ports_from": ports["source"],
    }
    if "link_server_with_app" in ports:
        out["link_server_with_app"] = ports["link_server_with_app"]
    warnings: list[str] = []
    if len(pids) > 1:
        warnings.append("%d vantage.exe processes are running. A second instance can't open the Live Link "
                        "port, but may still answer on the command port." % len(pids))
    if not pids:
        # No HTTP: a refused localhost connection takes about 2 s per address on Windows.
        out.update(phase="not_running", ready=False)
        return _with_warnings(out, warnings)
    probe = ping(ports["command"], PING_TIMEOUT if timeout is None else min(PING_TIMEOUT, timeout))
    out["listening"] = probe["listening"]
    if probe.get("error"):
        out["ping_error"] = probe["error"]
    if not probe["listening"]:
        out.update(phase="starting", ready=False)  # the command server opens about 5 s after launch
        return _with_warnings(out, warnings)
    if not probe["responsive"]:
        out.update(phase="unresponsive", ready=False)  # accepts connections but doesn't answer
        return _with_warnings(out, warnings)
    if probe["status"] == 503:
        out.update(phase="initializing", ready=False)
        return _with_warnings(out, warnings)
    if probe["status"] != 200:
        out.update(phase="unexpected", ready=False, ping_status=probe["status"])
        return _with_warnings(out, warnings)
    out.update(phase="ready", ready=True)
    try:
        status = get_status(ports["command"], COMMAND_TIMEOUT if timeout is None else min(COMMAND_TIMEOUT, timeout))
    except VantageError as exc:
        out.update(status_error=str(exc), status_code=exc.code, status_retryable=exc.retryable)
        return _with_warnings(out, warnings)
    for source, name in _STATUS_FIELDS:
        if source in status:
            out[name] = status[source]
    out["linked"] = status.get("liveLinkState") == "Connected"
    extra = {k: v for k, v in status.items() if k not in dict(_STATUS_FIELDS)}
    if extra:
        out["other_fields"] = extra
    return _with_warnings(out, warnings)


def _with_warnings(out: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if warnings:
        out["warnings"] = warnings
    return out
