"""Guarded typed plugin edits using inspector-issued references."""
import json
import math
from typing import Any
from ..server import client, mcp
from ..helpers.maxscript import safe_string
from ..max_client import MaxBridgeError
from ..helpers.plugin_schema import native
from ..helpers import plugin_schema
from ..helpers.plugin_semantics import resolve_enum


@mcp.tool()
def plugin_patch(edits: list[dict[str, Any]]) -> dict:
    """Atomically edit inspected PB2 properties with native typed readback.

    Each edit: owner_ref, property_ref, expected_schema (schema_token),
    expected_state (state_token), value. Get these with
    inspect_plugin_instance(schema_version=2, fields=[...]). References are
    explicit owner_ref objects; arrays preserve their existing count. Values
    use the inspector's native units. Optional sharing="all_instances" explicitly
    permits changing a shared owner. Animated parameters are refused. No raw
    MAXScript, implicit cloning or controller replacement. Named enum values use
    value={"enum":"name"} from the inspector's published/provider choices.
    One exception: V-Ray GPU's output_* render settings ({root:renderer}) are
    read-only mirrors of its V-Ray settings sub-anim; those edits are written
    there (renderers.current[1].value) with readback, route
    "vray_gpu_settings_subanim".
    """
    if not edits or len(edits) > 512:
        raise ValueError("Supply 1..512 property edits.")
    compiled = []
    for edit in edits:
        value = edit.get("value")
        if isinstance(value, dict) and "enum" in value:
            schema = plugin_schema.inspect(owner_ref=edit["owner_ref"], fields=[edit["property_ref"]])
            if schema["schema_token"] != edit["expected_schema"] or len(schema["properties"]) != 1:
                raise plugin_schema.PluginGuardError("STALE_SCHEMA", "inspect the property again.")
            value = resolve_enum(schema["properties"][0], value)
        compiled.append({**edit, "value": value})
    try:
        return native("native:plugin_patch", {"version": 1, "edits": compiled})
    except MaxBridgeError as exc:
        if _bridge_code(exc) != "READ_ONLY":
            raise
        result = _vray_gpu_output_patch(compiled)
        if result is None:
            raise
        return result


# ── #49: V-Ray GPU output_* settings ──────────────────────────────────────────
# V-Ray GPU 7.40's own output_* parameters (VFB output, raw .vrimg file, ...) are
# read-only mirrors (P_READ_ONLY, transient, accessor) of the V-Ray settings object
# in its sub-anim 1, so renderers.current.output_saveRawFile raises "Attempt to set
# read-only property" and the native patch refuses READ_ONLY. Writes to
# renderers.current[1].value work. When the native patch refuses READ_ONLY and
# every edit is an output_* property of the V-Ray GPU production renderer, whose
# schema and inspected state are unchanged, the writes go there in one undo step,
# with readback; a mismatch puts the earlier values back.
VRAY_GPU_CLASS_ID = [1770671000, 1323107829]
_FALLBACK_TYPES = {"bool": bool, "int": int, "float": float, "string": str, "filename": str}


def _bridge_code(exc: BaseException) -> str:
    try:
        payload = json.loads(getattr(exc, "bridge_message", "") or "")
    except (TypeError, ValueError):
        return ""
    return str(payload.get("code") or "") if isinstance(payload, dict) else ""


def _is_renderer_root(owner_ref: Any) -> bool:
    return isinstance(owner_ref, dict) and (owner_ref.get("root") == "renderer"
                                            or (owner_ref.get("root_binding") or {}).get("root") == "renderer")


def _mxs_literal(value: Any, kind: str) -> str | None:
    wanted = _FALLBACK_TYPES.get(kind)
    if wanted is None or isinstance(value, bool) != (wanted is bool):
        return None
    if wanted is bool:
        return "true" if value else "false"
    if wanted is int and isinstance(value, int):
        return str(value)
    if wanted is float and isinstance(value, (int, float)) and math.isfinite(value):
        return format(float(value), ".9g")
    if wanted is str and isinstance(value, str):
        return '"%s"' % safe_string(value)
    return None


