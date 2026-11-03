import json
import re

from ..helpers.error_hints import cloth_success_warning, current_modifier_hint, suggest_tools_for_maxscript
from ..helpers.precheck import PrecheckBlocked
from ..helpers.python_execution import python_execution_script
from ..max_client import DialogBlocked, MaxBridgeError
from ..server import mcp, client


_MAXSCRIPT_ERROR_SENTINEL = "__MCP_MS_ERR__:"

# Mirrors QuietPolicy::MentionsSceneFileCommand (native/include/mcp_bridge/quiet_policy.h):
# commands whose "save changes?" prompt quiet mode would answer by discarding work (#12).
_SCENE_FILE_COMMANDS = ("resetmaxfile", "loadmaxfile", "fetchmaxfile", "quitmax", "checkforsave",
                        "max reset file", "max file new", "max file open", "max fetch",
                        # Interface spellings, e.g. through Autodesk.Max's COREInterface.
                        "filereset", "filefetch", "loadfromfile")
_ASCII_SPACE = re.compile(r"[ \t\n\v\f\r]+")


def _normalized(script: str) -> str:
    return _ASCII_SPACE.sub(" ", script).strip(" ").lower()


def mentions_scene_file_command(script: str) -> bool:
    """True when the script's text mentions a command that can prompt to save or discard the scene."""
    text = _normalized(script)
    return any(name in text for name in _SCENE_FILE_COMMANDS)


# The same state Max's own "save changes?" prompt reads. Edits made with undo off don't set it,
# in Max itself as well, so they are not protected by the prompt or by this check.
_SAVE_REQUIRED_SCRIPT = "getSaveRequired()"


# #47: execute_maxscript runs the script inside one labelled undo record, so Ctrl+Z in Max undoes
# the agent's change in one step. Bare scripts ran with undo off: their node deletes left older
# undo records pointing at freed nodes, and a later Ctrl+Z crashed Max (pure virtual call in
# UndoManager::undo). The prefix shares the script's first line, so compile errors keep the
# script's own line numbers.
UNDO_LABEL = "MCP execute_maxscript"
_UNDO_PREFIX = 'undo "' + UNDO_LABEL + '" on ('
_UNDO_SUFFIX = "\n)"
# Scripts that drive the undo system themselves run as sent: a nested hold would change what
# their theHold calls, clearUndoBuffer or max undo/redo do.
_OWN_UNDO_HANDLING = re.compile(r"\btheHold\b|\bclearUndoBuffer\b|\bmax\s+(?:undo|redo)\b", re.IGNORECASE)
_UNDO_OFF = re.compile(r"\bundo\s+(?:off|false)\b", re.IGNORECASE)
_DELETES = re.compile(r"\bdelete\b", re.IGNORECASE)
_UNDO_OFF_DELETE_WARNING = (
    "Parts of this script ran with undo off and it mentions delete. If it deleted nodes there, tell the user "
    "not to press Ctrl+Z in this Max until the next save or reload: older undo records may point at the "
    "deleted nodes, and that crashed Max before (#47). Use delete_objects, or keep undo on.")
# #48: a raw script's own time limit, the range MCG's long tools use.
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 10, 600


def undo_wrapped(script: str) -> str:
    """The script inside one labelled undo record (#47). Line numbers stay the script's own."""
    return _UNDO_PREFIX + script + _UNDO_SUFFIX


def strip_undo_prefix(exc: MaxBridgeError) -> None:
    """Drop the #47 prefix from a bridge error's text, in place.

    The bridge quotes the failing line of a parse error ("In line: ..."). On line 1 that
    line starts with the undo prefix, which is not the agent's code, so the agent would
    see it as part of its own script. Plain and JSON-escaped forms are both removed.
    """
    forms = (_UNDO_PREFIX, json.dumps(_UNDO_PREFIX)[1:-1])
    message = exc.bridge_message
    if not isinstance(message, str) or not any(f in message for f in forms):
        return
    for form in forms:
        message = message.replace(form, "")
    exc.bridge_message = message
    exc.args = (f"MAXScript error: {message}",)
    response = exc.bridge_response
    if isinstance(response, dict) and isinstance(response.get("error"), str):
        error = response["error"]
        for form in forms:
            error = error.replace(form, "")
        response["error"] = error


def undo_plan(script: str, undo: bool, file_command: bool) -> str | None:
    """Why the script runs without the undo record, or None to wrap it."""
    if not undo:
        return "undo_false"
    if file_command:
        return "file_command"  # a reset, open or fetch clears the undo stack anyway
    if _OWN_UNDO_HANDLING.search(script):
        return "own_undo_handling"
    return None


