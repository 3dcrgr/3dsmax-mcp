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
# Search order for the source; source_from restricts the search to one of these.
_SOURCE_FROM = ("node", "sub_material", "material_editor", "scene_materials", "material_library")

# Why a matching sub-material slot was left unchanged (skipped[].reason).
_SKIP_REASONS = {
    "parent_is_source": "the slot's parent is the source itself (a reference loop)",
    "source_contains_parent": "the source contains the slot's parent (a reference loop)",
    "reference_loop": "the source already references the slot's parent (a reference loop)",
    "set_failed": "the parent material did not accept the assignment",
}


def _source_from_error(value) -> str | None:
    if value in ("", None) or value in _SOURCE_FROM:
        return None
    return f"source_from must be one of {', '.join(_SOURCE_FROM)} (got {value!r})"


def _source_from_ignored(source_from: str) -> str:
    # An older native bridge does not know source_from and searches every place.
    return (f"source_from='{source_from}' was not echoed back, so the bridge may have ignored it and used "
            "the default search order; check source_found_in and update the native bridge.")


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


def _skipped(result: dict) -> list:
    skipped = result.get("skipped")
    return skipped if isinstance(skipped, list) else []


def _blocked_warning(target: str) -> str:
    return (f"Target '{target}' was found, but every match was left unchanged (see skipped[].reason), so "
            "nothing was replaced. source and target are not reversed; do not swap them.")


def _set_no_match(result: dict) -> bool:
    """Mark a result with no replaced objects or slots; True if it became "no_match".

    When matches were found but all of them were skipped (loop guard, set_failed),
    the status is "blocked" instead: the target exists, so the swap hint must not
    be given.
    """
    if "error" in result or str(result.get("status", "")).lower() in {
            "error", "failed", "skipped", "no_match", "blocked"}:
        return False
    objects, slots = _match_counts(result)
    if objects != 0 or slots:
        return False
    if _skipped(result):
        result["status"] = "blocked"
        return False
    result["status"] = "no_match"
    return True


def _detail_warnings(result: dict, source: str) -> list[str]:
    """Warnings for an ambiguous source name, a source equal to the target, and skipped slots."""
    messages = []
    target = result.get("target_material")
    if result.get("source_ambiguous"):
        if result.get("source_from"):
            where = f"source_from={result.get('source_from')}"
        else:
            where = f"{result.get('source_found_in')} (order: {', '.join(_SOURCE_FROM)})"
        messages.append(
            f"{result.get('source_candidates')} different materials are named '{source}'; used the one found "
            f"in {where}. Pass source_from to pick another place."
        )
    elif target is not None and target == source and not result.get("source_from"):
        messages.append(
            f"source and target are both named '{source}'; the source is the first one found "
            f"({', '.join(_SOURCE_FROM)}) and objects already using it are left alone. To apply a re-imported "
            "copy, pass source_from (e.g. source_from=\"material_editor\")."
        )
    skipped = _skipped(result)
    if skipped:
        by_reason: dict[str, int] = {}
        for entry in skipped:
            reason = str(entry.get("reason", "unknown")) if isinstance(entry, dict) else "unknown"
            by_reason[reason] = by_reason.get(reason, 0) + 1
        parts = [f"{count} {reason}: {_SKIP_REASONS.get(reason, 'see skipped[]')}"
                 for reason, count in by_reason.items()]
        messages.append(f"{len(skipped)} sub-material slot(s) skipped, left unchanged ({'; '.join(parts)}).")
    if str(result.get("status", "")).lower() == "blocked" and target is not None:
        messages.append(_blocked_warning(str(target)))
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
    source_from: str = "",
) -> str:
    """Put source_material wherever target_material is used: on objects, and with
    include_sub_materials also in Multi/Sub (or other) sub-material slots.

    source = material to apply; target = material to remove/replace. The source is the
    first material with that exact name found in this order: node, sub_material,
    material_editor, scene_materials, material_library (source_found_in says which).
    source_from = one of those places limits the search to it, e.g. "material_editor"
    to apply a re-imported copy that shares its name with the material on objects.
    preview lists affected_objects/affected_slots (slot_index is 1-based).
    Zero matches -> status "no_match" + warning; matches that were all skipped
    (skipped[].reason) -> status "blocked".
    Example: source_material="Brick_A", target_material="Brick_B" -> every Brick_B use becomes Brick_A.
    """
    error = _source_from_error(source_from)
    if error:
        return _json.dumps({"error": error, "status": "failed"})

    def transform(result: dict) -> dict:
        if source_from and "error" not in result and result.get("source_from") != source_from:
            _add_warning(result, _source_from_ignored(source_from))
        if preview and result.get("source_exists") is False:
            _add_warning(result, f"Source material '{source_material}' was not found on "
                                 f"{source_from or _SOURCE_PLACES}; a real run would fail.")
        marked = _set_no_match(result)
        for message in _detail_warnings(result, source_material):
            _add_warning(result, message)
        if marked:
            _add_warning(result, _no_match_warning([target_material], include_sub_materials))
        return result

    raw = _replace_one(source_material, target_material, preview, include_sub_materials, source_from)
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
    -- srcFrom (non-empty) limits the search to one of those places.
    local roots = #()
    for o in objects do (
        local m = try (o.material) catch undefined
        if (mcpIsMtl m) do appendIfUnique roots m
    )
    local cands = #()
    local wheres = #()
    if srcFrom == "" or srcFrom == "node" do (
        for m in roots do mcpAddCand m srcName "node" cands wheres
    )
    if srcFrom == "" or srcFrom == "sub_material" do (
        local seenSrc = #()
        for m in roots do mcpWalkSrc m srcName cands wheres seenSrc
    )
    if srcFrom == "" or srcFrom == "material_editor" do (
        try (for i = 1 to meditMaterials.count do mcpAddCand meditMaterials[i] srcName "material_editor" cands wheres) catch ()
    )
    if srcFrom == "" or srcFrom == "scene_materials" do (
        try (for m in sceneMaterials do mcpAddCand m srcName "scene_materials" cands wheres) catch ()
    )
    if srcFrom == "" or srcFrom == "material_library" do (
        try (for m in currentMaterialLibrary do mcpAddCand m srcName "material_library" cands wheres) catch ()
    )
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
        local msg = "Source material '" + srcName + "' not found "
        if srcFrom == "" then (
            msg += "(searched node materials, their sub-materials, "
            msg += "Material Editor slots, scene materials and the current material library)"
        ) else msg += "(searched only source_from=" + srcFrom + ")"
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
        if srcFrom != "" do s += ",\"source_from\":" + (mcpStr srcFrom)
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
                       include_sub_materials: bool, source_from: str = "") -> str:
    header = (
        f'    local srcName = "{safe_string(source_material)}"\n'
        f'    local tgtName = "{safe_string(target_material)}"\n'
        f'    local isPreview = {"true" if preview else "false"}\n'
        f'    local includeSubs = {"true" if include_sub_materials else "false"}\n'
        f'    local srcFrom = "{safe_string(source_from or "")}"\n'
    )
    return "(\n" + header + _REPLACE_MAXSCRIPT_BODY + ")"


