"""Material replacement tools for 3ds Max.

Reassigns an existing source material wherever a target material is used: on
objects and in the sub-material slots of their material trees.  Useful for
unifying split material assignments (e.g. after partial glTF conversion) or
swapping materials across many objects at once.
"""

import json as _json
from ..server import mcp, client
from ..coerce import DictList
from ..helpers.maxscript import safe_string


@mcp.tool()
def replace_material(
    source_material: str,
    target_material: str,
    preview: bool = False,
) -> str:
    """Put source_material wherever target_material is used, on objects and in sub-material slots.

    Names match exactly. Objects whose own material is the target are
    reassigned; a target in any sub-material slot of an object's material tree
    (Multi/Sub, Blend, Shell...) is replaced in that slot. The source may be on
    an object, in a sub-material slot or in a Material Editor slot. A slot where
    the source would create a reference loop (for example the source contains
    that slot's parent) is skipped with a reason.

    Reports replaced_objects, replaced_slots (parent material, 1-based
    slot_index, slot_name) and skipped. status: "replaced" when something
    changed, "no_match" when no material has the target name, "no_objects" when
    it exists only in the Material Editor, "blocked" when every match was
    skipped, "unchanged" when every match already uses the source. preview=True
    returns the same report with status "preview" and changes nothing.
    """
    if client.native_available:
        payload = _json.dumps({"source_material": source_material, "target_material": target_material, "preview": preview})
        response = client.send_command(payload, cmd_type="native:replace_material")
        return response.get("result", "{}")

    entry = _run_fallback([(source_material, target_material)], preview)[0]
    if entry.get("status") == "error":
        entry["status"] = "failed"
    return _json.dumps(entry)


@mcp.tool()
def batch_replace_materials(
    replacements: DictList,
    preview: bool = False,
    dry_run: bool = False,
) -> str:
    """Run several replace_material entries ({source, target}) as one undoable operation.

    Entries apply in order, and each one is evaluated against the scene as the
    previous entries left it, so chains such as A->B then B->C are
    deterministic. Each result carries replace_material's fields and status;
    preview/dry_run plans the same chain without changing the scene.
    """
    preview = preview or dry_run
    if client.native_available:
        payload = _json.dumps({"replacements": list(replacements), "preview": preview, "dry_run": dry_run})
        response = client.send_command(payload, cmd_type="native:batch_replace_materials")
        return response.get("result", "{}")

    results: list[dict | None] = []
    pairs: list[tuple[str, str]] = []
    for entry in replacements:
        entry = entry if isinstance(entry, dict) else {}
        src = entry.get("source") or entry.get("source_material") or ""
        tgt = entry.get("target") or entry.get("target_material") or ""
        if not src or not tgt:
            results.append({
                "source_material": src,
                "target_material": tgt,
                "status": "error",
                "error": "missing source or target",
            })
            continue
        results.append(None)
        pairs.append((src, tgt))

    ran = iter(_run_fallback(pairs, preview) if pairs else [])
    results = [entry if entry is not None else next(ran) for entry in results]

    total_replaced = sum(r.get("replaced_count", 0) for r in results)
    out = {
        "results": results,
        "total_replaced": total_replaced,
        "total_skipped": sum(r.get("skipped_count", 0) for r in results),
        "error_count": sum(1 for r in results if r.get("status") == "error"),
        "status": ("preview" if preview else "replaced") if total_replaced else "no_changes",
        "preview": preview,
        "dry_run": dry_run,
    }
    if preview and len(pairs) > 1:
        out["warnings"] = [
            "The MAXScript fallback previews each entry against the current scene; "
            "later entries do not see changes planned by earlier ones."
        ]
    return _json.dumps(out)


# ── MAXScript fallback ──────────────────────────────────────────────────────
# Used without the native bridge. The script reports raw matches and outcomes;
# status and warnings are derived here with the same rules as the native
# handler. Entries run in order inside one undo record when applied.

