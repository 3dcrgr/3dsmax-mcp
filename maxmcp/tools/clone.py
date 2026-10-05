import json as _json
import math
from typing import Optional

from ..coerce import FloatList, StrList
from ..server import mcp, client
from ..helpers.maxscript import safe_string
from ..helpers.spatial import build_clone_spatial_maxscript, enrich_spatial_payload


@mcp.tool()
def clone_objects(
    names: StrList,
    mode: str = "copy",
    offset: Optional[FloatList] = None,
    count: int = 1,
    clone_whole_group: bool = False,
) -> str:
    """Clone (copy/instance/reference) objects in the scene.

    count is the number of NEW copies per source (1-200), excluding the original.
    Each repetition uses offset * (1..count) from the original in world units.
    Example: count=14, offset=[60,0,0], mode="instance" builds a 15-beam row.
    Repeated arrays preserve hierarchies and run in one undo step.
    Returns actual cloned names and spatial snapshots; duplicate sources are ignored.
    Group members are cloned alone (with their children) and the copies are
    detached from the group (reported in group_members); Max would otherwise
    clone the whole closed group. clone_whole_group=True keeps Max's behaviour.
    Naming a group head clones the whole group as usual.
    """
    names = list(dict.fromkeys(names))
    if not names:
        raise ValueError("names is required")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 200:
        raise ValueError("count must be an integer from 1 to 200 (new copies per source)")
    mode = mode.strip().lower()
    if mode not in {"copy", "instance", "reference"}:
        raise ValueError("mode must be copy, instance, or reference")
    if offset is not None:
        if len(offset) != 3 or any(isinstance(v, bool) or not math.isfinite(float(v)) for v in offset):
            raise ValueError("offset must contain three finite numbers")
    if not clone_whole_group:
        members, shadows = _find_group_members(names)
        if members or shadows:
            return _clone_group_members(
                names, members, shadows, mode, list(offset or [0.0, 0.0, 0.0]), count
            )
    if count > 1:
        return _clone_array(names, mode, list(offset or [0.0, 0.0, 0.0]), count)
    if client.native_available:
        try:
            params: dict = {"names": names, "mode": mode}
            if offset:
                params["offset"] = offset
            response = client.send_command(_json.dumps(params), cmd_type="native:clone_objects")
            raw = response.get("result", "")
            if raw:
                payload = _json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(payload, dict):
                    for node in payload.get("nodes", []):
                        if isinstance(node, dict):
                            enrich_spatial_payload(node, str(node.get("class", "")))
                    return _json.dumps(payload)
            return raw
        except RuntimeError:
            pass

    if offset is None:
        offset = [0.0, 0.0, 0.0]

    mode_map = {"copy": "#copy", "instance": "#instance", "reference": "#reference"}
    ms_mode = mode_map.get(mode, "#copy")
    name_arr = "#(" + ", ".join(f'"{safe_string(n)}"' for n in names) + ")"

    maxscript = f"""(
        local nameList = {name_arr}
        local srcNodes = #()
        local notFound = #()
        for n in nameList do (
            local obj = getNodeByName n
            if obj != undefined then
                append srcNodes obj
            else
                append notFound n
        )
        if srcNodes.count == 0 then (
            "{{\\\"error\\\":\\\"No valid objects found to clone\\\"}}"
        ) else (
            local newNodes = #()
            maxOps.cloneNodes srcNodes cloneType:{ms_mode} newNodes:&newNodes
            local offsetVec = [{offset[0]},{offset[1]},{offset[2]}]
            for n in newNodes do move n offsetVec
            local cloneNames = for n in newNodes collect n.name
            local namesJson = "["
            for i = 1 to cloneNames.count do (
                if i > 1 do namesJson += ","
                namesJson += ("\\\"" + cloneNames[i] + "\\\"")
            )
            namesJson += "]"
            local notFoundJson = "["
            for i = 1 to notFound.count do (
                if i > 1 do notFoundJson += ","
                notFoundJson += ("\\\"" + notFound[i] + "\\\"")
            )
            notFoundJson += "]"
            "{{\\\"cloned\\\":" + namesJson + ",\\\"notFound\\\":" + notFoundJson + "}}"
        )
    )"""
    response = client.send_command(maxscript)
    raw = response.get("result", "")
    if not raw:
        return raw

    try:
        payload = _json.loads(raw)
    except (_json.JSONDecodeError, TypeError):
        return raw

    if payload.get("error"):
        return raw

    cloned = payload.get("cloned", [])
    if cloned:
        spatial_response = client.send_command(build_clone_spatial_maxscript(cloned))
        spatial_raw = spatial_response.get("result", "")
        if spatial_raw:
            try:
                spatial_data = _json.loads(spatial_raw)
                payload["nodes"] = spatial_data.get("nodes", [])
                payload["space"] = spatial_data.get("space", {})
                for node in payload.get("nodes", []):
                    if isinstance(node, dict):
                        enrich_spatial_payload(node, str(node.get("class", "")))
            except (_json.JSONDecodeError, TypeError):
                pass

    return _json.dumps(payload)


