"""Native, deterministic scene-graph QA with narrowly safe repairs."""

from __future__ import annotations

import base64
import json
import math
from collections import Counter
from typing import Optional

from ..coerce import DictList, IntList, StrList
from ..helpers.maxscript import safe_string
from ..server import client, mcp

# Python-side check (#16): stacked duplicate groups left by cloning a member
# of a closed group. Never repaired automatically.
DUPLICATE_GROUP_CHECK = "duplicate_group_heads"
_MAX_CANDIDATE_HEADS = 20000
_MAX_SETS_LISTED = 50
_MAX_HEADS_PER_SET = 20
_NAME_BUDGET_CHARS = 1_000_000
_MIN_POSITION_CELL = 0.01
_NEAR_PAIR_LIMIT = 200
_ROTATION_REL_TOL = 1e-4
_DUPLICATE_FIX_TEXT = (
    "Report only: scene_qa never deletes. After confirming the copies are stacked "
    "(isolate one head and compare), keep one head (usually the lowest handle) and "
    "delete each extra head together with all its members, or ungroup/explode the "
    "extras and delete their members. Avoid maxOps.cloneNodes on closed-group "
    "members: copy/instance the single node, then set .parent = undefined and "
    "setGroupMember n false."
)
_PARTIAL_FIX_NOTE = (
    " The child lists differ slightly: compare the members of each head first, "
    "since an extra head may hold a node that exists nowhere else."
)
_RENAME_NOTE = (
    "name_collisions renamed members of stacked duplicate groups; delete the "
    "extra heads first. The renamed copies are still matched by normalized names."
)

