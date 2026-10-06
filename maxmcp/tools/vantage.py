"""Chaos Vantage: read its state, and start or stop V-Ray GPU Live Link from 3ds Max."""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from ..helpers import vantage_client as vc
from ..helpers.cosmos import _open_main_dialogs
from ..helpers.native_compat import is_missing_native_route_error
from ..helpers.vantage_client import VantageError
from ..max_client import MaxBridgeError
from ..server import client, mcp

_MXS_ERROR = "__MCP_MS_ERR__:"
POLL_INTERVAL = 0.5
# Indirection so tests can replace the clock without patching the time module.
_sleep = time.sleep
_clock = time.monotonic
STOP_WAIT_LIMIT = 15
# V-Ray GPU's renderer class ID, as Chaos's Vantage scripts check it.
VRAY_GPU_CLASS_ID = (1770671000, 1323107829)
# A Max global naming the Live Link server this tool started. It survives an
# MCP server restart; stop without force only ends a link marked here.
MARKER = "mcp_vantageLiveLink"

_NOT_RUNNING_HINT = ("Ask the user to start Chaos Vantage (it needs about 7 s to initialise), then call "
                     "vantage_live_link again. This tool doesn't launch Vantage.")


@mcp.tool()
def vantage_status() -> dict[str, Any]:
    """Read Chaos Vantage's state over its local command port, without touching 3ds Max.

    Reports whether Vantage runs and is ready (phase: not_running, starting,
    initializing, ready), its state (e.g. RenderingInteractive, RenderingOffline),
    live_link (Online: waiting for a DCC; Connected: linked; Offline: Live Link
    server off), offline render progress and version. linked is true while a Live
    Link is connected; it doesn't say which 3ds Max is linked.
    Uses Vantage's undocumented local command server (localhost, port 20702 by
    default), as Chaos's own 3ds Max scripts do. It can change between versions.
    """
    return vc.read_status()


@mcp.tool()
def vantage_live_link(
    action: Literal["start", "stop"] = "start",
    wait_seconds: int = 20,
    force: bool = False,
) -> dict[str, Any]:
    """Start or stop V-Ray GPU Live Link from this 3ds Max to a running Chaos Vantage.

    start: needs Vantage running and ready, and V-Ray GPU as the production
    renderer. It refuses rather than switching renderers, and refuses while any
    V-Ray IPR runs (the user's VFB IPR, a viewport IPR, the agent viewport's),
    while the agent viewport holds a render preview, during an open undo operation,
    or while Vantage renders offline. If Vantage already reports a connected link,
    nothing is sent to Max. Then waits up to wait_seconds (0-60) for Vantage to
    report the link as Connected; Live Link then streams scene, camera and light
    changes from Max to Vantage.
    stop: ends a Live Link this tool started. force=True also ends one the user
    started; use it only when the user asked. It never stops an IPR that Vantage
    doesn't report as linked.
    Doesn't launch Vantage, switch renderers, export .vrscene files or touch
    Chaos's Vantage toolbar scripts. While linked, agent viewport render modes
    refuse with RENDER_BUSY; shaded captures still work.
    """
    if action not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 60:
        raise ValueError("wait_seconds must be an integer from 0 to 60")
    if action == "start":
        return _start(wait_seconds)
    return _stop(min(wait_seconds, STOP_WAIT_LIMIT), force)


def _require_ready() -> dict[str, Any]:
    status = vc.read_status()
    phase = status.get("phase")
    if phase == "not_running":
        raise VantageError("Chaos Vantage isn't running.", "VANTAGE_NOT_RUNNING", False, _NOT_RUNNING_HINT)
    if phase in ("starting", "initializing"):
        raise VantageError("Chaos Vantage is still starting (%s)." % phase, "VANTAGE_STARTING", True,
                           "Wait a few seconds; vantage_status says when it's ready.")
    if not status.get("ready") or "status_error" in status:
        raise VantageError("Chaos Vantage isn't answering its command port (localhost:%s): %s"
                           % (status.get("command_port"), status.get("status_error") or status.get("ping_error")
                              or "ping status %s" % status.get("ping_status")),
                           status.get("status_code") or "VANTAGE_UNAVAILABLE", True,
                           "Check that Vantage is responsive; vantage_status shows what it reports.",
                           {"vantage": status})
    return status


def _summary(status: dict[str, Any]) -> dict[str, Any]:
    keys = ("phase", "state", "live_link", "offline_task", "version", "pids", "link_port")
    return {k: status[k] for k in keys if k in status}