def _clone_array(names: list[str], mode: str, offset: list[float], count: int) -> str:
    """One bridge call for the mutation, compatible with already installed bridges."""
    name_arr = "#(" + ",".join(f'"{safe_string(n)}"' for n in names) + ")"
    vec = "[" + ",".join(format(float(v), ".9g") for v in offset) + "]"
    # Keep the clone API's dependency/hierarchy mapping intact. Moving individual
    # nodes afterward would move parented descendants twice.
    script = f'''(
        local src = #()
        local missing = #()
        for nm in {name_arr} do (
            local matches = getNodeByName nm exact:true all:true
            if matches.count != 1 then append missing nm else append src matches[1]
        )
        if missing.count > 0 then ("__ERROR__|Sources must resolve uniquely: " + (missing as string))
        else (
            local made = #()
            local failure = ""
            undo "Clone array" on (
                try (
                    for step = 1 to {count} do (
                        local batch = #()
                        local ok = maxOps.cloneNodes src offset:({vec} * step) expandHierarchy:true cloneType:#{mode} newNodes:&batch
                        join made batch
                        if not ok do throw "CloneNodes failed"
                    )
                ) catch (
                    failure = getCurrentException() as string
                    for n in made where isValidNode n do delete n
                )
            )
            if failure != "" then ("__ERROR__|" + failure)
            else (
                local handles = for n in made collect (formattedPrint ((getHandleByAnim n) as integer64) format:"d")
                local out = ""
                for h in handles do out += h + ","
                out
            )
        )
    )'''
    raw = str(client.send_command(script).get("result", ""))
    if raw.startswith("__ERROR__|"):
        raise RuntimeError(raw.split("|", 1)[1])
    handles = [int(h) for h in raw.split(",") if h.strip()]
    if not handles:
        raise RuntimeError("Clone array returned no node handles")
    # Resolve by stable handles, not generated names. The helper then supplies
    # the same detailed spatial contract used by single clones.
    spatial_script = build_clone_spatial_maxscript([], node_handles=handles)
    response = client.send_command(spatial_script)
    payload = _json.loads(response.get("result", "{}"))
    nodes = payload.get("nodes", [])
    if len(nodes) != len(handles):
        raise RuntimeError("Clone array spatial readback is incomplete; inspect scene before retrying")
    for node in nodes:
        enrich_spatial_payload(node, str(node.get("class", "")))
    payload.update(cloned=[n["name"] for n in nodes], notFound=[], count=count, offset=offset)
    return _json.dumps(payload)


# ── Group members (issue #16) ─────────────────────────────────────
# maxOps.cloneNodes / Interface::CloneNodes on a member of a closed group
# clones the WHOLE group, stacked invisibly on the original. Members are
# therefore cloned node by node (copy/instance/reference) and detached.

_MS_JSON_STRING = r'''
    fn mcpJsonString s = (
        local slash = bit.intAsChar 92
        local quote = bit.intAsChar 34
        s = substituteString s slash (slash + slash)
        s = substituteString s quote (slash + quote)
        s = substituteString s (bit.intAsChar 10) (slash + "n")
        s = substituteString s (bit.intAsChar 13) (slash + "r")
        s = substituteString s (bit.intAsChar 9) (slash + "t")
        quote + s + quote
    )
    fn mcpHandle n = (formattedPrint ((getHandleByAnim n) as integer64) format:"d")
'''

_MS_GROUP_FNS = r'''
    fn mcpInClosedGroup n = (
        local p = n.parent
        local hit = false
        while p != undefined and not hit do (
            if (isGroupHead p) and not (isOpenGroupHead p) do hit = true
            p = p.parent
        )
        hit
    )
    fn mcpIsMember n = (isGroupMember n) or (mcpInClosedGroup n)
'''

