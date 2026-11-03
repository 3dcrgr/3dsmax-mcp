import json as _json
from ..server import mcp, client

# #46: a reset or fetch keeps its prompt, so the user can decline it. #systemPostReset fires
# only for a reset that ran, #filePostOpen when a fetch loaded the hold file (a reset fires
# it too, for its start-up scene). The script returns both counts and the scene state
# (file, object count, getSaveRequired) before and after, separated by "|" (not allowed in
# Windows paths); the MAXScript error, if any, comes last.
# quiet=false keeps a native bridge from running the script in quiet mode, where the
# prompts take their default; the TCP listener ignores the field.
_SCENE_SWAP = r"""(
    global mcp_sceneSwap46 = #(0, 0)
    callbacks.removeScripts id:#mcpManageScene46
    callbacks.addScript #systemPostReset "mcp_sceneSwap46[1] += 1" id:#mcpManageScene46
    callbacks.addScript #filePostOpen "mcp_sceneSwap46[2] += 1" id:#mcpManageScene46
    fn mcpSceneState = ((if maxFileName == "" then "" else maxFilePath + maxFileName) + "|" + (objects.count as string) + "|" + ((getSaveRequired()) as string))
    local before = mcpSceneState()
    local err = ""
    try (__COMMAND__) catch (err = getCurrentException())
    callbacks.removeScripts id:#mcpManageScene46
    local fired = mcp_sceneSwap46
    mcp_sceneSwap46 = undefined
    (fired[1] as string) + "|" + (fired[2] as string) + "|" + before + "|" + (mcpSceneState()) + "|" + (err as string)
)"""


def _scene_swap_result(action: str, raw: object) -> str:
    """The native handler's JSON for a fallback reset or fetch; raw text when the reply can't be read."""
    text = str(raw).strip()
    if text.startswith('"') and text.endswith('"') and len(text) > 1:
        text = text[1:-1]
    parts = text.split("|", 8)
    if len(parts) != 9 or not parts[0].isdigit() or not parts[1].isdigit():
        return str(raw)
    resets, opens = int(parts[0]), int(parts[1])

    def state(path: str, count: str, dirty: str) -> dict:
        return {"file": path, "objects": int(count) if count.isdigit() else None,
                "save_required": dirty.strip().lower() == "true"}

    before, after, error = state(*parts[2:5]), state(*parts[5:8]), parts[8].strip()
    reset = action == "reset"
    done = resets > 0 if reset else (opens > 0 and resets == 0)
    result: dict = {"action": action, ("reset" if reset else "fetched"): done, "before": before, "after": after}
    if done:
        result["message"] = "Scene reset to empty" if reset else "Fetched (restored) held state"
    else:
        result["status"] = "not_reset" if reset else "not_fetched"
        result["message"] = (
            "Scene was not reset: the prompt was declined or cancelled, so the scene is unchanged" if reset else
            "Hold was not fetched: the prompt was declined or cancelled (or there is no hold), so the scene is "
            "unchanged")
        result["warnings"] = [result["message"]]
    if error:
        result["maxscript_error"] = error
        result.setdefault("warnings", []).append("MAXScript reported: " + error)
    return _json.dumps(result)


@mcp.tool()
def manage_scene(action: str) -> str:
    """Manage the 3ds Max scene state."""
    action = action.lower().strip()

    if client.native_available:
        payload = _json.dumps({"action": action})
        response = client.send_command(payload, cmd_type="native:manage_scene")
        return response.get("result", "")

    if action == "hold":
        maxscript = """(
            holdMaxFile()
            "Hold saved successfully"
        )"""
    elif action in ("fetch", "reset"):
        # Prompts shown, as in the native handler: Fetch confirms, and Reset asks to save
        # unsaved changes instead of discarding them. The user can decline either (#46).
        command = "fetchMaxFile quiet:false" if action == "fetch" else "resetMaxFile()"
        maxscript = _SCENE_SWAP.replace("__COMMAND__", command)
        response = client.send_command(maxscript, request_fields={"quiet": False})
        return _scene_swap_result(action, response.get("result", ""))
    elif action == "save":
        maxscript = """(
            local fp = maxFilePath + maxFileName
            if fp == "" then (
                "Error: Scene was not saved: it has no file path yet. Use File > Save As first."
            ) else if not (saveMaxFile fp) then (
                "Error: 3ds Max failed to save " + fp
            ) else (
                "Saved: " + fp
            )
        )"""
    elif action == "info":
        maxscript = r"""(
            local fp = maxFilePath + maxFileName
            if fp == "" do fp = "(unsaved)"
            local objCount = objects.count
            local polyCount = 0
            for obj in objects do (
                try (
                    local m = snapshotAsMesh obj
                    polyCount += m.numFaces
                    delete m
                ) catch ()
            )
            "{" + \
                "\"filePath\":\"" + (substituteString fp "\\" "\\\\") + "\"," + \
                "\"objectCount\":" + (objCount as string) + "," + \
                "\"polyCount\":" + (polyCount as string) + \
            "}"
        )"""
    else:
        return f"Unknown action: {action}. Use hold, fetch, reset, save, or info."

    response = client.send_command(maxscript)
    return response.get("result", "")


@mcp.tool()
def undo_last() -> str:
    """Undo the last 3ds Max scene operation."""
    if client.native_available:
        try:
            response = client.send_command("{}", cmd_type="native:undo_last")
            return response.get("result", "")
        except RuntimeError:
            pass

    response = client.send_command('max undo; "Undid last scene operation"')
    return response.get("result", "")
