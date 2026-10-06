"""Resting-contact check: how far objects float above, or sink into, what lies below them.

The MAXScript side measures; this module validates input, builds the scripts,
parses their line reports, classifies each item and plans drops. Classification
and the drop plan live here so they are deterministic and testable without Max.

Method (fork issue #17):

* Each item's underside comes from its own evaluated mesh: the lowest vertex
  in each cell of an XY grid over its footprint. Never from rays cast up into
  the item, which miss back faces of single-sided cloth and sparse needles.
* Below each underside point, one double-sided segment is cast straight down
  against every support, from ``max_sink`` above the point (to see items
  sunk into a surface) to ``max_drop`` below it. Each support gets one
  ``RayMeshGridIntersect``, built once, sized by its face count and freed on
  every path. A segment's nearest hit gives the signed gap: positive above
  the surface, negative below it.
* Supports are, unless given: visible, renderable mesh nodes that start
  strictly lower than the item, overlap its footprint and reach within
  ``max_drop`` of its underside. Starting strictly lower keeps stacks acyclic.
  Scatter objects (Mesher, Forest Pack, RailClone, tyFlow, particles, fur) are
  left out. Coincident duplicates and reference twins of an item are never
  its supports; twins sharing its base object move with it.

Lengths are always given in millimetres and converted with
``units.decodeValue "1mm"``; results come back in scene units and are
reported in millimetres too. The time budget counts from when Python built the
request (``sent_ms``), like contact_check, so queue wait uses it up.

Drops (``drop_to_surface``) move only along world Z, bottom-up, in one undo
step. The apply script re-measures every moved item inside the hold and
cancels it when any ends up floating or sunk beyond tolerance.
"""
from __future__ import annotations

import base64
import math
import time
from typing import Any

from .contact_check import MAX_QUEUE_WAIT_MS, validate_budgets
from .maxscript import safe_string

STATUSES = ("penetrating", "floating", "no_support", "resting", "unchecked")
GROUP_MODES = ("members", "rigid")

DEFAULT_TOLERANCE_MM = 0.5
DEFAULT_CLEARANCE_MM = 0.2
DEFAULT_MAX_DROP_MM = 50.0
DEFAULT_MAX_SINK_MM = 20.0
DEFAULT_SAMPLES = 64
DEFAULT_MAX_ITEM_VERTS = 100_000
DEFAULT_MAX_SUPPORT_FACES = 1_000_000
DEFAULT_TIME_BUDGET_S = 20.0
MAX_UNITS = 500
MIN_APPLY_MS = 3000  # an apply needs at least this much budget left after measuring

SKIP_REASONS = {
    "not_mesh": "not mesh-convertible geometry",
    "empty_group": "group with no mesh members",
    "already_included": "already included through another item",
}
SUPPORT_SKIP_REASONS = {
    "not_mesh": "not mesh-convertible geometry",
    "empty_mesh": "no faces",
    "face_limit": "face count above max_support_faces",
    "coincident": "coincident duplicate of an item",
}
NOT_MOVABLE_REASONS = ("frozen", "locked_z", "linked", "xref", "constrained", "animated", "check_failed",
                       "companion_not_movable")
APPLY_REFUSALS = {
    "__BUSY__": ("USER_BUSY", "An undo operation is open in 3ds Max."),
    "__AUTOKEY__": ("AUTO_KEY_ON", "Auto Key is on in 3ds Max, so a move would set keys."),
    "__DEADLINE__": ("DEADLINE_BEFORE_APPLY", "Too little of the time budget was left to apply and verify."),
}

_BIG = 1e29