_FALLBACK_SCRIPT = r"""(
    fn mcpJs value = (
        local q = bit.intAsChar 34
        q + (MCP_Server.escapeJsonString (value as string)) + q
    )
    fn mcpField key value = ((mcpJs key) + ":" + value)
    fn mcpJoin items = (
        local out = "["
        for i = 1 to items.count do (
            if i > 1 do out += ","
            out += items[i]
        )
        out + "]"
    )
    fn mcpObject fields = (
        local out = "{"
        for i = 1 to fields.count do (
            if i > 1 do out += ","
            out += fields[i]
        )
        out + "}"
    )
    fn mcpIsMtl m = (m != undefined and superClassOf m == material)
    fn mcpSubCount m = (try (getNumSubMtls m) catch (0))
    fn mcpSubAt m i = (try (getSubMtl m i) catch (undefined))
    fn mcpSlotName m i = (
        try (getSubMtlSlotName m i localizedName:false) catch (try (getSubMtlSlotName m i) catch (""))
    )
    -- true when owner referencing src would close a reference loop.
    fn mcpLoop src owner = (try (refs.dependencyLoopTest src owner) catch (undefined))

    fn mcpFindTree m wanted found places visited = (
        if (mcpIsMtl m) and (findItem visited m) == 0 do (
            append visited m
            for i = 1 to (mcpSubCount m) do (
                local sub = mcpSubAt m i
                if (mcpIsMtl sub) do (
                    if sub.name == wanted and (findItem found sub) == 0 do (
                        append found sub
                        append places "sub_material"
                    )
                    mcpFindTree sub wanted found places visited
                )
            )
        )
    )
    -- Lookup order: object materials, their sub-material trees, then Material
    -- Editor slots and their trees.
    fn mcpFindNamed wanted found places = (
        for obj in objects where (mcpIsMtl obj.material) and obj.material.name == wanted and (findItem found obj.material) == 0 do (
            append found obj.material
            append places "object"
        )
        local visited = #()
        for obj in objects do mcpFindTree obj.material wanted found places visited
        for i = 1 to meditMaterials.count do (
            local m = meditMaterials[i]
            if (mcpIsMtl m) and m.name == wanted and (findItem found m) == 0 do (
                append found m
                append places "material_editor"
            )
            mcpFindTree m wanted found places visited
        )
    )
    -- Slots holding a material named wanted in one object's material tree. A
    -- matched material is not descended: its slot is the one replaced.
    fn mcpCollectSlots m wanted parents indices hits visited = (
        if (mcpIsMtl m) and (findItem visited m) == 0 do (
            append visited m
            for i = 1 to (mcpSubCount m) do (
                local sub = mcpSubAt m i
                if (mcpIsMtl sub) do (
                    if sub.name == wanted then (
                        local k = 0
                        for j = 1 to parents.count while k == 0 do (
                            if parents[j] == m and indices[j] == i do k = j
                        )
                        if k == 0 do (
                            append parents m
                            append indices i
                            k = parents.count
                        )
                        append hits k
                    ) else mcpCollectSlots sub wanted parents indices hits visited
                )
            )
        )
    )

    fn mcpReplaceOne srcName tgtName applyChanges = (
        local found = #()
        local places = #()
        mcpFindNamed srcName found places
        if found.count == 0 then (
            mcpObject #((mcpField "source_found" "false"))
        ) else (
            local src = found[1]
            local objMatches = #()
            local parents = #()
            local indices = #()
            local hitObjs = #()
            local objHits = #()
            for obj in objects where (mcpIsMtl obj.material) do (
                if obj.material.name == tgtName then append objMatches obj
                else (
                    local hits = #()
                    mcpCollectSlots obj.material tgtName parents indices hits #()
                    if hits.count > 0 do (
                        append hitObjs obj
                        append objHits hits
                    )
                )
            )

            local replacedObjs = #()
            local changedSlots = #()
            local slotJson = #()
            local skipped = #()
            for obj in objMatches do (
                local reason = ""
                if obj.material == src then reason = "already_source"
                else (
                    local looped = mcpLoop src obj
                    if looped == undefined then reason = "loop_test_failed"
                    else if looped then reason = "would_create_loop"
                    else if applyChanges do (
                        try (obj.material = src) catch ()
                        if obj.material != src do reason = "readback_mismatch"
                    )
                )
                if reason == "" then append replacedObjs obj
                else append skipped (mcpObject #(
                    (mcpField "kind" (mcpJs "object")),
                    (mcpField "object" (mcpJs obj.name)),
                    (mcpField "handle" (obj.handle as string)),
                    (mcpField "reason" (mcpJs reason))))
            )
            for k = 1 to parents.count do (
                local p = parents[k]
                local slot = indices[k]
                local prev = mcpSubAt p slot
                local users = 0
                for h in objHits do (if (findItem h k) > 0 do users += 1)
                local fields = #(
                    (mcpField "parent_material" (mcpJs p.name)),
                    (mcpField "parent_class" (mcpJs (classOf p))),
                    (mcpField "slot_index" (slot as string)),
                    (mcpField "slot_name" (mcpJs (mcpSlotName p slot))),
                    (mcpField "previous_material" (mcpJs (if prev != undefined then prev.name else ""))),
                    (mcpField "object_count" (users as string)))
                local reason = ""
                if prev == src then reason = "already_source"
                else if p == src then reason = "parent_is_source"
                else (
                    local looped = mcpLoop src p
                    if looped == undefined then reason = "loop_test_failed"
                    else if looped then reason = "would_create_loop"
                    else if applyChanges do (
                        try (setSubMtl p slot src) catch ()
                        local after = mcpSubAt p slot
                        if after != src do reason = (if after == prev then "set_rejected" else "readback_mismatch")
                    )
                )
                if reason == "" then (
                    append changedSlots k
                    append slotJson (mcpObject fields)
                ) else (
                    insertItem (mcpField "kind" (mcpJs "sub_material")) fields 1
                    append fields (mcpField "reason" (mcpJs reason))
                    append skipped (mcpObject fields)
                )
            )

            local affected = for o in replacedObjs collect o
            for j = 1 to hitObjs.count do (
                local touched = false
                for k in objHits[j] while not touched do (if (findItem changedSlots k) > 0 do touched = true)
                if touched do appendIfUnique affected hitObjs[j]
            )
            local matchCount = objMatches.count + parents.count
            local targetExists = matchCount > 0
            if not targetExists do (
                local tgtFound = #()
                mcpFindNamed tgtName tgtFound #()
                targetExists = tgtFound.count > 0
            )
            mcpObject #(
                (mcpField "source_found" "true"),
                (mcpField "source" (mcpObject #(
                    (mcpField "name" (mcpJs src.name)),
                    (mcpField "class" (mcpJs (classOf src))),
                    (mcpField "found_in" (mcpJs places[1]))))),
                (mcpField "source_candidates" (found.count as string)),
                (mcpField "match_count" (matchCount as string)),
                (mcpField "target_exists" (if targetExists then "true" else "false")),
                (mcpField "replaced_objects" (mcpJoin (for o in replacedObjs collect (mcpJs o.name)))),
                (mcpField "replaced_slots" (mcpJoin slotJson)),
                (mcpField "skipped" (mcpJoin skipped)),
                (mcpField "affected_objects" (mcpJoin (for o in affected collect (mcpJs o.name)))))
        )
    )

    local mcpEntries = __ENTRIES__
    local mcpResults = #()
    __RUN__
    mcpJoin mcpResults
)"""