def _replace_one(source_material: str, target_material: str, preview: bool,
                 include_sub_materials: bool = True, source_from: str = "") -> str:
    """Run one replacement (native or MAXScript) and return the raw result string."""
    if client.native_available:
        request = {"source_material": source_material, "target_material": target_material,
                   "preview": preview, "include_sub_materials": include_sub_materials}
        if source_from:
            request["source_from"] = source_from
        response = client.send_command(_json.dumps(request), cmd_type="native:replace_material")
        return response.get("result", "{}")

    script = _replace_maxscript(source_material, target_material, preview, include_sub_materials, source_from)
    response = client.send_command(script)
    return response.get("result", "{}")


@mcp.tool()
def batch_replace_materials(
    replacements: DictList,
    preview: bool = False,
    dry_run: bool = False,
    include_sub_materials: bool = True,
    source_from: str = "",
) -> str:
    """Run several replace_material swaps in order: replacements=[{"source": apply, "target": remove}, ...].

    Same rules as replace_material (source lookup, sub-material slots, source_from as a
    default or per entry). Entries apply in order and each sees the scene left by the
    previous ones, so A->B then B->A does not swap (go through a temporary material).
    Preview plans every entry against the current scene: an entry that reuses a name an
    earlier entry applies or removes gets depends_on_entries plus a warning, because a
    real run can differ from its preview. Zero-match entries get status "no_match" plus
    one combined warning; entries whose matches were all skipped get "blocked".
    """
    preview = preview or dry_run
    entries = list(replacements)
    effective_from = []
    for index, entry in enumerate(entries, start=1):
        has_own = isinstance(entry, dict) and entry.get("source_from") is not None
        value = entry["source_from"] if has_own else source_from
        error = _source_from_error(value)
        if error:
            label = f"replacements[{index}].source_from" if has_own else "source_from"
            return _json.dumps({"error": f"{label}: {error}; nothing was changed", "status": "failed"})
        effective_from.append(value or "")

    def finish(result: dict) -> dict:
        _mark_batch_dependencies(result, entries, preview)
        _check_batch_source_from(result, effective_from)
        return _mark_batch_no_match(result, include_sub_materials)

    if client.native_available:
        request = {"replacements": entries, "preview": preview, "dry_run": dry_run,
                   "include_sub_materials": include_sub_materials}
        if source_from:
            request["source_from"] = source_from
        response = client.send_command(_json.dumps(request), cmd_type="native:batch_replace_materials")
        return _postprocess(response.get("result", "{}"), finish)

    results = []
    for entry, entry_from in zip(entries, effective_from):
        src, tgt = _entry_names(entry)
        if not src or not tgt:
            results.append({"source": src, "target": tgt, "status": "skipped", "error": "missing source or target"})
            continue
        raw = _replace_one(src, tgt, preview, include_sub_materials, entry_from)
        try:
            r = _json.loads(raw)
        except _json.JSONDecodeError:
            results.append({"source": src, "target": tgt, "status": "error", "raw": raw})
            continue
        if isinstance(r, dict) and r.get("source_exists") is False:
            # Match native batch: a missing source is an error entry, not counted.
            r = {"source_material": src, "target_material": tgt, "source_exists": False, "status": "error",
                 "error": "source material not found"}
            if entry_from:
                r["source_from"] = entry_from
        results.append(r)

    counts = [_match_counts(r) for r in results if isinstance(r, dict)]
    return _json.dumps(finish({
        "results": results,
        "total_replaced": sum(objects or 0 for objects, _ in counts),
        "total_replaced_slots": sum(slots or 0 for _, slots in counts),
        "include_sub_materials": include_sub_materials,
        "preview": preview,
    }))