def _start(wait_seconds: int) -> dict[str, Any]:
    status = _require_ready()
    live_link = status.get("live_link")
    if live_link == "Connected":
        return {"action": "start", "result": "already_linked", "changed": False, "vantage": _summary(status),
                "warnings": ["Vantage already reports a connected Live Link, so nothing was sent to Max. "
                             "It may belong to another 3ds Max or DCC."]}
    if live_link == "Offline":
        raise VantageError("Vantage's Live Link server is off (liveLinkState Offline).", "VANTAGE_LINK_OFFLINE",
                           False, "Turn on Vantage's 'Start Live Link server with application' preference, or "
                                  "restart Vantage; ask the user first.", {"vantage": _summary(status)})
    if status.get("state") in vc.OFFLINE_STATES or status.get("offline_task") in vc.OFFLINE_TASKS:
        raise VantageError("Chaos Vantage is busy with an offline render (%s, task %s)."
                           % (status.get("state"), status.get("offline_task")), "VANTAGE_BUSY", True,
                           "Wait for the render to finish, or ask the user.", {"vantage": _summary(status)})
    lease = _agent_render_lease()
    if lease:
        raise VantageError("The agent viewport holds a render preview (mode %s), and a Live Link would render "
                           "its view with its preview settings." % lease.get("mode"), "RENDER_BUSY", True,
                           "Switch the agent viewport back with agent_viewport(action='render', mode='shaded'), "
                           "then start the link.", {"agent_viewport": lease})

    port = int(status.get("link_port") or vc.DEFAULT_LINK_PORT)
    outcome, detail = _run(_start_script(port))
    if outcome != "started":
        _refuse_start(outcome, detail)
    warnings: list[str] = []
    result: dict[str, Any] = {"action": "start", "changed": True, "link_server": "localhost:%d" % port}
    if wait_seconds:
        final, elapsed, poll_error = _poll(lambda s: s.get("live_link") in ("Connected", "Offline"), wait_seconds)
        result["seconds"] = round(elapsed, 1)
        if final and final.get("live_link") == "Connected":
            result["result"] = "linked"
        elif final and final.get("live_link") == "Offline":
            result["result"] = "link_failed"
            warnings.append("Vantage reports its Live Link server as Offline. V-Ray's IPR is still running in Max: "
                            "stop it with vantage_live_link(action='stop') once checked.")
        else:
            result["result"] = "starting"
            warnings.append("V-Ray's Live Link IPR started in Max, but Vantage didn't report a connected link "
                            "within %d s. Large scenes take longer: check vantage_status, and stop with "
                            "vantage_live_link(action='stop') if it never connects." % wait_seconds)
            if poll_error:
                warnings.append("Last Vantage status error: %s" % poll_error)
        if final:
            result["vantage"] = _summary(final)
    else:
        result["result"] = "requested"
    ipr = _probe_ipr()
    if ipr is not None:
        result["vray_ipr"] = ipr
    dialogs = _open_main_dialogs(client)
    if dialogs:
        result["dialogs"] = dialogs
        warnings.append("A dialog is open in 3ds Max; read it with max_dialogs(action='inspect').")
    if warnings:
        result["warnings"] = warnings
    return result


def _refuse_start(outcome: str, detail: str) -> None:
    if outcome == "ipr_running":
        raise VantageError("A V-Ray IPR is already running in 3ds Max (vrayIsRenderingIPR() = %s), and Vantage "
                           "isn't linked to it." % detail, "RENDER_BUSY", True,
                           "Ask the user to stop that IPR (VFB, viewport IPR or agent viewport render mode) first. "
                           "This tool never stops an IPR it didn't start.", {"vray_ipr": detail})
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
                           "(V-Ray 6 and later)." % detail, "UNSUPPORTED", False,
                           "Use Chaos's Vantage toolbar in Max for this V-Ray.")
    if outcome == "no_live_link_api":
        raise VantageError("This V-Ray has no MAXScript Live Link functions (vrayGPUSetLiveLinkServers, "
                           "vrayGPUStartIPR, vrayIsRenderingIPR).", "UNSUPPORTED", False,
                           "Use Chaos's Vantage toolbar in Max.")
    raise VantageError("Starting Live Link failed in 3ds Max: %s" % (detail or outcome), "LIVE_LINK_FAILED", False,
                       "Check the V-Ray log and vantage_status; nothing else was changed.")


