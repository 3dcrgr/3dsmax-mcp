"""Forest Pack tools: read and edit scattered items, switch edit mode, place library items, check lists.

Facts used here were measured live in 3ds Max 2026 with Forest Pack Pro 9 (version 900); see
docs/fork/forest-pack.md. Scatters are created with scatter_forest_pack (tools/scattering.py).
"""
from __future__ import annotations

import math
import re
from typing import Any

from ..coerce import DictList, StrList
from ..helpers import forest as fh
from ..helpers import forest_library as flib
from ..helpers.forest import ForestError
from ..helpers.precheck import PrecheckBlocked
from ..max_client import DialogBlocked
from ..server import client, mcp

MAX_PAGE = 5000
MAX_EDITS = 5000
MAX_READBACK = 500
LIBRARY_TIMEOUT_S = 300.0
DEFAULT_3D_UNITS_CM = 300.0
ITEM_FIELDS = ("position", "transform", "geom_id", "item_name", "source", "rotation_z", "scale", "size", "seed")
DEFAULT_ITEM_FIELDS = ("position", "transform", "geom_id", "size")
_MAX_FIELDS = ("position", "transform", "rotation_z", "scale", "size", "seed")  # computed in Max, in this order

EDIT_OPS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "move": (frozenset({"index", "position"}), frozenset()),
    "delete": (frozenset({"index"}), frozenset()),
    "create": (frozenset({"position"}), frozenset({"geom_id", "width", "height", "rotation_z", "seed"})),
    "set_size": (frozenset({"index", "width", "height"}), frozenset()),
    "set_width": (frozenset({"index", "width"}), frozenset()),
    "set_height": (frozenset({"index", "height"}), frozenset()),
    "set_rotation": (frozenset({"index", "rotation_z"}), frozenset()),
    "set_seed": (frozenset({"index", "seed"}), frozenset()),
    "set_geom": (frozenset({"index", "geom_id"}), frozenset()),
}


def _send(body: str, *, lib: str | None = None, timeout: float | None = None) -> list[str]:
    script = fh.wrap(body, lib or fh.MXS_LIB)
    response = client.send_command(script, timeout=timeout) if timeout else client.send_command(script)
    return fh.lines(response)


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be true or false")
    return value


def _mxs_bool(value: bool) -> str:
    return "true" if value else "false"


def _viewport_notes(count: int | None, vmax: float | None) -> list[tuple[str, str]]:
    """VIEWPORT_LIMIT_POSSIBLE when trees.count() may be the viewport-thinned build (no estimate to compare)."""
    limit = int(vmax) if vmax is not None and vmax > 0 else None
    return fh.count_checks(None, count, viewport_max=limit, maxdensity=None, candidates=None,
                           render_max=None)["notes"]


# --------------------------------------------------------------------------------------------
# forest_items
# --------------------------------------------------------------------------------------------

_ITEMS_BODY = r'''
local nd = fpForest "@@NAME@@"
fpGuard nd
local t = nd.trees
if @@REFRESH@@ do (
    with undo off (
        local was = nd.surfanim
        nd.surfanim = true
        t.update()
        nd.surfanim = was
    )
)
t.update()
local n = t.count()
local nl = nd.namelist as array, cl = nd.cobjlist as array, cn = nd.conamelist as array, gl = nd.geomlist as array
local nT = nl.count
local offInv = #(), srcOk = #()
for g = 1 to nT do (
    local src = if g <= cl.count then cl[g] else undefined
    if isValidNode src then (append offInv (inverse (fpOffTM src)); append srcOk true) else (append offInv undefined; append srcOk false)
)
local hist = for g = 1 to nT collect 0
local invalid = 0
for i = 0 to n - 1 do (
    local g = t.getGeomID i
    if g >= 1 and g <= nT then hist[g] += 1 else invalid += 1
)
local ss = stringStream ""
format "FPOK\n" to:ss
format "H|%|%|%|%|%|%|%\n" (fpEnc nd.name) (nd.mode as string) n nT invalid (fpF (units.decodeValue "1m")) (fpPropV nd #vmaxitems) to:ss
for g = 1 to nT do (
    local src = if g <= cl.count then cl[g] else undefined
    format "G|%|%|%|%|%|%|%\n" g hist[g] (if g <= gl.count then fpVal gl[g] else "-") (fpB srcOk[g]) (fpEnc (nl[g] as string)) (if g <= cn.count then fpEnc (cn[g] as string) else "") (if isValidNode src then fpEnc src.name else "") to:ss
)
local objTM = nd.objectTransform
local i0 = @@OFFSET@@
local i1 = amin (n - 1) (i0 + @@LIMIT@@ - 1)
for i = i0 to i1 do (
    local g = t.getGeomID i
    local tm = t.getFullTransform i
    local hasSrc = g >= 1 and g <= nT and srcOk[g]
    local piv = if hasSrc then (offInv[g] * tm) else tm
    if @@WORLD@@ do piv = piv * objTM
    local line = "T|" + (i as string) + "|" + (g as string) + "|" + (fpB hasSrc)
    if @@F_POSITION@@ do line += "|" + (fpP piv.row4)
    if @@F_TRANSFORM@@ do line += "|" + (fpTM piv)
    if @@F_ROTATION_Z@@ do line += "|" + (fpF (atan2 piv.row1.y piv.row1.x))
    if @@F_SCALE@@ do line += "|" + (fpF (length piv.row1)) + "," + (fpF (length piv.row2)) + "," + (fpF (length piv.row3))
    if @@F_SIZE@@ do line += "|" + (fpF (t.getWidth i)) + "," + (fpF (t.getHeight i))
    if @@F_SEED@@ do line += "|" + ((t.getSeed i) as string)
    format "%\n" line to:ss
)
ss as string
'''


def _fields(value: Any) -> list[str]:
    if value is None:
        return list(DEFAULT_ITEM_FIELDS)
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("fields must be a non-empty list")
    out: list[str] = []
    for v in value:
        if v not in ITEM_FIELDS:
            raise ValueError(f"unknown field {v!r}; choose from {', '.join(ITEM_FIELDS)}")
        if v not in out:
            out.append(v)
    return out


def _type_rows(report: list[str]) -> dict[int, dict[str, Any]]:
    types: dict[int, dict[str, Any]] = {}
    for line in report:
        f = line.split("|")
        if f[0] == "G":
            geom = fh.num(f[3])
            types[int(f[1])] = {"geom_id": int(f[1]), "name": fh.unb64(f[5]), "source": fh.unb64(f[7]) or None,
                                "source_label": fh.unb64(f[6]) or None, "count": int(f[2]),
                                "geom_type": None if geom is None else int(geom) if geom.is_integer() else geom,
                                "source_missing": f[4] != "1"}
    return types