def _vray_gpu_output_patch(edits: list[dict[str, Any]]) -> dict | None:
    """The sub-anim write for a READ_ONLY refusal of V-Ray GPU output_* edits; None when it doesn't apply."""
    planned = []
    for edit in edits:
        if not _is_renderer_root(edit.get("owner_ref")) or edit.get("sharing"):
            return None
        state = edit.get("expected_state") or {}
        if not isinstance(state, dict) or not state.get("fields"):
            return None
        schema = plugin_schema.inspect(owner_ref=edit["owner_ref"], fields=state["fields"])
        if list((schema.get("identity") or {}).get("class_id") or []) != VRAY_GPU_CLASS_ID:
            return None
        if schema.get("schema_token") != edit.get("expected_schema"):
            raise plugin_schema.PluginGuardError("STALE_SCHEMA", "Property schema changed; inspect it again.")
        if (schema.get("state_token") or {}).get("hash") != state.get("hash"):
            raise plugin_schema.PluginGuardError("STALE_STATE", "An inspected value changed; inspect it again.")
        prop = next((p for p in schema.get("properties") or [] if p.get("property_ref") == edit.get("property_ref")),
                    None)
        name = str((prop or {}).get("name") or "")
        if not prop or not prop.get("read_only") or not name.lower().startswith("output_") \
                or not name.replace("_", "").isalnum():
            return None
        literal = _mxs_literal(edit.get("value"), str(prop.get("type") or ""))
        if literal is None:
            return None
        planned.append((edit, name, literal))
    names = "#(" + ", ".join("#" + name for _, name, _ in planned) + ")"
    values = "#(" + ", ".join(literal for _, _, literal in planned) + ")"
    raw = client.send_command(_VRAY_GPU_OUTPUT_SCRIPT.replace("__NAMES__", names).replace("__VALUES__", values),
                              cmd_type="maxscript").get("result", "")
    lines = str(raw).strip().strip('"').splitlines()
    head = lines[0] if lines else ""
    if head.startswith("ERR|"):
        code, _, detail = head[4:].partition("|")
        messages = {"not_vray_gpu": "The production renderer is no longer V-Ray GPU; no changes were made.",
                    "user_busy": "3ds Max has an open undo operation; no changes were made.",
                    "no_settings": "V-Ray GPU's settings sub-anim (renderers.current[1]) is missing; no changes "
                                   "were made.",
                    "mismatch": "V-Ray GPU's settings didn't take the values; the earlier values were put back."}
        raise plugin_schema.PluginGuardError("VERIFY_MISMATCH" if code == "mismatch" else code.upper(),
                                             messages.get(code, detail or code))
    proofs = []
    for (edit, name, _), line in zip(planned, lines[1:]):
        _, before, actual, matched = (line.split("\t") + ["", "", "", ""])[:4]
        proofs.append({"owner_ref": edit["owner_ref"], "property_ref": edit["property_ref"], "name": name,
                       "before": before, "actual": actual, "matched": matched == "true"})
    return {"status": "applied", "resources": [], "properties": proofs, "verification": "maxscript_readback_matched",
            "transaction": "one_maxscript_undo", "route": "vray_gpu_settings_subanim",
            "warnings": ["V-Ray GPU marks its output_* parameters read-only: they mirror its V-Ray settings sub-anim, "
                         "so the values were written there (renderers.current[1].value) and read back."]}


_VRAY_GPU_OUTPUT_SCRIPT = r"""(
 local out = ""
 try (
  local r = renderers.current
  local cid = if r == undefined then #(0, 0) else r.classid
  if not (cid[1] == 1770671000 and cid[2] == 1323107829) then out = "ERR|not_vray_gpu"
  else if theHold.Holding() then out = "ERR|user_busy"
  else (
   local s = try (r[1].value) catch (undefined)
   local names = __NAMES__
   local values = __VALUES__
   local ok = s != undefined
   for n in names while ok do ok = isProperty s n
   if not ok then out = "ERR|no_settings"
   else (
    local before = for n in names collect (getProperty s n)
    undo "MCP V-Ray GPU output settings" on (for i = 1 to names.count do setProperty s names[i] values[i])
    fn same a b = (if classof b == Float then (abs (a - b)) < 1e-4 else a == b)
    local matched = for i = 1 to names.count collect (same (getProperty s names[i]) values[i])
    if (findItem matched false) > 0 then (
     for i = 1 to names.count do try (setProperty s names[i] before[i]) catch ()
     out = "ERR|mismatch"
    ) else (
     out = "OK"
     for i = 1 to names.count do
      out += "\n" + (names[i] as string) + "\t" + (before[i] as string) + "\t" + ((getProperty s names[i]) as string) + "\t" + (matched[i] as string)
    )
   )
  )
 ) catch (out = "ERR|error|" + (getCurrentException()))
 out
)"""