def _mm(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a number of millimetres from {low:g} to {high:g}")
    value = float(value)
    if not low <= value <= high:
        raise ValueError(f"{label} must be from {low:g} to {high:g} mm")
    return value


def _int(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} to {high}")
    return value


def _names(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{label} must be a list of node names")
    seen: list[str] = []
    for v in value:
        if v not in seen:
            seen.append(v)
    return seen


def validate(names: Any, supports: Any, exclude_supports: Any, group_mode: Any, tolerance_mm: Any,
             max_drop_mm: Any, max_sink_mm: Any, samples: Any, max_item_verts: Any, max_support_faces: Any,
             time_budget_s: Any, request_timeout_s: Any = None) -> dict[str, Any]:
    """The validated measurement settings."""
    if group_mode not in GROUP_MODES:
        raise ValueError("group_mode must be members or rigid")
    names_l = _names(names, "names")
    supports_l = _names(supports, "supports")
    exclude_l = _names(exclude_supports, "exclude_supports")
    if supports_l and exclude_l:
        raise ValueError("Pass supports or exclude_supports, not both")
    _, budget_s = validate_budgets(1, time_budget_s, request_timeout_s)
    return {
        "names": names_l,
        "supports": supports_l,
        "exclude": exclude_l,
        "rigid": group_mode == "rigid",
        "tolerance_mm": _mm(tolerance_mm, "tolerance_mm", 0.01, 50.0),
        "max_drop_mm": _mm(max_drop_mm, "max_drop_mm", 1.0, 2000.0),
        "max_sink_mm": _mm(max_sink_mm, "max_sink_mm", 0.1, 500.0),
        "samples": _int(samples, "samples", 4, 1024),
        "max_item_verts": _int(max_item_verts, "max_item_verts", 1000, 2_000_000),
        "max_support_faces": _int(max_support_faces, "max_support_faces", 1000, 5_000_000),
        "time_budget_s": budget_s,
    }


def _ms_array(names: list[str]) -> str:
    return "#(" + ", ".join(f'"{safe_string(n)}"' for n in names) + ")"


def _ms_float(value: float) -> str:
    """A MAXScript Float literal; never an Integer literal like ``2``."""
    return format(float(value), ".9f")


def _fill(template: str, values: dict[str, Any]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("@@" + key + "@@", str(value))
    if "@@" in out:
        raise ValueError("unfilled placeholder in resting script")
    return out


def _deadline_values(time_budget_s: float, sent_ms: int | None) -> dict[str, Any]:
    if sent_ms is None:
        sent_ms = int(time.time() * 1000)
    return {"SENT_MS": int(sent_ms), "MAX_WAIT_MS": MAX_QUEUE_WAIT_MS, "LIMIT_MS": int(round(time_budget_s * 1000))}


def build_measure_script(settings: dict[str, Any], *, sent_ms: int | None = None) -> str:
    """MAXScript that measures every item within the time budget and returns a line report. Read only."""
    values = _deadline_values(settings["time_budget_s"], sent_ms)
    values.update({
        "NAMES": _ms_array(settings["names"]),
        "SUPPORTS": _ms_array(settings["supports"]),
        "EXCLUDE": _ms_array(settings["exclude"]),
        "RIGID": "true" if settings["rigid"] else "false",
        "TOL_MM": _ms_float(settings["tolerance_mm"]),
        "DROP_MM": _ms_float(settings["max_drop_mm"]),
        "SINK_MM": _ms_float(settings["max_sink_mm"]),
        "SAMPLES": int(settings["samples"]),
        "MAX_VERTS": int(settings["max_item_verts"]),
        "MAX_SUP_FACES": int(settings["max_support_faces"]),
        "MAX_UNITS": MAX_UNITS,
    })
    return _fill(_LIBRARY + _MEASURE_BODY, values)


def build_apply_script(settings: dict[str, Any], moves: list[dict[str, Any]], verify: list[dict[str, Any]],
                       *, label: str, eps: float, time_budget_s: float, sent_ms: int | None = None) -> str:
    """MAXScript that moves nodes along world Z in one undo step, re-measures, and accepts or cancels.

    moves: {handle, dz, z_before, z_after} in scene units (z is the node's world position).
    verify: {unit, meshes: [handles], supports: [handles], low, high} gap bounds in scene units.
    """
    if not moves:
        raise ValueError("build_apply_script needs at least one move")
    mv = "#(" + ", ".join("#(%d, %s, %s, %s)" % (int(m["handle"]), _ms_float(m["dz"]), _ms_float(m["z_before"]),
                                                 _ms_float(m["z_after"])) for m in moves) + ")"
    vf = "#(" + ", ".join(
        "#(%d, #(%s), #(%s), %s, %s)" % (int(v["unit"]), ", ".join(str(int(h)) for h in v["meshes"]),
                                          ", ".join(str(int(h)) for h in v["supports"]),
                                          _ms_float(v["low"]), _ms_float(v["high"])) for v in verify) + ")"
    values = _deadline_values(time_budget_s, sent_ms)
    values.update({
        "MOVES": mv,
        "VERIFY": vf,
        "EPS": _ms_float(eps),
        "LABEL": safe_string(label),
        "DROP_MM": _ms_float(settings["max_drop_mm"]),
        "SINK_MM": _ms_float(settings["max_sink_mm"]),
        "SAMPLES": int(settings["samples"]),
        "MAX_VERTS": int(settings["max_item_verts"]),
        "MIN_APPLY_MS": MIN_APPLY_MS,
    })
    return _fill(_LIBRARY + _APPLY_BODY, values)


# ── parsing ───────────────────────────────────────────────────────────────────

def _float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _opt(text: str) -> float | None:
    value = _float(text)
    return None if abs(value) >= _BIG else value


def _point(text: str) -> list[float] | None:
    if not text:
        return None
    parts = text.split(",")
    if len(parts) != 3:
        raise ValueError("bad point")
    return [_float(p) for p in parts]


def _name(b64: str) -> str:
    return base64.b64decode(b64, validate=True).decode("utf-8")


def _handle(text: str) -> int:
    handle = int(text)
    if handle <= 0:
        raise ValueError("invalid node handle")
    return handle


def _handles(text: str) -> list[int]:
    return [_handle(h) for h in text.split(",") if h]


def _check_raw(raw: str, head_prefix: str, what: str) -> list[str]:
    if raw.startswith("__ERROR__|"):
        raise RuntimeError(raw.split("|", 1)[1])
    lines = [ln for ln in raw.replace("\r", "").split("\n") if ln]
    if not lines or not lines[0].startswith(head_prefix):
        raise RuntimeError(f"{what} returned no report")
    if lines[-1] != "END" or lines.count("END") != 1:
        raise RuntimeError(f"{what} report truncated or malformed")
    return lines


def parse_measure(raw: str) -> dict[str, Any]:
    """Parse the measuring script's line report into units, supports and gaps (scene units)."""
    lines = _check_raw(raw, "U|", "Resting check")
    try:
        head = lines[0].split("|")
        if len(head) != 16:
            raise ValueError("bad header")
        report: dict[str, Any] = {
            "tolerance": _float(head[1]), "lift": _float(head[2]), "max_drop": _float(head[3]),
            "units_name": head[4], "upm": _float(head[5]), "unit_count": int(head[6]), "measured": int(head[7]),
            "elapsed_ms": int(head[8]), "time_budget_ms": int(head[9]), "queue_wait_ms": int(head[10]),
            "stop": head[11], "frame": head[12], "auto_key": head[13] == "true", "pool": int(head[14]),
            "scatter_excluded": int(head[15]),
        }
        if report["upm"] <= 0 or report["tolerance"] <= 0:
            raise ValueError("bad units")
        units: dict[int, dict[str, Any]] = {}
        gaps: list[dict[str, Any]] = []
        skipped, supports_skipped, unchecked, dependents, companions = [], [], [], [], []
        mesh_unit: dict[int, int] = {}
        declared: dict[int, dict[str, Any]] = {}
        for line in lines[1:-1]:
            f = line.split("|")
            kind = f[0]
            if kind == "I" and len(f) == 22:
                idx = int(f[1])
                units[idx] = {
                    "index": idx, "handle": _handle(f[2]), "name": _name(f[3]), "kind": f[4],
                    "group": ({"handle": _handle(f[5]), "name": _name(f[6])} if f[5] != "0" else None),
                    "min_z": _float(f[7]), "verts": int(f[8]), "stride": int(f[9]), "samples": int(f[10]),
                    "hits": int(f[11]), "min_gap": _opt(f[12]), "p10": _opt(f[13]), "p50": _opt(f[14]),
                    "contact": int(f[15]), "min_gap_point": _point(f[16]), "not_movable": f[17],
                    "node_z": _float(f[18]), "ancestors": _handles(f[19]), "classes": f[20].split(",") if f[20] else [],
                    "meshes": _handles(f[21]),
                }
            elif kind == "G" and len(f) == 7:
                gaps.append({"unit": int(f[1]), "support": _handle(f[2]), "support_name": _name(f[3]),
                             "gap": _float(f[4]), "hits": int(f[5]), "point": _point(f[6])})
            elif kind == "P" and len(f) == 5:
                supports_skipped.append({"handle": _handle(f[1]), "name": _name(f[2]), "info": f[3],
                                         "faces": int(f[4]), "used": True})
            elif kind == "Q" and len(f) == 5:
                if f[3] not in SUPPORT_SKIP_REASONS:
                    raise ValueError("unknown support skip reason")
                supports_skipped.append({"handle": _handle(f[1]), "name": _name(f[2]), "reason": f[3],
                                         "detail": f[4], "used": False})
            elif kind == "K" and len(f) == 4:
                if f[3] not in SKIP_REASONS:
                    raise ValueError("unknown skip reason")
                skipped.append({"handle": _handle(f[1]), "name": _name(f[2]), "reason": f[3]})
            elif kind == "X" and len(f) == 3:
                unchecked.append({"unit": int(f[1]), "reason": f[2]})
            elif kind == "R" and len(f) == 4:
                dependents.append({"unit": int(f[1]), "handle": _handle(f[2]), "name": _name(f[3])})
            elif kind == "C" and len(f) == 7:
                companions.append({"unit": int(f[1]), "handle": _handle(f[2]), "name": _name(f[3]),
                                   "not_movable": f[4], "node_z": _float(f[5]), "ancestors": _handles(f[6])})
            elif kind == "D" and len(f) == 5:
                # unit declared (before measuring): index, head handle, name, mesh handles
                declared[int(f[1])] = {"handle": _handle(f[2]), "name": _name(f[3]), "meshes": _handles(f[4])}
                mesh_unit.update({h: int(f[1]) for h in declared[int(f[1])]["meshes"]})
            else:
                raise ValueError("unknown record")
    except (ValueError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Resting check report is malformed: {exc}") from exc
    for u in units.values():
        for h in u["meshes"]:
            mesh_unit[h] = u["index"]
    report.update(units=units, gaps=gaps, skipped=skipped, unchecked=unchecked, dependents=dependents,
                  companions=companions, mesh_unit=mesh_unit, declared=declared,
                  supports_used=[s for s in supports_skipped if s.get("used")],
                  supports_skipped=[s for s in supports_skipped if not s.get("used")])
    return report


def classify(unit: dict[str, Any], tolerance: float) -> str:
    """penetrating, floating, no_support or resting, from the unit's minimum signed gap."""
    gap = unit.get("min_gap")
    if not unit.get("hits") or gap is None:
        return "no_support"
    if gap < -tolerance:
        return "penetrating"
    if gap > tolerance:
        return "floating"
    return "resting"


# ── drop plan ─────────────────────────────────────────────────────────────────

def plan_drops(report: dict[str, Any], *, clearance: float, lift_penetrating: bool = False) -> dict[int, dict[str, Any]]:
    """Per unit: {own, total, gap_now, reason}: Z offsets in scene units, bottom-up.

    A pure Z move shifts every vertical gap exactly: gap' = gap0 + dz_item - dz_support.
    Units are processed by their lowest point, so every item support is planned
    before the items on it (supports start strictly lower). A unit linked under a
    moved unit inherits that move; ``own`` is what it moves by itself.
    """
    tol = report["tolerance"]
    units = report["units"]
    unchecked = {u["unit"] for u in report["unchecked"]}
    by_unit: dict[int, list[dict[str, Any]]] = {}
    for g in report["gaps"]:
        by_unit.setdefault(g["unit"], []).append(g)
    head_unit = {u["handle"]: idx for idx, u in units.items()}
    blocked = {c["unit"] for c in report["companions"] if c["not_movable"]}
    plan: dict[int, dict[str, Any]] = {}
    for idx in sorted(units, key=lambda i: (units[i]["min_z"], i)):
        u = units[idx]
        inherited = 0.0
        for anc in u["ancestors"]:  # nearest first
            if anc in head_unit and head_unit[anc] in plan:
                inherited = plan[head_unit[anc]]["total"]
                break
        gap0 = gap_now = None
        for g in by_unit.get(idx, []):
            sup_unit = report["mesh_unit"].get(g["support"])
            moved = plan[sup_unit]["total"] if sup_unit is not None and sup_unit in plan else 0.0
            gap0 = g["gap"] if gap0 is None else min(gap0, g["gap"])
            value = g["gap"] - moved
            gap_now = value if gap_now is None else min(gap_now, value)
        if gap_now is not None:
            gap_now += inherited
        own, reason = 0.0, ""
        if idx in unchecked:
            reason = "unchecked"
        elif gap_now is None:
            reason = "no_support"
        elif u["not_movable"]:
            reason = u["not_movable"]
        elif idx in blocked:
            reason = "companion_not_movable"
        else:
            # Floating items (and sunk ones, on request) go to clearance; the rest
            # keep the contact they had, following any support that moved.
            fix = gap0 > tol or (lift_penetrating and gap0 < -tol)
            target = clearance if fix else gap0
            if abs(target - gap_now) > tol * 1e-3:
                own = target - gap_now
            if not fix:
                reason = ("follows_support" if own else "resting") if gap0 >= -tol else "penetrating_not_lifted"
        plan[idx] = {"own": own, "total": inherited + own, "inherited": inherited, "gap_now": gap_now,
                     "gap0": gap0, "reason": reason}
    return plan


def moves_and_checks(report: dict[str, Any], plan: dict[int, dict[str, Any]], *, clearance: float
                     ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(moves, verify, companions_moved) for build_apply_script."""
    tol = report["tolerance"]
    units = report["units"]
    moved_heads = {units[i]["handle"] for i, p in plan.items() if p["own"]}
    moves, verify, companions_moved = [], [], []
    supports_of: dict[int, list[int]] = {}
    for g in report["gaps"]:
        supports_of.setdefault(g["unit"], []).append(g["support"])
    for idx, p in plan.items():
        if not p["own"]:
            continue
        u = units[idx]
        moves.append({"handle": u["handle"], "dz": p["own"], "z_before": u["node_z"], "z_after": u["node_z"] + p["total"]})
        verify.append({"unit": idx, "meshes": u["meshes"], "supports": supports_of.get(idx, []),
                       "low": -tol, "high": clearance + tol})
        for c in report["companions"]:
            if c["unit"] != idx or any(a in moved_heads for a in c["ancestors"]):
                continue  # a twin under a moved node moves with it
            moves.append({"handle": c["handle"], "dz": p["own"], "z_before": c["node_z"], "z_after": c["node_z"] + p["own"]})
            companions_moved.append({"of": u["name"], "name": c["name"], "handle": c["handle"]})
    return moves, verify, companions_moved


def parse_apply(raw: str) -> dict[str, Any]:
    """{status, detail, elapsed_ms, verify: [{unit, gap, hits, ok}]} from the apply script."""
    if raw.startswith("__ERROR__|"):
        raise RuntimeError(raw.split("|", 1)[1])
    for marker in APPLY_REFUSALS:
        if raw.startswith(marker):
            return {"status": "refused", "code": APPLY_REFUSALS[marker][0], "detail": APPLY_REFUSALS[marker][1]}
    for marker, code in (("__STALE__|", "SCENE_CHANGED"), ("__GONE__|", "NODE_GONE"), ("__READBACK__|", "READBACK_FAILED")):
        if raw.startswith(marker):
            return {"status": "refused" if code != "READBACK_FAILED" else "rolled_back", "code": code,
                    "detail": raw.split("|", 1)[1].strip()}
    lines = _check_raw(raw, "W|", "Drop")
    try:
        head = lines[0].split("|")
        if len(head) != 4 or head[1] not in ("applied", "rolled_back", "rolled_back_deadline"):
            raise ValueError("bad header")
        verify = []
        for line in lines[1:-1]:
            f = line.split("|")
            if f[0] != "V" or len(f) != 5:
                raise ValueError("unknown record")
            verify.append({"unit": int(f[1]), "gap": _opt(f[2]), "hits": int(f[3]), "ok": f[4] == "1"})
        return {"status": head[1], "elapsed_ms": int(head[2]), "time_budget_ms": int(head[3]), "verify": verify}
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"Drop report is malformed: {exc}") from exc


# ── MAXScript ─────────────────────────────────────────────────────────────────
# Placeholders are @@NAME@@ (no % formatting, so MAXScript's own % stays literal).

_LIBRARY = r'''(
    fn rcElapsed t0 = (
        local d = timeStamp() - t0
        if d < 0 do d += 86400000
        d
    )
    fn rcG v = formattedPrint v format:".7g"
    fn rcP p = if p == undefined then "" else (rcG p.x) + "," + (rcG p.y) + "," + (rcG p.z)
    fn rcH n = formattedPrint ((getHandleByAnim n) as integer64) format:"d"
    fn rcB64 s = (dotNetClass "System.Convert").ToBase64String ((dotNetClass "System.Text.Encoding").UTF8.GetBytes s)
    fn rcIsMesh n = isValidNode n and (isKindOf n GeometryClass) and not (isKindOf n TargetObject) and (canConvertTo n TriMeshGeometry)
    fn rcResolve nm = (
        local m = getNodeByName nm exact:true all:true
        if m.count != 1 do throw ("Node name must resolve uniquely: " + nm)
        m[1]
    )
    -- mesh nodes below n, any depth (nested group members included)
    fn rcMeshDesc n out = (
        for c in n.children do (
            if rcIsMesh c do append out c
            rcMeshDesc c out
        )
        out
    )
    fn rcGroupOf n = (
        local p = n.parent, g = undefined
        while p != undefined and g == undefined do (
            if isGroupHead p do g = p
            p = p.parent
        )
        g
    )
    fn rcIsAncestor a n = (
        local p = n.parent, hit = false
        while p != undefined and not hit do (
            if p == a do hit = true
            p = p.parent
        )
        hit
    )
    fn rcAncestors n = (
        local s = "", p = n.parent
        while p != undefined do (
            s += (if s == "" then "" else ",") + (rcH p)
            p = p.parent
        )
        s
    )
    fn rcScatter n = (
        local c = toLower ((classOf n) as string)
        local hit = false
        for k in #("mesher", "forest", "railclone", "tyflow", "fur", "particle", "pf_source", "scatter", "pcloud", "blizzard", "spray", "snow") while not hit do (
            if (findString c k) != undefined do hit = true
        )
        hit
    )
    fn rcSameTM a b posEps = (
        local ta = a.transform, tb = b.transform
        (distance ta.row1 tb.row1) < 1e-4 and (distance ta.row2 tb.row2) < 1e-4 and \
        (distance ta.row3 tb.row3) < 1e-4 and (distance ta.row4 tb.row4) < posEps
    )
    -- "" when the node may be moved along world Z, else the reason
    fn rcNotMovable n = (
        try (
            if n.isFrozen then "frozen"
            else if (getTransformLockFlags n)[3] then "locked_z"
            else if matchPattern ((classOf n.baseObject) as string) pattern:"Link*" then "linked"
            else if (classOf n.baseObject) == XRefObject then "xref"
            else if (classOf n.transform.controller) != prs then "constrained"
            else if (findItem #(Position_XYZ, Bezier_Position, TCB_Position, Linear_Position) (classOf n.pos.controller)) == 0 then "constrained"
            else if n.pos.isAnimated then "animated"
            else ""
        ) catch ("check_failed")
    )
    fn rcGrid s = (
        local nf = try ((getPolygonCount s)[1]) catch (0)
        local rm = RayMeshGridIntersect()
        rm.Initialize (amax 10 (amin 100 ((ceil ((nf / 2.0) ^ (1.0 / 3.0))) as integer)))
        rm.addNode s
        rm.buildGrid()
        rm
    )
    -- lowest vertex per XY cell over the footprint; #(points, verts, stride, cellW, cellH),
    -- or undefined when the deadline passes
    fn rcSamples meshNodes bmin bmax cells maxVerts t0 limitMs = (
        local ex = bmax.x - bmin.x, ey = bmax.y - bmin.y
        if ex < 1e-6 do ex = 1e-6
        if ey < 1e-6 do ey = 1e-6
        local nx = (sqrt (cells * ex / ey)) as integer
        if nx < 1 do nx = 1
        if nx > cells do nx = cells
        local ny = (cells / nx) as integer
        if ny < 1 do ny = 1
        local cw = ex / nx, ch = ey / ny
        local nCells = nx * ny
        local lowZ = #(), lowX = #(), lowY = #()
        lowZ[nCells] = 1e30
        for k = 1 to nCells do (lowZ[k] = 1e30; lowX[k] = 0.0; lowY[k] = 0.0)
        local verts = 0, stride = 1, expired = false, tick = 0
        for nd in meshNodes while not expired do (
            local m = try (snapshotAsMesh nd renderMesh:false) catch (try (snapshotAsMesh nd) catch (undefined))
            if m != undefined do (
                local nv = getNumVerts m
                verts += nv
                local st = if nv > maxVerts then ((ceil ((nv as float) / maxVerts)) as integer) else 1
                if st > stride do stride = st
                local i = 1
                while i <= nv and not expired do (
                    tick += 1
                    if tick >= 512 do (
                        tick = 0
                        if (rcElapsed t0) > limitMs do expired = true
                    )
                    local p = getVert m i
                    local ix = ((p.x - bmin.x) / cw) as integer + 1
                    local iy = ((p.y - bmin.y) / ch) as integer + 1
                    if ix < 1 do ix = 1
                    if ix > nx do ix = nx
                    if iy < 1 do iy = 1
                    if iy > ny do iy = ny
                    local k = (iy - 1) * nx + ix
                    if p.z < lowZ[k] do (
                        lowZ[k] = p.z
                        lowX[k] = p.x
                        lowY[k] = p.y
                    )
                    i += st
                )
                try (delete m) catch ()
            )
        )
        if expired then undefined else (
            local pts = for k = 1 to nCells where lowZ[k] < 1e29 collect [lowX[k], lowY[k], lowZ[k]]
            #(pts, verts, stride, cw, ch)
        )
    )
    -- signed gap below p: nearest hit distance from p + lift, minus lift; undefined without a hit
    fn rcGap rm p lift maxDrop jit = (
        local q = p + jit
        local cnt = rm.intersectSegment (q + [0, 0, lift]) (q - [0, 0, maxDrop]) true
        if cnt > 0 then (rm.getHitDist (rm.getClosestHit())) - lift else undefined
    )
'''

_DEADLINE = r'''
        local t0 = timeStamp()
        local budgetMs = @@LIMIT_MS@@
        local waitMs = -1
        try (
            local wq = ((dotNetClass "System.DateTimeOffset").UtcNow.ToUnixTimeMilliseconds()) - @@SENT_MS@@L
            if wq >= 0 and wq <= @@MAX_WAIT_MS@@ do waitMs = wq as integer
        ) catch ()
        local limitMs = budgetMs
        if waitMs > 0 do limitMs = budgetMs - waitMs
        if limitMs < 1 do limitMs = -1
        local upm = units.decodeValue "1mm"
        local lift = (@@SINK_MM@@ as float) * upm
        local maxDrop = (@@DROP_MM@@ as float) * upm
'''

_MEASURE_BODY = r'''
    local rms = #(), result = undefined
    try (''' + _DEADLINE + r'''
        local tol = (@@TOL_MM@@ as float) * upm
        if not (tol > 0) do throw "Invalid tolerance after unit conversion"
        local eps = tol * 0.01
        local cells = @@SAMPLES@@
        local maxVerts = @@MAX_VERTS@@
        local maxSupFaces = @@MAX_SUP_FACES@@
        local rigid = @@RIGID@@
        local namesIn = @@NAMES@@
        local supIn = @@SUPPORTS@@
        local exclIn = @@EXCLUDE@@
        local ss = stringStream ""
        local expired = false, stop = "none"
        -- items: names (group heads expand), else the selection; never the whole scene
        local named = namesIn.count > 0
        local roots = if named then (for nm in namesIn collect rcResolve nm) else (for n in selection collect n)
        if roots.count == 0 do throw "Pass names, or select the items to check"
        roots = (for r in roots where isGroupHead r collect r) + (for r in roots where not (isGroupHead r) collect r)
        local unitHead = #(), unitMeshes = #(), claimed = #()
        for r in roots do (
            if isGroupHead r then (
                local ms = for m in (rcMeshDesc r #()) where (findItem claimed m) == 0 collect m
                if ms.count == 0 then (
                    if named do format "K|%|%|%\n" (rcH r) (rcB64 r.name) (if (rcMeshDesc r #()).count == 0 then "empty_group" else "already_included") to:ss
                ) else if rigid then (
                    append unitHead r
                    append unitMeshes ms
                    join claimed ms
                ) else (
                    for m in ms do (
                        append unitHead m
                        append unitMeshes #(m)
                        append claimed m
                    )
                )
            ) else if rcIsMesh r then (
                if (findItem claimed r) == 0 then (
                    append unitHead r
                    append unitMeshes #(r)
                    append claimed r
                ) else if named do format "K|%|%|already_included\n" (rcH r) (rcB64 r.name) to:ss
            ) else if named do format "K|%|%|not_mesh\n" (rcH r) (rcB64 r.name) to:ss
        )
        local nU = unitHead.count
        if nU == 0 do throw "No mesh items to check (non-mesh nodes and empty groups are skipped)"
        if nU > @@MAX_UNITS@@ do throw ("Too many items (" + nU as string + "); narrow names to at most @@MAX_UNITS@@")
        local uMin = #(), uMax = #()
        local gMin = [1e30, 1e30, 1e30], gMax = [-1e30, -1e30, -1e30]
        for u = 1 to nU do (
            local bmin = [1e30, 1e30, 1e30], bmax = [-1e30, -1e30, -1e30]
            for m in unitMeshes[u] do (
                local a = m.min, b = m.max
                bmin.x = amin bmin.x a.x; bmin.y = amin bmin.y a.y; bmin.z = amin bmin.z a.z
                bmax.x = amax bmax.x b.x; bmax.y = amax bmax.y b.y; bmax.z = amax bmax.z b.z
            )
            uMin[u] = bmin
            uMax[u] = bmax
            gMin.x = amin gMin.x bmin.x; gMin.y = amin gMin.y bmin.y; gMin.z = amin gMin.z bmin.z
            gMax.x = amax gMax.x bmax.x; gMax.y = amax gMax.y bmax.y; gMax.z = amax gMax.z bmax.z
            local hs = ""
            for m in unitMeshes[u] do hs += (if hs == "" then "" else ",") + (rcH m)
            format "D|%|%|%|%\n" u (rcH unitHead[u]) (rcB64 unitHead[u].name) hs to:ss
        )
        -- support pool
        local exclAll = #()
        for nm in exclIn do (
            local e = rcResolve nm
            append exclAll e
            join exclAll (rcMeshDesc e #())
        )
        local explicit = supIn.count > 0
        local pool = #(), nScatter = 0
        if explicit then (
            for nm in supIn do (
                local s = rcResolve nm
                local cand = if isGroupHead s then (rcMeshDesc s #()) else #(s)
                for c in cand do (
                    if not (rcIsMesh c) then format "Q|%|%|not_mesh|0\n" (rcH c) (rcB64 c.name) to:ss
                    else if (findItem pool c) == 0 do append pool c
                )
            )
        ) else (
            for n in geometry where rcIsMesh n and not n.isHiddenInVpt and n.renderable do (
                local a = n.min, b = n.max
                if b.z >= gMin.z - maxDrop - lift and a.z <= gMax.z + tol and a.x <= gMax.x and b.x >= gMin.x and a.y <= gMax.y and b.y >= gMin.y do (
                    if (findItem claimed n) == 0 and (rcScatter n) then (nScatter += 1) else append pool n
                )
            )
        )
        pool = for p in pool where (findItem exclAll p) == 0 collect p
        local poolMin = for p in pool collect p.min
        local poolMax = for p in pool collect p.max
        local gridOf = #()
        if pool.count > 0 do gridOf[pool.count] = undefined
        local measured = 0
        local posEps = upm * 0.001
        for u = 1 to nU do (
            local head = unitHead[u]
            if not expired and (rcElapsed t0) > limitMs do (expired = true; stop = "deadline")
            if expired then (format "X|%|deadline\n" u to:ss) else (
                -- reference twins (same base object, same transform) move with the item
                local twins = #()
                for m in unitMeshes[u] do (
                    for d in (refs.dependentNodes m.baseObject) where isValidNode d and d != m and (findItem unitMeshes[u] d) == 0 and (findItem twins d) == 0 and (rcSameTM d m posEps) do (
                        append twins d
                        format "C|%|%|%|%|%|%\n" u (rcH d) (rcB64 d.name) (rcNotMovable d) (rcG d.transform.row4.z) (rcAncestors d) to:ss
                    )
                )
                -- supports: start strictly lower, overlap the footprint, reach within max_drop
                local sup = #()
                for k = 1 to pool.count do (
                    local s = pool[k]
                    if (findItem unitMeshes[u] s) == 0 and (findItem twins s) == 0 and poolMin[k].x <= uMax[u].x and poolMax[k].x >= uMin[u].x and poolMin[k].y <= uMax[u].y and poolMax[k].y >= uMin[u].y do (
                        local dup = false
                        for m in unitMeshes[u] while not dup do (
                            if (rcSameTM s m posEps) do dup = true
                        )
                        if dup then (format "Q|%|%|coincident|%\n" (rcH s) (rcB64 s.name) u to:ss) else (
                            if poolMin[k].z < uMin[u].z - eps and poolMax[k].z >= uMin[u].z - maxDrop - eps do (
                                local related = false
                                for m in unitMeshes[u] while not related do (
                                    if (rcIsAncestor s m) or (rcIsAncestor m s) do related = true
                                )
                                if not related do append sup k
                            )
                        )
                    )
                )
                local supOk = #()
                for k in sup while not expired do (
                    if gridOf[k] == undefined do (
                        local nf = try ((getPolygonCount pool[k])[1]) catch (0)
                        if nf == 0 then (
                            gridOf[k] = false
                            format "Q|%|%|empty_mesh|0\n" (rcH pool[k]) (rcB64 pool[k].name) to:ss
                        ) else if (not explicit) and nf > maxSupFaces then (
                            gridOf[k] = false
                            format "Q|%|%|face_limit|%\n" (rcH pool[k]) (rcB64 pool[k].name) nf to:ss
                        ) else if (rcElapsed t0) > limitMs then (expired = true; stop = "deadline") else (
                            local rm = rcGrid pool[k]
                            append rms rm
                            gridOf[k] = rm
                            format "P|%|%|%|%\n" (rcH pool[k]) (rcB64 pool[k].name) ((classOf pool[k].baseObject) as string) nf to:ss
                        )
                    )
                    if gridOf[k] != undefined and gridOf[k] != false do append supOk k
                )
                local smp = if expired then undefined else (rcSamples unitMeshes[u] uMin[u] uMax[u] cells maxVerts t0 limitMs)
                if smp == undefined then (
                    expired = true
                    stop = "deadline"
                    format "X|%|deadline\n" u to:ss
                ) else (
                    local pts = smp[1]
                    local jit = [smp[4] * 0.0011, smp[5] * 0.0013, 0.0]
                    local best = for p in pts collect 1e30
                    local gLines = #()
                    for k in supOk while not expired do (
                        local sMin = 1e30, sHits = 0, sPt = undefined
                        for i = 1 to pts.count while not expired do (
                            if (mod i 64) == 0 and (rcElapsed t0) > limitMs do expired = true
                            local g = rcGap gridOf[k] pts[i] lift maxDrop jit
                            if g != undefined do (
                                sHits += 1
                                if g < sMin do (sMin = g; sPt = pts[i])
                                if g < best[i] do best[i] = g
                            )
                        )
                        if sHits > 0 do append gLines ("G|" + u as string + "|" + (rcH pool[k]) + "|" + (rcB64 pool[k].name) + "|" + (rcG sMin) + "|" + sHits as string + "|" + (rcP sPt))
                    )
                    if expired then (
                        stop = "deadline"
                        format "X|%|deadline\n" u to:ss
                    ) else (
                        for gl in gLines do format "%\n" gl to:ss
                        local gaps = #(), minGap = 1e30, minPt = undefined, nContact = 0
                        for i = 1 to pts.count where best[i] < 1e29 do (
                            append gaps best[i]
                            if best[i] < minGap do (minGap = best[i]; minPt = pts[i])
                            if (abs best[i]) <= tol do nContact += 1
                        )
                        sort gaps
                        local p10 = if gaps.count > 0 then gaps[amax 1 ((ceil (gaps.count * 0.1)) as integer)] else 1e30
                        local p50 = if gaps.count > 0 then gaps[amax 1 ((ceil (gaps.count * 0.5)) as integer)] else 1e30
                        local grp = rcGroupOf head
                        local classes = ""
                        for m in unitMeshes[u] do (
                            local c = (classOf m.baseObject) as string
                            if (findString classes c) == undefined do classes += (if classes == "" then "" else ",") + c
                        )
                        local hs = ""
                        for m in unitMeshes[u] do hs += (if hs == "" then "" else ",") + (rcH m)
                        format "I|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%\n" u (rcH head) (rcB64 head.name) \
                            (if isGroupHead head then "group" else "node") \
                            (if grp == undefined then "0" else rcH grp) (if grp == undefined then "" else rcB64 grp.name) \
                            (rcG uMin[u].z) smp[2] smp[3] pts.count gaps.count (rcG minGap) (rcG p10) (rcG p50) nContact (rcP minPt) \
                            (rcNotMovable head) (rcG head.transform.row4.z) (rcAncestors head) classes hs to:ss
                        measured += 1
                    )
                )
                -- nodes outside the set that sit on this item
                if not explicit do (
                    for k = 1 to pool.count do (
                        local s = pool[k]
                        if (findItem claimed s) == 0 and (findItem twins s) == 0 and poolMin[k].z >= uMin[u].z + eps and poolMin[k].z <= uMax[u].z + tol and poolMin[k].x <= uMax[u].x and poolMax[k].x >= uMin[u].x and poolMin[k].y <= uMax[u].y and poolMax[k].y >= uMin[u].y do (
                            format "R|%|%|%\n" u (rcH s) (rcB64 s.name) to:ss
                        )
                    )
                )
            )
        )
        result = "U|" + (rcG tol) + "|" + (rcG lift) + "|" + (rcG maxDrop) + "|" + (units.SystemType as string) + "|" + (rcG upm) + "|" + nU as string + "|" + measured as string + "|" + (rcElapsed t0) as string + "|" + budgetMs as string + "|" + waitMs as string + "|" + stop + "|" + ((sliderTime as integer) / TicksPerFrame) as string + "|" + (animButtonState as string) + "|" + pool.count as string + "|" + nScatter as string + "\n" + (ss as string) + "END\n"
    ) catch (
        result = "__ERROR__|" + (getCurrentException() as string)
    )
    for r in rms where r != undefined do try (r.free()) catch ()
    result
)'''

_APPLY_BODY = r'''
    local rms = #(), result = undefined, ownsHold = false
    try (''' + _DEADLINE + r'''
        local eps = @@EPS@@ as float
        local cells = @@SAMPLES@@
        local maxVerts = @@MAX_VERTS@@
        local moves = @@MOVES@@
        local checks = @@VERIFY@@
        if (limitMs - (rcElapsed t0)) < @@MIN_APPLY_MS@@ then (result = "__DEADLINE__") \
        else if theHold.Holding() then (result = "__BUSY__") \
        else if animButtonState then (result = "__AUTOKEY__") \
        else (
            local nodesM = #(), gone = undefined, stale = undefined
            for mv in moves do (
                local n = getAnimByHandle mv[1]
                if not (isValidNode n) then (if gone == undefined do gone = mv[1] as string) else (
                    append nodesM n
                    if stale == undefined and (abs (n.transform.row4.z - mv[3])) > eps do stale = n.name
                )
            )
            if gone != undefined then (result = "__GONE__|" + gone) \
            else if stale != undefined then (result = "__STALE__|" + stale) \
            else (
                theHold.Begin()
                ownsHold = true
                for k = 1 to moves.count do (in coordsys world move nodesM[k] [0, 0, moves[k][2]])
                local bad = undefined
                for k = 1 to moves.count while bad == undefined do (
                    if (abs (nodesM[k].transform.row4.z - moves[k][4])) > eps do bad = nodesM[k].name
                )
                if bad != undefined then (
                    theHold.Cancel()
                    ownsHold = false
                    result = "__READBACK__|" + bad
                ) else (
                    -- re-measure every moved item against fresh grids of its supports
                    local ss = stringStream ""
                    local allOk = true, expired = false
                    for c in checks while not expired do (
                        local ms = for h in c[2] collect (getAnimByHandle h)
                        ms = for m in ms where isValidNode m collect m
                        local bmin = [1e30, 1e30, 1e30], bmax = [-1e30, -1e30, -1e30]
                        for m in ms do (
                            local a = m.min, b = m.max
                            bmin.x = amin bmin.x a.x; bmin.y = amin bmin.y a.y; bmin.z = amin bmin.z a.z
                            bmax.x = amax bmax.x b.x; bmax.y = amax bmax.y b.y; bmax.z = amax bmax.z b.z
                        )
                        local smp = if ms.count == 0 then undefined else (rcSamples ms bmin bmax cells maxVerts t0 limitMs)
                        if smp == undefined then (expired = true) else (
                            local pts = smp[1]
                            local jit = [smp[4] * 0.0011, smp[5] * 0.0013, 0.0]
                            local best = 1e30, hits = 0
                            for h in c[3] while not expired do (
                                local s = getAnimByHandle h
                                if isValidNode s do (
                                    local rm = rcGrid s
                                    append rms rm
                                    for i = 1 to pts.count while not expired do (
                                        if (mod i 64) == 0 and (rcElapsed t0) > limitMs do expired = true
                                        local g = rcGap rm pts[i] lift maxDrop jit
                                        if g != undefined do (
                                            hits += 1
                                            if g < best do best = g
                                        )
                                    )
                                )
                            )
                            local ok = hits > 0 and best >= c[4] and best <= c[5]
                            if not ok do allOk = false
                            format "V|%|%|%|%\n" c[1] (rcG best) hits (if ok then 1 else 0) to:ss
                        )
                    )
                    local status = "applied"
                    if expired then (
                        theHold.Cancel()
                        status = "rolled_back_deadline"
                    ) else if not allOk then (
                        theHold.Cancel()
                        status = "rolled_back"
                    ) else (
                        theHold.Accept "@@LABEL@@"
                    )
                    ownsHold = false
                    result = "W|" + status + "|" + (rcElapsed t0) as string + "|" + budgetMs as string + "\n" + (ss as string) + "END\n"
                )
            )
        )
    ) catch (
        if ownsHold and theHold.Holding() do theHold.Cancel()
        result = "__ERROR__|" + (getCurrentException() as string)
    )
    for r in rms where r != undefined do try (r.free()) catch ()
    result
)'''