# Names are matched case-exactly (like native CloneObjects) for members, but
# case-insensitive member matches are reported too: the MAXScript fallback and
# array paths ignore case and would otherwise reach the group via cloneNodes.
_DETECT_TEMPLATE = r'''(
    __JSON_FN__
    __GROUP_FNS__
    fn mcpGroupHeadOf n = (
        local p = n.parent
        while p != undefined and not (isGroupHead p) do p = p.parent
        p
    )
    local reqNames = __NAMES__
    local out = "["
    local first = true
    for i = 1 to reqNames.count do (
        local nm = reqNames[i]
        local exact = getNodeByName nm exact:true ignoreCase:false all:true
        local loose = getNodeByName nm exact:true all:true
        local hits = for m in exact where mcpIsMember m collect m
        local shadows = for m in loose where (findItem exact m) == 0 and (mcpIsMember m) collect m
        if hits.count > 0 or shadows.count > 0 do (
            if not first do out += ","
            first = false
            out += "{\"i\":" + (i as string) + ",\"cs\":" + (exact.count as string)
            if hits.count > 0 then (
                local m = hits[1]
                local g = mcpGroupHeadOf m
                out += ",\"handle\":\"" + (mcpHandle m) + "\",\"group\":" + (if g == undefined then "null" else mcpJsonString g.name) + ",\"open\":" + (if mcpInClosedGroup m then "false" else "true")
            ) else (
                out += ",\"shadow\":" + (mcpJsonString shadows[1].name)
            )
            out += "}"
        )
    )
    out + "]"
)'''

_CLONE_TEMPLATE = r'''(
    __JSON_FN__
    __GROUP_FNS__
    fn mcpSubtree root = (
        local out = #(root)
        local i = 1
        while i <= out.count do (
            for ch in out[i].children do append out ch
            i += 1
        )
        out
    )
    fn mcpHasAncestorIn n arr = (
        local p = n.parent
        local found = false
        while p != undefined and not found do (
            if (findItem arr p) > 0 do found = true
            p = p.parent
        )
        found
    )
    fn mcpSortedHas arr v = (
        local lo = 1
        local hi = arr.count
        local hit = false
        while lo <= hi and not hit do (
            local mid = (lo + hi) / 2
            if arr[mid] == v then hit = true
            else if arr[mid] < v then lo = mid + 1
            else hi = mid - 1
        )
        hit
    )
    -- Rollback: delete every node that did not exist before this call.
    fn mcpDeleteNew pre = (
        if objects.count > pre.count do (
            local hs = sort (for n in pre where isValidNode n collect ((getHandleByAnim n) as integer64))
            local extra = for o in objects where not (mcpSortedHas hs ((getHandleByAnim o) as integer64)) collect o
            for o in extra where isValidNode o do delete o
        )
    )
    local memberSrc = #()
    local lost = #()
    for h in __HANDLES__ do (
        local n = getAnimByHandle h
        if isValidNode n then appendIfUnique memberSrc n else append lost (h as string)
    )
    local plainSrc = #()
    local missing = #()
    local ambiguous = #()
    local plainMembers = #()
    for nm in __PLAIN__ do (
        local matches = getNodeByName nm exact:true ignoreCase:false all:true
        if matches.count == 0 then append missing nm
        else if matches.count > 1 then append ambiguous nm
        else if mcpIsMember matches[1] then append plainMembers nm
        else appendIfUnique plainSrc matches[1]
    )
    if lost.count > 0 then ("__ERROR__|Group member nodes vanished before cloning: " + (lost as string))
    else if ambiguous.count > 0 then ("__ERROR__|Sources must resolve uniquely: " + (ambiguous as string))
    else if plainMembers.count > 0 then ("__ERROR__|Group members would reach the whole-group clone path, nothing cloned: " + (plainMembers as string))
    else if __STRICT__ and missing.count > 0 then ("__ERROR__|Every array source must exist before cloning: " + (missing as string))
    else (
        local allSrc = #()
        join allSrc plainSrc
        join allSrc memberSrc
        -- A source inside another requested hierarchy is cloned with it.
        plainSrc = for n in plainSrc where not (mcpHasAncestorIn n allSrc) collect n
        memberSrc = for n in memberSrc where not (mcpHasAncestorIn n allSrc) collect n
        local preNodes = objects as array
        local made = #()
        local failure = ""
        undo "Clone group member" on (
            try (
                for step = 1 to __COUNT__ do (
                    local stepOffset = __VEC__ * step
__PLAIN_BLOCK__
                    for src in memberSrc do (
                        local tree = mcpSubtree src
                        local copies = #()
                        for s in tree do (
                            local c = __CLONE_FN__ s
                            if not (isValidNode c) do throw ("Could not clone " + s.name)
                            append made c
                            append copies c
                            if c.name == s.name do c.name = uniqueName s.name
                        )
                        for i = 1 to tree.count do (
                            local s = tree[i]
                            local c = copies[i]
                            if i == 1 then (
                                c.parent = undefined
                                setGroupMember c false
                            ) else (
                                local p = copies[findItem tree s.parent]
                                c.parent = p
                                setGroupMember c ((isGroupHead p) or (isGroupMember p))
                            )
                            setGroupHead c (isGroupHead s)
                            if isGroupHead s do setGroupOpen c (isOpenGroupHead s)
                        )
                        local root = copies[1]
                        if root.parent != undefined or (isGroupMember root) do throw ("Could not detach copy of " + src.name + " from its group")
                        move root stepOffset
                    )
                )
                -- Max must not have created anything beyond the tracked copies
                -- (e.g. a group expanded by the clone call): roll back if it did.
                local extra = objects.count - preNodes.count - made.count
                if extra != 0 do throw ("Cloning created " + (extra as string) + " untracked node(s), likely a group expansion; rolled back")
            ) catch (
                failure = getCurrentException() as string
                for n in made where isValidNode n do delete n
                mcpDeleteNew preNodes
            )
        )
        if failure != "" then ("__ERROR__|" + failure)
        else (
            local out = "{\"handles\":["
            for i = 1 to made.count do (
                if i > 1 do out += ","
                out += "\"" + (mcpHandle made[i]) + "\""
            )
            out += "],\"members\":["
            for i = 1 to memberSrc.count do (
                if i > 1 do out += ","
                out += "\"" + (mcpHandle memberSrc[i]) + "\""
            )
            out += "],\"missing\":["
            for i = 1 to missing.count do (
                if i > 1 do out += ","
                out += mcpJsonString missing[i]
            )
            out + "]}"
        )
    )
)'''