@mcp.tool()
def forest_items(
    name: str,
    offset: int = 0,
    limit: int = 500,
    space: str = "world",
    fields: StrList | None = None,
    refresh_surfaces: bool = False,
) -> dict[str, Any]:
    """Read a Forest Pack object's items, paged: pivot position, instance-ready transform, item type, size.

    Builds the forest first (Forest Pack builds its item list lazily). offset is 0-based; in procedural mode an
    index is valid until the forest rebuilds (a seed, units or surface change reorders items).

    fields: position, transform, geom_id, item_name, source, rotation_z, scale, size, seed (default: position,
    transform, geom_id, size). geom_id is 1-based into the item list (item_types). transform is 12 floats (rows
    1-4) that place an instance of the source object exactly: inverse(srcOffsetTM) * trees.getFullTransform i *
    forest.objectTransform. srcOffsetTM is the source object's pivot offset (objectOffsetScale, Rot, Pos);
    getFullTransform maps the source's object-space vertices, so removing that offset gives the pivot's TM.
    position is that pivot (surface Z, jitter, rotation and scale included). space="object" returns the same in
    the forest's object space. rotation_z is in degrees, scale is per axis relative to the source object, size is
    the stored [width, height] in scene units. refresh_surfaces pulses surfanim first: Forest Pack caches
    surfaces, so items keep the old Z after a surface moves until then. limit is at most 5000 (~100 bytes each).
    The items are the viewport build: over Max items Viewport (vmaxitems) Forest Pack thins it, so count can be
    lower than what renders (VIEWPORT_LIMIT_POSSIBLE).
    """
    fh.node_name(name, "name")
    offset = fh.integer(offset, "offset", 0, 2**31 - 2)
    limit = fh.integer(limit, "limit", 1, MAX_PAGE)
    if space not in ("world", "object"):
        raise ValueError("space must be 'world' or 'object'")
    wanted = _fields(fields)
    refresh = _bool(refresh_surfaces, "refresh_surfaces")
    flags = {f"F_{f.upper()}": _mxs_bool(f in wanted) for f in _MAX_FIELDS}
    report = _send(fh.fill(_ITEMS_BODY, NAME=fh.b64(name), REFRESH=_mxs_bool(refresh), OFFSET=offset, LIMIT=limit,
                           WORLD=_mxs_bool(space == "world"), **flags))
    head = next((line.split("|") for line in report if line.startswith("H|")), None)
    if head is None:
        raise ForestError("INVALID_READBACK", "forest_items got no header line")
    mode, count, invalid = int(head[2]), int(head[3]), int(head[5])
    types = _type_rows(report)
    present = [f for f in _MAX_FIELDS if f in wanted]
    items: list[dict[str, Any]] = []
    no_source = 0
    for line in report:
        if not line.startswith("T|"):
            continue
        f = line.split("|")
        index, geom_id, ok = int(f[1]), int(f[2]), f[3] == "1"
        item: dict[str, Any] = {"index": index}
        if "geom_id" in wanted:
            item["geom_id"] = geom_id
        row = types.get(geom_id, {})
        if "item_name" in wanted:
            item["item_name"] = row.get("name")
        if "source" in wanted:
            item["source"] = row.get("source")
        for key, raw in zip(present, f[4:]):
            if key == "position":
                item["position"] = fh.floats(raw)
            elif key == "transform":
                item["transform"] = fh.floats(raw)
            elif key == "rotation_z":
                item["rotation_z"] = float(raw)
            elif key == "scale":
                item["scale"] = fh.floats(raw)
            elif key == "size":
                item["size"] = fh.floats(raw)
            elif key == "seed":
                item["seed"] = int(raw)
        if not ok:
            item["no_source"] = True
            if row.get("geom_type") != 1:  # billboards have no source object
                no_source += 1
        items.append(item)
    end = offset + len(items)
    warnings: list[str] = []
    if invalid:
        warnings.append(f"{invalid} items have a geom_id outside the item list; they produce nothing")
    if no_source:
        warnings.append(f"{no_source} returned items have no valid source object; their transform is the raw "
                        "item TM (getFullTransform * objectTransform), not an instance-ready pivot")
    if mode == 0:
        warnings.append("procedural mode: indices are valid until the forest rebuilds")
        warnings += [f"{code}: {detail}" for code, detail in
                     _viewport_notes(count, fh.num(head[7]) if len(head) > 7 else None)]
    return {
        "name": fh.unb64(head[1]),
        "mode": fh.mode_name(mode),
        "count": count,
        "offset": offset,
        "returned": len(items),
        "next_offset": end if end < count else None,
        "truncated": end < count,
        "space": space,
        "units_per_meter": float(head[6]),
        "item_types": [types[k] for k in sorted(types)],
        "items": items,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------------------------
# forest_edit_items
# --------------------------------------------------------------------------------------------

_EDIT_BODY = r'''
fn fpToObj p hasZ inv baseZ = (
    local q = if hasZ then p else [p.x, p.y, baseZ]
    q * inv
)
fn fpWorldAngle t nd i = (
    local cl = nd.cobjlist as array
    local g = t.getGeomID i
    local src = if g >= 1 and g <= cl.count then cl[g] else undefined
    local tm = t.getFullTransform i
    local piv = (if isValidNode src then (inverse (fpOffTM src)) * tm else tm) * nd.objectTransform
    atan2 piv.row1.y piv.row1.x
)
-- Forest Pack adds the stored rotation to the item's random one (live: stored 0 gave 54.3 deg,
-- stored 45 gave 99.3 deg), so store the target minus the random part: rotation_z is final
fn fpSetAngle t nd i target = (
    local randomPart = (fpWorldAngle t nd i) - (radToDeg ((t.getRotation i).z))
    t.setRotation i [0, 0, target - (degToRad randomPart)]
)
-- The ray starts just above the surface's bounding box: from z = 1e7 it missed a plane at z = 0 (live,
-- float precision)
fn fpOnSurface nd p = (
    local hit = false
    local sl = nd.surflist as array
    for s in sl while not hit do (
        if isValidNode s do (
            local z0 = s.max.z + 1.0 + 0.01 * (s.max.z - s.min.z)
            if (intersectRay s (ray [p.x, p.y, z0] [0, 0, -1])) != undefined do hit = true
        )
    )
    hit
)
fn fpItemLine t nd i tag = (
    local cl = nd.cobjlist as array
    local g = t.getGeomID i
    local tm = t.getFullTransform i
    local src = if g >= 1 and g <= cl.count then cl[g] else undefined
    local hasSrc = isValidNode src
    local piv = (if hasSrc then (inverse (fpOffTM src)) * tm else tm) * nd.objectTransform
    local pt = t.getPosition i
    local ptw = if pt == undefined then "" else fpP (pt * nd.objectTransform)
    "C|" + tag + "|" + (i as string) + "|" + (g as string) + "|" + (fpB hasSrc) + "|" + (fpP piv.row4) + "|" + ptw + "|" + (fpF (t.getWidth i)) + "|" + (fpF (t.getHeight i)) + "|" + (fpF (atan2 piv.row1.y piv.row1.x)) + "|" + ((t.getSeed i) as string)
)
if fpBusy() do throw "USER_BUSY: an undo operation is open in 3ds Max; finish it first"
local nd = fpForest "@@NAME@@"
fpGuard nd
local t = nd.trees
local m0 = nd.mode
if m0 != 0 and m0 != 1 do throw ("UNSUPPORTED_MODE: the forest is in mode " + (m0 as string) + "; only procedural (0) and custom edit (1) are supported")
if m0 == 0 and not @@ALLOW@@ do throw "CUSTOM_EDIT_REQUIRED: the forest is procedural, where Forest Pack silently ignores item edits. Pass allow_custom_edit=true to build it and switch it to custom edit; switching back to procedural later discards every edit"
local mods = @@MODS@@
local dels = @@DELETES@@
local adds = @@CREATES@@
local nl = nd.namelist as array, gl = nd.geomlist as array, cl = nd.cobjlist as array
local wl = nd.widthlist as array, hl = nd.heightlist as array
for g in @@GEOM_IDS@@ do (
    if g > nl.count do throw ("BAD_GEOM_ID: geom_id " + (g as string) + " is outside the item list 1.." + (nl.count as string))
    local gt = if g <= gl.count then gl[g] else 0
    if gt != 1 and gt != 2 do throw ("BAD_GEOM_ID: item type " + (g as string) + " has geometry type " + (gt as string) + "; only 1 (billboard) and 2 (custom object) produce items")
    if gt == 2 and not (g <= cl.count and isValidNode cl[g]) do throw ("BAD_GEOM_ID: item type " + (g as string) + " has no source object")
)
local addW = #(), addH = #()
for c in adds do (
    local g = c[3]
    local w = c[4], h = c[5]
    if w <= 0 do w = if g <= wl.count and isKindOf wl[g] Number then wl[g] else 0.0
    if h <= 0 do h = if g <= hl.count and isKindOf hl[g] Number then hl[g] else 0.0
    if w <= 0 or h <= 0 do throw ("BAD_SIZE: item type " + (g as string) + " has no positive width and height to copy; pass width and height")
    append addW w
    append addH h
)
t.update()
local n0 = t.count()
if @@MAX_INDEX@@ >= n0 do throw ("BAD_INDEX: index @@MAX_INDEX@@ is outside 0.." + ((n0 - 1) as string) + " (" + (n0 as string) + " items)")
local switched = false, mismatch = -1, created = #(), offSurface = #()
local pre = stringStream ""
undo "MCP Forest edit items" on (
    local n1 = n0
    if m0 == 0 do (
        nd.mode = 1
        t.update()
        n1 = t.count()
        switched = true
    )
    if n1 != n0 then (
        nd.mode = m0
        t.update()
        mismatch = n1
    ) else (
        local inv = inverse nd.objectTransform
        local baseZ = nd.objectTransform.row4.z
        for i in @@TOUCHED@@ do (
            local pt = t.getPosition i
            format "P|%|%|%|%|%|%|%\n" i (if pt == undefined then "" else fpP (pt * nd.objectTransform)) (fpF (t.getWidth i)) (fpF (t.getHeight i)) (fpF (fpWorldAngle t nd i)) ((t.getSeed i) as string) ((t.getGeomID i) as string) to:pre
        )
        for op in mods where op[1] != #rotation do (
            local i = op[2]
            case op[1] of (
                #move: t.setPosition i (fpToObj op[3] op[4] inv baseZ)
                #width: t.setWidth i op[3]
                #height: t.setHeight i op[3]
                #size: t.setSize i [op[3], op[3], op[4]]
                #seed: t.setSeed i op[3]
                #geom: t.setGeomID i op[3]
            )
        )
        -- rotations last: the random part depends on the seed and the item type
        t.update()
        for op in mods where op[1] == #rotation do fpSetAngle t nd op[2] op[3]
        for d in dels do t.delete d
        for k = 1 to adds.count do (
            local c = adds[k]
            t.create (fpToObj c[1] c[2] inv baseZ) addW[k] addH[k] c[3]
            local j = t.count() - 1
            if c[7] != undefined do t.setSeed j c[7]
            append created j
            if not (fpOnSurface nd c[1]) do append offSurface j
        )
        t.update()
        for k = 1 to adds.count where adds[k][6] != undefined do fpSetAngle t nd created[k] adds[k][6]
        t.update()
    )
)
if mismatch >= 0 do throw ("FREEZE_MISMATCH: the forest built " + (n0 as string) + " items but froze " + (mismatch as string) + "; it was switched back to procedural and nothing was edited")
local ss = stringStream ""
format "FPOK\n" to:ss
format "E|%|%|%|%|%|%\n" (fpEnc nd.name) m0 nd.mode n0 (t.count()) (fpB switched) to:ss
local nAfter = t.count()
for r in @@READBACK@@ where r[2] < nAfter do format "%\n" (fpItemLine t nd r[2] (r[1] as string)) to:ss
for k = 1 to (amin created.count @@MAX_READBACK@@) do format "%\n" (fpItemLine t nd created[k] "new") to:ss
for j in offSurface do format "O|%\n" j to:ss
format "%" (pre as string) to:ss
ss as string
'''


def _position(value: Any, label: str) -> tuple[list[float], bool]:
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise ValueError(f"{label} must be a world position [x, y] or [x, y, z]")
    point = [fh.finite(v, label, -1e9, 1e9) for v in value]
    return (point + [0.0])[:3], len(value) == 3


def _edit_plan(edits: Any) -> dict[str, Any]:
    if isinstance(edits, dict):
        edits = [edits]
    if not isinstance(edits, (list, tuple)) or not edits:
        raise ValueError("edits must be a non-empty list of {op, ...} objects")
    if len(edits) > MAX_EDITS:
        raise ValueError(f"at most {MAX_EDITS} edits per call")
    mods: list[str] = []
    deletes: list[int] = []
    creates: list[str] = []
    geom_ids: set[int] = set()
    modified: list[int] = []
    touched_ops: dict[int, set[str]] = {}
    counts: dict[str, int] = {}
    for k, edit in enumerate(edits):
        label = f"edits[{k}]"
        if not isinstance(edit, dict):
            raise ValueError(f"{label} must be an object with an op")
        op = edit.get("op")
        if op not in EDIT_OPS:
            raise ValueError(f"{label}.op must be one of {', '.join(EDIT_OPS)}")
        required, optional = EDIT_OPS[op]
        keys = set(edit) - {"op"}
        missing, unknown = required - keys, keys - required - optional
        if missing:
            raise ValueError(f"{label} ({op}) needs {', '.join(sorted(missing))}")
        if unknown:
            raise ValueError(f"{label} ({op}) doesn't take {', '.join(sorted(unknown))}")
        counts[op] = counts.get(op, 0) + 1
        index = fh.integer(edit["index"], f"{label}.index", 0, 2**31 - 2) if "index" in edit else None
        size = {key: fh.finite(edit[key], f"{label}.{key}", 0.0, 1e7, low_open=True)
                for key in ("width", "height") if key in edit}
        rotation = (math.radians(fh.finite(edit["rotation_z"], f"{label}.rotation_z", -1e6, 1e6))
                    if "rotation_z" in edit else None)
        seed = fh.integer(edit["seed"], f"{label}.seed", -2**31, 2**31 - 1) if "seed" in edit else None
        geom = fh.integer(edit["geom_id"], f"{label}.geom_id", 1, 100000) if "geom_id" in edit else None
        if geom is not None:
            geom_ids.add(geom)
        if op == "delete":
            if index in deletes:
                raise ValueError(f"{label}: index {index} is deleted twice")
            deletes.append(index)
            continue
        if op == "create":
            point, has_z = _position(edit["position"], f"{label}.position")
            creates.append("#(" + ", ".join([
                fh.mxs_point(point), _mxs_bool(has_z), str(geom or 1),
                fh.mxs_float(size.get("width", -1.0)), fh.mxs_float(size.get("height", -1.0)),
                "undefined" if rotation is None else f"{rotation:.9f}",
                "undefined" if seed is None else str(seed)]) + ")")
            geom_ids.add(geom or 1)
            continue
        if op == "move":
            point, has_z = _position(edit["position"], f"{label}.position")
            mods.append(f"#(#move, {index}, {fh.mxs_point(point)}, {_mxs_bool(has_z)})")
        elif op == "set_size":
            mods.append(f"#(#size, {index}, {fh.mxs_float(size['width'])}, {fh.mxs_float(size['height'])})")
        elif op == "set_width":
            mods.append(f"#(#width, {index}, {fh.mxs_float(size['width'])})")
        elif op == "set_height":
            mods.append(f"#(#height, {index}, {fh.mxs_float(size['height'])})")
        elif op == "set_rotation":
            # the final angle is computed from the item's current transform, so a second rotation of the
            # same item in one call would start from a stale angle
            if "set_rotation" in touched_ops.get(index, set()):
                raise ValueError(f"{label}: index {index} has two set_rotation edits; send one")
            mods.append(f"#(#rotation, {index}, {rotation:.9f})")
        elif op == "set_seed":
            mods.append(f"#(#seed, {index}, {seed})")
        elif op == "set_geom":
            mods.append(f"#(#geom, {index}, {geom})")
        modified.append(index)
        touched_ops.setdefault(index, set()).add(op)
    clash = sorted(set(modified) & set(deletes))
    if clash:
        raise ValueError(f"indices {clash} are both edited and deleted; drop one of the edits")
    deletes.sort(reverse=True)
    order = list(dict.fromkeys(modified))
    readback = [(i, i - sum(1 for d in deletes if d < i)) for i in order[:MAX_READBACK]]
    indices = modified + deletes
    return {
        "mods": "#(" + ", ".join(mods) + ")",
        "deletes": "#(" + ", ".join(str(d) for d in deletes) + ")",
        "creates": "#(" + ", ".join(creates) + ")",
        "geom_ids": "#(" + ", ".join(str(g) for g in sorted(geom_ids)) + ")",
        "max_index": max(indices) if indices else -1,
        "readback": "#(" + ", ".join(f"#({a}, {b})" for a, b in readback) + ")",
        "counts": counts,
        "n_deletes": len(deletes),
        "n_creates": len(creates),
        "deletes_list": list(deletes),
        "touched_ops": touched_ops,
        "touched": "#(" + ", ".join(str(i) for i in sorted(set(modified) | set(deletes))) + ")",
    }


def _revert_edits(plan: dict[str, Any], before: dict[int, dict[str, Any]], created: list[int]) -> list[dict]:
    """Edits that put the items back (deleted items come back at the end of the list)."""
    deletes = plan["deletes_list"]
    out: list[dict[str, Any]] = [{"op": "delete", "index": j} for j in sorted(created, reverse=True)]
    for i, ops in sorted(plan["touched_ops"].items()):
        b = before.get(i)
        if b is None:
            continue
        ni = i - sum(1 for d in deletes if d < i)
        if "move" in ops and b["point"]:
            out.append({"op": "move", "index": ni, "position": b["point"][:2]})
        if ops & {"set_size", "set_width", "set_height"}:
            out.append({"op": "set_size", "index": ni, "width": b["width"], "height": b["height"]})
        if "set_seed" in ops:
            out.append({"op": "set_seed", "index": ni, "seed": b["seed"]})
        if "set_geom" in ops:
            out.append({"op": "set_geom", "index": ni, "geom_id": b["geom_id"]})
        if ops & {"set_rotation", "set_seed", "set_geom"}:
            out.append({"op": "set_rotation", "index": ni, "rotation_z": b["rotation_z"]})
    for i in sorted(deletes):
        b = before.get(i)
        if b is not None and b["point"]:
            out.append({"op": "create", "position": b["point"][:2], "geom_id": b["geom_id"], "width": b["width"],
                        "height": b["height"], "rotation_z": b["rotation_z"], "seed": b["seed"]})
    return out


def _changed_row(f: list[str]) -> dict[str, Any]:
    row: dict[str, Any] = {"index": int(f[2])}
    if f[1] == "new":
        row["created"] = True
    else:
        row["was_index"] = int(f[1])
    row.update(geom_id=int(f[3]), position=fh.floats(f[5]), point=fh.floats(f[6]) if f[6] else None,
               width=float(f[7]), height=float(f[8]), rotation_z=float(f[9]), seed=int(f[10]))
    if f[4] != "1":
        row["no_source"] = True
    return row


@mcp.tool()
def forest_edit_items(name: str, edits: DictList, allow_custom_edit: bool = False) -> dict[str, Any]:
    """Edit Forest Pack items: move, delete, create, set size/width/height/rotation/seed/item type.

    Forest Pack applies item edits only in custom edit mode; in procedural mode it silently ignores them. A
    procedural forest is refused unless allow_custom_edit=true, which builds it and switches it to custom edit
    first (indices stay those of the built forest). Switching back to procedural later discards every edit.

    edits: [{op, index, ...}], indices 0-based as forest_items returns them, all referring to the items before
    this call. move {index, position}; create {position, geom_id=1, width, height, rotation_z, seed} (size
    defaults to the item type's width/height; created items are appended); delete {index}; set_size {index,
    width, height}; set_width; set_height; set_rotation {index, rotation_z} (the final angle in degrees about
    world Z, random rotation included; Forest Pack ignores X and Y); set_seed; set_geom {index, geom_id}
    (1-based). Positions are world [x, y] or [x, y, z] in scene units; Forest Pack projects items onto the
    surfaces, and with random translation on, the pivot lands off the point. Edits run first (rotations last),
    then deletes (highest index first), then creates. Returns counts before/after and the edited items read
    back (point = stored distribution point, position = pivot).

    Forest Pack doesn't record item edits in undo (verified live). undoable is true only when this call
    switched the forest to custom edit: undo then switches it back to procedural, which drops every edit.
    revert is an edit list that puts the items back when passed to forest_edit_items (deleted items come back
    at the end of the list).
    """
    fh.node_name(name, "name")
    allow = _bool(allow_custom_edit, "allow_custom_edit")
    plan = _edit_plan(edits)
    report = _send(fh.fill(
        _EDIT_BODY, NAME=fh.b64(name), ALLOW=_mxs_bool(allow), MODS=plan["mods"], DELETES=plan["deletes"],
        CREATES=plan["creates"], GEOM_IDS=plan["geom_ids"], MAX_INDEX=plan["max_index"],
        READBACK=plan["readback"], MAX_READBACK=MAX_READBACK, TOUCHED=plan["touched"]))
    head = next((line.split("|") for line in report if line.startswith("E|")), None)
    if head is None:
        raise ForestError("INVALID_READBACK", "forest_edit_items got no header line")
    count_before, count_after = int(head[4]), int(head[5])
    changed = [_changed_row(line.split("|")) for line in report if line.startswith("C|")]
    warnings: list[str] = []
    expected = count_before - plan["n_deletes"] + plan["n_creates"]
    if count_after != expected:
        warnings.append(f"expected {expected} items after the edits but the forest has {count_after}")
    moved = {}
    for edit in (edits if isinstance(edits, (list, tuple)) else [edits]):
        if edit.get("op") == "move":
            moved[edit["index"]] = [float(v) for v in edit["position"]]
    for row in changed:
        target = moved.get(row.get("was_index"))
        if target and row.get("point"):
            if math.dist(target[:2], row["point"][:2]) > 1e-3 * max(1.0, abs(target[0]), abs(target[1])):
                warnings.append(f"item {row['index']}: the stored point {row['point'][:2]} differs from the "
                                f"requested {target[:2]}")
            elif math.dist(row["point"][:2], row["position"][:2]) > 1e-3:
                warnings.append(f"item {row['index']}: random translation offsets the pivot from the requested "
                                "point (position is the actual pivot)")
    off = [int(line.split("|")[1]) for line in report if line.startswith("O|")]
    if off:
        warnings.append(f"created items {off} lie off every surface, so they were kept at the given point "
                        "without projection")
    created = [row["index"] for row in changed if row.get("created")]
    before: dict[int, dict[str, Any]] = {}
    for line in report:
        if line.startswith("P|"):
            f = line.split("|")
            before[int(f[1])] = {"point": fh.floats(f[2]) if f[2] else None, "width": float(f[3]),
                                 "height": float(f[4]), "rotation_z": float(f[5]), "seed": int(f[6]),
                                 "geom_id": int(f[7])}
    switched = head[6] == "1"
    if not switched:
        # Forest Pack doesn't record item edits in the undo stack (live): undo can't revert them
        warnings.append("Forest Pack doesn't record item edits in the undo stack, so undo can't revert them; "
                        "pass revert to forest_edit_items to put the items back")
    return {
        "name": fh.unb64(head[1]),
        "mode": fh.mode_name(int(head[3])),
        "switched_to_custom_edit": switched,
        "count_before": count_before,
        "count_after": count_after,
        "applied": plan["counts"],
        "created_indices": created,
        "changed": changed,
        # undo works only when this call switched the forest out of procedural mode: it goes back to procedural
        "undoable": switched,
        "revert": _revert_edits(plan, before, created),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------------------------
# forest_set_mode
# --------------------------------------------------------------------------------------------

_MODE_BODY = r'''
local nd = fpForest "@@NAME@@"
fpGuard nd
local t = nd.trees
local m0 = nd.mode
t.update()
local n0 = t.count()
local target = @@TARGET@@
local changed = false
if m0 != target do (
    if fpBusy() do throw "USER_BUSY: an undo operation is open in 3ds Max; finish it first"
    if target == 1 then (
        if m0 != 0 do throw ("UNSUPPORTED_MODE: the forest is in mode " + (m0 as string) + "; switch it to procedural first")
        undo "MCP Forest custom edit" on (nd.mode = 1)
    ) else (
        if not @@CONFIRM@@ do throw "CONFIRM_DISCARD_REQUIRED: switching to procedural discards every custom edit irreversibly (switching back to custom edit doesn't restore them); pass confirm_discard=true"
        undo "MCP Forest procedural" on (nd.mode = 0)
    )
    t.update()
    changed = true
)
"FPOK\nM|" + (fpEnc nd.name) + "|" + (m0 as string) + "|" + (nd.mode as string) + "|" + (n0 as string) + "|" + ((t.count()) as string) + "|" + (fpB changed)
'''


@mcp.tool()
def forest_set_mode(name: str, mode: str = "custom_edit", confirm_discard: bool = False) -> dict[str, Any]:
    """Switch a Forest Pack object between procedural and custom edit mode (where item edits apply).

    custom_edit builds the forest first, then freezes those items (freezing an unbuilt forest freezes an empty
    set). procedural regenerates items from the settings and discards every custom edit irreversibly; it is
    refused unless confirm_discard=true. Returns the mode and item count before and after.
    """
    fh.node_name(name, "name")
    if mode not in ("procedural", "custom_edit"):
        raise ValueError("mode must be 'procedural' or 'custom_edit'")
    confirm = _bool(confirm_discard, "confirm_discard")
    report = _send(fh.fill(_MODE_BODY, NAME=fh.b64(name), TARGET=1 if mode == "custom_edit" else 0,
                           CONFIRM=_mxs_bool(confirm)))
    f = next((line.split("|") for line in report if line.startswith("M|")), None)
    if f is None:
        raise ForestError("INVALID_READBACK", "forest_set_mode got no result line")
    result = {"name": fh.unb64(f[1]), "previous_mode": fh.mode_name(int(f[2])), "mode": fh.mode_name(int(f[3])),
              "changed": f[6] == "1", "count_before": int(f[4]), "count": int(f[5])}
    if result["changed"] and mode == "procedural":
        result["note"] = "custom edits were discarded"
    return result


# --------------------------------------------------------------------------------------------
# forest_library
# --------------------------------------------------------------------------------------------

@mcp.tool()
def forest_library(query: str = "", kind: str = "all", limit: int = 50) -> dict[str, Any]:
    """Search the Forest Pack library: presets, 3D items, 2D billboards and Forest Ivy items. Python only.

    Roots: Forest Pack's LibraryDir registry list plus %LOCALAPPDATA%\\Itoo Software\\Forest Pack Pro\\lib. query
    matches words in the name, object, family, category or uuid (all words must match). kind: all (default),
    presets, 3d or 2d (items under folders of those names), ivy (Forest Ivy .fp items) or other (every other
    item, such as a studio library's). Each item gives name, kind, placeable (forest_from_library can place it,
    by uuid, object or name), category, maxfile, object, matlib ($(RENDERER) left in), matlib_renderers,
    material and thumbnail. Other library macros, such as $(PRESET_STATE), take their default option.
    """
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("query must be a string of at most 200 characters")
    if kind not in flib.KINDS:
        raise ValueError(f"kind must be one of {', '.join(flib.KINDS)}")
    limit = fh.integer(limit, "limit", 1, 500)
    return flib.search(query, kind, limit)


# --------------------------------------------------------------------------------------------
# forest_from_library
# --------------------------------------------------------------------------------------------

_FROM_LIBRARY_BODY = r'''
if fpBusy() do throw "USER_BUSY: an undo operation is open in 3ds Max; finish it first"
if not (fpHasForest()) do throw "PLUGIN_MISSING: Forest Pack Pro (Forest_Pro) is not installed"
local libfile = fpDec "@@FILE@@"
local objname = fpDec "@@OBJECT@@"
if not (doesFileExist libfile) do throw ("FILE_NOT_FOUND: " + libfile)
local surfs = for b in @@SURFACES@@ collect (fpNode b)
for s in surfs do (if superclassOf s != GeometryClass or classOf s.baseObject == Forest_Pro do throw ("BAD_SURFACE: '" + s.name + "' is not mesh geometry"))
local incs = for b in @@INCLUDE@@ collect (fpNode b)
local excs = for b in @@EXCLUDE@@ collect (fpNode b)
for s in (incs + excs) do (if superclassOf s != shape do throw ("BAD_SPLINE: '" + s.name + "' is not a spline"))
local fileNames = getMAXFileObjectNames libfile quiet:true
if (findItem fileNames objname) == 0 do throw ("OBJECT_NOT_IN_FILE: '" + objname + "' is not in " + libfile)
-- Phase 1, outside the undo stack: merge the object alone to learn what it references. Merged alone,
-- a preset's cobjlist/arnodelist entries come back undefined; conamelist names its source objects.
local kind = "preset", deps = #(), dropped = #()
local mark = fpMark()
with undo off (
    mergeMAXFile libfile #(objname) #autoRenameDups #useSceneMtlDups #neverReparent quiet:true
    local probeNodes = fpNewNodes mark
    local probe = undefined
    for o in probeNodes while probe == undefined do (if classOf o.baseObject == Forest_Pro do probe = o)
    if probe == undefined then (
        kind = ""
        for o in probeNodes do (if superclassOf o == GeometryClass do kind = "3d")
    ) else (
        local nl = probe.namelist as array, cl = probe.cobjlist as array, cn = probe.conamelist as array
        for i = 1 to nl.count do (
            if not (i <= cl.count and isValidNode cl[i]) do (
                local nm = if i <= cn.count and cn[i] != undefined and cn[i] != "" then (cn[i] as string) else (nl[i] as string)
                if (findItem fileNames nm) > 0 do appendIfUnique deps nm
            )
        )
        local an = probe.arnamelist as array, atype = probe.artypelist as array
        for i = 1 to an.count do (if not (i <= atype.count and atype[i] == 3) do append dropped (an[i] as string))
    )
    for o in probeNodes where isValidNode o do delete o
)
if kind == "" do throw ("UNSUPPORTED_ITEM: '" + objname + "' merged neither a Forest Pack object nor geometry")
-- Phase 2: the object and its sources in ONE merge, so Max relinks cobjlist (also through renames).
-- Live: merging a library file clears 3ds Max's whole undo history and is never recorded in undo, and
-- loadTempMaterialLibrary clears the history too, so this placement can't be undone. The merge and every
-- change after it run with undo off, so no undo record refers to these nodes. On an error, everything
-- created since mark2 is deleted (with undo off) and the error is passed on.
local f = undefined, src = undefined, newNodes = #(), leftovers = #()
local swapped = #(), missing = #(), hidden = #(), notes = #()
local ss = stringStream ""
local mark2 = fpMark()
try (
    with undo off (
        mergeMAXFile libfile (#(objname) + deps) #autoRenameDups #useSceneMtlDups #neverReparent quiet:true
        newNodes = fpNewNodes mark2
        if kind == "preset" then (
            for o in newNodes while f == undefined do (if classOf o.baseObject == Forest_Pro do f = o)
        ) else (
            for o in newNodes while src == undefined do (if superclassOf o == GeometryClass and classOf o.baseObject != Forest_Pro do src = o)
        )
        if f == undefined and src == undefined do throw ("MERGE_FAILED: merging '" + objname + "' again created nothing usable")
        local wantName = fpDec "@@NEWNAME@@"
        if kind == "3d" then (
            f = Forest_Pro name:(fpUnique (if wantName != "" then wantName else ("Forest_" + objname))) pos:[0, 0, 0]
            local u = units.decodeValue "@@UNITS_CM@@cm"
            f.units_x = u
            f.units_y = u
            f.cobjlist = #(src)
            f.namelist = #(src.name)
            f.conamelist = #(src.name)
            f.geomlist = #(2)
            f.problist = #(100.0)
            local bb = nodeGetBoundingBox src src.transform
            local d = bb[2] - bb[1]
            local wv = amax d.x d.y
            if wv <= 0 do wv = amax #(d.z, 1.0)
            local hv = if d.z > 0 then d.z else wv
            f.widthlist = #(wv)
            f.heightlist = #(hv)
        ) else (
            if wantName != "" and f.name != wantName do f.name = fpUnique wantName
        )
        -- Retarget: the surfaces, only the Surface Area (presets ship it inactive: the cause of 0 items),
        -- active unless include splines are given (areas add up), then the include/exclude spline areas.
        f.surflist = surfs
        local k3 = findItem (f.artypelist as array) 3
        fpKeepAreas f (if k3 > 0 then #(k3) else #())
        k3 = findItem (f.artypelist as array) 3
        if k3 == 0 and (fpGeomBad f) == 0 do (f.trees.update(); k3 = findItem (f.artypelist as array) 3)
        if k3 > 0 then (
            local act = f.pf_aractivelist as array
            while act.count < k3 do append act false
            act[k3] = (incs.count == 0)
            f.pf_aractivelist = act
        ) else append notes "NO_SURFACE_AREA"
        for s in incs do fpAppendArea f s 0 0 true
        for s in excs do fpAppendArea f s 0 1 true
        local mp = fpDec "@@MATLIB@@"
        if mp != "" do (
            local lib = loadTempMaterialLibrary mp
            if lib == undefined then append notes "MATLIB_NOT_LOADED" else (
                local libNames = for m in lib collect m.name
                if kind == "preset" then (
                    local ml = f.matList as array
                    for i = 1 to ml.count where ml[i] != undefined do (
                        local k = findItem libNames ml[i].name
                        if k > 0 then (append swapped ml[i].name; ml[i] = lib[k]) else appendIfUnique missing ml[i].name
                    )
                    f.matList = ml
                ) else (
                    local want = fpDec "@@MATERIAL@@"
                    if want == "" then append notes "NO_MATERIAL_NAME" else (
                        local k = findItem libNames want
                        if k > 0 then (src.material = lib[k]; append swapped want) else append missing want
                    )
                )
            )
        )
        if @@HIDE@@ do (
            for c in (f.cobjlist as array) where isValidNode c and (findItem newNodes c) > 0 and not c.isHidden do (hide c; append hidden c.name)
        )
        if (fpGeomBad f) > 0 then append notes "CRASH_RISK" else f.trees.update()
    )
    -- The report, still inside the try: a failure here also removes the placed nodes
    format "FPOK\n" to:ss
    local n = if (fpGeomBad f) > 0 then -1 else f.trees.count()
    format "F|%|%|%|%|%|%|%|%\n" (fpEnc f.name) kind n (fpF f.units_x) (fpF f.units_y) (f.mode as string) (fpF (units.decodeValue "1m")) (fpPropV f #vmaxitems) to:ss
    local nl = f.namelist as array, cl = f.cobjlist as array, gl = f.geomlist as array, pl = f.problist as array
    for g = 1 to nl.count do (
        local c = if g <= cl.count then cl[g] else undefined
        format "G|%|%|%|%|%|%\n" g (if g <= gl.count then fpVal gl[g] else "-") (if g <= pl.count then fpVal pl[g] else "-") (fpEnc (nl[g] as string)) (if isValidNode c then fpEnc c.name else "") (fpState c) to:ss
    )
    local atype = f.artypelist as array, aa = f.pf_aractivelist as array, ai = f.arincexclist as array, anm = f.arnamelist as array
    for i = 1 to atype.count do format "A|%|%|%|%|%\n" i (fpVal atype[i]) (if i <= aa.count then fpVal aa[i] else "-") (if i <= ai.count then fpVal ai[i] else "-") (if i <= anm.count then fpEnc (anm[i] as string) else "") to:ss
    for s in (f.surflist as array) where isValidNode s do format "S|%\n" (fpEnc s.name) to:ss
    for o in newNodes where isValidNode o do format "M|%|%\n" (fpEnc o.name) (fpEnc ((classOf o) as string)) to:ss
    for x in deps do format "P|%\n" (fpEnc x) to:ss
    for x in dropped do format "D|%\n" (fpEnc x) to:ss
    for x in swapped do format "X|swapped|%\n" (fpEnc x) to:ss
    for x in missing do format "X|missing|%\n" (fpEnc x) to:ss
    for x in hidden do format "Z|%\n" (fpEnc x) to:ss
    for x in notes do format "W|%\n" x to:ss
) catch (
    -- fpNewNodes again: it also finds a 3D item's new forest and nodes merged before the error
    leftovers = fpNewNodes mark2
    with undo off (for o in leftovers where isValidNode o do delete o)
    throw()
)
ss as string
'''

_NOTES = {
    "NO_SURFACE_AREA": "the forest has no Surface Area entry, so the surfaces aren't a distribution area",
    "MATLIB_NOT_LOADED": "the material library didn't load; the item keeps the materials from its file",
    "NO_MATERIAL_NAME": "the library item names no material, so nothing was taken from the material library",
    "CRASH_RISK": "geomlist holds a value other than 0, 1 or 2, so the forest was not rebuilt (3 crashed Max)",
}


def _parse_from_library(report: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"items": [], "areas": [], "surfaces": [], "merged_nodes": [], "sources_merged": [],
                              "dropped_preset_areas": [], "materials": {"swapped": [], "missing": []},
                              "hidden_sources": [], "warnings": []}
    for line in report:
        f = line.split("|")
        tag = f[0]
        if tag == "F":
            count = int(f[3])
            result.update(name=fh.unb64(f[1]), kind=f[2], count=None if count < 0 else count,
                          units_x=float(f[4]), units_y=float(f[5]), mode=fh.mode_name(int(f[6])),
                          units_per_meter=float(f[7]))
            vmax = fh.num(f[8]) if len(f) > 8 else None
            result["viewport_max_items"] = None if vmax is None else int(vmax)
            result["warnings"] += [f"{code}: {detail}" for code, detail in
                                   _viewport_notes(result["count"], vmax)]
        elif tag == "G":
            geom = fh.num(f[2])
            result["items"].append({"geom_id": int(f[1]), "name": fh.unb64(f[4]), "source": fh.unb64(f[5]) or None,
                                    "geom_type": None if geom is None else int(geom), "probability": fh.num(f[3]),
                                    "source_linked": f[6] == "n"})
        elif tag == "A":
            area_type = fh.num(f[2])
            result["areas"].append({"index": int(f[1]), "name": fh.unb64(f[5]),
                                    "type": "surface" if area_type == 3 else "spline" if area_type == 0 else area_type,
                                    "active": f[3] == "1", "exclude": fh.num(f[4]) == 1})
        elif tag == "S":
            result["surfaces"].append(fh.unb64(f[1]))
        elif tag == "M":
            result["merged_nodes"].append({"name": fh.unb64(f[1]), "class": fh.unb64(f[2])})
        elif tag == "P":
            result["sources_merged"].append(fh.unb64(f[1]))
        elif tag == "D":
            result["dropped_preset_areas"].append(fh.unb64(f[1]))
        elif tag == "X":
            result["materials"][f[1]].append(fh.unb64(f[2]))
        elif tag == "Z":
            result["hidden_sources"].append(fh.unb64(f[1]))
        elif tag == "W":
            result["warnings"].append(_NOTES.get(f[1], f[1]))
    unlinked = [i["name"] for i in result["items"] if i["geom_type"] == 2 and not i["source_linked"]]
    if unlinked:
        result["warnings"].append(f"item types without a linked source (empty items): {', '.join(unlinked)}")
    return result


@mcp.tool()
def forest_from_library(
    item: str,
    surfaces: StrList,
    include_splines: StrList | None = None,
    exclude_splines: StrList | None = None,
    name: str = "",
    matlib_renderer: str = "",
    hide_sources: bool = True,
    items_per_m2: float | None = None,
    allow_undo_flush: bool = False,
) -> dict[str, Any]:
    """Place a Forest Pack library preset or 3D item on surfaces; returns the forest, item count and sources.

    Merging a library file clears 3ds Max's whole undo history (verified live), so this is refused unless
    allow_undo_flush=true. The placement itself can't be undone either: it returns undoable=false and
    remove_with, the new forest and the merged sources, which are the nodes to delete to take it out again.

    item: a uuid, object or English name from forest_library. A preset is merged with its source objects in one
    merge (a probe merge first finds them; Max relinks cobjlist only when they arrive together), then
    retargeted: surflist = surfaces, its own spline areas dropped, its Surface Area activated (presets ship it
    inactive, which gives 0 items). include_splines limit the scatter to those splines (the Surface Area is
    then inactive, since areas add up); exclude_splines cut holes. A 3D item becomes the source of a new forest
    (items_per_m2 sets its density; default 300 cm tiles, about 160 per m2). matlib_renderer (vray, corona, ...)
    swaps materials for the item library's .mat ones by name. Fails with ZERO_ITEMS when nothing is scattered.
    2D billboards and Forest Ivy (.fp) items can't be placed (placeable=false in forest_library).
    """
    if not isinstance(item, str) or not item.strip() or len(item) > 300:
        raise ValueError("item must be a library uuid, object name or English name")
    surface_names = fh.name_list(surfaces, "surfaces")
    include = fh.name_list(include_splines, "include_splines", allow_empty=True)
    exclude = fh.name_list(exclude_splines, "exclude_splines", allow_empty=True)
    overlap = (set(include) & set(exclude)) | ((set(include) | set(exclude)) & set(surface_names))
    if overlap:
        raise ValueError(f"{', '.join(sorted(overlap))} is listed twice among surfaces and splines")
    if not isinstance(name, str) or "\x00" in name or len(name) > 1024:
        raise ValueError("name must be a string")
    if not isinstance(matlib_renderer, str) or (matlib_renderer and not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}",
                                                                                      matlib_renderer)):
        raise ValueError("matlib_renderer must be a renderer name such as vray or corona (or empty)")
    hide = _bool(hide_sources, "hide_sources")
    density = None if items_per_m2 is None else fh.finite(items_per_m2, "items_per_m2", 0.0, 100000.0,
                                                          low_open=True)
    entry = flib.find_item(item)
    if not entry["placeable"]:
        if entry["class"] == "tree_billboard":
            what = "a 2D billboard (tree_billboard)"
        elif entry["kind"] == "ivy":
            what = "a Forest Ivy item: its file is an .fp file, not a .max file"
        elif not entry["maxfile"] or not entry["object"]:
            what = "an item without a library file or object name"
        else:
            what = f"an item whose library file isn't a .max file ({entry['maxfile']})"
        raise ForestError("UNSUPPORTED_ITEM", f"'{entry['name']}' is {what}; only presets and 3D objects merged "
                          "from a .max file can be placed. Nothing was sent to 3ds Max.")
    if not entry["maxfile_exists"]:
        raise ForestError("FILE_NOT_FOUND", f"the library file {entry['maxfile']} is missing")
    matlib = ""
    if matlib_renderer:
        template = entry.get("_matlib_template") or ""
        if not template:
            raise ValueError(f"'{entry['name']}' names no material library")
        matlib = flib.resolve_path(template, entry["_index_dir"], entry["_root_path"], matlib_renderer) or ""
        if not matlib.lower().endswith(".mat"):
            raise ValueError(f"only .mat material libraries can be loaded; this item uses {matlib}")
        if matlib_renderer.lower() not in [r.lower() for r in entry["matlib_renderers"]]:
            raise ForestError("FILE_NOT_FOUND", f"no material library for '{matlib_renderer}' ({matlib}); available: "
                              f"{', '.join(entry['matlib_renderers']) or 'none'}")
    if not _bool(allow_undo_flush, "allow_undo_flush"):
        raise ValueError("Placing a library item merges a .max file, which clears 3ds Max's undo history, so "
                         "earlier steps can no longer be undone, and the placement itself can't be undone either "
                         "(the result lists the nodes to delete in remove_with). Pass allow_undo_flush=true to "
                         "place it anyway. Nothing was sent to 3ds Max.")
    units_cm = fh.units_for_items_per_m2(density) if density else DEFAULT_3D_UNITS_CM
    report = _send(fh.fill(
        _FROM_LIBRARY_BODY, FILE=fh.b64(entry["maxfile"]), OBJECT=fh.b64(entry["object"]),
        SURFACES=fh.mxs_b64_array(surface_names), INCLUDE=fh.mxs_b64_array(include),
        EXCLUDE=fh.mxs_b64_array(exclude), NEWNAME=fh.b64(name), UNITS_CM=f"{units_cm:.4f}", MATLIB=fh.b64(matlib),
        MATERIAL=fh.b64(entry.get("material") or ""), HIDE=_mxs_bool(hide)), timeout=LIBRARY_TIMEOUT_S)
    result = _parse_from_library(report)
    if "name" not in result:
        raise ForestError("INVALID_READBACK", "forest_from_library got no result line")
    result["library_item"] = {k: entry[k] for k in ("uuid", "name", "object", "maxfile", "kind", "category")}
    # The merge cleared the undo history (live) and the script ran with undo off: undo can't remove the
    # placement, so the caller deletes these nodes instead (the forest first, then its merged sources)
    result["undoable"] = False
    result["undo_history_cleared"] = True
    result["remove_with"] = list(dict.fromkeys([result["name"]] + [m["name"] for m in result["merged_nodes"]]))
    if matlib:
        result["materials"]["matlib"] = matlib
    if density and result.get("kind") == "preset":
        result["warnings"].append("items_per_m2 applies to 3D items only; the preset keeps its own density")
    if result.get("kind") == "3d":
        result["estimated_items_per_m2"] = round(fh.ITEMS_PER_TILE / (units_cm / 100.0) ** 2, 2)
        if not matlib:
            result["warnings"].append("3D library meshes carry no material; pass matlib_renderer to load one")
    if result.get("count") == 0:
        raise ForestError("ZERO_ITEMS", f"'{result['name']}' was placed but scatters 0 items; run forest_validate "
                          "on it, check the surfaces and splines, or delete the nodes in details.remove_with",
                          details=result)
    return result