# Candidates are heads that share a coarse position cell with another head
# (8 half-shifted grids, so near neighbours always share one key). Child names
# are normalized (trailing digits, `_mcp`, case) so clone/MakeNameUnique
# renames still match; exact names are tracked separately.
_DUPLICATE_GROUP_SCRIPT = r"""(
    fn mcpB64 s = (dotNetClass "System.Convert").ToBase64String ((dotNetClass "System.Text.Encoding").UTF8.GetBytes (s as string))
    fn mcpNum v = formattedPrint (v as float) format:".6f"
    fn mcpCell v = formattedPrint (floor v) format:".0f"
    fn mcpDesc n = (
        local c = 0
        local stack = #(n)
        while stack.count > 0 do (
            local cur = stack[stack.count]
            deleteItem stack stack.count
            for ch in cur.children do (c += 1; append stack ch)
        )
        c
    )
    fn mcpAddHeads n acc = (
        local cur = n
        while cur != undefined do (
            if isGroupHead cur do PutDictValue acc cur.inode.handle true
            if not (isGroupMember cur) then (cur = undefined) else (cur = cur.parent)
        )
    )
    fn mcpNearestHead h = (
        local found = 0
        local a = if isGroupMember h then h.parent else undefined
        while a != undefined and found == 0 do (
            if isGroupHead a then (found = a.inode.handle)
            else if isGroupMember a then (a = a.parent)
            else (a = undefined)
        )
        found
    )
    fn mcpNorm nm = (
        local s = toLower (trimRight (nm as string) "0123456789")
        if s.count >= 4 and (substring s (s.count - 3) 4) == "_mcp" do s = substring s 1 (s.count - 4)
        trimRight (trimRight s "0123456789") " _-."
    )
    fn mcpJoin arr = (
        sort arr
        local ss = stringStream ""
        for t in arr do format "%\n" t to:ss
        ss as string
    )
    fn mcpRows tm = (
        local rows = #(tm.row1, tm.row2, tm.row3)
        local ss = stringStream ""
        for r = 1 to 3 do (
            local v = rows[r]
            format "%,%,%" (mcpNum v.x) (mcpNum v.y) (mcpNum v.z) to:ss
            if r < 3 do format "," to:ss
        )
        ss as string
    )
    fn mcpIdOf dict key = (
        if HasDictValue dict key then (GetDictValue dict key) else (
            local v = dict.count + 1
            PutDictValue dict key v
            v
        )
    )
    try (
        local scopeMode = "__SCOPE__"
        local maxOut = __MAX_OUT__
        local nameBudget = __NAME_BUDGET__
        local cell = __CELL__
        local scopeSet = Dictionary #integer
        if scopeMode == "selection" do (
            for n in selection do mcpAddHeads n scopeSet
        )
        if scopeMode == "targets" do (
            for h in #(__HANDLES__) do (
                local n = maxOps.getNodeByHandle h
                if n != undefined do mcpAddHeads n scopeSet
            )
            for nm in #(__NAMES__) do (
                local found = getNodeByName nm all:true
                if found != undefined do for n in found do mcpAddHeads n scopeSet
            )
        )
        local heads = for o in objects where isGroupHead o collect o
        local shared = for i = 1 to heads.count collect false
        local cells = Dictionary #string
        for i = 1 to heads.count do (
            local p = heads[i].pos
            local kx = #(mcpCell (p.x / cell), mcpCell (p.x / cell + 0.5))
            local ky = #(mcpCell (p.y / cell), mcpCell (p.y / cell + 0.5))
            local kz = #(mcpCell (p.z / cell), mcpCell (p.z / cell + 0.5))
            for a = 1 to 2 do for b = 1 to 2 do for c = 1 to 2 do (
                local key = (a as string) + (b as string) + (c as string) + "|" + kx[a] + "," + ky[b] + "," + kz[c]
                if HasDictValue cells key then (
                    local lst = GetDictValue cells key
                    for j in lst do shared[j] = true
                    shared[i] = true
                    append lst i
                ) else PutDictValue cells key #(i)
            )
        )
        local normIds = Dictionary #string
        local exactIds = Dictionary #string
        local namesSent = Dictionary #integer
        local out = stringStream ""
        format "G|%\n" heads.count to:out
        local emitted = 0
        local nameChars = 0
        local truncated = false
        local namesTruncated = false
        for i = 1 to heads.count while not truncated do (
            if shared[i] do (
                if emitted >= maxOut then (truncated = true) else (
                    local h = heads[i]
                    local hh = h.inode.handle
                    local normKey = mcpJoin (for ch in h.children collect (((classOf ch) as string) + "/" + (mcpNorm ch.name)))
                    local exactKey = mcpJoin (for ch in h.children collect (((classOf ch) as string) + "/" + ch.name))
                    local nid = mcpIdOf normIds normKey
                    local eid = mcpIdOf exactIds exactKey
                    if not (HasDictValue namesSent nid) do (
                        PutDictValue namesSent nid true
                        if nameChars + normKey.count > nameBudget then (namesTruncated = true) else (
                            nameChars += normKey.count
                            format "N|%|%\n" nid (mcpB64 normKey) to:out
                        )
                    )
                    local p = h.pos
                    local inScope = if scopeMode == "scene" or (HasDictValue scopeSet hh) then 1 else 0
                    local layerName = if h.layer != undefined then h.layer.name else ""
                    format "H|%|%|%|%|%|%|%|%|%|%|%|%|%\n" hh (mcpNearestHead h) h.children.count (1 + (mcpDesc h)) (mcpNum p.x) (mcpNum p.y) (mcpNum p.z) (mcpRows h.transform) inScope nid eid (mcpB64 h.name) (mcpB64 layerName) to:out
                    emitted += 1
                )
            )
        )
        if truncated do format "T|heads\n" to:out
        if namesTruncated do format "T|names\n" to:out
        format "END" to:out
        out as string
    ) catch (
        "__ERROR__|" + (getCurrentException())
    )
)"""


def _ms_int_list(values: list[int]) -> str:
    return ", ".join(str(int(v)) for v in values)


def _ms_str_list(values: list[str]) -> str:
    return ", ".join(f'"{safe_string(v)}"' for v in values)


