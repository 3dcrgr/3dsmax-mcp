"""Main-thread (UI) hygiene.

One tool to see what quietly runs on the 3ds Max main/UI thread — redraw-views
callbacks, live .NET timers held in globals, general-callback count, playback
state — and to kill/disable a specific hook. callbacks.show() alone misses
timers and per-redraw callbacks; this fills that gap. Backed by the native
bridge (native:main_thread).
"""

import json
import time
from ..server import mcp, client


def _dialog_command(payload: dict) -> dict:
    # Do not probe native_available: its ping could itself wait on the blocked UI.
    # Reads are probes (dropped at the deadline); a press is not.
    response = client.send_command(json.dumps(payload), cmd_type="native:max_dialogs", timeout=5.0,
                                   probe=payload.get("action") != "respond")
    return json.loads(response.get("result") or "{}")


@mcp.tool()
def max_dialogs(action: str = "inspect", dialog_id: str = "", expected_dialog: str = "",
                button: str | int | None = None, wait_seconds: float = 10.0) -> str:
    """Read and answer dialogs blocking 3ds Max, over an independent connection.

    Works while Max's main thread sits inside the dialog, including a dialog
    that interrupted another MCP call (that call returns BLOCKED_BY_DIALOG).

    inspect: every blocking dialog with title, text, buttons (label, kind,
    enabled, default, role, checked), fields, which MCP calls it interrupted
    (during_requests) and expected_dialog; blocked_calls with the status or
    final result of interrupted calls; recent_actions.
    respond: press one button. Pass dialog_id and expected_dialog from a fresh
    inspect and button as its label or index; a changed dialog is refused.
    Then waits up to wait_seconds for interrupted calls to finish and returns
    their results, or the next dialog holding them.

    Answering is a decision. Choose yourself only when the user authorized
    unattended work and the task determines the answer. Otherwise show the
    user the dialog and ask; always ask before saving, overwriting, discarding
    changes, Fetch, Reset or licensing choices. Pressing posts a click; check
    what followed. Never repeat an interrupted call while it is waiting.
    Automatic: Script Controller Exception boxes are closed; recognized
    MAXScript error boxes with a sole OK are acknowledged during MCP calls,
    which then fail with MAX_DIALOG_ERROR. Requires the native bridge.
    """
    if action not in {"inspect", "respond"}:
        raise ValueError("action must be inspect or respond")
    if action == "respond" and (not dialog_id or not expected_dialog or button is None or button == ""):
        raise ValueError("respond requires dialog_id, expected_dialog and button from inspect")
    if client.transport == "tcp":
        raise ValueError("max_dialogs requires the native bridge control channel")
    if action == "inspect":
        inspection = _dialog_command({"action": "inspect"})
        inspection["blocked_calls"] = client.blocked_calls()
        return json.dumps(inspection)

    result = _dialog_command({"action": "respond", "dialog_id": dialog_id,
                              "expected_dialog": expected_dialog, "button": button})
    blocked = client.blocked_calls(wait=max(0.0, min(float(wait_seconds), 60.0)))
    if not blocked:
        time.sleep(0.3)  # Let the click land before reporting what remains open.
    after = _dialog_command({"action": "inspect"})
    return json.dumps({"response": result.get("response"), "blocked_calls": blocked,
                       "dialogs": after.get("dialogs", [])})


@mcp.tool()
def main_thread(action: str = "list", name: str = "") -> str:
    """Inspect or clean up what runs on the Max main/UI thread.

    action:
      - "list" (default) — enumerate main-thread activity as JSON:
            redrawCallbacks {enabled, count, raw}  (run on EVERY viewport redraw)
            dotNetTimers [{scope,name,class,enabled,intervalMs}]
                (enabled=true => firing; tiny intervalMs = a landmine)
            generalCallbackHandlers (count of event-driven callbacks)
            globalsScanned, state {isAnimPlaying, activeShadeRenderer}
      - "native_threads" — the C++ side: every process thread attributed to its
            owning DLL, with per-thread CPU sampled over ~500 ms. Returns
            byModule [{module,threads,cpuMsDelta,cpuMsTotal,pctBusy}] and
            topThreads [{tid,module,cpuMsDelta}] — shows which native plugin is
            burning cycles (SetTimer/RegisterNotification have no enumeration API,
            but a chugging C++ timer/worker shows up here as CPU on its module).
      - "unregister_redraw"  name=<global fn>    remove one redraw-views callback
                                                 (e.g. name="gw_viewInfo")
      - "disable_all_redraw"                     disable ALL redraw-views callbacks
      - "enable_all_redraw"                      re-enable them
      - "stop_timer"         name=<global timer> .Stop() + .Enabled=false
                                                 (e.g. name="gSimClock")
      - "remove_callback"    name=<callback id>  callbacks.removeScripts id:<name>

    Reversible: redraw callbacks re-register on Max restart; enable_all_redraw
    undoes disable_all_redraw. Kill actions return JSON {ok, action, name?, error?}.
    """
    if not client.native_available:
        return json.dumps({"ok": False, "error": "native bridge required for main_thread"})
    payload = json.dumps({"action": action, "name": name})
    response = client.send_command(payload, cmd_type="native:main_thread")
    return response.get("result", "")