# --------------------------------------------------------------------------------------------
# forest_validate
# --------------------------------------------------------------------------------------------

_VALIDATE_BODY = r'''
local nd = fpForest "@@NAME@@"
local ss = stringStream ""
format "FPOK\n" to:ss
fpCollect nd ss true
ss as string
'''

_FIX_BODY = r'''
if fpBusy() do throw "USER_BUSY: an undo operation is open in 3ds Max; finish it first"
local nd = fpForest "@@NAME@@"
local n = @@N@@
if nd.namelist.count != n do throw "STALE: the item list changed since it was read; run forest_validate again"
for x in @@EXPECT@@ do (if (getProperty nd x[1]).count != x[2] do throw ("STALE: " + x[1] + " changed since it was read; run forest_validate again"))
local ss = stringStream ""
format "FPOK\n" to:ss
undo "MCP Forest validate fix" on (
    local nl = nd.namelist as array
    for op in @@OPS@@ do (
        local p = op[1]
        local v = (getProperty nd p) as array
        local c0 = v.count
        local relinked = 0
        case p of (
            "cobjlist": (
                local cn = nd.conamelist as array
                for i = 1 to n do (
                    if i > c0 or (findItem op[2] i) > 0 do (
                        local nm = if i <= cn.count and cn[i] != undefined and cn[i] != "" then (cn[i] as string) else (nl[i] as string)
                        local hits = getNodeByName nm exact:true all:true
                        local pick = undefined
                        if hits.count == 1 and superclassOf hits[1] == GeometryClass and classOf hits[1].baseObject != Forest_Pro do pick = hits[1]
                        if pick != undefined do relinked += 1
                        if i > c0 then append v pick else (if pick != undefined do v[i] = pick)
                    )
                )
            )
            "geomlist": (
                local cl = nd.cobjlist as array
                for i = c0 + 1 to n do append v (if i <= cl.count and isValidNode cl[i] then 2 else 0)
            )
            "problist": (for i = c0 + 1 to n do append v 100.0)
            "conamelist": (
                local cl = nd.cobjlist as array
                for i = c0 + 1 to n do append v (if i <= cl.count and isValidNode cl[i] then cl[i].name else (nl[i] as string))
            )
            default: (
                if op[2] == #size then (
                    local cl = nd.cobjlist as array
                    for i = c0 + 1 to n do (
                        local val = op[3]
                        local src = if i <= cl.count then cl[i] else undefined
                        if isValidNode src do (
                            local bb = nodeGetBoundingBox src src.transform
                            local d = bb[2] - bb[1]
                            local sv = if p == "widthlist" then (amax d.x d.y) else d.z
                            if sv > 0 do val = sv
                        )
                        append v val
                    )
                ) else (
                    for i = c0 + 1 to op[3] do (
                        local val = op[2]
                        if p == "aridlist" do (val = 1; for x in v where isKindOf x Number do val = amax val (x + 1))
                        append v val
                    )
                )
            )
        )
        setProperty nd p v
        format "X|%|%|%|%\n" p c0 v.count relinked to:ss
    )
)
fpCollect nd ss true
ss as string
'''

