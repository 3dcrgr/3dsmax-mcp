"""Scattering tools for plugin-based and native workflows in 3ds Max."""

from __future__ import annotations

import math
from typing import Any

from ..coerce import FloatList, StrList
from ..helpers import forest as fh
from ..helpers.forest import ForestError
from ..server import client, mcp

_FACING = {0: 0, 1: 100, "normal": 0, "up": 100}  # Forest Pack direction: 0 = surface normal ... 100 = world up

_SCATTER_BODY = r'''
if fpBusy() do throw "USER_BUSY: an undo operation is open in 3ds Max; finish it first"
if not (fpHasForest()) do throw "PLUGIN_MISSING: Forest Pack Pro (Forest_Pro) is not installed"
local surfs = for b in @@SURFACES@@ collect (fpNode b)
for s in surfs do (if superclassOf s != GeometryClass or classOf s.baseObject == Forest_Pro do throw ("BAD_SURFACE: '" + s.name + "' is not mesh geometry"))
local geos = for b in @@GEOMETRY@@ collect (fpNode b)
for g in geos do (
    if superclassOf g != GeometryClass or classOf g.baseObject == Forest_Pro do throw ("BAD_GEOMETRY: '" + g.name + "' is not mesh geometry")
    if (findItem surfs g) > 0 do throw ("BAD_GEOMETRY: '" + g.name + "' is also a surface")
)
local ws = #(), hs = #()
for g in geos do (
    local bb = nodeGetBoundingBox g g.transform
    local d = bb[2] - bb[1]
    local w = amax d.x d.y
    if w <= 0.0 do w = units.decodeValue "@@FALLBACK_W@@cm"
    local h = if d.z > 0.0 then d.z else (units.decodeValue "@@FALLBACK_H@@cm")
    append ws w
    append hs h
)
local fp = undefined
undo "MCP Forest scatter" on (
    fp = Forest_Pro name:(fpUnique (fpDec "@@NAME@@")) pos:[0, 0, 0]
    fp.surflist = surfs
    -- A new forest has one Surface Area entry, inactive: activating it is what makes items appear.
    fp.pf_aractivelist = #(true)
    fp.cobjlist = geos
    fp.namelist = for g in geos collect g.name
    fp.conamelist = for g in geos collect g.name
    fp.geomlist = for g in geos collect 2
    fp.problist = @@PROBS@@
    fp.widthlist = ws
    fp.heightlist = hs
    -- Density is the tile size: about 1437 items per units_x * units_y tile (the max density setting does nothing).
    fp.units_x = units.decodeValue "@@UNITS_X@@cm"
    fp.units_y = units.decodeValue "@@UNITS_Y@@cm"
    fp.seed = @@SEED@@
    fp.iconSize = units.decodeValue "@@ICON@@cm"
    fp.vmesh = @@VMESH@@
    fp.rmesh = @@RMESH@@
    fp.direction = @@DIRECTION@@
    fp.applyScale = true
    fp.scalelock = true
    fp.scalexmin = @@SMIN@@
    fp.scaleymin = @@SMIN@@
    fp.scalezmin = @@SMIN@@
    fp.scalexmax = @@SMAX@@
    fp.scaleymax = @@SMAX@@
    fp.scalezmax = @@SMAX@@
    fp.applyRotation = true
    fp.zrotmin = @@RMIN@@
    fp.zrotmax = @@RMAX@@
)
fp.trees.update()
local n = fp.trees.count()
local area = 0.0
for s in surfs do area += fpXYArea s 50000
local ss = stringStream ""
format "FPOK\n" to:ss
format "F|%|%|%|%|%|%\n" (fpEnc fp.name) n (fpF area) (fpF (units.decodeValue "1m")) (fpF fp.units_x) (fpF fp.units_y) to:ss
for k = 1 to geos.count do format "G|%|%|%\n" (fpEnc geos[k].name) (fpF ws[k]) (fpF hs[k]) to:ss
ss as string
'''


def _facing(value: Any) -> int:
    key = value.strip().lower() if isinstance(value, str) else value
    if isinstance(key, bool) or key not in _FACING:
        raise ValueError("facing_mode must be 0 or 'normal' (align to the surface) or 1 or 'up' (world up)")
    return _FACING[key]