def position_cell(tolerance: float) -> float:
    """Coarse MAXScript grid cell; must exceed 2x tolerance for the shifted grids."""
    return max(4.0 * tolerance, _MIN_POSITION_CELL)


def build_duplicate_group_script(
    scope: str,
    handles: Optional[list[int]] = None,
    names: Optional[list[str]] = None,
    max_out: int = _MAX_CANDIDATE_HEADS,
    tolerance: float = 0.001,
    name_budget: int = _NAME_BUDGET_CHARS,
) -> str:
    """Build the read-only MAXScript that lists co-located group heads."""
    return (
        _DUPLICATE_GROUP_SCRIPT
        .replace("__SCOPE__", scope)
        .replace("__MAX_OUT__", str(int(max_out)))
        .replace("__NAME_BUDGET__", str(int(name_budget)))
        .replace("__CELL__", f"{position_cell(tolerance):.6f}")
        .replace("__HANDLES__", _ms_int_list(handles or []))
        .replace("__NAMES__", _ms_str_list(names or []))
    )


def _target_refs(
    names: Optional[list[str]],
    handles: Optional[list[int]],
    refs: Optional[list[dict]],
) -> tuple[list[int], list[str]]:
    """Collect handles/names for scope=targets (path-only refs are skipped)."""
    out_handles = [int(h) for h in (handles or [])]
    out_names = [str(n) for n in (names or [])]
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        handle = ref.get("handle")
        if handle is not None and not isinstance(handle, bool):
            try:
                out_handles.append(int(handle))
                continue
            except (TypeError, ValueError):
                pass
        name = ref.get("name")
        if isinstance(name, str) and name:
            out_names.append(name)
    return out_handles, out_names


def _b64(text: str) -> str:
    return base64.b64decode(text, validate=True).decode("utf-8") if text else ""


def parse_duplicate_group_output(raw: str) -> dict:
    """Parse the G/N/H/T/END line records emitted by the MAXScript query."""
    if raw.startswith("__ERROR__|"):
        raise RuntimeError(raw.split("|", 1)[1])
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or lines[-1].strip() != "END":
        raise RuntimeError("duplicate_group_heads query returned an incomplete report")
    heads_scanned = 0
    truncated = names_truncated = False
    names: dict[int, Counter] = {}
    candidates: list[dict] = []
    for line in lines[:-1]:
        if not line:
            continue
        parts = line.split("|")
        if parts[0] == "G":
            heads_scanned = int(parts[1])
        elif parts[0] == "T":
            if parts[1:] == ["names"]:
                names_truncated = True
            else:
                truncated = True
        elif parts[0] == "N" and len(parts) == 3:
            names[int(parts[1])] = Counter(t for t in _b64(parts[2]).split("\n") if t)
        elif parts[0] == "H" and len(parts) == 14:
            rows = [float(v) for v in parts[8].split(",")]
            if len(rows) != 9:
                raise RuntimeError("duplicate_group_heads: bad transform record")
            candidates.append({
                "handle": int(parts[1]),
                "parent_head": int(parts[2]),
                "child_count": int(parts[3]),
                "objects": int(parts[4]),
                "position": [float(parts[5]), float(parts[6]), float(parts[7])],
                "rows": rows,
                "in_scope": parts[9] == "1",
                "norm_id": int(parts[10]),
                "exact_id": int(parts[11]),
                "name": _b64(parts[12]),
                "layer": _b64(parts[13]),
            })
        else:
            raise RuntimeError(f"duplicate_group_heads: unexpected record {parts[0]!r}")
    return {"group_heads_scanned": heads_scanned, "candidates": candidates,
            "names": names, "truncated": truncated, "names_truncated": names_truncated}


