import json

from ..helpers.error_hints import suggest_tools_for_maxscript
from ..helpers.python_execution import python_execution_script
from ..server import mcp, client


_MAXSCRIPT_ERROR_SENTINEL = "__MCP_MS_ERR__:"


@mcp.tool()
def execute_maxscript(code: str = "", command: str = "", quiet: bool | None = None) -> str:
    """Execute arbitrary MAXScript in 3ds Max and return the result.

    Use when: no dedicated MCP tool covers the operation (custom one-offs, rare APIs).
    Not when: objects, materials, selection, transforms, modifiers, layers, or scene queries —
    prefer the matching dedicated tool instead of raw MAXScript.

    By default the script runs in Max's quiet mode: prompts such as queryBox,
    overwrite and missing-file warnings silently take their default answer.
    A script that mentions resetMaxFile, loadMaxFile, fetchMaxFile, quitMax,
    checkForSave or "max file new/open"/"max reset file" runs with prompts shown,
    so a save-changes prompt returns BLOCKED_BY_DIALOG instead of discarding work
    (#noPrompt still suppresses it). quiet=False always shows prompts; quiet=True
    forces quiet mode. Answer a shown prompt with max_dialogs.

    Always pass #noPrompt to importFile, including OBJ/FBX imports:
    importFile @"C:/assets/model.fbx" #noPrompt
    Import dialogs block execution and can cause MCP timeouts. Configure importer
    options before importing; do not override #noPrompt with quiet:false.
    """
    script = code or command
    if not script:
        return "Error: provide MAXScript code in the 'code' parameter"
    response = client.send_command(script, cmd_type="maxscript",
                                   request_fields=None if quiet is None else {"quiet": bool(quiet)})
    result = response.get("result", "")

    if isinstance(result, str) and result.startswith(_MAXSCRIPT_ERROR_SENTINEL):
        message = result[len(_MAXSCRIPT_ERROR_SENTINEL):].strip()
        payload: dict[str, object] = {
            "status": "error",
            "error_type": "MAXScriptError",
            "error": message,
        }
        suggested = suggest_tools_for_maxscript(script)
        if suggested:
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
def execute_python(code: str) -> str:
    """Execute Python in 3ds Max; return stdout, stderr and a JSON-compatible value.

    Use when no dedicated tool covers the operation. Runs on Max's main thread
    with access to pymxs; requires bridge safe_mode=false.
    Assign `result` to return it in `value` (JSON-compatible; otherwise null).
    Each call has fresh variables; imported modules remain loaded in Max.
    Undoable scene edits form one undo step and roll back on an uncaught error.
    File I/O and other effects outside Max's undo system cannot be rolled back.
    Python errors include captured output and a traceback in error.details.
    """
    if not code.strip():
        return "Error: provide Python code in the 'code' parameter"
    return execute_maxscript(code=python_execution_script(code))
