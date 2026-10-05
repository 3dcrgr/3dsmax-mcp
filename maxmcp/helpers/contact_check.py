"""Pairwise contact and interpenetration checks between evaluated meshes.

The MAXScript side measures; this module validates input, builds the script,
parses its line report and classifies each pair. Classification lives here so
it is deterministic and testable without 3ds Max.

Measurements per candidate pair, in world space and scene units:

* ``depth``: the largest distance from a vertex that lies inside the other
  (closed) mesh to that mesh's surface. A lower bound on penetration depth.
* ``gap``: the smallest distance from a vertex outside the other mesh to its
  surface. Vertex to surface, so it is an upper bound on the true clearance.
* ``crossings``: edges whose two endpoints are both clear of the other
  surface yet cross it. Catches meshes that pass through each other with no
  vertex inside, such as two crossing thin plates, and meshes that pierce an
  open surface, which has no inside.

Thresholds (``tolerance``, ``near_gap``) are always given in millimetres and
converted to scene units in MAXScript with ``units.decodeValue "1mm"``, the
same path for defaults and explicit values.

Cost control: after the cheap bounding-box pass the script estimates the work
(sum over candidate pairs of the two vertex counts), measures pairs cheapest
first until ``work_budget`` is used, and stops at a wall-clock deadline
(``time_budget_s``) checked between pairs and inside the per-vertex loops.
The deadline counts from when Python built the request (``sent_ms``), so time
spent queued behind other Max work is subtracted. Pairs it did not measure are
reported, never guessed.
"""
from __future__ import annotations

import base64
import math
import time
from typing import Any

from .maxscript import safe_string

STATUSES = ("penetrating", "intersecting", "touching", "near_gap", "separate")

DEFAULT_TOLERANCE_MM = 0.1
DEFAULT_NEAR_GAP_MM = 10.0
MAX_THRESHOLD_MM = 100_000.0  # 100 m
MIN_TOLERANCE_MM = 1e-6
DEFAULT_WORK_BUDGET = 2_000_000
MAX_WORK_BUDGET = 1_000_000_000
DEFAULT_TIME_BUDGET_S = 30.0
# The caller that gives up first is the MCP host (often 60 s), not MaxClient
# (120 s, plus grace), so the cap stays well under 60 s.
MAX_TIME_BUDGET_S = 45.0
TIMEOUT_MARGIN_S = 20.0  # time_budget_s must stay this far below the request timeout
MAX_QUEUE_WAIT_MS = 600_000  # a larger send-to-start difference is clock skew, ignored

SKIP_REASONS = {
    "not_mesh": "not mesh-convertible geometry",
    "no_mesh": "no evaluated mesh",
    "empty_mesh": "evaluated mesh has no faces",
    "face_limit": "face count above max_faces",
    "deadline": "time budget ran out before its mesh was read",
}
UNCHECKED_REASONS = ("work_budget", "deadline", "skipped_node")
STOP_REASONS = ("none", "work_budget", "deadline", "deadline_search")

_BIG = 1e29  # MAXScript side uses 1e30 as "not measured"
_UNCHECKED_LIMIT = 50


def classify(depth: float, crossings: int, gap: float | None, tolerance: float, near_gap: float) -> str:
    """Return one of STATUSES. Penetration wins over contact, contact over gap."""
    if depth > tolerance:
        return "penetrating"
    if crossings > 0:
        return "intersecting"
    if gap is None:
        return "separate"
    if gap <= tolerance:
        return "touching"
    if gap <= near_gap:
        return "near_gap"
    return "separate"


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a nonnegative finite number of millimetres (0 = default)")
    if value > MAX_THRESHOLD_MM:
        raise ValueError(f"{label} must be at most {MAX_THRESHOLD_MM:g} mm")
    return float(value)


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


def resolve_thresholds(tolerance: Any, near_gap: Any) -> tuple[float, float]:
    """Return (tolerance_mm, near_gap_mm) with defaults applied; 0 means default.

    An explicit tolerance above the default near_gap raises near_gap to it; an
    explicit near_gap below the effective tolerance is an error.
    """
    tol = _number(tolerance, "tolerance")
    near = _number(near_gap, "near_gap")
    if tol and tol < MIN_TOLERANCE_MM:
        raise ValueError(f"tolerance must be at least {MIN_TOLERANCE_MM:g} mm (or 0 for the default)")
    tol_mm = tol or DEFAULT_TOLERANCE_MM
    if near:
        if near < tol_mm:
            raise ValueError(
                f"near_gap ({near:g} mm) must be at least tolerance ({tol_mm:g} mm"
                f"{'' if tol else ', the default'}); both are in millimetres")
        near_mm = near
    else:
        near_mm = max(DEFAULT_NEAR_GAP_MM, tol_mm)
    return tol_mm, near_mm