def _cluster_by_position(heads: list[dict], tolerance: float) -> list[list[dict]]:
    """Group heads whose positions are within tolerance of a cluster seed."""
    cell = max(tolerance, 1e-9)
    grid: dict[tuple[int, int, int], list[int]] = {}
    clusters: list[list[dict]] = []
    for head in sorted(heads, key=lambda h: h["handle"]):
        p = head["position"]
        key = tuple(math.floor(c / cell) for c in p)
        found = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for idx in grid.get((key[0] + dx, key[1] + dy, key[2] + dz), ()):
                        seed = clusters[idx][0]["position"]
                        if all(abs(a - b) <= tolerance for a, b in zip(p, seed)):
                            found = idx
                            break
                    if found is not None:
                        break
                if found is not None:
                    break
            if found is not None:
                break
        if found is None:
            grid.setdefault(key, []).append(len(clusters))
            clusters.append([head])
        else:
            clusters[found].append(head)
    return [c for c in clusters if len(c) >= 2]


def _same_rotation_scale(a: list[float], b: list[float]) -> bool:
    return all(abs(x - y) <= _ROTATION_REL_TOL * max(1.0, abs(x), abs(y))
               for x, y in zip(a, b))


def _split_by_transform(cluster: list[dict]) -> list[list[dict]]:
    """Split a position cluster into groups sharing rotation and scale."""
    groups: list[list[dict]] = []
    for head in cluster:
        for group in groups:
            if _same_rotation_scale(head["rows"], group[0]["rows"]):
                group.append(head)
                break
        else:
            groups.append([head])
    return groups


def _near_names(a: Counter, b: Counter) -> bool:
    """Child lists nearly match: small multiset difference (e.g. one detached clone)."""
    if not a or not b:
        return False
    diff = sum((a - b).values()) + sum((b - a).values())
    return diff <= max(1, int(0.1 * max(sum(a.values()), sum(b.values()))))


def _match_heads(group: list[dict], names: dict[int, Counter]) -> list[list[dict]]:
    """Union heads with equal normalized child names, or nearly equal child lists."""
    parent = {h["handle"]: h["handle"] for h in group}
    head_of = {h["handle"]: h for h in group}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def is_ancestor(anc: int, h: dict) -> bool:
        seen = set()
        cur = h["parent_head"]
        while cur and cur not in seen:
            if cur == anc:
                return True
            seen.add(cur)
            cur = head_of[cur]["parent_head"] if cur in head_of else 0
        return False

    by_norm: dict[int, int] = {}
    for h in group:
        first = by_norm.setdefault(h["norm_id"], h["handle"])
        if first != h["handle"]:
            parent[find(h["handle"])] = find(first)
    if len(group) <= _NEAR_PAIR_LIMIT:
        near_cache: dict[tuple[int, int], bool] = {}
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a["norm_id"] == b["norm_id"] or find(a["handle"]) == find(b["handle"]):
                    continue
                key = (min(a["norm_id"], b["norm_id"]), max(a["norm_id"], b["norm_id"]))
                if key not in near_cache:
                    na, nb = names.get(key[0]), names.get(key[1])
                    near_cache[key] = bool(na and nb and _near_names(na, nb))
                if (near_cache[key] and not is_ancestor(a["handle"], b)
                        and not is_ancestor(b["handle"], a)):
                    parent[find(b["handle"])] = find(a["handle"])
    comps: dict[int, list[dict]] = {}
    for h in group:
        comps.setdefault(find(h["handle"]), []).append(h)
    return [sorted(c, key=lambda h: h["handle"]) for c in comps.values() if len(c) >= 2]


def _name_match(heads: list[dict]) -> str:
    if len({h["exact_id"] for h in heads}) == 1:
        return "exact"
    if len({h["norm_id"] for h in heads}) == 1:
        return "normalized"
    return "partial"