_AREA_DEFAULT = dict(fh.AREA_DEFAULTS)


def _fix_ops(plan: list[dict[str, Any]]) -> str:
    ops = []
    for step in plan:
        name = step["list"]
        if step["action"] == "pad_relink":
            ops.append(f'#("cobjlist", #({", ".join(str(i) for i in step["relink"])}))')
        elif name in ("widthlist", "heightlist"):
            ops.append(f'#("{name}", #size, {fh.mxs_float(step["fallback"])})')
        elif step["action"] == "pad_area":
            ops.append(f'#("{name}", {_AREA_DEFAULT[name]}, {step["to"]})')
        else:
            ops.append(f'#("{name}")')
    return "#(" + ", ".join(ops) + ")"


def _validation(state: dict[str, Any]) -> dict[str, Any]:
    analysis = fh.analyze(state)
    out = {
        "name": state.get("name"),
        "ok": not analysis["errors"],
        "mode": fh.mode_name(state.get("mode", 0)),
        "count": state.get("count"),
        "estimated_count": analysis["estimated_count"],
        "errors": analysis["errors"],
        "warnings": analysis["warnings"],
        "notes": analysis["notes"],
        "item_types": state["lengths"].get("namelist", 0),
        "areas": [{"index": a["index"], "name": a["name"],
                   "type": "surface" if a["type"] == 3 else "spline" if a["type"] == 0 else a["type"],
                   "active": a["active"], "exclude": a["incexc"] == 1} for a in state["areas"]],
        "surfaces": [s["name"] or None for s in state["surfaces"]],
        "units": {"x": state.get("units_x"), "y": state.get("units_y"),
                  "per_meter": state.get("upm"), "lock_ratio": state.get("lock_ratio")},
        "direction": state.get("direction"),
        "limits": {"viewport_max_items": state.get("vmaxitems"), "render_max_items": state.get("rmaxitems"),
                   "maxdensity_millions": state.get("maxdensity"), "candidate_cells": analysis["candidate_cells"],
                   "viewport_thinned": analysis["viewport_thinned"],
                   "estimated_viewport_count": analysis["estimated_viewport_count"]},
    }
    if analysis["estimate_note"]:
        out["estimate_note"] = analysis["estimate_note"]
    if state.get("vmesh") in (3, 4):
        out["viewport_note"] = f"vmesh={state['vmesh']} shows no item meshes in the viewport; that isn't an error"
    return out