def _stop(wait_seconds: int, force: bool) -> dict[str, Any]:
    status = vc.read_status()
    if status.get("live_link") != "Connected":
        state = status.get("live_link") or status.get("phase")
        return {"action": "stop", "result": "not_linked", "changed": False, "vantage": _summary(status),
                "warnings": ["Vantage doesn't report a connected Live Link (%s), so nothing was sent to Max. If a "
                             "V-Ray IPR is still running in Max, stop it with V-Ray's own controls." % state]}
    outcome, detail = _run(_stop_script(force))
    if outcome == "not_running":
        return {"action": "stop", "result": "not_linked", "changed": False, "vantage": _summary(status),
                "warnings": ["Vantage reports a connected Live Link, but no V-Ray IPR runs in this 3ds Max. "
                             "The link may belong to another 3ds Max or DCC."]}
    if outcome == "not_owned":
        raise VantageError("This Live Link wasn't started by vantage_live_link.", "LIVE_LINK_NOT_OWNED", False,
                           "Pass force=true only when the user asked to stop their Live Link.",
                           {"vray_ipr": detail})
    if outcome == "no_live_link_api":
        _refuse_start(outcome, detail)
    if outcome != "stopped":
        raise VantageError("Stopping Live Link failed in 3ds Max: %s" % (detail or outcome), "LIVE_LINK_FAILED",
                           False, "Check vantage_status and the V-Ray IPR state in Max.")
    result: dict[str, Any] = {"action": "stop", "result": "stopped", "changed": True}
    if wait_seconds:
        final, elapsed, _ = _poll(lambda s: s.get("live_link") not in (None, "Connected"), wait_seconds)
        result["seconds"] = round(elapsed, 1)
        if final:
            result["vantage"] = _summary(final)
            if final.get("live_link") == "Connected":
                result["warnings"] = ["V-Ray's IPR was stopped, but Vantage still reports a connected Live Link "
                                      "after %d s." % wait_seconds]
    return result


def _poll(done, wait_seconds: int):
    """Poll Vantage (never Max) until done(status) or the deadline. Returns (status, seconds, last_error)."""
    started = _clock()
    deadline = started + wait_seconds
    final = None
    error = None
    while True:
        _sleep(POLL_INTERVAL)
        status = vc.read_status()
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
    """The agent viewport's render info when it holds a render preview, else None."""
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
    # No render info means the window isn't visible; it can't be minimized while rendering.
    if not isinstance(render, dict):
        return None
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
    try:
        raw = client.send_command("(if vrayIsRenderingIPR != undefined then vrayIsRenderingIPR() else -1) as string",
                                  cmd_type="maxscript", timeout=10, probe=True).get("result", "")
        value = int(str(raw).strip().strip('"'))
    except Exception:
        return None
    return value if value >= 0 else None


def _start_script(port: int) -> str:
    # One call checks and starts, so nothing can start an IPR in between. No
    # dialogs, sleeps or timers; Chaos's VRayVantage_* functions aren't called
    # (they can switch renderers, show message boxes or block on sleep).
    return f'''(
    global {MARKER}
    local out = ""
    try (
        if vrayGPUStartIPR == undefined or vrayGPUSetLiveLinkServers == undefined or vrayIsRenderingIPR == undefined then
            out = "no_live_link_api|"
        else if theHold.Holding() then
            out = "holding|"
        else (
            local r = renderers.current
            local cid = if r == undefined then #(0, 0) else r.classid
            if not (cid[1] == {VRAY_GPU_CLASS_ID[0]} and cid[2] == {VRAY_GPU_CLASS_ID[1]}) then
                out = "not_vray_gpu|" + ((classOf r) as string)
            else if vrayGetDRVersion == undefined or vrayGetDRVersion() != 2 then
                out = "dr_version|" + (if vrayGetDRVersion == undefined then "1" else (vrayGetDRVersion()) as string)
            else (
                local ipr = vrayIsRenderingIPR()
                local viewportIPR = if vrayViewportIPRControl == undefined then false else
                    (try ((vrayViewportIPRControl 0 2) == true or (vrayViewportIPRControl 1 2) == true) catch false)
                if ipr != 0 then out = "ipr_running|" + (ipr as string)
                else if viewportIPR then out = "viewport_ipr|"
                else (
                    vrayGPUSetLiveLinkServers #("localhost:{port}")
                    vrayGPUStartIPR liveLinkMode:true
                    {MARKER} = "localhost:{port}"
                    out = "started|"
                )
            )
        )
    ) catch (out = "error|" + ((getCurrentException()) as string))
    out
)'''


def _stop_script(force: bool) -> str:
    return f'''(
    global {MARKER}
    local out = ""
    try (
        if vrayGPUStopIPR == undefined or vrayIsRenderingIPR == undefined then
            out = "no_live_link_api|"
        else (
            local ipr = vrayIsRenderingIPR()
            local ours = {MARKER} != undefined
            if ipr == 0 then (
                {MARKER} = undefined
                out = "not_running|"
            )
            else if not ours and not {"true" if force else "false"} then
                out = "not_owned|" + (ipr as string)
            else (
                vrayGPUStopIPR()
                {MARKER} = undefined
                out = "stopped|" + (ipr as string)
            )
        )
    ) catch (out = "error|" + ((getCurrentException()) as string))
    out
)'''