_LOCATION_TEXT = {
    "object": "on an object",
    "sub_material": "in a sub-material slot",
    "material_editor": "in the Material Editor",
}


def _fallback_script(pairs: list[tuple[str, str]], preview: bool) -> str:
    entries = "#(" + ", ".join(
        f'#("{safe_string(src)}", "{safe_string(tgt)}")' for src, tgt in pairs
    ) + ")"
    run = "for e in mcpEntries do append mcpResults (mcpReplaceOne e[1] e[2] {apply})"
    if preview:
        run = run.format(apply="false")
    else:
        run = 'undo "MCP replace_material" on (' + run.format(apply="true") + ")"
    return _FALLBACK_SCRIPT.replace("__ENTRIES__", entries).replace("__RUN__", run)


def _run_fallback(pairs: list[tuple[str, str]], preview: bool) -> list[dict]:
    response = client.send_command(_fallback_script(pairs, preview))
    raw = response.get("result", "")
    try:
        parsed = _json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, list) or len(parsed) != len(pairs):
        return [
            {
                "source_material": src,
                "target_material": tgt,
                "status": "error",
                "error": "Unexpected MAXScript fallback result",
                "raw": raw,
            }
            for src, tgt in pairs
        ]
    return [_fallback_entry(item, src, tgt, preview) for item, (src, tgt) in zip(parsed, pairs)]