def validate_budgets(work_budget: Any, time_budget_s: Any, request_timeout_s: Any = None) -> tuple[int, float]:
    """Validate the work and time budgets; time_budget_s stays below the request timeout."""
    if isinstance(work_budget, bool) or not isinstance(work_budget, int) or not 1 <= work_budget <= MAX_WORK_BUDGET:
        raise ValueError(f"work_budget must be an integer from 1 to {MAX_WORK_BUDGET}")
    timeout = 120.0
    if (isinstance(request_timeout_s, (int, float)) and not isinstance(request_timeout_s, bool)
            and math.isfinite(request_timeout_s) and request_timeout_s > 0):
        timeout = float(request_timeout_s)
    ceiling = min(MAX_TIME_BUDGET_S, timeout - TIMEOUT_MARGIN_S)
    if ceiling < 1:
        raise ValueError(f"The request timeout ({timeout:g} s) is too short for contact_check")
    if (isinstance(time_budget_s, bool) or not isinstance(time_budget_s, (int, float))
            or not math.isfinite(time_budget_s) or not 1 <= time_budget_s <= ceiling):
        raise ValueError(f"time_budget_s must be from 1 to {ceiling:g} seconds (below the {timeout:g} s request timeout)")
    return work_budget, float(time_budget_s)


def validate_args(names: Any, against: Any, tolerance: Any, near_gap: Any,
                  max_pairs: Any, max_faces: Any) -> tuple[list[str], list[str], float, float, int, int]:
    """Return (names, against, tolerance_mm, near_gap_mm, max_pairs, max_faces)."""
    names_l = _names(names, "names")
    against_l = _names(against, "against")
    if against_l and not names_l:
        raise ValueError("against needs names: pass the nodes to test against it")
    overlap = set(names_l) & set(against_l)
    if overlap:
        raise ValueError(f"A node cannot be in both names and against: {sorted(overlap)}")
    tol_mm, near_mm = resolve_thresholds(tolerance, near_gap)
    for val, label, high in ((max_pairs, "max_pairs", 5000), (max_faces, "max_faces", 2_000_000)):
        if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= high:
            raise ValueError(f"{label} must be an integer from 1 to {high}")
    return names_l, against_l, tol_mm, near_mm, max_pairs, max_faces


def _ms_array(names: list[str]) -> str:
    return "#(" + ", ".join(f'"{safe_string(n)}"' for n in names) + ")"


def _ms_float(value: float) -> str:
    """A MAXScript Float literal; never an Integer literal like ``2``."""
    return format(float(value), ".9f")


def build_script(names: list[str], against: list[str], tolerance_mm: float, near_gap_mm: float,
                 max_pairs: int, max_faces: int, *, work_budget: int = DEFAULT_WORK_BUDGET,
                 time_budget_s: float = DEFAULT_TIME_BUDGET_S, sent_ms: int | None = None) -> str:
    """MAXScript that measures candidate pairs within the budgets and returns a line report.

    Thresholds are millimetres. ``sent_ms`` (Unix epoch ms, default now) starts
    the deadline, so queue wait before the script runs uses up the time budget.
    Read only: no scene node is created, selected, modified or collapsed. The
    evaluated TriMeshes it snapshots are freed on every path.
    """
    if not tolerance_mm > 0 or near_gap_mm < tolerance_mm:
        raise ValueError("build_script needs resolved thresholds in mm (tolerance > 0, near_gap >= tolerance)")
    if sent_ms is None:
        sent_ms = int(time.time() * 1000)
    return _SCRIPT_TEMPLATE % {
        "sent_ms": int(sent_ms),
        "max_wait_ms": MAX_QUEUE_WAIT_MS,
        "names": _ms_array(names),
        "against": _ms_array(against),
        "tol_mm": _ms_float(tolerance_mm),
        "near_mm": _ms_float(near_gap_mm),
        "max_pairs": max_pairs,
        "max_faces": max_faces,
        "work_budget": int(work_budget),
        "limit_ms": int(round(time_budget_s * 1000)),
    }


