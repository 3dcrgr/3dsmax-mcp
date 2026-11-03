"""Shared error → next-tool hints for MCP envelopes."""

from __future__ import annotations

import re
from typing import Any, TypedDict


class ToolHint(TypedDict, total=False):
    message: str
    suggested_tools: list[str]
    next: list[str]


# Keyword patterns → dedicated tools that handle the same intent. Each pattern
# is scanned against user-supplied MAXScript; matches contribute tool names to
# a deduplicated suggestion list returned in the error envelope's hint.
MAXSCRIPT_SUGGESTION_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    # OSL-related failures are particularly opaque and rarely tractable by
    # retrying MAXScript. introspect_osl is the only way to learn the shader's
    # actual parameter names, output channels, and connection rules.
    (
        re.compile(
            r"\bOSLMap\b|\bOSLPath\b|\bOSLAutoUpdate\b|\bUberBitmap2?\b|\.osl\b|"
            r"\bsetOSL\w*\b|\bgetOSL\w*\b",
            re.IGNORECASE,
        ),
        ("introspect_osl", "write_osl_shader"),
    ),
    (
        re.compile(
            r"\b(Box|Sphere|Cylinder|Cone|Plane|Pyramid|Torus|GeoSphere|Teapot|Tube)\b\s*(?:\(|\w+:)",
            re.IGNORECASE,
        ),
        ("create_object",),
    ),
    (
        re.compile(r"\bclassOf\s+\$|\bshowProperties\b|\bgetProperty\b", re.IGNORECASE),
        ("inspect_object", "get_object_properties"),
    ),
    (re.compile(r"\baddModifier\b", re.IGNORECASE), ("add_modifier",)),
    (re.compile(r"\bdeleteModifier\b", re.IGNORECASE), ("remove_modifier",)),
    (re.compile(r"\.material\s*="), ("assign_material",)),
    (
        re.compile(
            r"\bcurrentMaterialLibrary\b|\bloadTempMaterialLibrary\b|"
            r"\bsaveTempMaterialLibrary\b|\bsaveMaterialLibrary\b|\bloadMaterialLibrary\b",
            re.IGNORECASE,
        ),
        ("get_material_library", "backup_material_library"),
    ),
    (
        re.compile(r"\bmeditMaterials\b|\bgetClassName\b.*[Mm]aterial"),
        ("get_materials", "get_material_slots", "get_material_library"),
    ),
    (
        re.compile(r"\.position\s*=|\.rotation\s*=|\.scale\s*=|\bmove\s+\$|\brotate\s+\$|\bscale\s+\$"),
        ("transform_object",),
    ),
    (
        re.compile(r"\bselect\s+\$|\bselectmore\s+\$|\bclearSelection\b", re.IGNORECASE),
        ("select_objects",),
    ),
    (re.compile(r"\.parent\s*="), ("set_parent",)),
    (
        re.compile(r"\.isHidden\s*=|hide\s+\$|unhide\s+\$", re.IGNORECASE),
        ("set_visibility",),
    ),
    (re.compile(r"\.name\s*=\s*\""), ("batch_rename_objects",)),
    (
        re.compile(r"\bcopy\s+\$|\binstance\s+\$|\breference\s+\$", re.IGNORECASE),
        ("clone_objects",),
    ),
    (
        re.compile(r"\bdelete\s+\$|\bdelete\s+\(getNodeByName", re.IGNORECASE),
        ("delete_objects",),
    ),
    (
        re.compile(r"\bILayerManager\b|\bLayerManager\.", re.IGNORECASE),
        ("manage_layers",),
    ),
    (
        re.compile(r"\bgroup\s+selection\b|\bungroup\b|\bopenGroup\b|\bcloseGroup\b", re.IGNORECASE),
        ("manage_groups",),
    ),
    (
        re.compile(r"\brender\s*\(|\bmax\s+quick\s+render\b", re.IGNORECASE),
        ("render_scene",),
    ),
    (
        re.compile(
            r"\bholdMaxFile\b|\bfetchMaxFile\b|\bresetMaxFile\b|\bsaveMaxFile\b",
            re.IGNORECASE,
        ),
        ("manage_scene",),
    ),
    (
        re.compile(r"\bSelectionSets\b|\bnamedSelectionSets\b", re.IGNORECASE),
        ("manage_selection_sets",),
    ),
    (
        re.compile(r"\bData_Channel\b|\bDataChannelModifier\b", re.IGNORECASE),
        ("add_data_channel", "add_dc_script_operator"),
    ),
    (
        re.compile(r"\bgetSubAnim\b|\bsetController\b|\bassignNewController\b", re.IGNORECASE),
        ("assign_controller", "inspect_controller"),
    ),
    (
        re.compile(r"\bparamWire\.connect\b|\bparamwire\.disconnect\b", re.IGNORECASE),
        ("wire_params", "unwire_params"),
    ),
)