def _replace_status(match_count: int, changed: int, skipped: list, target_exists: bool, preview: bool) -> str:
    """Status rules shared with the native handler."""
    if match_count == 0:
        return "no_objects" if target_exists else "no_match"
    if changed:
        return "preview" if preview else "replaced"
    if all(skip.get("reason") == "already_source" for skip in skipped):
        return "unchanged"
    return "blocked"


def _fallback_entry(raw: dict, source: str, target: str, preview: bool) -> dict:
    entry: dict = {"source_material": source, "target_material": target}
    if not isinstance(raw, dict) or not raw.get("source_found"):
        entry["status"] = "error"
        entry["error"] = (
            f"Source material '{source}' not found on objects, in sub-material slots "
            "or in the Material Editor"
        )
        return entry

    objects = list(raw.get("replaced_objects") or [])
    slots = list(raw.get("replaced_slots") or [])
    skipped = list(raw.get("skipped") or [])
    affected = list(raw.get("affected_objects") or [])
    match_count = int(raw.get("match_count") or 0)
    changed = len(objects) + len(slots)
    status = _replace_status(match_count, changed, skipped, bool(raw.get("target_exists")), preview)

    source_info = dict(raw.get("source") or {})
    candidates = int(raw.get("source_candidates") or 1)
    source_info["candidates"] = candidates

    warnings = []
    if candidates > 1:
        where = _LOCATION_TEXT.get(source_info.get("found_in", ""), "first")
        warnings.append(f"{candidates} materials are named '{source}'; used the one found {where}.")
    verb = "would be" if preview else "was"
    refused = sum(1 for skip in skipped if skip.get("reason") != "already_source")
    if status == "no_match":
        warnings.append(
            f"No material named '{target}' exists on objects, in their sub-material slots "
            "or in the Material Editor."
        )
    elif status == "no_objects":
        warnings.append(
            f"'{target}' exists only in the Material Editor; no object or sub-material slot "
            f"uses it, so nothing {verb} replaced."
        )
    elif status == "blocked":
        warnings.append(
            f"Every match of '{target}' was skipped, so nothing {verb} replaced; "
            "see skipped for reasons."
        )
    elif status == "unchanged":
        warnings.append(f"Every match of '{target}' already uses '{source}', so nothing {verb} replaced.")
    elif refused:
        warnings.append(f"{refused} match(es) were skipped; see skipped for reasons.")

    entry.update({
        "source": source_info,
        "status": status,
        "preview": preview,
        "match_count": match_count,
        "replaced_count": changed,
        "reassigned_count": len(objects),
        "replaced_objects": objects,
        "replaced_slot_count": len(slots),
        "replaced_slots": slots,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "affected_count": len(affected),
        "affected_objects": affected,
    })
    if warnings:
        entry["warnings"] = warnings
    return entry
