"""Batch material replacement tool for 3ds Max.

Replaces materials on objects by reassigning an existing source material
to all objects that currently use a target material.  Useful for unifying
split material assignments (e.g. after partial glTF conversion) or
swapping materials across many objects at once.
"""

import json as _json
from ..server import mcp, client
from ..coerce import DictList
from ..helpers.maxscript import safe_string


def _no_match_warning(targets: list[str]) -> str:
    names = ", ".join(f"'{t}'" for t in targets)
    return (
        f"No object has top-level material named {names}. source = material to apply (must already be "
        "on an object), target = material to replace; if reversed, swap them. To apply an unassigned "
        "material use assign_material. Multi/Sub-Object sub-materials are not matched."
    )


def _add_warning(result: dict, message: str) -> None:
    warnings = result.get("warnings")
    warnings = list(warnings) if isinstance(warnings, list) else ([warnings] if warnings else [])
    warnings.append(message)
    result["warnings"] = warnings


def _set_no_match(result: dict) -> bool:
    """Set status "no_match" on a zero-match result; True if it was marked."""
    if "error" in result or str(result.get("status", "")).lower() in {"error", "failed", "skipped", "no_match"}:
        return False
    key = "affected_count" if "affected_count" in result else "replaced_count"
    if result.get(key) != 0:
        return False
    result["status"] = "no_match"
    return True


def _mark_no_match(result: dict, target: str) -> dict:
    """Flag a zero-match replace result as status "no_match" with a warning."""
    if _set_no_match(result):
        _add_warning(result, _no_match_warning([target]))
    return result


def _postprocess(raw: str, transform) -> str:
    try:
        result = _json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(result, dict):
        return raw
    return _json.dumps(transform(result))


@mcp.tool()
def replace_material(
    source_material: str,
    target_material: str,
    preview: bool = False,
) -> str:
    """Assign source_material to every object whose top-level material is named target_material.

    source = material to apply/keep (must already be on at least one object, else use
    assign_material); target = material to remove. Multi/Sub sub-materials are not matched.
    Zero matches -> status "no_match" + warning.
    Example: source_material="Brick_A", target_material="Brick_B" -> objects using Brick_B get Brick_A.
    """
    def transform(result: dict) -> dict:
        if preview and result.get("source_exists") is False:
            _add_warning(result, f"Source material '{source_material}' is not assigned to any object; "
                                 "a real run would fail (use assign_material).")
        return _mark_no_match(result, target_material)

    return _postprocess(_replace_one(source_material, target_material, preview), transform)


def _replace_one(source_material: str, target_material: str, preview: bool) -> str:
    """Run one replacement (native or MAXScript) and return the raw result string."""
    if client.native_available:
        payload = _json.dumps({"source_material": source_material, "target_material": target_material, "preview": preview})
        response = client.send_command(payload, cmd_type="native:replace_material")
        return response.get("result", "{}")

    safe_src = safe_string(source_material)
    safe_tgt = safe_string(target_material)

    if preview:
        maxscript = f"""(
            local tgtObjs = for obj in objects
                where obj.material != undefined
                  and obj.material.name == "{safe_tgt}"
                collect obj.name
            local srcExists = false
            for obj in objects where obj.material != undefined do (
                if obj.material.name == "{safe_src}" do (srcExists = true; exit)
            )
            local names = "["
            for i = 1 to tgtObjs.count do (
                if i > 1 do names += ","
                names += "\\"" + tgtObjs[i] + "\\""
            )
            names += "]"
            "{{" + \
                "\\"source_material\\":\\"" + "{safe_src}" + "\\"," + \
                "\\"target_material\\":\\"" + "{safe_tgt}" + "\\"," + \
                "\\"source_exists\\":" + (if srcExists then "true" else "false") + "," + \
                "\\"affected_count\\":" + (tgtObjs.count as string) + "," + \
                "\\"affected_objects\\":" + names + "," + \
                "\\"preview\\":true" + \
            "}}"
        )"""
    else:
        maxscript = f"""(
            -- Find the source material instance from scene objects
            local srcMat = undefined
            for obj in objects where obj.material != undefined do (
                if obj.material.name == "{safe_src}" do (
                    srcMat = obj.material
                    exit
                )
            )
            if srcMat == undefined then (
                "{{" + \
                    "\\"error\\":\\"source material '{safe_src}' not found on any object\\"," + \
                    "\\"status\\":\\"failed\\"" + \
                "}}"
            ) else (
                local replaced = #()
                for obj in objects where obj.material != undefined do (
                    if obj.material.name == "{safe_tgt}" do (
                        obj.material = srcMat
                        append replaced obj.name
                    )
                )
                local names = "["
                for i = 1 to replaced.count do (
                    if i > 1 do names += ","
                    names += "\\"" + replaced[i] + "\\""
                )
                names += "]"
                "{{" + \
                    "\\"source_material\\":\\"" + "{safe_src}" + "\\"," + \
                    "\\"target_material\\":\\"" + "{safe_tgt}" + "\\"," + \
                    "\\"replaced_count\\":" + (replaced.count as string) + "," + \
                    "\\"replaced_objects\\":" + names + "," + \
                    "\\"status\\":\\"success\\"" + \
                "}}"
            )
        )"""

    response = client.send_command(maxscript)
    return response.get("result", "{}")


@mcp.tool()
def batch_replace_materials(
    replacements: DictList,
    preview: bool = False,
    dry_run: bool = False,
) -> str:
    """Run several replace_material swaps: replacements=[{"source": apply, "target": remove}, ...].

    Same rules as replace_material (each source must already be on an object; top-level
    materials only); zero-match entries get status "no_match" plus one combined warning.
    """
    preview = preview or dry_run
    if client.native_available:
        payload = _json.dumps({"replacements": list(replacements), "preview": preview, "dry_run": dry_run})
        response = client.send_command(payload, cmd_type="native:batch_replace_materials")
        return _postprocess(response.get("result", "{}"), _mark_batch_no_match)

    results = []
    for entry in replacements:
        src = entry.get("source", "")
        tgt = entry.get("target", "")
        if not src or not tgt:
            results.append({"source": src, "target": tgt, "status": "skipped", "error": "missing source or target"})
            continue
        raw = _replace_one(src, tgt, preview)
        try:
            r = _json.loads(raw)
        except _json.JSONDecodeError:
            results.append({"source": src, "target": tgt, "status": "error", "raw": raw})
            continue
        if isinstance(r, dict) and r.get("source_exists") is False:
            # Match native batch: an unassigned source is an error entry, not counted.
            r = {"source_material": src, "target_material": tgt, "status": "error",
                 "error": "source material not found"}
        results.append(r)

    total_replaced = sum(r.get("replaced_count", r.get("affected_count", 0)) or 0 for r in results)
    return _json.dumps(_mark_batch_no_match({
        "results": results,
        "total_replaced": total_replaced,
        "preview": preview,
    }))


def _mark_batch_no_match(result: dict) -> dict:
    """Mark zero-match batch entries "no_match" and add one top-level warning naming them."""
    entries = result.get("results")
    if not isinstance(entries, list):
        return result
    missed = [
        str(entry.get("target_material", entry.get("target", "")))
        for entry in entries
        if isinstance(entry, dict) and _set_no_match(entry)
    ]
    if missed:
        _add_warning(result, _no_match_warning(missed))
    return result