@mcp.tool()
def scatter_forest_pack(
    surfaces: StrList,
    geometry: StrList,
    probabilities: FloatList | None = None,
    density: int = 100,
    seed: int = 12345,
    scale_min: float = 85.0,
    scale_max: float = 115.0,
    z_rotation_min: float = -180.0,
    z_rotation_max: float = 180.0,
    source_width_cm: float = 5.0,
    source_height_cm: float = 5.0,
    icon_size_cm: float = 30.0,
    density_units_x_cm: float = 300.0,
    density_units_y_cm: float = 300.0,
    facing_mode: int | str = 0,
    name: str = "ForestScatter",
    viewport_mode: int = 2,
    render_mode: int = 0,
    items_per_m2: float | None = None,
) -> dict[str, Any]:
    """Create a Forest Pack scatter of source objects over surfaces; returns the real item count.

    Density is Forest Pack's tile size (units_x/units_y): about 1437 items per tile with the default density
    map, so count ~ 1437 * surface area / (units_x * units_y). items_per_m2 sets both units to
    100 * sqrt(1437 / items_per_m2) cm. Otherwise density is a percentage of density_units_x/y_cm: 100 keeps
    them, 200 gives twice the items (units / sqrt(2)). Very small tiles saturate (100 cm on 100 m2 gave 35800,
    not 143700). facing_mode 0/'normal' aligns items to the surface normal, 1/'up' to world up (Forest Pack
    direction 0 and 100). Width/height per item come from each source's bounding box (source_width_cm and
    source_height_cm when it is flat). Fails with ZERO_ITEMS when the scatter has no items (the object stays).
    Hide the source objects afterwards.
    """
    surface_names = fh.name_list(surfaces, "surfaces")
    geometry_names = fh.name_list(geometry, "geometry")
    if set(surface_names) & set(geometry_names):
        raise ValueError("an object can't be both a surface and scattered geometry")
    if probabilities is None:
        weights = [1.0] * len(geometry_names)
    else:
        if isinstance(probabilities, (int, float)) and not isinstance(probabilities, bool):
            probabilities = [probabilities]
        if not isinstance(probabilities, (list, tuple)) or len(probabilities) != len(geometry_names):
            raise ValueError("probabilities must have one weight per geometry object "
                             f"({len(probabilities) if isinstance(probabilities, (list, tuple)) else 1} != "
                             f"{len(geometry_names)})")
        weights = [fh.finite(p, "probabilities", 0.0, 1e6, low_open=True) for p in probabilities]
    density = fh.integer(density, "density", 1, 10000)
    seed = fh.integer(seed, "seed", -2**31, 2**31 - 1)
    smin = fh.finite(scale_min, "scale_min", 1.0, 10000.0)
    smax = fh.finite(scale_max, "scale_max", 1.0, 10000.0)
    if smin > smax:
        raise ValueError("scale_min must not exceed scale_max")
    rmin = fh.finite(z_rotation_min, "z_rotation_min", -360.0, 360.0)
    rmax = fh.finite(z_rotation_max, "z_rotation_max", -360.0, 360.0)
    if rmin > rmax:
        raise ValueError("z_rotation_min must not exceed z_rotation_max")
    fallback_w = fh.finite(source_width_cm, "source_width_cm", 0.0, 1e6, low_open=True)
    fallback_h = fh.finite(source_height_cm, "source_height_cm", 0.0, 1e6, low_open=True)
    icon_cm = fh.finite(icon_size_cm, "icon_size_cm", 0.0, 1e6, low_open=True)
    base_x = fh.finite(density_units_x_cm, "density_units_x_cm", 1.0, 1e7)
    base_y = fh.finite(density_units_y_cm, "density_units_y_cm", 1.0, 1e7)
    direction = _facing(facing_mode)
    vmesh = fh.integer(viewport_mode, "viewport_mode", 0, 5)
    rmesh = fh.integer(render_mode, "render_mode", 0, 5)
    if not isinstance(name, str) or not name.strip() or "\x00" in name or len(name) > 1024:
        raise ValueError("name must be a non-empty object name")
    if items_per_m2 is not None:
        per_m2 = fh.finite(items_per_m2, "items_per_m2", 0.0, 100000.0, low_open=True)
        units_x = units_y = fh.units_for_items_per_m2(per_m2)
    else:
        factor = math.sqrt(density / 100.0)
        units_x, units_y = base_x / factor, base_y / factor
    if min(units_x, units_y) < 1.0:
        raise ValueError("the density tile would be under 1 cm; lower density or items_per_m2")

    report = fh.lines(client.send_command(fh.wrap(fh.fill(
        _SCATTER_BODY,
        SURFACES=fh.mxs_b64_array(surface_names), GEOMETRY=fh.mxs_b64_array(geometry_names),
        NAME=fh.b64(name), PROBS="#(" + ", ".join(fh.mxs_float(w) for w in weights) + ")",
        FALLBACK_W=f"{fallback_w:.4f}", FALLBACK_H=f"{fallback_h:.4f}", UNITS_X=f"{units_x:.4f}",
        UNITS_Y=f"{units_y:.4f}", SEED=seed, ICON=f"{icon_cm:.4f}", VMESH=vmesh, RMESH=rmesh, DIRECTION=direction,
        SMIN=int(round(smin)), SMAX=int(round(smax)), RMIN=int(round(rmin)), RMAX=int(round(rmax))))))
    head = next((line.split("|") for line in report if line.startswith("F|")), None)
    if head is None:
        raise ForestError("INVALID_READBACK", "scatter_forest_pack got no result line")
    count, area, upm = int(head[2]), float(head[3]), float(head[4])
    area_m2 = area / (upm * upm) if upm > 0 else 0.0
    estimate = fh.estimate_count(area_m2, units_x / 100.0, units_y / 100.0)
    result: dict[str, Any] = {
        "name": fh.unb64(head[1]),
        "count": count,
        "estimated_count": None if estimate is None else round(estimate),
        "surface_area_m2": round(area_m2, 3),
        "surfaces": surface_names,
        "geometry": [{"name": fh.unb64(f[1]), "width": float(f[2]), "height": float(f[3])}
                     for f in (line.split("|") for line in report if line.startswith("G|"))],
        "units_x_cm": round(units_x, 3),
        "units_y_cm": round(units_y, 3),
        "items_per_m2_estimate": round(fh.ITEMS_PER_TILE / ((units_x / 100.0) * (units_y / 100.0)), 3),
        "facing": "normal" if direction == 0 else "up",
        "direction": direction,
        "seed": seed,
        "warnings": [],
    }
    if estimate is not None and count > 0 and estimate > 2 * count:
        result["warnings"].append("far fewer items than estimated: very small density tiles saturate, or the "
                                  "surfaces face down")
    if count == 0:
        raise ForestError("ZERO_ITEMS", f"'{result['name']}' was created but scatters 0 items (about "
                          f"{0 if estimate is None else round(estimate, 2)} expected for {result['surface_area_m2']} "
                          "m2 of surface). Lower the density units or raise items_per_m2, check that the surfaces "
                          "face up, or delete it", details=result)
    return result