_PLAIN_BLOCK = r'''                    if plainSrc.count > 0 do (
                        local batch = #()
                        local ok = maxOps.cloneNodes plainSrc offset:stepOffset expandHierarchy:true cloneType:#__MODE__ newNodes:&batch
                        join made batch
                        if not ok do throw "CloneNodes failed"
                    )'''


def _ms_name_array(names: list[str]) -> str:
    return "#(" + ",".join(f'"{safe_string(n)}"' for n in names) + ")"


def _find_group_members(names: list[str]) -> tuple[list[dict], list[dict]]:
    """Return (members, shadows) for the requested names (one MAXScript read).

    members: names matching a group member case-exactly.
    shadows: names whose only member match differs in case.
    """
    script = (_DETECT_TEMPLATE.replace("__JSON_FN__", _MS_JSON_STRING)
              .replace("__GROUP_FNS__", _MS_GROUP_FNS)
              .replace("__NAMES__", _ms_name_array(names)))
    raw = client.send_command(script).get("result", "")
    try:
        entries = _json.loads(raw) if isinstance(raw, str) else raw
        members, shadows, ambiguous = [], [], []
        for e in entries:
            name = names[int(e["i"]) - 1]  # index, not the round-tripped string
            if "handle" in e:
                if int(e.get("cs", 1)) > 1:
                    ambiguous.append(name)
                members.append({"name": name, "group": e.get("group"),
                                "handle": int(e["handle"]), "open": bool(e.get("open"))})
            else:
                shadows.append({"name": name, "member": str(e["shadow"])})
    except (_json.JSONDecodeError, TypeError, KeyError, ValueError, IndexError):
        raise RuntimeError(f"Group membership check returned unexpected output: {str(raw)[:200]}")
    if ambiguous:
        raise ValueError(
            f"Names match several nodes, at least one inside a group: {ambiguous}. "
            "Rename to unique names before cloning."
        )
    unique: dict[int, dict] = {}
    for m in members:
        unique.setdefault(m["handle"], m)
    return list(unique.values()), shadows