def _point(text: str) -> list[float] | None:
    if not text:
        return None
    parts = text.split(",")
    if len(parts) != 3:
        raise ValueError("bad point")
    point = [float(p) for p in parts]
    if not all(math.isfinite(v) for v in point):
        raise ValueError("non-finite point")
    return point


def _measure(text: str) -> float | None:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid distance")
    return None if value >= _BIG else value


def _name(b64: str) -> str:
    return base64.b64decode(b64, validate=True).decode("utf-8")


def _handle(text: str) -> int:
    handle = int(text)
    if handle <= 0:
        raise ValueError("invalid node handle")
    return handle


def parse_report(raw: str, *, include_separate: bool = False, limit: int = 100) -> dict[str, Any]:
    """Parse the MAXScript line report into the tool result."""
    if raw.startswith("__ERROR__|"):
        raise RuntimeError(raw.split("|", 1)[1])
    lines = [ln for ln in raw.replace("\r", "").split("\n") if ln]
    if not lines or not lines[0].startswith("U|"):
        raise RuntimeError("Contact check returned no report")
    try:
        head = lines[0].split("|")
        if len(head) != 14 or lines[-1] != "END" or lines.count("END") != 1:
            raise ValueError("report truncated or malformed")
        tol, near, upm = float(head[1]), float(head[2]), float(head[4])
        if not all(math.isfinite(v) for v in (tol, near, upm)) or tol <= 0 or near < tol or upm <= 0:
            raise ValueError(f"invalid thresholds (tolerance {head[1]}, near_gap {head[2]}, units per mm {head[4]})")
        units = head[3]
        node_count, pairs_total, pairs_checked = int(head[5]), int(head[6]), int(head[7])
        work_total, work_checked = int(head[8]), int(head[9])
        elapsed, limit_ms, wait_ms, stop = int(head[10]), int(head[11]), int(head[12]), head[13]
        if stop not in STOP_REASONS:
            raise ValueError(f"unknown stop reason {stop!r}")
        if min(node_count, pairs_total, pairs_checked, work_total, work_checked, elapsed, limit_ms) < 0:
            raise ValueError("negative count")
        if not -1 <= wait_ms <= MAX_QUEUE_WAIT_MS:
            raise ValueError("invalid queue wait")
        nodes: dict[int, dict[str, Any]] = {}
        skipped: dict[int, dict[str, Any]] = {}
        pairs: list[dict[str, Any]] = []
        unchecked: list[dict[str, Any]] = []
        seen_pairs: set[tuple[int, int]] = set()
        work_measured = 0

        def pair_key(ha: int, hb: int) -> tuple[int, int]:
            key = (min(ha, hb), max(ha, hb))
            if ha == hb or key in seen_pairs:
                raise ValueError("invalid or repeated pair")
            seen_pairs.add(key)
            return key

        for line in lines[1:-1]:
            parts = line.split("|")
            tag = parts[0]
            if tag == "N":
                if len(parts) != 6 or parts[4] not in {"0", "1"} or int(parts[3]) <= 0 or int(parts[5]) < 0:
                    raise ValueError("invalid mesh record")
                handle = _handle(parts[1])
                if handle in nodes or handle in skipped:
                    raise ValueError("invalid or repeated node")
                nodes[handle] = {
                    "name": _name(parts[2]),
                    "handle": handle,
                    "faces": int(parts[3]),
                    "closed": parts[4] == "1",
                    "verts": int(parts[5]),
                }
            elif tag == "S":
                if len(parts) != 5 or parts[3] not in SKIP_REASONS or int(parts[4]) < 0:
                    raise ValueError("invalid skipped-node record")
                handle = _handle(parts[1])
                if handle in nodes or handle in skipped:
                    raise ValueError("invalid or repeated node")
                skipped[handle] = {"name": _name(parts[2]), "handle": handle, "reason": parts[3],
                                   "faces": int(parts[4])}
            elif tag == "P":
                if len(parts) != 12:
                    raise ValueError("invalid pair record")
                ha, hb = int(parts[1]), int(parts[2])
                pair_key(ha, hb)
                depth = float(parts[3])
                gap = _measure(parts[4])
                if not math.isfinite(depth) or depth < 0 or any(int(p) < 0 for p in parts[5:9]):
                    raise ValueError("invalid measurement")
                inside = int(parts[5]) + int(parts[6])
                crossings = int(parts[7]) + int(parts[8])
                status = classify(depth, crossings, gap, tol, near)
                work_measured += nodes[ha]["verts"] + nodes[hb]["verts"]
                pairs.append({
                    "a": nodes[ha]["name"], "b": nodes[hb]["name"],
                    "a_handle": ha, "b_handle": hb,
                    "status": status,
                    "depth": round(depth, 6) if depth > 0 else 0.0,
                    "gap": None if gap is None else round(gap, 6),
                    "inside_vertices": inside,
                    "edge_crossings": crossings,
                    "depth_point": _point(parts[9]),
                    "gap_point": _point(parts[10]),
                    "crossing_point": _point(parts[11]),
                })
            elif tag == "X":
                if len(parts) != 5 or parts[4] not in UNCHECKED_REASONS or int(parts[3]) < 0:
                    raise ValueError("invalid unchecked-pair record")
                ha, hb = int(parts[1]), int(parts[2])
                pair_key(ha, hb)
                known = {**skipped, **nodes}
                unchecked.append({"a": known[ha]["name"], "b": known[hb]["name"],
                                  "a_handle": ha, "b_handle": hb,
                                  "work": int(parts[3]), "reason": parts[4]})
            else:
                raise ValueError(f"unknown record {tag!r}")
        meshed = len(nodes) + sum(1 for s in skipped.values() if s["reason"] != "not_mesh")
        if (node_count < 2 or node_count < meshed or pairs_checked != len(pairs)
                or pairs_total != len(pairs) + len(unchecked) or work_checked != work_measured
                or work_total != work_measured + sum(u["work"] for u in unchecked)):
            raise ValueError("report counts do not match measurements")
        referenced = {h for pair in seen_pairs for h in pair}
        if set(nodes) | {h for h, s in skipped.items() if s["reason"] != "not_mesh"} != referenced:
            raise ValueError("unmeasured node record")
        if stop == "work_budget" and not any(u["reason"] == "work_budget" for u in unchecked):
            raise ValueError("work budget stop without skipped pairs")
    except (ValueError, IndexError, KeyError, UnicodeError) as exc:
        raise RuntimeError(f"Invalid contact check report: {exc}") from exc

    order = {s: i for i, s in enumerate(STATUSES)}
    pairs.sort(key=lambda p: (order[p["status"]], -p["depth"], p["gap"] if p["gap"] is not None else math.inf))
    summary = {s: sum(1 for p in pairs if p["status"] == s) for s in STATUSES}
    shown = [p for p in pairs if include_separate or p["status"] != "separate"]
    open_nodes = [n["name"] for n in nodes.values() if not n["closed"]]
    unchecked.sort(key=lambda u: (-u["work"], u["a"], u["b"]))
    skipped_list = [{"name": s["name"], "reason": s["reason"]} for s in skipped.values()]
    heaviest = _heaviest_nodes(unchecked, nodes)
    complete = stop == "none" and not unchecked
    return {
        "units": units,
        "tolerance": tol,
        "near_gap": near,
        "tolerance_mm": round(tol / upm, 6),
        "near_gap_mm": round(near / upm, 6),
        "nodes_checked": node_count,
        "candidate_pairs": pairs_total,
        "pairs_total": pairs_total,
        "pairs_checked": pairs_checked,
        "work_estimate": work_total,
        "work_checked": work_checked,
        "summary": summary,
        "pairs": shown[:limit],
        "pairs_truncated": max(0, len(shown) - limit),
        "open_meshes": open_nodes,
        "elapsed_ms": elapsed,
        "time_budget_ms": limit_ms,
        "queue_wait_ms": None if wait_ms < 0 else wait_ms,
        "complete": complete,
        "stop_reason": None if stop == "none" else stop,
        "skipped_nodes": skipped_list,
        "unchecked_pairs": [{k: u[k] for k in ("a", "b", "work", "reason")} for u in unchecked[:_UNCHECKED_LIMIT]],
        "unchecked_pairs_truncated": max(0, len(unchecked) - _UNCHECKED_LIMIT),
        "heaviest_nodes": heaviest,
        "warnings": _warnings(stop, skipped_list, unchecked, heaviest, pairs_checked, pairs_total, limit_ms,
                              wait_ms),
    }