def find_duplicate_group_sets(parsed: dict, tolerance: float) -> dict:
    """Turn parsed candidates into duplicate sets (nested implied sets folded)."""
    sets: list[list[dict]] = []
    transform_mismatch = 0
    for cluster in _cluster_by_position(parsed["candidates"], tolerance):
        for group in _split_by_transform(cluster):
            if len(group) < 2:
                transform_mismatch += 1
                continue
            sets.extend(_match_heads(group, parsed.get("names", {})))
    dup_handles = {h["handle"] for s in sets for h in s}
    reported, implied = [], 0
    for heads in sets:
        # A set whose heads all sit inside other duplicated groups (nearest
        # ancestor head) is implied by the outer duplicate.
        parents = {h["parent_head"] for h in heads}
        if 0 not in parents and len(parents) == len(heads) and parents <= dup_handles:
            implied += 1
            continue
        if not any(h["in_scope"] for h in heads):
            continue
        reported.append(heads)
    reported.sort(key=lambda s: (-(sum(h["objects"] for h in s) - s[0]["objects"]),
                                 s[0]["handle"]))
    return {"sets": reported, "nested_sets_implied": implied,
            "transform_mismatch_heads": transform_mismatch}


_MATCH_MESSAGES = {
    "exact": "share position, rotation/scale, child count and child names",
    "normalized": ("share position, rotation/scale and child names apart from "
                   "numeric suffixes (e.g. Prop vs Prop001)"),
    "partial": "share position and rotation/scale, and their child lists nearly match",
}


def _duplicate_issue(heads: list[dict]) -> dict:
    keep = heads[0]
    total = sum(h["objects"] for h in heads)
    listed = heads[:_MAX_HEADS_PER_SET]
    match = _name_match(heads)
    fix = _DUPLICATE_FIX_TEXT + (_PARTIAL_FIX_NOTE if match == "partial" else "")
    return {
        "code": DUPLICATE_GROUP_CHECK,
        "severity": "warning",
        "message": (
            f"{len(heads)} group heads {_MATCH_MESSAGES[match]}; likely stacked "
            "duplicate groups (e.g. cloning a closed-group member)."
        ),
        "node": {"name": keep["name"], "handle": keep["handle"], "layer": keep["layer"]},
        "details": {
            "heads": [{"name": h["name"], "handle": h["handle"], "layer": h["layer"],
                       "children": h["child_count"], "objects": h["objects"]}
                      for h in listed],
            "head_count": len(heads),
            "heads_listed": len(listed),
            "child_count": keep["child_count"],
            "name_match": match,
            "position": keep["position"],
            "object_total": total,
            "extra_heads": len(heads) - 1,
            "extra_objects": total - keep["objects"],
            "repairable": False,
            "suggested_fix": fix,
        },
    }


def run_duplicate_group_check(
    scope: str,
    tolerance: float,
    names: Optional[list[str]] = None,
    handles: Optional[list[int]] = None,
    refs: Optional[list[dict]] = None,
) -> dict:
    """Run the read-only MAXScript query and build issues plus a summary block."""
    t_handles, t_names = _target_refs(names, handles, refs) if scope == "targets" else ([], [])
    script = build_duplicate_group_script(scope, t_handles, t_names, tolerance=tolerance)
    raw = str(client.send_command(script, timeout=30.0).get("result", ""))
    parsed = parse_duplicate_group_output(raw)
    found = find_duplicate_group_sets(parsed, tolerance)
    sets = found["sets"]
    issues = [_duplicate_issue(s) for s in sets]
    by_match: dict[str, int] = {}
    for issue in issues:
        m = issue["details"]["name_match"]
        by_match[m] = by_match.get(m, 0) + 1
    info = {
        "group_heads_scanned": parsed["group_heads_scanned"],
        "candidate_heads": len(parsed["candidates"]),
        "sets_found": len(sets),
        "sets_by_name_match": by_match,
        "nested_sets_implied": found["nested_sets_implied"],
        "transform_mismatch_heads": found["transform_mismatch_heads"],
        "extra_heads": sum(i["details"]["extra_heads"] for i in issues),
        "extra_objects": sum(i["details"]["extra_objects"] for i in issues),
        "position_tolerance": tolerance,
        "candidates_truncated": parsed["truncated"],
        "child_names_truncated": parsed["names_truncated"],
        "report_only": True,
    }
    if scope == "targets" and refs and any(
            isinstance(r, dict) and r.get("handle") is None and not r.get("name") for r in refs):
        info["note"] = "Path-only refs are not used to scope duplicate_group_heads."
    return {"issues": issues, "info": info}


