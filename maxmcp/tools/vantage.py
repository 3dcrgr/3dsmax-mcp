"""Chaos Vantage: read its state, and start or stop V-Ray GPU Live Link from 3ds Max."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from typing import Any, Literal, Optional

from ..helpers import vantage_client as vc
from ..helpers import vantage_image as vimg
from ..helpers.dialogs import open_main_dialogs
from ..helpers.maxscript import safe_string
from ..helpers.native_compat import is_missing_native_route_error
from ..helpers.vantage_client import VantageError
from ..max_client import MaxBridgeError, RequestOutcomeUnknown
from ..server import client, mcp

_MXS_ERROR = "__MCP_MS_ERR__:"
POLL_INTERVAL = 0.5
# Indirection so tests can replace the clock without patching the time module.
_sleep = time.sleep
_clock = time.monotonic
STOP_WAIT_LIMIT = 15
# V-Ray GPU's renderer class ID, as Chaos's Vantage scripts check it.
VRAY_GPU_CLASS_ID = (1770671000, 1323107829)
# A Max global holding the token of the Live Link this tool started. No V-Ray
# query identifies an IPR session, so ownership is: this server process holds
# the same token (_OWN), and Chaos's toolbar hasn't been used since (its log
# file is unchanged). Any call that sees no IPR running clears the global.
MARKER = "mcp_vantageLiveLink"
CHAOS_LOG = "VRayToVantageLog.log"  # Chaos's toolbar recreates it on every Live Link action

_OWN: Optional[dict[str, Any]] = None  # {token, log, connected_seen} of the link this process started
_USER_SCRIPTS: Optional[str] = None

_NOT_RUNNING_HINT = ("Ask the user to start Chaos Vantage (it needs about 7 s to initialise), then call "
                     "vantage_live_link again. This tool doesn't launch Vantage.")
IMAGE_DIR = os.path.join(tempfile.gettempdir(), "3dsmax-mcp")
_STOP_BY_HAND = ("ask the user to stop it with the V-Ray Frame Buffer's IPR button or Chaos's Vantage toolbar")


@mcp.tool()
def vantage_status() -> dict[str, Any]:
    """Read Chaos Vantage's state over its local command port, without touching 3ds Max.

    Reports whether Vantage runs and is ready (phase: not_running, starting,
    initializing, unresponsive, unexpected, ready), its state (e.g.
    RenderingInteractive, LoadingScene, RenderingOffline), live_link (Connected
    while linked; Online and Offline are inferred to mean waiting for a DCC and
    Live Link server off), offline render progress and version. linked is true
    while a Live Link is connected; it doesn't say which 3ds Max is linked.
    Uses Vantage's undocumented local command server (localhost, port 20702 by
    default), as Chaos's own 3ds Max scripts do. It can change between versions.
    """
    return vc.read_status()


@mcp.tool()
def vantage_get_image(
    max_width: int = 1600,
    minimum_samples: int = 0,
    timeout_seconds: int = 5,
) -> dict[str, Any]:
    """Save the frame Chaos Vantage is showing as a PNG, without touching 3ds Max.

    Read the returned `file` to view it. It's Vantage's current interactive frame
    (with its exposure and colour corrections), from the scene Vantage has open:
    with Live Link, Max's linked view. Downscaled by a whole factor to at most
    max_width pixels wide (0 = full size). minimum_samples (1-16384) waits until
    Vantage has rendered that many samples, at most timeout_seconds (1-60); 0 takes
    the next frame. While it waits, Vantage spins one CPU core and holds its other
    commands, so keep waits short. Each call also leaks a few process handles
    inside Vantage (a Vantage 3.4 bug): call it when an image is needed, never in
    a polling loop. Uses Vantage's undocumented getImage command.
    """
    if isinstance(max_width, bool) or not isinstance(max_width, int) or not 0 <= max_width <= 16384:
        raise ValueError("max_width must be an integer from 0 to 16384")
    if isinstance(minimum_samples, bool) or not isinstance(minimum_samples, int) or not 0 <= minimum_samples <= 16384:
        raise ValueError("minimum_samples must be an integer from 0 to 16384")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be an integer from 1 to 60")
    status = _require_ready()
    if status.get("state") in vc.OFFLINE_STATES + ("LoadingScene",) or status.get("offline_task") in vc.OFFLINE_TASKS:
        raise VantageError("Chaos Vantage is busy (%s, offline task %s), so it has no interactive frame to give."
                           % (status.get("state"), status.get("offline_task")), "VANTAGE_BUSY", True,
                           "Wait for Vantage to finish, or ask the user.", {"vantage": _summary(status)})
    started = _clock()
    doc = vc.get_image(int(status["command_port"]), timeout_seconds, minimum_samples)
    try:
        bmp = vimg.decode_bitmap(doc)
        if bmp is None:
            raise VantageError("Chaos Vantage had no frame to return.", "VANTAGE_NO_FRAME", True,
                               "Check vantage_status; Vantage may be paused or still preparing the scene.")
        png, info = vimg.bmp_to_png(bmp, int(doc.get("headerSize") or 0), max_width)
    except vimg.VantageImageError as exc:
        raise VantageError("Chaos Vantage's image couldn't be read: %s" % exc, "VANTAGE_BAD_REPLY", False) from exc
    os.makedirs(IMAGE_DIR, exist_ok=True)
    path = os.path.join(IMAGE_DIR, "vantage_%s.png" % uuid.uuid4().hex)
    with open(path, "wb") as f:
        f.write(png)
    result: dict[str, Any] = {"type": "image_file", "file": path, "mime_type": "image/png", "size_bytes": len(png),
                              **info, "seconds": round(_clock() - started, 2), "vantage": _summary(status)}
    if minimum_samples:
        result["minimum_samples"] = minimum_samples
    if status.get("live_link") != "Connected":
        result["warnings"] = ["Vantage isn't linked to 3ds Max (live_link %s): the image shows whatever scene "
                              "Vantage has open." % status.get("live_link")]
    return result


@mcp.tool()
def vantage_live_link(
    action: Literal["start", "stop"],
    wait_seconds: int = 20,
    force: bool = False,
) -> dict[str, Any]:
    """Start or stop V-Ray GPU Live Link from this 3ds Max to a running Chaos Vantage; only on request.

    start: needs Vantage running and ready, and V-Ray GPU as the production
    renderer. It refuses rather than switching renderers, and refuses while a
    dialog is open in Max, while any V-Ray IPR runs (the user's VFB IPR, a
    viewport IPR, the agent viewport's), while the agent viewport holds a render
    preview, during an open undo operation, or while Vantage loads a scene or
    renders offline. A production render in progress isn't detected. If Vantage
    already reports a connected link, nothing is sent to Max. Then waits up to
    wait_seconds (0-60) for Vantage to report the link as Connected.
    stop: ends the link this MCP server started, while it's still running and
    Chaos's toolbar hasn't been used since, even if Vantage never connected.
    force=True ends whatever V-Ray GPU IPR runs in this Max while Vantage reports
    a connected link (Vantage doesn't say which DCC is linked); use it only when
    the user asked. Without a connected link, force changes nothing.
    Doesn't launch Vantage, export .vrscene files or touch Chaos's Vantage toolbar
    scripts; the toolbar button doesn't show a link started here, and clicking it
    stops that link.
    """
    if action not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 60:
        raise ValueError("wait_seconds must be an integer from 0 to 60")
    if action == "start":
        return _start(wait_seconds)
    return _stop(min(wait_seconds, STOP_WAIT_LIMIT), force)


# ── start ─────────────────────────────────────────────────────────────────────

def _require_ready() -> dict[str, Any]:
    status = vc.read_status()
    phase = status.get("phase")
    if phase == "not_running":
        raise VantageError("Chaos Vantage isn't running.", "VANTAGE_NOT_RUNNING", False, _NOT_RUNNING_HINT)
    if phase in ("starting", "initializing") or status.get("state") == "Startup":
        raise VantageError("Chaos Vantage is still starting (%s)." % (status.get("state") or phase),
                           "VANTAGE_STARTING", True, "Wait a few seconds; vantage_status says when it's ready.")
    if phase == "unresponsive":
        raise VantageError("Chaos Vantage accepts connections on localhost:%s but doesn't answer (%s)."
                           % (status.get("command_port"), status.get("ping_error")), "VANTAGE_UNRESPONSIVE", True,
                           "It may be loading a large scene. Wait, check vantage_status, or ask the user.")
    if not status.get("ready") or "status_error" in status:
        retryable = bool(status.get("status_retryable", phase != "unexpected"))
        raise VantageError("Chaos Vantage isn't answering its command port (localhost:%s) as expected: %s"
                           % (status.get("command_port"), status.get("status_error")
                              or "ping status %s" % status.get("ping_status")),
                           status.get("status_code") or "VANTAGE_UNAVAILABLE", retryable,
                           "Check vantage_status. A Vantage update may have changed its command port.",
                           {"vantage": _summary(status)})
    if status.get("state") == "Shutdown":
        raise VantageError("Chaos Vantage is shutting down.", "VANTAGE_UNAVAILABLE", False, _NOT_RUNNING_HINT)
    return status


def _summary(status: dict[str, Any]) -> dict[str, Any]:
    keys = ("phase", "state", "live_link", "offline_task", "version", "pids", "link_port")
    return {k: status[k] for k in keys if k in status}


def _refuse_if_dialog() -> None:
    dialogs = open_main_dialogs(client)
    if dialogs:
        raise VantageError("A dialog is open in 3ds Max, so nothing was sent.", "BLOCKED_BY_DIALOG", True,
                           "Read it with max_dialogs(action='inspect') and ask the user how to answer it, then "
                           "retry.", {"dialogs": dialogs})


def _start(wait_seconds: int) -> dict[str, Any]:
    global _OWN
    status = _require_ready()
    live_link = status.get("live_link")
    if live_link == "Connected":
        return {"action": "start", "result": "already_linked", "changed": False, "vantage": _summary(status),
                "warnings": ["Vantage already reports a connected Live Link, so nothing was sent to Max. "
                             "It may belong to another 3ds Max or DCC."]}
    if live_link == "Offline":
        raise VantageError("Vantage's Live Link server reports Offline.", "VANTAGE_LINK_OFFLINE", False,
                           "Check Vantage's 'Start Live Link server with application' preference, or restart "
                           "Vantage; ask the user first.", {"vantage": _summary(status)})
    if (status.get("state") in vc.OFFLINE_STATES + ("LoadingScene",)
            or status.get("offline_task") in vc.OFFLINE_TASKS):
        raise VantageError("Chaos Vantage is busy (%s, offline task %s)."
                           % (status.get("state"), status.get("offline_task")), "VANTAGE_BUSY", True,
                           "Wait for Vantage to finish, or ask the user.", {"vantage": _summary(status)})
    _refuse_if_dialog()
    # A running IPR is refused in the start script anyway; skip the agent viewport
    # status read then, because it briefly activates the agent's view.
    if not _probe_ipr():
        lease = _agent_render_lease()
        if lease:
            raise VantageError("The agent viewport holds a render preview (%s), and a Live Link would render its "
                               "view with its preview settings." % (lease.get("mode") or lease.get("window_state")),
                               "RENDER_BUSY", True,
                               "Make the agent viewport visible and switch it to shaded with "
                               "agent_viewport(action='render', mode='shaded'), or release it; then start the link.",
                               {"agent_viewport": lease})

    port = int(status.get("link_port") or vc.DEFAULT_LINK_PORT)
    if _OWN and not _log_unchanged(_OWN):
        _OWN = None  # Chaos's toolbar was used since: a running IPR may be the user's
    previous = _OWN["token"] if _OWN else ""
    token = "%d-%s" % (os.getpid(), uuid.uuid4().hex[:12])
    log_before = _chaos_log_state()
    try:
        outcome, detail = _run(_start_script(port, token, previous))
    except RequestOutcomeUnknown as exc:
        _OWN = {"token": token, "log": log_before, "connected_seen": False}  # it may have started
        raise VantageError("The start request reached 3ds Max, but its reply was lost: Live Link may or may not "
                           "have started.", "REQUEST_OUTCOME_UNKNOWN", False,
                           "Check vantage_status and Max before anything else; don't start again until they "
                           "show the state. vantage_live_link(action='stop') can end it if it started.") from exc
    if outcome == "own_running":
        result: dict[str, Any] = {"action": "start", "changed": False, "link_server": "localhost:%d" % port,
                                  "note": "The Live Link IPR this server started earlier is still running; "
                                          "waiting for Vantage to connect."}
    elif outcome == "started":
        _OWN = {"token": token, "log": _chaos_log_state(), "connected_seen": False}
        result = {"action": "start", "changed": True, "link_server": "localhost:%d" % port,
                  "note": "Chaos's Vantage toolbar button in Max doesn't show this link; clicking it stops it."}
    else:
        _refuse_start(outcome, detail)

    warnings: list[str] = []
    if wait_seconds:
        final, elapsed, poll_error = _poll(lambda s: s.get("live_link") in ("Connected", "Offline"), wait_seconds)
        result["seconds"] = round(elapsed, 1)
        if final and final.get("live_link") == "Connected":
            result["result"] = "linked"
            _OWN["connected_seen"] = True
        elif final and final.get("live_link") == "Offline":
            result["result"] = "link_failed"
            warnings.append("Vantage reports its Live Link server as Offline. V-Ray's Live Link IPR is still "
                            "running in Max; end it with vantage_live_link(action='stop').")
        else:
            result["result"] = "starting"
            warnings.append("V-Ray's Live Link IPR is running in Max, but Vantage didn't report a connected link "
                            "within %d s. Large scenes take longer: check vantage_status. If it never connects, "
                            "end it with vantage_live_link(action='stop'); if this MCP server restarts first, "
                            "%s." % (wait_seconds, _STOP_BY_HAND))
            if poll_error:
                warnings.append("Last Vantage status error: %s" % poll_error)
        if final:
            result["vantage"] = _summary(final)
    else:
        result["result"] = "requested"
    ipr = _probe_ipr()
    if ipr is not None:
        result["vray_ipr"] = ipr
        if ipr == 0 and wait_seconds >= 2 and result["result"] != "linked":
            result["result"] = "start_failed"
            _OWN = None
            warnings.append("No V-Ray IPR is running in Max after the start: V-Ray didn't start the Live Link IPR. "
                            "Check the V-Ray log in Max.")
    dialogs = open_main_dialogs(client)
    if dialogs:
        result["dialogs"] = dialogs
        if not (client.get_last_transport() or {}).get("open_dialogs"):
            warnings.append("A dialog is open in 3ds Max; read it with max_dialogs(action='inspect').")
    if warnings:
        result["warnings"] = warnings
    return result


def _refuse_start(outcome: str, detail: str) -> None:
    if outcome == "ipr_running":
        raise VantageError("A V-Ray IPR is already running in 3ds Max (vrayIsRenderingIPR() = %s)." % detail,
                           "RENDER_BUSY", True,
                           "Ask the user to stop that IPR (VFB, viewport IPR or agent viewport render mode) first. "
                           "If it's a Live Link that an earlier MCP session started, %s." % _STOP_BY_HAND,
                           {"vray_ipr": detail})
    if outcome == "viewport_ipr":
        raise VantageError("A V-Ray viewport IPR is running in 3ds Max.", "RENDER_BUSY", True,
                           "Ask the user to stop the viewport IPR first.")
    if outcome == "holding":
        raise VantageError("An undo operation is open in 3ds Max.", "USER_BUSY", True,
                           "Retry once the current operation in Max finishes.")
    if outcome == "not_vray_gpu":
        raise VantageError("Live Link needs V-Ray GPU as the production renderer; it's %s." % detail,
                           "UNSUPPORTED", False,
                           "Ask the user to switch Render Setup to V-Ray GPU. This tool never switches renderers.",
                           {"renderer": detail})
    if outcome == "dr_version":
        raise VantageError("V-Ray reports distributed-rendering protocol %s; this tool supports only protocol 2 "
                           "(V-Ray 7.3 and later DR2 builds)." % detail, "UNSUPPORTED", False,
                           "Use Chaos's Vantage toolbar in Max for this V-Ray.")
    if outcome == "no_live_link_api":
        raise VantageError("This V-Ray has no MAXScript Live Link functions (vrayGPUSetLiveLinkServers, "
                           "vrayGPUStartIPR, vrayGPUStopIPR, vrayIsRenderingIPR).", "UNSUPPORTED", False,
                           "Use Chaos's Vantage toolbar in Max.")
    raise VantageError("The Live Link script failed in 3ds Max: %s" % (detail or outcome), "LIVE_LINK_FAILED", False,
                       "Check the V-Ray log in Max and vantage_status.")


# ── stop ──────────────────────────────────────────────────────────────────────

def _stop(wait_seconds: int, force: bool) -> dict[str, Any]:
    global _OWN
    status = vc.read_status()
    connected = status.get("live_link") == "Connected"
    if connected and _OWN:
        _OWN["connected_seen"] = True
    owned = bool(_OWN) and _log_unchanged(_OWN)
    if _OWN and not owned:
        _OWN = None  # Chaos's toolbar was used since: whatever runs now may be the user's
    if not connected and not (owned and not _OWN.get("connected_seen")):
        # Without a connected link, only a link this server started that never
        # connected is ended; a link that dropped may have been replaced by the
        # user's own IPR.
        state = status.get("live_link") or status.get("phase")
        return {"action": "stop", "result": "not_linked", "changed": False, "vantage": _summary(status),
                "warnings": ["Vantage doesn't report a connected Live Link (%s), so nothing was sent to Max. If a "
                             "V-Ray IPR is still running there, %s." % (state, _STOP_BY_HAND)]}
    _refuse_if_dialog()
    token = _OWN["token"] if owned else ""
    outcome, detail = _run(_stop_script(token, force and connected))
    if outcome == "not_running":
        _OWN = None
        return {"action": "stop", "result": "not_linked", "changed": False, "vantage": _summary(status),
                "warnings": ["No V-Ray IPR runs in this 3ds Max." + (" Vantage's connected link belongs to another "
                                                                    "3ds Max or DCC." if connected else "")]}
    if outcome == "not_owned":
        raise VantageError("This Live Link can't be confirmed as one this MCP server started (a different MCP "
                           "session started it, or Chaos's toolbar was used since).", "LIVE_LINK_NOT_OWNED", False,
                           "Pass force=true only when the user asked to stop their Live Link.",
                           {"vray_ipr": detail})
    if outcome == "not_vray_gpu":
        raise VantageError("The production renderer isn't V-Ray GPU (%s), so the running IPR isn't a Live Link "
                           "this tool can stop." % detail, "UNSUPPORTED", False, _STOP_BY_HAND.capitalize() + ".")
    if outcome != "stopped":
        _refuse_start(outcome, detail)
    _OWN = None
    before, _, after = detail.partition("|")
    result: dict[str, Any] = {"action": "stop", "result": "stopped", "changed": True}
    if after.strip().isdigit():
        result["vray_ipr_after"] = int(after)
    if wait_seconds and connected:
        final, elapsed, _ = _poll(lambda s: s.get("live_link") not in (None, "Connected"), wait_seconds)
        result["seconds"] = round(elapsed, 1)
        if final:
            result["vantage"] = _summary(final)
            if final.get("live_link") == "Connected":
                result["warnings"] = ["V-Ray's IPR was stopped, but Vantage still reports a connected Live Link "
                                      "after %d s." % wait_seconds]
    return result


# ── helpers ───────────────────────────────────────────────────────────────────

def _poll(done, wait_seconds: int):
    """Poll Vantage (never Max) until done(status) or the deadline. Returns (status, seconds, last_error)."""
    started = _clock()
    deadline = started + wait_seconds
    final = None
    error = None
    while True:
        _sleep(POLL_INTERVAL)
        remaining = deadline - _clock()
        status = vc.read_status(timeout=max(0.5, remaining))
        if status.get("ready") and "status_error" not in status:
            final, error = status, None
            if done(status):
                break
        else:
            error = status.get("status_error") or status.get("ping_error") or status.get("phase")
        if _clock() >= deadline:
            break
    return final, _clock() - started, error


def _agent_render_lease() -> dict[str, Any] | None:
    """The agent viewport's render state when it may hold a render preview, else None."""
    if not getattr(client, "native_available", False):
        return None
    try:
        data = json.loads(client.send_command('{"action":"status"}', cmd_type="native:agent_viewport")["result"])
    except (MaxBridgeError, RuntimeError) as exc:
        if is_missing_native_route_error(exc):
            return None
        raise
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(data, dict) or data.get("owner") != "agent":
        return None
    render = data.get("render")
    if not isinstance(render, dict):
        # Render state is reported only for a visible window. A minimized one can't
        # be rendering (minimizing is refused then); a hidden or offscreen one can
        # still hold a preview lease.
        state = data.get("window_state")
        return None if state == "minimized" else {"window_state": state}
    if (render.get("mode") != "shaded" or render.get("progressive_requested")
            or render.get("capture_target", "agent") != "agent"):
        return {k: render.get(k) for k in ("mode", "capture_target", "session_state", "progressive_requested")}
    return None


def _run(script: str) -> tuple[str, str]:
    """Run a start/stop script; returns (outcome, detail) from its "outcome|detail" reply."""
    response = client.send_command(script, cmd_type="maxscript", request_fields={"quiet": True})
    raw = response.get("result", "")
    if not isinstance(raw, str):
        raw = str(raw)
    if raw.startswith(_MXS_ERROR):
        return "error", raw[len(_MXS_ERROR):].strip()
    raw = raw.strip().strip('"')
    outcome, _, detail = raw.partition("|")
    return outcome, detail


def _probe_ipr() -> int | None:
    """vrayIsRenderingIPR() as a read-only probe; None when unknown."""
    try:
        raw = client.send_command("(if vrayIsRenderingIPR != undefined then vrayIsRenderingIPR() else -1) as string",
                                  cmd_type="maxscript", timeout=10, probe=True).get("result", "")
        value = int(str(raw).strip().strip('"'))
    except Exception:
        return None
    return value if value >= 0 else None


def _chaos_log_state() -> Optional[tuple]:
    """(exists, mtime_ns, size) of Chaos's Vantage toolbar log; None if unknown."""
    global _USER_SCRIPTS
    if _USER_SCRIPTS is None:
        try:
            raw = client.send_command("(getDir #userScripts) as string", cmd_type="maxscript", timeout=10,
                                      probe=True).get("result", "")
            _USER_SCRIPTS = str(raw).strip().strip('"')
        except Exception:
            return None
    if not _USER_SCRIPTS:
        return None
    try:
        st = os.stat(os.path.join(_USER_SCRIPTS, CHAOS_LOG))
    except FileNotFoundError:
        return (False, 0, 0)
    except OSError:
        return None
    return (True, st.st_mtime_ns, st.st_size)


def _log_unchanged(own: dict[str, Any]) -> bool:
    """True when Chaos's toolbar log is as it was right after this tool's start."""
    recorded = own.get("log")
    return recorded is not None and _chaos_log_state() == recorded


def _start_script(port: int, token: str, previous: str) -> str:
    # One call checks and starts, so nothing can start an IPR in between. No
    # dialogs, sleeps or timers; Chaos's VRayVantage_* functions aren't called
    # (they can switch renderers, show message boxes or block on sleep).
    return f'''(
    global {MARKER}
    local out = ""
    try (
        if vrayGPUStartIPR == undefined or vrayGPUStopIPR == undefined or vrayGPUSetLiveLinkServers == undefined or vrayIsRenderingIPR == undefined then
            out = "no_live_link_api|"
        else (
            local ipr = vrayIsRenderingIPR()
            if ipr == 0 do {MARKER} = undefined
            if theHold.Holding() then
                out = "holding|"
            else (
                local r = renderers.current
                local cid = if r == undefined then #(0, 0) else r.classid
                if not (cid[1] == {VRAY_GPU_CLASS_ID[0]} and cid[2] == {VRAY_GPU_CLASS_ID[1]}) then
                    out = "not_vray_gpu|" + ((classOf r) as string)
                else if vrayGetDRVersion == undefined or vrayGetDRVersion() != 2 then
                    out = "dr_version|" + (if vrayGetDRVersion == undefined then "1" else (vrayGetDRVersion()) as string)
                else (
                    local viewportIPR = if vrayViewportIPRControl == undefined then false else
                        (try ((vrayViewportIPRControl 0 2) == true or (vrayViewportIPRControl 1 2) == true) catch false)
                    if ipr != 0 then
                        out = (if "{safe_string(previous)}" != "" and {MARKER} == "{safe_string(previous)}" then "own_running|" else "ipr_running|") + (ipr as string)
                    else if viewportIPR then out = "viewport_ipr|"
                    else (
                        vrayGPUSetLiveLinkServers #("localhost:{port}")
                        vrayGPUStartIPR liveLinkMode:true
                        {MARKER} = "{safe_string(token)}"
                        out = "started|"
                    )
                )
            )
        )
    ) catch (out = "error|" + ((getCurrentException()) as string))
    out
)'''


def _stop_script(token: str, force: bool) -> str:
    return f'''(
    global {MARKER}
    local out = ""
    try (
        if vrayGPUStopIPR == undefined or vrayIsRenderingIPR == undefined then
            out = "no_live_link_api|"
        else (
            local ipr = vrayIsRenderingIPR()
            local r = renderers.current
            local cid = if r == undefined then #(0, 0) else r.classid
            local ours = "{safe_string(token)}" != "" and {MARKER} == "{safe_string(token)}"
            if ipr == 0 then (
                {MARKER} = undefined
                out = "not_running|"
            )
            else if not (cid[1] == {VRAY_GPU_CLASS_ID[0]} and cid[2] == {VRAY_GPU_CLASS_ID[1]}) then
                out = "not_vray_gpu|" + ((classOf r) as string)
            else if not ours and not {"true" if force else "false"} then
                out = "not_owned|" + (ipr as string)
            else (
                vrayGPUStopIPR()
                {MARKER} = undefined
                out = "stopped|" + (ipr as string) + "|" + ((vrayIsRenderingIPR()) as string)
            )
        )
    ) catch (out = "error|" + ((getCurrentException()) as string))
    out
)'''