# Message patterns applied to any tool error when no tool-authored hint exists.
_MESSAGE_HINT_RULES: tuple[tuple[re.Pattern[str], ToolHint], ...] = (
    # MAX_NOT_RESPONDING texts (max_client: "3ds Max (PID n) is not responding") and the
    # fork's IMPORT_SETTLING texts ("3ds Max (PID n) is still settling after ..."). First:
    # they can also mention "the bridge" or "main window not found".
    (
        re.compile(r"\b3ds Max \([^)]*\) is (still )?not responding\b|\bis still settling after\b", re.IGNORECASE),
        {
            "message": (
                "3ds Max is hung or still settling. Wait up to ~10 min, re-checking with get_bridge_status: "
                "V-Ray preview stalls cleared in 5-8 min, the post-Cosmos-import deadlock never did. "
                "capture_hang_diagnostics (OS-only, pauses each Max thread for milliseconds) "
                "shows what blocks its main thread."
            ),
            "suggested_tools": ["capture_hang_diagnostics", "get_bridge_status"],
        },
    ),
    # No bridge to connect to (max_client: "Named pipe ... not found"). Before the
    # target rule: a missing pipe is not a missing scene object.
    (
        re.compile(r"\bnamed pipe\b.*\bnot found\b", re.IGNORECASE),
        {
            "message": (
                "The 3ds Max bridge could not be reached: Max may not be running or still starting, or the "
                "MCP bridge plugin is not loaded. list_max_instances shows the Max instances this server can "
                "reach; start 3ds Max if none is listed, then retry."
            ),
            "suggested_tools": ["list_max_instances", "get_bridge_status"],
        },
    ),
    (
        re.compile(r"\bnot found\b|\bunknown (object|node|material)\b", re.IGNORECASE),
        {
            "message": (
                "Target was not found. Use query_scene(action=filter) or "
                "query_scene(action=selection) to locate names, then retry."
            ),
            "suggested_tools": ["query_scene", "select_objects"],
        },
    ),
    (
        re.compile(r"blocked by safe mode", re.IGNORECASE),
        {
            "message": (
                "Command blocked by safe mode. Do not retry the same call; "
                "use a dedicated MCP tool or adjust safe_mode settings."
            ),
        },
    ),
    # A script exception that the bridge relayed. The transport worked, so this
    # must win over the "bridge" rule below.
    (
        re.compile(r"__MCP_MS_ERR__|MAXScript error|-- \w+ error:", re.IGNORECASE),
        {
            "message": (
                "MAXScript error inside 3ds Max; the bridge connection is fine. "
                "Do not retry unchanged: read the error, then fix the arguments or script."
            ),
        },
    ),
    # Deliberately narrow: bare "connect"/"connection"/"claim" also appear in
    # MAXScript errors (Slate node connections, etc.) and must not match.
    (
        re.compile(
            r"named.?pipe|\bbridge\b|\bno claimed\b|\btransport\b",
            re.IGNORECASE,
        ),
        {
            "message": (
                "Bridge/transport issue. Diagnose with get_bridge_status only — "
                "not as a session preamble. Ensure MCP Claim This Max in the target window."
            ),
            "suggested_tools": ["get_bridge_status"],
        },
    ),
    (
        re.compile(r"unknown action:|unsupported |missing (required )?param", re.IGNORECASE),
        {
            "message": "Bad action or parameter. Re-read the tool schema and retry with valid args.",
        },
    ),
)


# Issue #47: Edit_Poly MODIFIER methods (ButtonOp, SetSelection, GetFaceNormal,
# SetOperation, Commit, ...) need that modifier to be the current object in the
# Modify panel. Otherwise they fault with EXCEPTION_ACCESS_VIOLATION (MAXScript
# catches it as a system exception) or silently do nothing. The script pattern
# names the modifier class or its interface, or calls one of those methods on
# something reached through .modifiers.
_EDIT_POLY_SCRIPT = re.compile(
    r"\bEdit_Poly\b|\bEditPolyMod\b|"
    r"\bmodifiers\b.*?\.(ButtonOp|SetSelection|GetSelection|GetFaceNormal|GetFaceCenter|"
    r"SetOperation|Commit|SetSlicePlane|SetEPolySelLevel)\b",
    re.IGNORECASE | re.DOTALL,
)
_EDIT_POLY_FAULT = re.compile(r"access.?violation|system exception|unknown exception", re.IGNORECASE)
_SET_CURRENT_OBJECT = re.compile(r"\bmodPanel\.setCurrentObject\b", re.IGNORECASE)