def merge_duplicate_groups(scan: dict, found: dict, max_issues: int,
                           detected_before_fix: bool = False) -> None:
    """Merge duplicate-group findings into one native scan result in place."""
    issues = found["issues"]
    info = dict(found["info"])
    scan_issues = scan.setdefault("issues", [])
    room = max(0, min(_MAX_SETS_LISTED, max_issues - len(scan_issues)))
    listed = []
    for issue in issues[:room]:
        if detected_before_fix:
            issue = {**issue, "details": {**issue["details"], "detected_before_fix": True}}
        listed.append(issue)
    scan_issues.extend(listed)
    info["sets_listed"] = len(listed)
    if len(listed) < len(issues):
        scan["truncated"] = True
    if issues:
        summary = scan.setdefault("summary", {})
        summary["issue_count"] = summary.get("issue_count", 0) + len(issues)
        by_code = summary.setdefault("by_code", {})
        by_code[DUPLICATE_GROUP_CHECK] = by_code.get(DUPLICATE_GROUP_CHECK, 0) + len(issues)
        by_sev = summary.setdefault("by_severity", {})
        by_sev["warning"] = by_sev.get("warning", 0) + len(issues)
    checks = scan.setdefault("checks", [])
    if DUPLICATE_GROUP_CHECK not in checks:
        checks.append(DUPLICATE_GROUP_CHECK)
    scan[DUPLICATE_GROUP_CHECK] = info


def _dumps(result: dict) -> str:
    """Compact UTF-8 JSON, matching the native encoder (no ASCII escaping)."""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _merge_into_result(text: str, found: dict, max_issues: int) -> str:
    """Merge findings into a scan or fix response string; non-JSON passes through."""
    try:
        result = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(result, dict):
        return text
    if result.get("action") == "fix":
        if isinstance(result.get("before"), dict):
            merge_duplicate_groups(result["before"], found, max_issues)
        if isinstance(result.get("after"), dict):
            merge_duplicate_groups(result["after"], found, max_issues, detected_before_fix=True)
            applied = result.get("applied")
            renamed = isinstance(applied, list) and any(
                isinstance(a, dict) and a.get("fix") == "name_collisions" for a in applied)
            if renamed and found["issues"]:
                result["after"][DUPLICATE_GROUP_CHECK]["note"] = _RENAME_NOTE
    else:
        merge_duplicate_groups(result, found, max_issues)
    return _dumps(result)


def _merge_error(text: str, error: str) -> str:
    try:
        result = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(result, dict):
        return text
    target = result.get("before") if result.get("action") == "fix" else result
    if isinstance(target, dict):
        target[DUPLICATE_GROUP_CHECK] = {"error": error, "report_only": True}
    return _dumps(result)


