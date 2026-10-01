"""Batch material replacement tool for 3ds Max.

Replaces materials on objects by reassigning an existing source material
wherever a target material is used: as an object's material and, optionally,
inside Multi/Sub-Object (or any other) sub-material slots.  Useful for
unifying split material assignments (e.g. after partial glTF conversion or a
Revit import) or swapping materials across many objects at once.
"""

import json as _json
from ..server import mcp, client
from ..coerce import DictList
from ..helpers.maxscript import safe_string

_SOURCE_PLACES = "an object, a sub-material, a Material Editor slot or a material library"


def _no_match_warning(targets: list[str], include_sub_materials: bool = True) -> str:
    names = ", ".join(f"'{t}'" for t in targets)
    scope = "top-level or sub-material" if include_sub_materials else "top-level"
    message = (
        f"No object has a {scope} material named {names}. source = material to apply (on {_SOURCE_PLACES}), "
        "target = material to replace; if reversed, swap them. To put a material on specific objects use "
        "assign_material."
    )
    if not include_sub_materials:
        message += " Sub-material slots were not searched (include_sub_materials=False)."
    return message


def _add_warning(result: dict, message: str) -> None:
    warnings = result.get("warnings")
    warnings = list(warnings) if isinstance(warnings, list) else ([warnings] if warnings else [])
    warnings.append(message)
    result["warnings"] = warnings


def _match_counts(result: dict) -> tuple:
    """(objects, sub-material slots) matched by a replace result; preview keys win."""
    prefix = "affected" if "affected_count" in result else "replaced"
    return result.get(f"{prefix}_count"), result.get(f"{prefix}_slot_count") or 0


def _set_no_match(result: dict) -> bool:
    """Set status "no_match" when neither objects nor slots matched; True if it was marked."""
    if "error" in result or str(result.get("status", "")).lower() in {"error", "failed", "skipped", "no_match"}:
        return False
    objects, slots = _match_counts(result)
    if objects != 0 or slots:
        return False
    result["status"] = "no_match"
    return True


def _detail_warnings(result: dict, source: str) -> list[str]:
    """Warnings for an ambiguous source name and loop-guarded (skipped) slots."""
    messages = []
    if result.get("source_ambiguous"):
        messages.append(
            f"{result.get('source_candidates')} different materials are named '{source}'; used the one found "
            f"in {result.get('source_found_in')} (order: node, sub_material, material_editor, scene_materials, "
            "material_library)."
        )
    skipped = result.get("skipped")
    if isinstance(skipped, list) and skipped:
        messages.append(f"{len(skipped)} sub-material slot(s) left unchanged; see skipped[].reason "
                        "(the source contains that parent, so assigning it would create a reference loop).")
    return messages


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
    include_sub_materials: bool = True,
) -> str:
    """Put source_material wherever target_material is used: on objects, and with
    include_sub_materials also in Multi/Sub (or other) sub-material slots.

    source = material to apply (looked up on objects, then sub-materials, Material Editor
    slots, scene materials, the material library); target = material to remove/replace.
    preview lists affected_objects/affected_slots (slot_index is 1-based).
    Zero matches -> status "no_match" + warning.
    Example: source_material="Brick_A", target_material="Brick_B" -> every Brick_B use becomes Brick_A.
    """
    def transform(result: dict) -> dict:
        if preview and result.get("source_exists") is False:
            _add_warning(result, f"Source material '{source_material}' was not found on {_SOURCE_PLACES}; "
                                 "a real run would fail.")
        for message in _detail_warnings(result, source_material):
            _add_warning(result, message)
        if _set_no_match(result):
            _add_warning(result, _no_match_warning([target_material], include_sub_materials))
        return result

    raw = _replace_one(source_material, target_material, preview, include_sub_materials)
    return _postprocess(raw, transform)