def build_group_member_clone_maxscript(
    plain_names: list[str], member_handles: list[int], mode: str, offset: list[float], count: int
) -> str:
    """MAXScript cloning group members alone and detaching the copies, in one undo step."""
    vec = "[" + ",".join(format(float(v), ".9g") for v in offset) + "]"
    plain_block = _PLAIN_BLOCK.replace("__MODE__", mode) if plain_names else ""
    return (_CLONE_TEMPLATE.replace("__JSON_FN__", _MS_JSON_STRING)
            .replace("__GROUP_FNS__", _MS_GROUP_FNS)
            .replace("__HANDLES__", "#(" + ",".join(str(int(h)) for h in member_handles) + ")")
            .replace("__PLAIN__", _ms_name_array(plain_names))
            .replace("__STRICT__", "true" if count > 1 else "false")
            .replace("__COUNT__", str(int(count)))
            .replace("__VEC__", vec)
            .replace("__PLAIN_BLOCK__", plain_block)
            .replace("__CLONE_FN__", mode))


def _group_warnings(alone: list[dict], with_ancestor: list[str], shadows: list[dict]) -> list[str]:
    """Explain what the group-safe path did."""
    warnings = []
    closed = [m for m in alone if not m["open"]]
    opened = [m for m in alone if m["open"]]
    if closed:
        listed = ", ".join(f"{m['name']} (group {m['group'] or '?'})" for m in closed)
        warnings.append(
            f"Closed-group members requested: {listed}. Max clones the WHOLE closed group when a "
            "member is cloned (copies stack invisibly on the originals), so only the requested "
            "node(s) and their children were cloned and each copy was detached from the group; "
            "the original group is unchanged. Pass clone_whole_group=true for Max's default."
        )
    if opened:
        listed = ", ".join(f"{m['name']} (group {m['group'] or '?'})" for m in opened)
        warnings.append(
            f"Open-group members requested: {listed}. They were cloned alone and each copy was "
            "detached, so it is NOT a member of the open group; use set_parent/manage_groups to add "
            "it back, or pass clone_whole_group=true for Max's default."
        )
    if with_ancestor:
        warnings.append(
            f"Group members {with_ancestor} were cloned with a requested ancestor (e.g. their group "
            "head), so their copies follow that ancestor's copy and were not detached separately."
        )
    if shadows:
        listed = ", ".join(f"'{s['name']}' (member '{s['member']}')" for s in shadows)
        warnings.append(
            f"Names matched case-exactly: {listed} differ only in case from a group member, "
            "which was not cloned."
        )
    return warnings


def _clone_group_members(
    names: list[str], members: list[dict], shadows: list[dict], mode: str, offset: list[float], count: int
) -> str:
    """Clone requested group members alone, detached; other names clone normally."""
    member_names = {m["name"] for m in members}
    plain = [n for n in names if n not in member_names]
    handles = [m["handle"] for m in members]
    raw = str(client.send_command(
        build_group_member_clone_maxscript(plain, handles, mode, offset, count)
    ).get("result", ""))
    if raw.startswith("__ERROR__|"):
        raise RuntimeError(raw.split("|", 1)[1])
    try:
        result = _json.loads(raw)
        made = [int(h) for h in result["handles"]]
        alone_handles = {int(h) for h in result.get("members", [])}
    except (_json.JSONDecodeError, TypeError, KeyError, ValueError):
        raise RuntimeError(f"Group-safe clone returned unexpected output: {raw[:200]}")
    missing = list(result.get("missing", []))
    alone = [m for m in members if m["handle"] in alone_handles]
    with_ancestor = [m["name"] for m in members if m["handle"] not in alone_handles]
    warnings = _group_warnings(alone, with_ancestor, shadows)
    if not made:
        if missing:
            return _json.dumps({"error": "No valid objects found to clone",
                                "notFound": missing, "warnings": warnings})
        raise RuntimeError("Group-safe clone returned no node handles")
    response = client.send_command(build_clone_spatial_maxscript([], node_handles=made))
    payload = _json.loads(response.get("result", "{}"))
    nodes = payload.get("nodes", [])
    if len(nodes) != len(made):
        raise RuntimeError("Clone spatial readback is incomplete; inspect scene before retrying")
    for node in nodes:
        enrich_spatial_payload(node, str(node.get("class", "")))
    payload.update(
        cloned=[n["name"] for n in nodes],
        notFound=missing,
        count=count,
        offset=offset,
        group_members=[{"name": m["name"], "group": m["group"], "open_group": m["open"]} for m in alone],
        detached_from_group=bool(alone),
    )
    if with_ancestor:
        payload["cloned_with_ancestor"] = with_ancestor
    if warnings:
        payload["warnings"] = warnings
    return _json.dumps(payload)