@mcp.tool()
def scene_qa(
    action: str = "scan",
    checks: Optional[StrList] = None,
    fixes: Optional[StrList] = None,
    scope: str = "scene",
    names: Optional[StrList] = None,
    handles: Optional[IntList] = None,
    refs: Optional[DictList] = None,
    expected_scene_seq: Optional[int] = None,
    dry_run: bool = False,
    max_issues: int = 1000,
    transform_epsilon: float = 1.0e-6,
    far_origin_threshold: Optional[float] = None,
    check_duplicate_groups: bool = True,
    group_position_tolerance: float = 0.001,
) -> str:
    """Scan or repair non-mesh scene hygiene using the native SDK.

    Checks are limited to names, transforms, hierarchy/group metadata, frame rate,
    and animation range. No mesh, UV, normals, topology, skinning, or visual-quality
    judgement is performed. ``action=fix`` only applies explicitly deterministic
    repairs (currently ``name_collisions`` and ``empty_names``), inside one undo step.
    Pass ``expected_scene_seq`` to reject an apply against persistently changed
    scene state; selection-only interaction does not invalidate the token.

    ``duplicate_group_heads`` (on by default; skip with
    ``check_duplicate_groups=False`` or a ``checks`` list without it) flags group
    heads stacked on each other (same position, rotation and scale) whose child
    names match, ignoring numeric/``_mcp`` suffixes, or nearly match, as left by
    cloning a closed-group member. Report only; never fixed or deleted.
    ``group_position_tolerance`` is in scene units.
    """
    normalized_action = action.strip().lower()
    if normalized_action not in {"scan", "fix"}:
        raise ValueError("action must be scan or fix")
    if scope not in {"scene", "selection", "targets"}:
        raise ValueError("scope must be scene, selection, or targets")
    if max_issues < 1 or max_issues > 100_000:
        raise ValueError("max_issues must be between 1 and 100000")
    if transform_epsilon <= 0:
        raise ValueError("transform_epsilon must be greater than zero")
    if far_origin_threshold is not None and far_origin_threshold <= 0:
        raise ValueError("far_origin_threshold must be greater than zero")
    if expected_scene_seq is not None and expected_scene_seq < 0:
        raise ValueError("expected_scene_seq must be non-negative")
    if handles and any(handle <= 0 for handle in handles):
        raise ValueError("handles must contain positive integers")
    if scope == "targets" and not names and not handles and not refs:
        raise ValueError("scope=targets requires refs, names, or handles")
    if not (group_position_tolerance >= 0 and math.isfinite(group_position_tolerance)):
        raise ValueError("group_position_tolerance must be a finite number >= 0")
    if not client.native_available:
        return "Native bridge is required for scene_qa."

    native_checks: Optional[list[str]] = None
    run_duplicates = check_duplicate_groups
    if checks:
        native_checks = [c for c in checks if c.strip().lower() != DUPLICATE_GROUP_CHECK]
        run_duplicates = run_duplicates and len(native_checks) != len(checks)

    payload: dict = {
        "action": normalized_action,
        "scope": scope,
        "dry_run": dry_run,
        "max_issues": max_issues,
        "transform_epsilon": transform_epsilon,
    }
    if native_checks is not None:
        # An empty list runs no native checks (only duplicate_group_heads).
        payload["checks"] = native_checks
    if fixes:
        payload["fixes"] = list(fixes)
    if names:
        payload["names"] = list(names)
    if handles:
        payload["handles"] = list(handles)
    if refs:
        payload["refs"] = list(refs)
    if expected_scene_seq is not None:
        payload["expected_scene_seq"] = expected_scene_seq
    if far_origin_threshold is not None:
        payload["far_origin_threshold"] = far_origin_threshold

    applying = normalized_action == "fix" and not dry_run

    def duplicate_scan() -> tuple[Optional[dict], Optional[str]]:
        try:
            return run_duplicate_group_check(
                scope, group_position_tolerance, names, handles, refs), None
        except Exception as exc:  # report-only check must never break scene_qa
            return None, str(exc)

    # Report the scene as it was before an applied fix (name_collisions renames
    # the duplicated children), so the read-only query runs first.
    found: Optional[dict] = None
    error: Optional[str] = None
    if run_duplicates and applying:
        found, error = duplicate_scan()

    # A dry-run repair is read-only and deliberately uses the scan route so it
    # never opens an empty undo record or trips safe-mode mutation gating.
    cmd_type = "native:scene_qa_fix" if applying else "native:scene_qa_scan"
    response = client.send_command(
        json.dumps(payload),
        cmd_type=cmd_type,
        timeout=30.0,
    )
    result = response.get("result", "")
    if not run_duplicates:
        return result
    if not applying:
        found, error = duplicate_scan()
    if found is not None:
        return _merge_into_result(result, found, max_issues)
    return _merge_error(result, error or "duplicate_group_heads query failed")