# MAXScript fallback (no native bridge). Mirrors the native handler: source
# lookup order, sub-material slot matching with a loop guard, same JSON keys.
_REPLACE_MAXSCRIPT_BODY = r"""
    fn mcpEsc v = (
        local s = v as string
        s = substituteString s "\\" "\\\\"
        s = substituteString s "\"" "\\\""
        s = substituteString s "\n" "\\n"
        s = substituteString s "\r" "\\r"
        s = substituteString s "\t" "\\t"
        s
    )
    fn mcpStr v = ("\"" + (mcpEsc v) + "\"")
    fn mcpArr items = (
        local s = "["
        for i = 1 to items.count do (
            if i > 1 do s += ","
            s += items[i]
        )
        s + "]"
    )
    fn mcpIsMtl m = (m != undefined and (try (superClassOf m == material) catch false))
    fn mcpNamed m wanted = ((mcpIsMtl m) and (try (m.name == wanted) catch false))
    fn mcpSubs m = (try (getNumSubMtls m) catch 0)
    fn mcpSub m i = (try (getSubMtl m i) catch undefined)
    fn mcpAddCand m wanted wh cands wheres = (
        if (mcpNamed m wanted) and (findItem cands m) == 0 do (
            append cands m
            append wheres wh
        )
    )
    fn mcpWalkSrc m wanted cands wheres seen = (
        if (mcpIsMtl m) and (findItem seen m) == 0 do (
            append seen m
            for i = 1 to (mcpSubs m) do (
                local sm = mcpSub m i
                if sm != undefined do (
                    mcpAddCand sm wanted "sub_material" cands wheres
                    mcpWalkSrc sm wanted cands wheres seen
                )
            )
        )
    )
    fn mcpWalkTgt m wanted src seen parents idxs = (
        if (mcpIsMtl m) and (findItem seen m) == 0 do (
            append seen m
            for i = 1 to (mcpSubs m) do (
                local sm = mcpSub m i
                if sm != undefined do (
                    if (mcpNamed sm wanted) then (
                        if sm != src do (
                            append parents m
                            append idxs i
                        )
                    ) else mcpWalkTgt sm wanted src seen parents idxs
                )
            )
        )
    )
    fn mcpContains root x seen = (
        if root == x then true
        else if root == undefined or (findItem seen root) != 0 then false
        else (
            append seen root
            local found = false
            for i = 1 to (mcpSubs root) while not found do (
                if (mcpContains (mcpSub root i) x seen) do found = true
            )
            found
        )
    )
    fn mcpLoopReason src parent = (
        local seen = #()
        if src == undefined then undefined
        else if parent == src then "parent_is_source"
        else if (mcpContains src parent seen) then "source_contains_parent"
        else undefined
    )
    fn mcpSlotJson pName pClass i label reason = (
        local s = "{\"parent_material\":" + (mcpStr pName)
        s += ",\"parent_class\":" + (mcpStr pClass)
        s += ",\"slot_index\":" + (i as string)
        s += ",\"slot_name\":" + (mcpStr label)
        if reason != undefined do s += ",\"reason\":" + (mcpStr reason)
        s + "}"
    )

    -- Source: node materials, their sub-materials, medit slots, scene materials, current library.
    local roots = #()
    for o in objects do (
        local m = try (o.material) catch undefined
        if (mcpIsMtl m) do appendIfUnique roots m
    )
    local cands = #()
    local wheres = #()
    for m in roots do mcpAddCand m srcName "node" cands wheres
    local seenSrc = #()
    for m in roots do mcpWalkSrc m srcName cands wheres seenSrc
    try (for i = 1 to meditMaterials.count do mcpAddCand meditMaterials[i] srcName "material_editor" cands wheres) catch ()
    try (for m in sceneMaterials do mcpAddCand m srcName "scene_materials" cands wheres) catch ()
    try (for m in currentMaterialLibrary do mcpAddCand m srcName "material_library" cands wheres) catch ()
    local srcMat = if cands.count > 0 then cands[1] else undefined

    -- Target: objects whose material is named tgtName, then sub-material slots.
    local hitNodes = #()
    for o in objects do (
        local m = try (o.material) catch undefined
        if (mcpNamed m tgtName) and m != srcMat do append hitNodes o
    )
    local slotParents = #()
    local slotIdx = #()
    if includeSubs do (
        local seenTgt = #()
        for m in roots where not (mcpNamed m tgtName) do mcpWalkTgt m tgtName srcMat seenTgt slotParents slotIdx
    )

    if (not isPreview) and srcMat == undefined then (
        local msg = "Source material '" + srcName + "' not found (searched node materials, their sub-materials, "
        msg += "Material Editor slots, scene materials and the current material library)"
        "{\"error\":" + (mcpStr msg) + ",\"status\":\"failed\"}"
    ) else (
        local slotJson = #()
        local skipJson = #()
        local nodeNames = #()
        local slotInfo = for k = 1 to slotParents.count collect (
            local p = slotParents[k]
            #((try (p.name) catch ""), ((classOf p) as string), (try (getSubMtlSlotName p slotIdx[k]) catch ""))
        )
        if isPreview then (
            for k = 1 to slotParents.count do (
                local reason = mcpLoopReason srcMat slotParents[k]
                local js = mcpSlotJson slotInfo[k][1] slotInfo[k][2] slotIdx[k] slotInfo[k][3] reason
                if reason == undefined then append slotJson js else append skipJson js
            )
            for o in hitNodes do append nodeNames (mcpStr o.name)
        ) else (
            undo "MCP replace_material" on (
                for k = 1 to slotParents.count do (
                    local p = slotParents[k]
                    local i = slotIdx[k]
                    local reason = mcpLoopReason srcMat p
                    if reason == undefined do (
                        local ok = try (setSubMtl p i srcMat; (getSubMtl p i) == srcMat) catch false
                        if not ok do reason = "set_failed"
                    )
                    local js = mcpSlotJson slotInfo[k][1] slotInfo[k][2] i slotInfo[k][3] reason
                    if reason == undefined then append slotJson js else append skipJson js
                )
                for o in hitNodes do (
                    o.material = srcMat
                    append nodeNames (mcpStr o.name)
                )
            )
        )
        local verb = if isPreview then "affected" else "replaced"
        local s = "{\"source_material\":" + (mcpStr srcName) + ",\"target_material\":" + (mcpStr tgtName)
        s += ",\"source_exists\":" + (if srcMat != undefined then "true" else "false")
        if srcMat != undefined do s += ",\"source_found_in\":" + (mcpStr wheres[1])
        if cands.count > 1 do s += ",\"source_ambiguous\":true,\"source_candidates\":" + (cands.count as string)
        s += ",\"include_sub_materials\":" + (if includeSubs then "true" else "false")
        s += ",\"" + verb + "_count\":" + (hitNodes.count as string)
        s += ",\"" + verb + "_objects\":" + (mcpArr nodeNames)
        s += ",\"" + verb + "_slot_count\":" + (slotJson.count as string)
        s += ",\"" + verb + "_slots\":" + (mcpArr slotJson)
        s += ",\"skipped\":" + (mcpArr skipJson)
        s += (if isPreview then ",\"preview\":true}" else ",\"status\":\"success\"}")
        s
    )
"""