def clamp_timeout(timeout: float | None) -> float | None:
    """execute_maxscript's timeout in seconds, kept within 10-600 (None: the client default)."""
    if timeout is None:
        return None
    return float(max(MIN_TIMEOUT_S, min(int(timeout), MAX_TIMEOUT_S)))


def _add_warning(response: dict, text: str) -> None:
    meta = response.get("meta")
    if not isinstance(meta, dict):
        meta = response["meta"] = {}
    meta.setdefault("mcpWarnings", []).append(text)


class UnsavedChangesError(RuntimeError):
    """The scene has unsaved changes and the script would reset, open, fetch or quit; nothing ran."""

    code = "UNSAVED_CHANGES"
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.details = {"script_sent": False}


def _refuse_if_unsaved(tool: str) -> None:
    # loadMaxFile never asks to save, even with prompts shown, so a modified scene would be replaced
    # without a word. No exception for scripts that call checkForSave(): the name may only sit in a
    # comment, and a script can ignore the answer. With allow_discard=True such a script still asks.
    try:
        response = client.send_command(_SAVE_REQUIRED_SCRIPT, cmd_type="maxscript")
    except DialogBlocked as blocked:
        # Only this read-only check waits on the dialog; the script itself was never sent.
        raise PrecheckBlocked(blocked, tool, "Nothing was run") from None
    if str(response.get("result", "")).strip().lower() == "true":
        raise UnsavedChangesError(
            "The scene has unsaved changes and this script resets, opens, fetches or quits, so they would "
            "be lost. Nothing was run. Ask the user: save first (saveMaxFile or manage_scene), or run it "
            "again with allow_discard=True if they agree to lose the changes.")


@mcp.tool()
def execute_maxscript(code: str = "", command: str = "", quiet: bool | None = None,
                      allow_discard: bool = False, undo: bool = True, timeout: int | None = None) -> str:
    """Execute arbitrary MAXScript in 3ds Max and return the result.

    Use when: no dedicated MCP tool covers the operation (custom one-offs, rare APIs).
    Not when: objects, materials, selection, transforms, modifiers, layers, or scene queries —
    prefer the matching dedicated tool instead of raw MAXScript.

    By default the script runs in Max's quiet mode: prompts such as queryBox,
    overwrite and missing-file warnings silently take their default answer.
    A script that mentions resetMaxFile, loadMaxFile, fetchMaxFile, quitMax,
    checkForSave, "max file new/open", "max reset file", "max fetch" or the
    Interface's FileReset/FileFetch/LoadFromFile runs with prompts shown, because
    quiet mode skips the save-changes prompt (checkForSave() returns true and
    resetMaxFile() discards unsaved work): a save-changes prompt then returns
    BLOCKED_BY_DIALOG instead of discarding work (#noPrompt or quiet:true arguments
    in the script still suppress it, and so discard unsaved changes: add them only
    with the user's OK). quiet=False always shows prompts. quiet=True forces quiet
    mode, except on such a script: there it needs allow_discard=True as well; pass
    both only when the user agreed to lose unsaved changes. The check reads only
    this text: for a script that runs other scripts (fileIn, include,
    python.ExecuteFile, macros.run) that may reset, open or quit, pass quiet=False.
    Answer a shown prompt with max_dialogs. Such a script is refused with
    UNSAVED_CHANGES, before anything runs, while the scene has unsaved changes
    (loadMaxFile never asks to save, even with prompts on); save first, or pass
    allow_discard=True with the user's OK (a script that calls checkForSave()
    still shows its prompt then).

    undo (default True): the script runs inside one undo record labelled
    "MCP execute_maxscript", so Ctrl+Z in Max undoes the whole change in one step,
    and its edits mark the scene modified. On a MAXScript error Max rolls the
    record back: the script's undoable edits before the error are undone (file
    writes and other effects outside undo stay). Scripts that mention theHold,
    clearUndoBuffer or "max undo/redo", and scene reset/open/fetch scripts, run as
    sent. Pass undo=False only for a huge batch edit whose undo data would cost too
    much memory (e.g. 100k+ vertex writes). Then, and in any part the script runs
    with undo off, never delete nodes: use delete_objects. A node deleted with undo
    off can make a later Ctrl+Z crash Max (#47). Never run "max undo" through MCP.

    timeout: seconds this call waits for Max (10-600, default 120). Max still runs
    a script past it: the call then ends with MAX_BUSY or MAX_NOT_RESPONDING and
    the script keeps running. The wait is never shorter than about 130 s, because
    the bridge's queue window is 120 s. Pass up to 600 for a known long script.
    Better: split long work into chunks that each finish well inside the wait.

    Always pass #noPrompt to importFile, including OBJ/FBX imports:
    importFile @"C:/assets/model.fbx" #noPrompt
    Import dialogs block execution and can cause MCP timeouts. Configure importer
    options before importing; do not override #noPrompt with quiet:false.
    """
    script = code or command
    if not script:
        return "Error: provide MAXScript code in the 'code' parameter"
    return _run_script(script, quiet, allow_discard, "execute_maxscript", undo=undo,
                       timeout=clamp_timeout(timeout))