EDIT_POLY_HINT_MESSAGE = (
    "Edit_Poly modifier methods (ButtonOp, SetSelection, GetFaceNormal, SetOperation, "
    "Commit...) only work while that modifier is the current object in the Modify panel. "
    "Otherwise they fault with EXCEPTION_ACCESS_VIOLATION or silently do nothing. Make it "
    "current first: select obj; max modify mode; modPanel.setCurrentObject "
    "obj.modifiers[#Edit_Poly]. For face selections, ep.Select #Face bits select:true "
    "node:obj worked where SetSelection returned true but selected nothing. To edit the "
    "Editable Poly base instead, use mesh_edit."
)


def edit_poly_hint(script: str | None, message: str = "") -> ToolHint | None:
    """Hint for a failed script that calls Edit_Poly modifier methods (#47).

    Fires on a fault-like error, or on any error when the script never makes
    the modifier current with modPanel.setCurrentObject.
    """
    if not script or not _EDIT_POLY_SCRIPT.search(script):
        return None
    if not (_EDIT_POLY_FAULT.search(message or "") or not _SET_CURRENT_OBJECT.search(script)):
        return None
    return {"message": EDIT_POLY_HINT_MESSAGE, "suggested_tools": ["mesh_edit", "inspect_mesh"]}


# Issue #52: the Cloth modifier has the same rule. simulate and simulateLocal return
# at once and do nothing (or fault, seen live on Max 2027) unless Cloth is the current
# modifier. get/setObjectProperty
# on a COLLISION object faults with EXCEPTION_ACCESS_VIOLATION, and after that every
# simulate on that modifier faults until it is deleted and added again.
_CLOTH_SCRIPT = re.compile(r"\bCloth\s*\(|\bclassOf\b[^\n]*\bCloth\b|\bmodifiers\s*\[\s*#Cloth\b", re.IGNORECASE)
_CLOTH_CALL = re.compile(
    r"\.(simulate|simulateLocal|setObjectType|getObjectType|getObjectProperty|setObjectProperty|"
    r"resetState|createSimCache|clearSimCache)\b",
    re.IGNORECASE,
)
_CLOTH_SIMULATE = re.compile(r"\.simulate(?:Local)?\b", re.IGNORECASE)
# Cloth's own method names: they identify a Cloth call even on a modifier reached by index.
_CLOTH_ONLY_CALL = re.compile(r"\.(simulateLocal|setObjectType|getObjectProperty|setObjectProperty)\b",
                              re.IGNORECASE)

CLOTH_HINT_MESSAGE = (
    "Cloth modifier calls (simulate, simulateLocal, setObjectType, getObjectProperty, setObjectProperty) "
    "need the Cloth modifier current in the Modify panel: select clothNode; max modify mode; "
    "modPanel.setCurrentObject theCloth. Otherwise simulate returns at once and does nothing, or faults "
    "with EXCEPTION_ACCESS_VIOLATION. Never read or write getObjectProperty/setObjectProperty on a COLLISION object "
    "(setObjectType 1): it faults with EXCEPTION_ACCESS_VIOLATION, and then every simulate on that "
    "modifier faults until the modifier is deleted and added again. Only cloth objects' properties "
    "(setObjectType 2) are safe."
)
CLOTH_SIMULATE_WARNING = (
    "This script calls Cloth simulate/simulateLocal but never runs modPanel.setCurrentObject. Cloth only "
    "simulates while it is the current modifier (select clothNode; max modify mode; "
    "modPanel.setCurrentObject theCloth), otherwise the call returns at once and does nothing (#52). Check "
    "that the cloth moved."
)


def _mentions_cloth(script: str) -> bool:
    return bool(_CLOTH_ONLY_CALL.search(script) or (_CLOTH_SCRIPT.search(script) and _CLOTH_CALL.search(script)))


def cloth_hint(script: str | None, message: str = "") -> ToolHint | None:
    """Hint for a failed script that calls Cloth modifier methods (#52).

    Same rule as edit_poly_hint: a fault-like error, or any error when the script
    never makes the modifier current.
    """
    if not script or not _mentions_cloth(script):
        return None
    if not (_EDIT_POLY_FAULT.search(message or "") or not _SET_CURRENT_OBJECT.search(script)):
        return None
    return {"message": CLOTH_HINT_MESSAGE}


def cloth_success_warning(script: str | None) -> str | None:
    """Warning for a script that ran Cloth simulate without making Cloth current (#52)."""
    if not script or not _CLOTH_SIMULATE.search(script) or not (
            _CLOTH_SCRIPT.search(script) or _CLOTH_ONLY_CALL.search(script)):
        return None
    return None if _SET_CURRENT_OBJECT.search(script) else CLOTH_SIMULATE_WARNING


def current_modifier_hint(script: str | None, message: str = "") -> ToolHint | None:
    """The Edit_Poly (#47) or Cloth (#52) "make the modifier current" hint, if one applies."""
    return edit_poly_hint(script, message) or cloth_hint(script, message)