def _heaviest_nodes(unchecked: list[dict[str, Any]], nodes: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Measured nodes in unchecked pairs, most vertices first (the likely cause of a budget stop)."""
    counts: dict[int, int] = {}
    for u in unchecked:
        for h in (u["a_handle"], u["b_handle"]):
            if h in nodes:
                counts[h] = counts.get(h, 0) + 1
    ranked = sorted(counts, key=lambda h: (-nodes[h]["verts"], nodes[h]["name"]))[:5]
    return [{"name": nodes[h]["name"], "verts": nodes[h]["verts"], "faces": nodes[h]["faces"],
             "unchecked_pairs": counts[h]} for h in ranked]


def _short_list(items: list[str], limit: int = 8) -> str:
    text = ", ".join(items[:limit])
    return text + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _warnings(stop: str, skipped: list[dict[str, Any]], unchecked: list[dict[str, Any]],
              heaviest: list[dict[str, Any]], checked: int, total: int, limit_ms: int,
              wait_ms: int = -1) -> list[str]:
    out: list[str] = []
    by_reason: dict[str, list[str]] = {}
    for s in skipped:
        by_reason.setdefault(s["reason"], []).append(s["name"])
    for reason, names in by_reason.items():
        out.append(f"Skipped {len(names)} node(s), {SKIP_REASONS[reason]}: {_short_list(names)}")
    heavy = _short_list([f"{h['name']} ({h['verts']} verts)" for h in heaviest], 5)
    if stop in ("deadline", "deadline_search"):
        queued = f" ({wait_ms / 1000:.1f} s of it spent queued behind other Max work)" if wait_ms > 0 else ""
        out.append(f"Stopped at the {limit_ms / 1000:g} s time budget{queued}: {checked} of {total}"
                   f"{'+' if stop == 'deadline_search' else ''} candidate pairs checked; results cover only those."
                   + (f" Heaviest unchecked: {heavy}." if heaviest else ""))
    n_budget = sum(1 for u in unchecked if u["reason"] == "work_budget")
    if n_budget:
        out.append(f"Work budget reached: {n_budget} pair(s) not checked (cheapest pairs go first)."
                   + (f" Heaviest unchecked: {heavy}." if heaviest else "")
                   + " Raise work_budget/time_budget_s or narrow names/against.")
    n_skip = sum(1 for u in unchecked if u["reason"] == "skipped_node")
    if n_skip:
        out.append(f"{n_skip} candidate pair(s) involve a skipped node and were not checked.")
    return out


# MAXScript. %% escapes a literal percent for Python formatting.
_SCRIPT_TEMPLATE = r'''(
    -- odd number of distinct surface hits along a ray = point is inside
    fn ccOddHits rm p dir eps = (
        local n = rm.intersectRay p dir true
        local ds = for i = 1 to n collect (rm.getHitDist i)
        ds = for d in ds where d > eps collect d
        sort ds
        local cnt = 0, last = -1e30
        for d in ds do (if (d - last) > eps do (cnt += 1; last = d))
        (mod cnt 2) == 1
    )
    fn ccBoxOverlap a b pad = (
        a[1].x - pad <= b[2].x and b[1].x - pad <= a[2].x and \
        a[1].y - pad <= b[2].y and b[1].y - pad <= a[2].y and \
        a[1].z - pad <= b[2].z and b[1].z - pad <= a[2].z
    )
    -- milliseconds since t0; timeStamp() wraps at midnight
    fn ccElapsed t0 = (
        local d = timeStamp() - t0
        if d < 0 do d += 86400000
        d
    )
    -- vertices and edges of mesh A measured against the surface of B.
    -- Returns undefined when the deadline passes; a partial pair is never reported.
    fn ccOneWay mA bbB rmB mpB closedB tol near eps t0 limitMs = (
        local nv = getNumVerts mA
        local dist = #(), inside = #()
        if nv > 0 do (dist[nv] = undefined; inside[nv] = false)
        local minGap = 1e30, maxDepth = 0.0, nInside = 0
        local gapPt = undefined, depthPt = undefined
        local dirs = #(normalize [0.5773502, 0.5812310, 0.5733181], normalize [-0.6112, 0.2317, 0.7567])
        local expired = false, tick = 0
        for i = 1 to nv do if not expired do (
            tick += 1
            if tick >= 256 do (tick = 0; if (ccElapsed t0) > limitMs do expired = true)
            if not expired do (
                local p = getVert mA i
                inside[i] = false
                if p.x >= bbB[1].x - near and p.x <= bbB[2].x + near and p.y >= bbB[1].y - near and p.y <= bbB[2].y + near and p.z >= bbB[1].z - near and p.z <= bbB[2].z + near do (
                    local d = if (mpB.closestFace p doubleSided:true) then mpB.getHitDist() else 1e30
                    dist[i] = d
                    local isIn = false
                    if closedB and d > tol and p.x > bbB[1].x and p.x < bbB[2].x and p.y > bbB[1].y and p.y < bbB[2].y and p.z > bbB[1].z and p.z < bbB[2].z do (
                        isIn = (ccOddHits rmB p dirs[1] eps) and (ccOddHits rmB p dirs[2] eps)
                    )
                    inside[i] = isIn
                    if isIn then (
                        nInside += 1
                        if d > maxDepth do (maxDepth = d; depthPt = p)
                    ) else (
                        if d < minGap do (minGap = d; gapPt = p)
                    )
                )
            )
        )
        local nCross = 0, crossPt = undefined
        local visited = dotNetObject "System.Collections.Hashtable"
        for fi = 1 to (getNumFaces mA) do if not expired do (
            tick += 1
            if tick >= 256 do (tick = 0; if (ccElapsed t0) > limitMs do expired = true)
            if not expired do (
                local f = getFace mA fi
                local ids = #(f.x as integer, f.y as integer, f.z as integer)
                for k = 1 to 3 do (
                    local i1 = ids[k], i2 = ids[(mod k 3) + 1]
                    -- unmeasured endpoints lie outside B's padded box, so they are clear of B
                    local d1 = dist[i1], d2 = dist[i2]
                    if (d1 == undefined or d1 > tol) and (d2 == undefined or d2 > tol) and not inside[i1] and not inside[i2] do (
                        local p1 = getVert mA i1, p2 = getVert mA i2
                        if (amin p1.x p2.x) <= bbB[2].x and (amax p1.x p2.x) >= bbB[1].x and (amin p1.y p2.y) <= bbB[2].y and (amax p1.y p2.y) >= bbB[1].y and (amin p1.z p2.z) <= bbB[2].z and (amax p1.z p2.z) >= bbB[1].z do (
                            -- Deduplicate only edges that pass the cheap tests above; both visits
                            -- of an edge pass or fail them alike. Boundary edges occur once;
                            -- winding is not a deduplication key.
                            local lo = amin i1 i2, hi = amax i1 i2
                            local edgeKey = (lo as integer64) * (nv as integer64) + hi
                            if not (visited.ContainsKey edgeKey) do (
                                visited.Add edgeKey true
                                if (rmB.intersectSegment p1 p2 true) > 0 do (
                                    nCross += 1
                                    if crossPt == undefined do crossPt = p1 + (normalize (p2 - p1)) * (rmB.getHitDist (rmB.getClosestHit()))
                                )
                            )
                        )
                    )
                )
            )
        )
        if expired then undefined else #(minGap, gapPt, maxDepth, depthPt, nInside, nCross, crossPt)
    )
    fn ccP p = if p == undefined then "" else (formattedPrint p.x format:".7g") + "," + (formattedPrint p.y format:".7g") + "," + (formattedPrint p.z format:".7g")
    fn ccG v = formattedPrint v format:".7g"
    fn ccH n = formattedPrint ((getHandleByAnim n) as integer64) format:"d"
    fn ccB64 s = (dotNetClass "System.Convert").ToBase64String ((dotNetClass "System.Text.Encoding").UTF8.GetBytes s)
    fn ccIsMesh n = isValidNode n and (isKindOf n GeometryClass) and not (isKindOf n TargetObject) and (canConvertTo n TriMeshGeometry)
    -- the node, or undefined (appended to skipped) when it is not mesh-convertible
    fn ccResolve nm skipped = (
        local m = getNodeByName nm exact:true all:true
        if m.count != 1 do throw ("Node name must resolve uniquely: " + nm)
        if ccIsMesh m[1] then m[1] else (append skipped m[1]; undefined)
    )
    fn ccCmp a b = if a[1] < b[1] then -1 else if a[1] > b[1] then 1 else (a[2] - b[2])

    local meshes = #(), rms = #(), mps = #()
    local result = undefined
    try (
        local t0 = timeStamp()
        local budgetMs = %(limit_ms)d
        -- The budget runs from when Python sent the request, so time queued behind
        -- other Max work counts. Same-machine clock; a negative or huge difference
        -- (skew) is ignored and the budget then runs from script start.
        local waitMs = -1
        try (
            local wq = ((dotNetClass "System.DateTimeOffset").UtcNow.ToUnixTimeMilliseconds()) - %(sent_ms)dL
            if wq >= 0 and wq <= %(max_wait_ms)d do waitMs = wq as integer
        ) catch ()
        local limitMs = budgetMs
        if waitMs > 0 do limitMs = budgetMs - waitMs
        if limitMs < 1 do limitMs = -1
        -- thresholds arrive in millimetres; defaults and explicit values convert alike
        local upm = units.decodeValue "1mm"
        local tol = (%(tol_mm)s as float) * upm
        local near = (%(near_mm)s as float) * upm
        if not (tol > 0) do throw "Invalid tolerance after unit conversion"
        if near < tol do near = tol
        local eps = tol * 0.01
        local namesIn = %(names)s
        local againstIn = %(against)s
        local isAgainst = againstIn.count > 0
        local skippedIn = #()
        local setA = #(), setB = #()
        if namesIn.count > 0 then (
            setA = for nm in namesIn collect ccResolve nm skippedIn
            setB = for nm in againstIn collect ccResolve nm skippedIn
            setA = for n in setA where n != undefined collect n
            setB = for n in setB where n != undefined collect n
        ) else if selection.count > 0 then (
            setA = for n in selection where ccIsMesh n collect n
        ) else (
            setA = for n in geometry where not n.isHiddenInVpt and ccIsMesh n collect n
        )
        local skipNames = ""
        for n in skippedIn do skipNames += " " + n.name
        if isAgainst and setA.count == 0 do throw ("No mesh-convertible node in names; skipped:" + skipNames)
        if isAgainst and setB.count == 0 do throw ("No mesh-convertible node in against; skipped:" + skipNames)
        local nodes = setA + setB
        local uniqueNodes = #()
        for n in nodes do (
            if (findItem uniqueNodes n) > 0 do throw "The same node resolved more than once; use disjoint unique targets"
            append uniqueNodes n
        )
        if nodes.count > 2000 do throw "Too many nodes; narrow names/against to at most 2000 nodes"
        if nodes.count < 2 do throw ("Need at least two mesh nodes (pass names, select nodes, or leave both empty for all visible geometry)" + (if skipNames == "" then "" else "; skipped (not mesh-convertible):" + skipNames))
        local bbs = for n in nodes collect #(n.min, n.max)
        local stop = "none"
        local expired = false
        -- candidate pairs by padded world bounding boxes
        local pairsI = #(), pairsJ = #()
        local nA = setA.count
        for i = 1 to nA do if not expired do (
            if (ccElapsed t0) > limitMs then (expired = true; stop = "deadline_search") else (
                local jFrom = if isAgainst then nA + 1 else i + 1
                local jTo = if isAgainst then nodes.count else nA
                for j = jFrom to jTo do if ccBoxOverlap bbs[i] bbs[j] near do (
                    if pairsI.count >= %(max_pairs)d do throw "Too many candidate pairs; narrow names/against or raise max_pairs"
                    append pairsI i; append pairsJ j
                )
            )
        )
        local ss = stringStream ""
        for n in skippedIn do format "S|%%|%%|not_mesh|0\n" (ccH n) (ccB64 n.name) to:ss
        local used = #{}
        for k = 1 to pairsI.count do (used[pairsI[k]] = true; used[pairsJ[k]] = true)
        -- evaluated meshes; accelerators are built later, only for pairs that get measured
        local verts = #(), closed = #(), okNode = #(), skipWhy = #()
        for i in used do (
            local reason = undefined, nf = 0
            okNode[i] = false
            if expired or (ccElapsed t0) > limitMs then (
                expired = true
                if stop == "none" do stop = "deadline"
                reason = "deadline"
            ) else (
                local m = try (snapshotAsMesh nodes[i]) catch (undefined)
                if m == undefined then (reason = "no_mesh") else (
                    nf = getNumFaces m
                    verts[i] = getNumVerts m
                    if nf == 0 then (reason = "empty_mesh") else if nf > %(max_faces)d do (reason = "face_limit")
                    if reason == undefined then meshes[i] = m else (try (delete m) catch ())
                )
            )
            if reason == undefined then (
                closed[i] = (meshop.getOpenEdges meshes[i]).numberSet == 0
                okNode[i] = true
                format "N|%%|%%|%%|%%|%%\n" (ccH nodes[i]) (ccB64 nodes[i].name) nf (if closed[i] then 1 else 0) verts[i] to:ss
            ) else (
                skipWhy[i] = reason
                format "S|%%|%%|%%|%%\n" (ccH nodes[i]) (ccB64 nodes[i].name) reason nf to:ss
            )
        )
        -- work estimate: sum over candidate pairs of the two vertex counts
        local order = #()
        local workTotal = 0 as integer64
        for k = 1 to pairsI.count do (
            local i = pairsI[k], j = pairsJ[k]
            local w = (if verts[i] == undefined then 0 else verts[i]) + (if verts[j] == undefined then 0 else verts[j])
            workTotal += w
            if okNode[i] and okNode[j] then (append order #(w, k)) else (
                format "X|%%|%%|%%|%%\n" (ccH nodes[i]) (ccH nodes[j]) w (if skipWhy[i] == "deadline" or skipWhy[j] == "deadline" then "deadline" else "skipped_node") to:ss
            )
        )
        qsort order ccCmp
        local workDone = 0 as integer64, budget = %(work_budget)d as integer64
        local checked = 0, budgetHit = false
        -- cheapest pairs first, until the work budget or the deadline
        for o in order do (
            local w = o[1], k = o[2]
            local i = pairsI[k], j = pairsJ[k]
            local why = undefined
            if not expired and (ccElapsed t0) > limitMs do expired = true
            if expired then (why = "deadline") else if (workDone + w) > budget then (why = "work_budget"; budgetHit = true) else (
                for idx in #(i, j) where rms[idx] == undefined and not expired do (
                    local nfIdx = getNumFaces meshes[idx]
                    local rm = RayMeshGridIntersect()
                    rms[idx] = rm
                    rm.Initialize (amax 10 (amin 100 ((ceil ((nfIdx / 2.0) ^ (1.0/3.0))) as integer)))
                    rm.addNode nodes[idx]
                    rm.buildGrid()
                    local mp = MeshProjIntersect()
                    mps[idx] = mp
                    mp.setNode nodes[idx]
                    mp.build()
                    -- Initialize ClosestFace; ignore this ray result and use rm for rays.
                    mp.intersectRay (bbs[idx][1] - [1,1,1]) [1,0,0] doubleSided:true
                    if (ccElapsed t0) > limitMs do expired = true
                )
                local ab = if expired then undefined else (ccOneWay meshes[i] bbs[j] rms[j] mps[j] closed[j] tol near eps t0 limitMs)
                local ba = if ab == undefined then undefined else (ccOneWay meshes[j] bbs[i] rms[i] mps[i] closed[i] tol near eps t0 limitMs)
                if ba == undefined then (expired = true; why = "deadline") else (
                    local useAB = ab[3] >= ba[3]
                    local gapAB = ab[1] <= ba[1]
                    format "P|%%|%%|%%|%%|%%|%%|%%|%%|%%|%%|%%\n" (ccH nodes[i]) (ccH nodes[j]) \
                        (ccG (amax ab[3] ba[3])) (ccG (amin ab[1] ba[1])) ab[5] ba[5] ab[6] ba[6] \
                        (ccP (if useAB then ab[4] else ba[4])) (ccP (if gapAB then ab[2] else ba[2])) \
                        (ccP (if ab[7] != undefined then ab[7] else ba[7])) to:ss
                    workDone += w
                    checked += 1
                )
            )
            if why != undefined do format "X|%%|%%|%%|%%\n" (ccH nodes[i]) (ccH nodes[j]) w why to:ss
        )
        if expired then (if stop == "none" do stop = "deadline") else if budgetHit do stop = "work_budget"
        result = "U|" + (ccG tol) + "|" + (ccG near) + "|" + (units.SystemType as string) + "|" + (ccG upm) + "|" + nodes.count as string + "|" + pairsI.count as string + "|" + checked as string + "|" + (formattedPrint workTotal format:"d") + "|" + (formattedPrint workDone format:"d") + "|" + (ccElapsed t0) as string + "|" + budgetMs as string + "|" + waitMs as string + "|" + stop + "\n" + (ss as string) + "END\n"
    ) catch (
        result = "__ERROR__|" + (getCurrentException() as string)
    )
    for m in meshes where m != undefined do try (delete m) catch ()
    for r in rms where r != undefined do try (r.free()) catch ()
    for r in mps where r != undefined do try (r.free()) catch ()
    result
)'''