def _run_script(script: str, quiet: bool | None, allow_discard: bool, tool: str, *,
                undo: bool = False, timeout: float | None = None) -> str:
    """Send a script. undo=True (execute_maxscript) wraps it in one undo record (#47).
    execute_python's script brings its own record."""
    file_command = mentions_scene_file_command(script)
    if file_command and not allow_discard:
        _refuse_if_unsaved(tool)
    override = None
    if file_command and quiet is None:
        # The bridge's quiet policy would pick this too; sent explicitly, it also holds on an older
        # bridge without that policy, which would run the script quiet and discard unsaved work.
        quiet, override = False, "file_command"
    elif file_command and quiet and not allow_discard:
        # #12: quiet would answer "save changes?" by discarding the scene
        quiet, override = False, "file_command_quiet_ignored"
    raw_tool = tool == "execute_maxscript"
    skip = undo_plan(script, undo, file_command) if raw_tool else "own_undo_handling"
    try:
        response = client.send_command(script if skip else undo_wrapped(script), cmd_type="maxscript",
                                       timeout=timeout,
                                       request_fields=None if quiet is None else {"quiet": bool(quiet)})
    except MaxBridgeError as exc:
        if not skip:
            strip_undo_prefix(exc)  # a line-1 parse error quotes the wrapped line
        raise
    if isinstance(response, dict):
        meta = response.get("meta")
        if not isinstance(meta, dict):
            meta = response["meta"] = {}
        if override:
            meta["quietOverride"] = override  # reported as a warning
        if raw_tool:
            meta["undoRecord"] = None if skip else UNDO_LABEL
            if skip:
                meta["undoSkipped"] = skip
            if _DELETES.search(script) and (skip == "undo_false" or _UNDO_OFF.search(script)):
                _add_warning(response, _UNDO_OFF_DELETE_WARNING)
    result = response.get("result", "")
    failed = isinstance(result, str) and result.startswith(_MAXSCRIPT_ERROR_SENTINEL)
    cloth = cloth_success_warning(script) if raw_tool and not failed else None
    if cloth:
        _add_warning(response, cloth)  # #52: simulate returned, maybe without simulating

    if failed:
        message = result[len(_MAXSCRIPT_ERROR_SENTINEL):].strip()
        payload: dict[str, object] = {
            "status": "error",
            "error_type": "MAXScriptError",
            "error": message,
        }
        suggested = suggest_tools_for_maxscript(script)
        current = current_modifier_hint(script, message)
        if current is not None:
            payload["hint"] = current
        elif suggested:
            payload["hint"] = {
                "message": (
                    "execute_maxscript is a fallback. Before retrying the same "
                    "script, consider using a dedicated MCP tool that handles "
                    "this intent directly."
                ),
                "suggested_tools": suggested,
            }
        return json.dumps(payload)

    return result


@mcp.tool()
def execute_python(code: str, allow_discard: bool = False) -> str:
    """Execute Python in 3ds Max; return stdout, stderr and a JSON-compatible value.

    Use when no dedicated tool covers the operation. Runs on Max's main thread
    with access to pymxs; requires bridge safe_mode=false.
    Assign `result` to return it in `value` (JSON-compatible; otherwise null).
    Each call has fresh variables; imported modules remain loaded in Max.
    Undoable scene edits form one undo step and roll back on an uncaught error.
    File I/O and other effects outside Max's undo system cannot be rolled back.
    Python errors include captured output and a traceback in error.details.
    Code that mentions a reset, open, fetch or quit command (rt.loadMaxFile,
    rt.resetMaxFile, ...; see execute_maxscript) runs with prompts shown and is
    refused with UNSAVED_CHANGES while the scene has unsaved changes; save
    first, or pass allow_discard=True with the user's OK.
    """
    if not code.strip():
        return "Error: provide Python code in the 'code' parameter"
    return _run_script(python_execution_script(code), None, allow_discard, "execute_python")