_VRAY_OUTPUT_READ_ONLY = re.compile(r"read-only property:\s*output_", re.IGNORECASE)


def vray_output_hint(message: str = "") -> ToolHint | None:
    """V-Ray GPU exposes output_* as read-only copies, the real settings live in sub-anim 1 (#49)."""
    if not _VRAY_OUTPUT_READ_ONLY.search(message or ""):
        return None
    return ToolHint(
        message=("On V-Ray GPU the output_* render settings are read-only through renderers.current. "
                 "Write them through its V-Ray settings object, renderers.current[1].value.<property>, "
                 "or use plugin_patch with root renderer, which does that for you."),
        suggested_tools=["plugin_patch"],
    )


def suggest_tools_for_maxscript(script: str) -> list[str]:
    """Return dedicated MCP tools that match MAXScript intent patterns."""
    suggestions: list[str] = []
    for pattern, tools in MAXSCRIPT_SUGGESTION_RULES:
        if pattern.search(script):
            for tool in tools:
                if tool not in suggestions:
                    suggestions.append(tool)
    return suggestions


def normalize_hint(raw: Any) -> ToolHint | None:
    """Coerce string / dict / plural ``hints`` into a canonical ToolHint."""
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        return {"message": text} if text else None
    if isinstance(raw, dict):
        message = raw.get("message")
        suggested = raw.get("suggested_tools")
        next_steps = raw.get("next")
        # Material-class catalogs and other structured hints without message:
        # preserve as-is under a synthetic message when needed.
        hint: ToolHint = {}
        if isinstance(message, str) and message.strip():
            hint["message"] = message.strip()
        elif not suggested and not next_steps:
            # Pass through structured catalogs (e.g. material_class_hint).
            return _json_safe_hint(raw)
        if isinstance(suggested, list):
            tools = [str(t) for t in suggested if t]
            if tools:
                hint["suggested_tools"] = tools
        if isinstance(next_steps, list):
            steps = [str(s) for s in next_steps if s]
            if steps:
                hint["next"] = steps
        # Preserve extra keys from tool-authored structured hints (renderers, etc.)
        for key, value in raw.items():
            if key in {"message", "suggested_tools", "next"}:
                continue
            if value is not None:
                hint[key] = value  # type: ignore[literal-required]
        return hint or None
    if isinstance(raw, list) and raw:
        # Plural "hints" sometimes arrives as a list of strings.
        parts = [str(item).strip() for item in raw if str(item).strip()]
        if not parts:
            return None
        return {"message": "; ".join(parts)}
    return None


def _json_safe_hint(raw: dict[str, Any]) -> ToolHint:
    """Keep non-canonical structured hints (material catalogs) intact."""
    out: ToolHint = {}
    for key, value in raw.items():
        if value is not None:
            out[key] = value  # type: ignore[literal-required]
    return out


def extract_tool_hint(result: Any) -> ToolHint | None:
    """Pull a tool-authored hint from a result dict (``hint`` or ``hints``)."""
    if not isinstance(result, dict):
        return None
    if "hint" in result and result["hint"]:
        return normalize_hint(result["hint"])
    if "hints" in result and result["hints"]:
        return normalize_hint(result["hints"])
    return None


def resolve_error_hint(
    *,
    tool_name: str = "",
    error: dict[str, str] | None = None,
    raw_result: Any = None,
    script: str | None = None,
) -> ToolHint | None:
    """Resolve a hint for an error envelope.

    Tool-authored hints always win. Otherwise apply MAXScript intent rules
    (when ``script`` is provided) then message-pattern rules.
    """
    authored = extract_tool_hint(raw_result)
    if authored is not None:
        return authored

    message = ""
    if error:
        message = str(error.get("message") or "")
    elif isinstance(raw_result, str):
        message = raw_result
    elif isinstance(raw_result, dict):
        message = str(raw_result.get("error") or raw_result.get("message") or "")

    current = current_modifier_hint(script, message) or vray_output_hint(message)
    if current is not None:
        return current

    if script:
        suggested = suggest_tools_for_maxscript(script)
        if suggested:
            return {
                "message": (
                    "execute_maxscript is a fallback. Before retrying the same "
                    "script, consider using a dedicated MCP tool that handles "
                    "this intent directly."
                ),
                "suggested_tools": suggested,
            }

    if not message:
        return None

    for pattern, hint in _MESSAGE_HINT_RULES:
        if pattern.search(message):
            # Avoid suggesting get_bridge_status when that tool itself failed.
            if tool_name == "get_bridge_status" and "get_bridge_status" in hint.get(
                "suggested_tools", []
            ):
                others = [t for t in hint["suggested_tools"] if t != "get_bridge_status"]
                return {"message": hint["message"], **({"suggested_tools": others} if others else {})}
            return dict(hint)

    return None
