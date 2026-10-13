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
"""
from __future__ import annotations

import base64
import math
from typing import Any

from .maxscript import safe_string

STATUSES = ("penetrating", "intersecting", "touching", "near_gap", "separate")

_BIG = 1e29  # MAXScript side uses 1e30 as "not measured"


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
        raise ValueError(f"{label} must be a nonnegative finite number (0 = automatic)")
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


def validate_args(names: Any, against: Any, tolerance: Any, near_gap: Any,
                  max_pairs: Any, max_faces: Any) -> tuple[list[str], list[str], float, float, int, int]:
    names_l = _names(names, "names")
    against_l = _names(against, "against")
    if against_l and not names_l:
        raise ValueError("against needs names: pass the nodes to test against it")
    overlap = set(names_l) & set(against_l)
    if overlap:
        raise ValueError(f"A node cannot be in both names and against: {sorted(overlap)}")
    tol = _number(tolerance, "tolerance")
    near = _number(near_gap, "near_gap")
    if tol and near and near < tol:
        raise ValueError("near_gap must be at least tolerance")
    for val, label, high in ((max_pairs, "max_pairs", 5000), (max_faces, "max_faces", 2_000_000)):
        if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= high:
            raise ValueError(f"{label} must be an integer from 1 to {high}")
    return names_l, against_l, tol, near, max_pairs, max_faces


def _ms_array(names: list[str]) -> str:
    return "#(" + ", ".join(f'"{safe_string(n)}"' for n in names) + ")"


def build_script(names: list[str], against: list[str], tolerance: float, near_gap: float,
                 max_pairs: int, max_faces: int) -> str:
    """MAXScript that measures every candidate pair and returns a line report.

    Read only: no scene node is created, selected, modified or collapsed. The
    evaluated TriMeshes it snapshots are freed on every path.
    """
    return _SCRIPT_TEMPLATE % {
        "names": _ms_array(names),
        "against": _ms_array(against),
        "tol": format(tolerance, ".9g"),
        "near": format(near_gap, ".9g"),
        "max_pairs": max_pairs,
        "max_faces": max_faces,
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


def parse_report(raw: str, *, include_separate: bool = False, limit: int = 100) -> dict[str, Any]:
    """Parse the MAXScript line report into the tool result."""
    if raw.startswith("__ERROR__|"):
        raise RuntimeError(raw.split("|", 1)[1])
    lines = [ln for ln in raw.replace("\r", "").split("\n") if ln]
    if not lines or not lines[0].startswith("U|"):
        raise RuntimeError("Contact check returned no report")
    try:
        head = lines[0].split("|")
        if len(head) != 7 or lines[-1] != "END" or lines.count("END") != 1:
            raise ValueError("report truncated or malformed")
        tol, near = float(head[1]), float(head[2])
        if not math.isfinite(tol) or not math.isfinite(near) or tol <= 0 or near < tol:
            raise ValueError("invalid thresholds")
        units, node_count, candidates, elapsed = head[3], int(head[4]), int(head[5]), int(head[6])
        nodes: dict[int, dict[str, Any]] = {}
        pairs: list[dict[str, Any]] = []
        complete = False
        seen_pairs: set[tuple[int, int]] = set()
        for line in lines[1:]:
            parts = line.split("|")
            tag = parts[0]
            if tag == "N":
                if len(parts) != 5 or parts[4] not in {"0", "1"} or int(parts[3]) <= 0:
                    raise ValueError("invalid mesh record")
                handle = int(parts[1])
                if handle <= 0 or handle in nodes:
                    raise ValueError("invalid or repeated node")
                nodes[handle] = {
                    "name": base64.b64decode(parts[2], validate=True).decode("utf-8"),
                    "handle": handle,
                    "faces": int(parts[3]),
                    "closed": parts[4] == "1",
                }
            elif tag == "P":
                if len(parts) != 12:
                    raise ValueError("invalid pair record")
                ha, hb = int(parts[1]), int(parts[2])
                pair_key = tuple(sorted((ha, hb)))
                if ha == hb or pair_key in seen_pairs:
                    raise ValueError("invalid or repeated pair")
                seen_pairs.add(pair_key)
                depth = float(parts[3])
                gap = _measure(parts[4])
                if not math.isfinite(depth) or depth < 0 or any(int(p) < 0 for p in parts[5:9]):
                    raise ValueError("invalid measurement")
                inside = int(parts[5]) + int(parts[6])
                crossings = int(parts[7]) + int(parts[8])
                status = classify(depth, crossings, gap, tol, near)
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
            elif tag == "END":
                complete = True
            else:
                raise ValueError(f"unknown record {tag!r}")
        if not complete or node_count < len(nodes) or node_count < 2 or candidates != len(pairs) or elapsed < 0:
            raise ValueError("report counts do not match measurements")
        if set(nodes) != {h for pair in seen_pairs for h in pair}:
            raise ValueError("unmeasured node record")
    except (ValueError, IndexError, KeyError, UnicodeError) as exc:
        raise RuntimeError(f"Invalid contact check report: {exc}") from exc

    order = {s: i for i, s in enumerate(STATUSES)}
    pairs.sort(key=lambda p: (order[p["status"]], -p["depth"], p["gap"] if p["gap"] is not None else math.inf))
    summary = {s: sum(1 for p in pairs if p["status"] == s) for s in STATUSES}
    shown = [p for p in pairs if include_separate or p["status"] != "separate"]
    open_nodes = [n["name"] for n in nodes.values() if not n["closed"]]
    return {
        "units": units,
        "tolerance": tol,
        "near_gap": near,
        "nodes_checked": node_count,
        "candidate_pairs": candidates,
        "summary": summary,
        "pairs": shown[:limit],
        "pairs_truncated": max(0, len(shown) - limit),
        "open_meshes": open_nodes,
        "elapsed_ms": elapsed,
        "complete": True,
    }


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
    -- vertices and edges of mesh A measured against the surface of B
    fn ccOneWay mA bbB rmB mpB closedB tol near eps = (
        local nv = getNumVerts mA
        local dist = #(), inside = #()
        if nv > 0 do (dist[nv] = undefined; inside[nv] = false)
        local minGap = 1e30, maxDepth = 0.0, nInside = 0
        local gapPt = undefined, depthPt = undefined
        local dirs = #(normalize [0.5773502, 0.5812310, 0.5733181], normalize [-0.6112, 0.2317, 0.7567])
        for i = 1 to nv do (
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
        local nCross = 0, crossPt = undefined
        local visited = dotNetObject "System.Collections.Hashtable"
        for fi = 1 to (getNumFaces mA) do (
            local f = getFace mA fi
            local ids = #(f.x as integer, f.y as integer, f.z as integer)
            for k = 1 to 3 do (
                local i1 = ids[k], i2 = ids[(mod k 3) + 1]
                -- Boundary edges occur once; winding is not a deduplication key.
                local lo = amin i1 i2, hi = amax i1 i2
                local edgeKey = (lo as integer64) * (nv as integer64) + hi
                if not (visited.ContainsKey edgeKey) do (
                    visited.Add edgeKey true
                    -- unmeasured endpoints lie outside B's padded box, so they are clear of B
                    local d1 = dist[i1], d2 = dist[i2]
                    if (d1 == undefined or d1 > tol) and (d2 == undefined or d2 > tol) and not inside[i1] and not inside[i2] do (
                        local p1 = getVert mA i1, p2 = getVert mA i2
                        if (amin p1.x p2.x) <= bbB[2].x and (amax p1.x p2.x) >= bbB[1].x and (amin p1.y p2.y) <= bbB[2].y and (amax p1.y p2.y) >= bbB[1].y and (amin p1.z p2.z) <= bbB[2].z and (amax p1.z p2.z) >= bbB[1].z and (rmB.intersectSegment p1 p2 true) > 0 do (
                            nCross += 1
                            if crossPt == undefined do crossPt = p1 + (normalize (p2 - p1)) * (rmB.getHitDist (rmB.getClosestHit()))
                        )
                    )
                )
            )
        )
        #(minGap, gapPt, maxDepth, depthPt, nInside, nCross, crossPt)
    )
    fn ccP p = if p == undefined then "" else (formattedPrint p.x format:".7g") + "," + (formattedPrint p.y format:".7g") + "," + (formattedPrint p.z format:".7g")
    fn ccG v = formattedPrint v format:".7g"
    fn ccIsMesh n = isValidNode n and (isKindOf n GeometryClass) and not (isKindOf n TargetObject) and (canConvertTo n TriMeshGeometry)
    fn ccResolve nm = (
        local m = getNodeByName nm exact:true all:true
        if m.count != 1 do throw ("Node name must resolve uniquely: " + nm)
        if not (ccIsMesh m[1]) do throw ("Not a mesh-convertible geometry node: " + nm)
        m[1]
    )

    local meshes = #(), rms = #(), mps = #()
    local result = undefined
    try (
        local t0 = timeStamp()
        -- Whole numbers format as Integer literals, which formattedPrint misreads.
        local tol = %(tol)s as float
        local near = %(near)s as float
        if tol <= 0 do tol = units.decodeValue "0.1mm"
        if near <= 0 do near = units.decodeValue "10mm"
        if near < tol do near = tol
        local eps = tol * 0.01
        local namesIn = %(names)s
        local againstIn = %(against)s
        local setA = #(), setB = #()
        if namesIn.count > 0 then (
            setA = for nm in namesIn collect ccResolve nm
            setB = for nm in againstIn collect ccResolve nm
        ) else if selection.count > 0 then (
            setA = for n in selection where ccIsMesh n collect n
        ) else (
            setA = for n in geometry where not n.isHiddenInVpt and ccIsMesh n collect n
        )
        local nodes = setA + setB
        local uniqueNodes = #()
        for n in nodes do (
            if (findItem uniqueNodes n) > 0 do throw "The same node resolved more than once; use disjoint unique targets"
            append uniqueNodes n
        )
        if nodes.count > 2000 do throw "Too many nodes; narrow names/against to at most 2000 nodes"
        if nodes.count < 2 do throw "Need at least two mesh nodes (pass names, select nodes, or leave both empty for all visible geometry)"
        local bbs = for n in nodes collect #(n.min, n.max)
        -- candidate pairs by padded world bounding boxes
        local pairsI = #(), pairsJ = #()
        local nA = setA.count
        if setB.count > 0 then (
            for i = 1 to nA do for j = nA + 1 to nodes.count do if ccBoxOverlap bbs[i] bbs[j] near do (
                if pairsI.count >= %(max_pairs)s do throw "Too many candidate pairs; narrow names/against or raise max_pairs"
                append pairsI i; append pairsJ j
            )
        ) else (
            for i = 1 to nA do for j = i + 1 to nA do if ccBoxOverlap bbs[i] bbs[j] near do (
                if pairsI.count >= %(max_pairs)s do throw "Too many candidate pairs; narrow names/against or raise max_pairs"
                append pairsI i; append pairsJ j
            )
        )
        local used = #{}
        for k = 1 to pairsI.count do (used[pairsI[k]] = true; used[pairsJ[k]] = true)
        local closed = #()
        local ss = stringStream ""
        for i in used do (
            local m = snapshotAsMesh nodes[i]
            if m == undefined do throw ("No evaluated mesh: " + nodes[i].name)
            meshes[i] = m
            local nf = getNumFaces m
            if nf == 0 do throw ("Empty evaluated mesh: " + nodes[i].name)
            if nf > %(max_faces)s do throw ("Face limit exceeded on " + nodes[i].name + " (" + nf as string + "); raise max_faces")
            closed[i] = (nf > 0) and ((meshop.getOpenEdges m).numberSet == 0)
            local rm = RayMeshGridIntersect()
            rms[i] = rm
            rm.Initialize (amax 10 (amin 100 ((ceil ((nf / 2.0) ^ (1.0/3.0))) as integer)))
            rm.addNode nodes[i]
            rm.buildGrid()
            local mp = MeshProjIntersect()
            mps[i] = mp
            mp.setNode nodes[i]
            mp.build()
            -- Initialize ClosestFace; ignore this ray result and use rm for rays.
            mp.intersectRay (bbs[i][1] - [1,1,1]) [1,0,0] doubleSided:true
            format "N|%%|%%|%%|%%\n" (formattedPrint ((getHandleByAnim nodes[i]) as integer64) format:"d") ((dotNetClass "System.Convert").ToBase64String ((dotNetClass "System.Text.Encoding").UTF8.GetBytes nodes[i].name)) nf (if closed[i] then 1 else 0) to:ss
        )
        for k = 1 to pairsI.count do (
            local i = pairsI[k], j = pairsJ[k]
            local ab = ccOneWay meshes[i] bbs[j] rms[j] mps[j] closed[j] tol near eps
            local ba = ccOneWay meshes[j] bbs[i] rms[i] mps[i] closed[i] tol near eps
            local useAB = ab[3] >= ba[3]
            local gapAB = ab[1] <= ba[1]
            format "P|%%|%%|%%|%%|%%|%%|%%|%%|%%|%%|%%\n" \
                (formattedPrint ((getHandleByAnim nodes[i]) as integer64) format:"d") \
                (formattedPrint ((getHandleByAnim nodes[j]) as integer64) format:"d") \
                (ccG (amax ab[3] ba[3])) (ccG (amin ab[1] ba[1])) ab[5] ba[5] ab[6] ba[6] \
                (ccP (if useAB then ab[4] else ba[4])) (ccP (if gapAB then ab[2] else ba[2])) \
                (ccP (if ab[7] != undefined then ab[7] else ba[7])) to:ss
        )
        result = "U|" + (ccG tol) + "|" + (ccG near) + "|" + (units.SystemType as string) + "|" + nodes.count as string + "|" + pairsI.count as string + "|" + (timeStamp() - t0) as string + "\n" + (ss as string) + "END\n"
    ) catch (
        result = "__ERROR__|" + (getCurrentException() as string)
    )
    for m in meshes where m != undefined do try (delete m) catch ()
    for r in rms where r != undefined do try (r.free()) catch ()
    for r in mps where r != undefined do try (r.free()) catch ()
    result
)'''