def _entry_names(entry) -> tuple:
    if not isinstance(entry, dict):
        return "", ""
    return (entry.get("source", entry.get("source_material", "")),
            entry.get("target", entry.get("target_material", "")))


def _is_error_entry(entry) -> bool:
    return (not isinstance(entry, dict) or "error" in entry
            or str(entry.get("status", "")).lower() in {"error", "failed", "skipped"})


def _batch_dependencies(entries: list, results: list) -> list[list[int]]:
    """For each entry, the 1-based earlier entries whose result changes what it sees.

    Entry i moves target_i's users onto source_i. A later entry j is affected when its
    source is an earlier target (the source may be gone from objects), its target is an
    earlier source (it gains the users moved there) or its target is an earlier target
    (those users were already moved). Entries that errored change nothing, so they are
    not dependencies; an errored entry is a dependent only when its source was not
    found and an earlier entry removed a material of that name.
    """
    names = [_entry_names(entry) for entry in entries]
    active = [index < len(results) and not _is_error_entry(results[index]) for index in range(len(names))]
    deps = []
    for j, (src_j, tgt_j) in enumerate(names):
        found = []
        if src_j and tgt_j:
            source_missing = (j < len(results) and isinstance(results[j], dict)
                              and results[j].get("source_exists") is False)
            for i, (src_i, tgt_i) in enumerate(names[:j]):
                if not active[i]:
                    continue
                if active[j] and (src_j == tgt_i or tgt_j == src_i or tgt_j == tgt_i):
                    found.append(i + 1)
                elif not active[j] and source_missing and src_j == tgt_i:
                    found.append(i + 1)
        deps.append(found)
    return deps


def _mark_batch_dependencies(result: dict, entries: list, preview: bool) -> None:
    results = result.get("results")
    if not isinstance(results, list):
        return
    dependent = []
    for index, (deps, entry) in enumerate(zip(_batch_dependencies(entries, results), results), start=1):
        if deps:
            entry["depends_on_entries"] = deps
            dependent.append(index)
    if not dependent:
        return
    listed = ", ".join(str(i) for i in dependent)
    if preview:
        message = (f"Entries {listed} reuse a material name that an earlier entry applies or removes "
                   "(see depends_on_entries). This preview plans every entry against the current scene, so "
                   "a real run can differ for them: entries apply in order, so A->B then B->A does not swap "
                   "and a source an earlier entry removed may not be found.")
    else:
        message = (f"Entries {listed} ran on the scene left by earlier entries (see depends_on_entries); "
                   "entries apply in order, so A->B then B->A does not swap.")
    _add_warning(result, message)


def _check_batch_source_from(result: dict, effective_from: list) -> None:
    results = result.get("results")
    if not isinstance(results, list):
        return
    for entry, wanted in zip(results, effective_from):
        if (wanted and isinstance(entry, dict) and "error" not in entry
                and entry.get("source_from") != wanted):
            _add_warning(result, _source_from_ignored(wanted))
            return


def _mark_batch_no_match(result: dict, include_sub_materials: bool = True) -> dict:
    """Mark zero-match batch entries "no_match" (or "blocked"); one combined warning plus entry details."""
    entries = result.get("results")
    if not isinstance(entries, list):
        return result
    missed = [
        str(entry.get("target_material", entry.get("target", "")))
        for entry in entries
        if isinstance(entry, dict) and _set_no_match(entry)
    ]
    details = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source_material", entry.get("source", "")))
        target = str(entry.get("target_material", entry.get("target", "")))
        details.extend(f"'{source}' -> '{target}': {message}" for message in _detail_warnings(entry, source))
    if missed:
        _add_warning(result, _no_match_warning(missed, include_sub_materials))
    for message in details:
        _add_warning(result, message)
    return result