def _replace_maxscript(source_material: str, target_material: str, preview: bool,
                       include_sub_materials: bool) -> str:
    header = (
        f'    local srcName = "{safe_string(source_material)}"\n'
        f'    local tgtName = "{safe_string(target_material)}"\n'
        f'    local isPreview = {"true" if preview else "false"}\n'
        f'    local includeSubs = {"true" if include_sub_materials else "false"}\n'
    )
    return "(\n" + header + _REPLACE_MAXSCRIPT_BODY + ")"


def _replace_one(source_material: str, target_material: str, preview: bool,
                 include_sub_materials: bool = True) -> str:
    """Run one replacement (native or MAXScript) and return the raw result string."""
    if client.native_available:
        payload = _json.dumps({"source_material": source_material, "target_material": target_material,
                               "preview": preview, "include_sub_materials": include_sub_materials})
        response = client.send_command(payload, cmd_type="native:replace_material")
        return response.get("result", "{}")

    script = _replace_maxscript(source_material, target_material, preview, include_sub_materials)
    response = client.send_command(script)
    return response.get("result", "{}")


@mcp.tool()
def batch_replace_materials(
    replacements: DictList,
    preview: bool = False,
    dry_run: bool = False,
    include_sub_materials: bool = True,
) -> str:
    """Run several replace_material swaps in order: replacements=[{"source": apply, "target": remove}, ...].

    Same rules as replace_material (source lookup, sub-material slots); each entry sees the
    scene left by the previous ones, so A->B then B->A does not swap (go through a temporary
    material). Zero-match entries get status "no_match" plus one combined warning.
    """
    preview = preview or dry_run
    if client.native_available:
        payload = _json.dumps({"replacements": list(replacements), "preview": preview, "dry_run": dry_run,
                               "include_sub_materials": include_sub_materials})
        response = client.send_command(payload, cmd_type="native:batch_replace_materials")
        return _postprocess(response.get("result", "{}"),
                            lambda result: _mark_batch_no_match(result, include_sub_materials))

    results = []
    for entry in replacements:
        src = entry.get("source", entry.get("source_material", ""))
        tgt = entry.get("target", entry.get("target_material", ""))
        if not src or not tgt:
            results.append({"source": src, "target": tgt, "status": "skipped", "error": "missing source or target"})
            continue
        raw = _replace_one(src, tgt, preview, include_sub_materials)
        try:
            r = _json.loads(raw)
        except _json.JSONDecodeError:
            results.append({"source": src, "target": tgt, "status": "error", "raw": raw})
            continue
        if isinstance(r, dict) and r.get("source_exists") is False:
            # Match native batch: a missing source is an error entry, not counted.
            r = {"source_material": src, "target_material": tgt, "source_exists": False, "status": "error",
                 "error": "source material not found"}
        results.append(r)

    counts = [_match_counts(r) for r in results if isinstance(r, dict)]
    return _json.dumps(_mark_batch_no_match({
        "results": results,
        "total_replaced": sum(objects or 0 for objects, _ in counts),
        "total_replaced_slots": sum(slots or 0 for _, slots in counts),
        "include_sub_materials": include_sub_materials,
        "preview": preview,
    }, include_sub_materials))


def _mark_batch_no_match(result: dict, include_sub_materials: bool = True) -> dict:
    """Mark zero-match batch entries "no_match"; add one combined warning plus entry details."""
    entries = result.get("results")
    if not isinstance(entries, list):
        return result
    details = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source_material", entry.get("source", "")))
        target = str(entry.get("target_material", entry.get("target", "")))
        details.extend(f"'{source}' -> '{target}': {message}" for message in _detail_warnings(entry, source))
    missed = [
        str(entry.get("target_material", entry.get("target", "")))
        for entry in entries
        if isinstance(entry, dict) and _set_no_match(entry)
    ]
    if missed:
        _add_warning(result, _no_match_warning(missed, include_sub_materials))
    for message in details:
        _add_warning(result, message)
    return result