@mcp.tool()
def forest_validate(name: str, fix: bool = False) -> dict[str, Any]:
    """Check a Forest Pack object's item and area lists, then build it and report the item count.

    Errors: item lists shorter than namelist (the master: Forest Pack pads them with values that give nothing
    or empty items), geomlist outside 0/1/2 (3 crashed Max, so the forest is then not rebuilt), sources missing,
    probabilities or sizes <= 0, area lists of unequal length, no active area, active areas without a spline or
    surface, 0 items. Warnings: long lists, conamelist mismatches, an exclude listed before an include it should
    cut, density tile too large, the viewport Max Items limit thinning count (trees.count()), the maxdensity cap
    (0 items), custom edit mode. Notes: geomlist 0 gap items. The estimate is for the stock map only. fix=true
    pads short item lists (geomlist 2 when a source exists, else 0; probability 100; size from the source),
    relinks missing sources by a unique matching name, pads area lists, in one undo step, then checks again. It
    never truncates lists or writes geomlist 3.
    """
    fh.node_name(name, "name")
    do_fix = _bool(fix, "fix")
    lib = fh.MXS_LIB + fh.COLLECT_FN
    try:
        check = _send(fh.fill(_VALIDATE_BODY, NAME=fh.b64(name)), lib=lib)
    except DialogBlocked as exc:
        if not do_fix:
            raise
        # only the read-only check is queued; the fix was never sent
        raise PrecheckBlocked(exc, "forest_validate", "Nothing was fixed") from exc
    state = fh.parse_state(check)
    result = _validation(state)
    if not do_fix:
        return result
    plan = fh.fix_plan(state)
    if not plan:
        result["fixed"] = []
        return result
    expect = [(step["list"], step["from"]) for step in plan]
    report = _send(fh.fill(_FIX_BODY, NAME=fh.b64(name), N=state["lengths"]["namelist"],
                           EXPECT="#(" + ", ".join(f'#("{n}", {c})' for n, c in expect) + ")",
                           OPS=_fix_ops(plan)), lib=lib)
    fixed = []
    for line in report:
        f = line.split("|")
        if f[0] == "X":
            row = {"list": f[1], "from": int(f[2]), "to": int(f[3])}
            if f[1] == "cobjlist":
                row["relinked"] = int(f[4])
            fixed.append(row)
    after = _validation(fh.parse_state(report))
    after["fixed"] = fixed
    after["errors_before"] = result["errors"]
    return after
