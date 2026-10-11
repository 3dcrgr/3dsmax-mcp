"""Scattering tools for plugin-based and native workflows in 3ds Max."""

from __future__ import annotations

import math
from typing import Any

from ..coerce import FloatList, StrList
from ..helpers import forest as fh
from ..helpers.forest import ForestError
from ..server import client, mcp

# Forest Pack direction (live): -100 = world down, 0 = surface normal, 100 = world up; not clamped by MAXScript
_FACING = {0: 0, 1: 100, "normal": 0, "up": 100}
_FACING_NAMES = {0: "normal", 100: "up", -100: "down"}
_STOCK_MAP = "dense.bmp"

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
-- Lists are collected into locals first: a for-collect written into an expression that reads the
-- caller's locals can fail in MAXScript ("Bad free thunk local member index", seen live)
local geoNames = for g in geos collect g.name
local geoTypes = for g in geos collect 2
local fp = undefined
undo "MCP Forest scatter" on (
    fp = Forest_Pro name:(fpUnique (fpDec "@@NAME@@")) pos:[0, 0, 0]
    fp.surflist = surfs
    -- A new forest has one Surface Area entry, inactive: activating it is what makes items appear.
    fp.pf_aractivelist = #(true)
    fp.cobjlist = geos
    fp.namelist = geoNames
    fp.conamelist = geoNames
    fp.geomlist = geoTypes
    fp.problist = @@PROBS@@
    fp.widthlist = ws
    fp.heightlist = hs
    -- Density is the tile size: about 1437 items per units_x * units_y tile with the stock map. The
    -- maxdensity setting is a cap on candidate cells (millions), not a density: it is never written.
    -- A new forest has lock_ratio on, which uses units_x for both sides (live): unequal units turn it off.
@@UNLOCK@@    fp.units_x = units.decodeValue "@@UNITS_X@@cm"
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
-- Read back what this call doesn't set: a saved default preset could change new forests, and "Scale to fit
-- sloped areas" forces direction to 0 (live). The caps say whether trees.count() is the full scatter.
format "D|%|%|%|%|%|%|%\n" (fpPropV fp #direction) (fpPropV fp #scalelope) (fpPropV fp #distmode) (fpEnc (fp.mapname as string)) (fpHasDistMap fp) (fpPropV fp #densityMap) (fpPropV fp #lock_ratio) to:ss
format "Q|%|%|%|%|%\n" (fpPropV fp #vmaxitems) (fpPropV fp #rmaxitems) (fpPropV fp #maxdensity) (fpPropV fp #pixels_x) (fpPropV fp #pixels_y) to:ss
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
    direction: int | None = None,
) -> dict[str, Any]:
    """Create a Forest Pack scatter of source objects over surfaces; returns the real item count.

    Density is Forest Pack's tile size (units_x/units_y): about 1437 items per tile with the default density
    map, so the full scatter is ~ 1437 * surface area / (units_x * units_y) (estimated_count). items_per_m2 sets
    both units to 100 * sqrt(1437 / items_per_m2) cm. Otherwise density is a percentage of density_units_x/y_cm:
    100 keeps them, 200 gives twice the items (units / sqrt(2)). Unequal units turn lock_ratio off (a new forest
    has it on, which uses units_x for both sides). count is trees.count(), the viewport build:
    over Max items Viewport (vmaxitems, 50000) Forest Pack thins it (100 cm tiles on 100 m2: 35800 shown,
    143700 rendered), reported as VIEWPORT_LIMIT. Over maxdensity (millions of candidate cells) it scatters 0.
    facing_mode 0/'normal' aligns items to the surface normal, 1/'up' to world up (direction 0 and 100);
    direction (-100 world down .. 0 normal .. 100 up) overrides it. Width/height per item come from each source's
    bounding box (source_width_cm and source_height_cm when it is flat). Fails with ZERO_ITEMS when the scatter
    has no items (the object stays). Hide the source objects afterwards.
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
    facing = _facing(facing_mode)
    if direction is not None:
        facing = fh.integer(direction, "direction", -100, 100)
    direction = facing
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
    square = f"{units_x:.4f}" == f"{units_y:.4f}"

    report = fh.lines(client.send_command(fh.wrap(fh.fill(
        _SCATTER_BODY,
        SURFACES=fh.mxs_b64_array(surface_names), GEOMETRY=fh.mxs_b64_array(geometry_names),
        NAME=fh.b64(name), PROBS="#(" + ", ".join(fh.mxs_float(w) for w in weights) + ")",
        UNLOCK="" if square else "    fp.lock_ratio = false\n",
        FALLBACK_W=f"{fallback_w:.4f}", FALLBACK_H=f"{fallback_h:.4f}", UNITS_X=f"{units_x:.4f}",
        UNITS_Y=f"{units_y:.4f}", SEED=seed, ICON=f"{icon_cm:.4f}", VMESH=vmesh, RMESH=rmesh, DIRECTION=direction,
        SMIN=int(round(smin)), SMAX=int(round(smax)), RMIN=int(round(rmin)), RMAX=int(round(rmax))))))
    head = next((line.split("|") for line in report if line.startswith("F|")), None)
    if head is None:
        raise ForestError("INVALID_READBACK", "scatter_forest_pack got no result line")
    count, area, upm = int(head[2]), float(head[3]), float(head[4])
    area_m2 = area / (upm * upm) if upm > 0 else 0.0
    back = next((line.split("|") for line in report if line.startswith("D|")), None)
    caps = next((line.split("|") for line in report if line.startswith("Q|")), None)
    warnings: list[str] = []
    settings: dict[str, Any] = {}
    if back is not None and len(back) >= 8:
        settings = {"direction": fh.num(back[1]), "scale_to_slope": back[2] == "1", "distmode": fh.num(back[3]),
                    "mapname": fh.unb64(back[4]), "distmap": back[5] == "1", "density_map": back[6] == "1",
                    "lock_ratio": back[7] == "1"}
    stock_map = not settings or (settings["mapname"].lower() == _STOCK_MAP and not settings["distmap"]
                                 and not settings["density_map"] and settings["distmode"] in (0, None))
    if settings and not stock_map:
        warnings.append(f"NON_DEFAULT_DISTRIBUTION: the new forest doesn't use the stock distribution map "
                        f"(mapname {settings['mapname']!r}, custom map {settings['distmap']}, density map "
                        f"{settings['density_map']}, distmode {settings['distmode']}); a default preset saved in "
                        "Forest Pack can change new forests. The item estimate assumes the stock map, so it is "
                        "left out")
    if settings and settings["direction"] is not None and settings["direction"] != direction:
        warnings.append(f"DIRECTION_NOT_APPLIED: direction reads {settings['direction']:g}, not {direction}"
                        + ("; 'Scale to fit sloped areas' is on, which forces it to 0" if settings["scale_to_slope"]
                           else ""))
    # The lock as read back (on when the units are equal: a new forest has it on, and it's only written off).
    # With it on Forest Pack uses units_x for both sides, so the reported tile is the one it uses.
    lock = bool(settings.get("lock_ratio")) if settings else square
    effective_y = units_x if lock else units_y
    if lock and not square:
        warnings.append(f"UNITS_Y_IGNORED: lock_ratio reads on, so Forest Pack uses units_x ({units_x:g} cm) for "
                        f"both sides and ignores units_y ({units_y:g} cm)")
    estimate = fh.estimate_count(area_m2, units_x / 100.0, units_y / 100.0, lock_ratio=lock) if stock_map else None
    limits: dict[str, Any] = {}
    if caps is not None and len(caps) >= 6:
        values = [fh.num(v) for v in caps[1:6]]
        limits = dict(zip(("viewport_max_items", "render_max_items", "maxdensity_millions", "pixels_x",
                           "pixels_y"), [None if v is None else int(v) if v.is_integer() else v for v in values]))
    pixels = (limits.get("pixels_x") or fh.DEFAULT_PIXELS) * (limits.get("pixels_y") or fh.DEFAULT_PIXELS)
    candidates = fh.candidate_cells(area_m2, units_x / 100.0, units_y / 100.0, lock_ratio=lock, pixels=pixels)
    checks = fh.count_checks(estimate, count, viewport_max=limits.get("viewport_max_items"),
                             maxdensity=limits.get("maxdensity_millions"), candidates=candidates,
                             render_max=limits.get("render_max_items"))
    warnings += [f"{code}: {detail}" for code, detail in checks["warnings"] + checks["notes"]]
    result: dict[str, Any] = {
        "name": fh.unb64(head[1]),
        "count": count,
        "estimated_count": None if estimate is None else round(estimate),
        "surface_area_m2": round(area_m2, 3),
        "surfaces": surface_names,
        "geometry": [{"name": fh.unb64(f[1]), "width": float(f[2]), "height": float(f[3])}
                     for f in (line.split("|") for line in report if line.startswith("G|"))],
        "units_x_cm": round(units_x, 3),
        "units_y_cm": round(effective_y, 3),
        "lock_ratio": lock,
        "items_per_m2_estimate": round(fh.ITEMS_PER_TILE / ((units_x / 100.0) * (effective_y / 100.0)), 3),
        "facing": _FACING_NAMES.get(direction, "custom"),
        "direction": direction,
        "seed": seed,
        "count_is_viewport_build": True,
        "viewport_thinned": checks["viewport_thinned"],
        "estimated_viewport_count": checks["estimated_viewport_count"],
        "limits": limits,
        "warnings": warnings,
    }
    if (estimate is not None and count > 0 and estimate > 2 * count and not checks["viewport_thinned"]):
        warnings.append("far fewer items than estimated: check that the surfaces face up")
    if count == 0:
        cap = next((d for c, d in checks["warnings"] if c == "MAXDENSITY_CAP"), None)
        advice = (f"MAXDENSITY_CAP: {cap}" if cap else "Lower the density units or raise items_per_m2, and check "
                  "that the surfaces face up")
        raise ForestError("ZERO_ITEMS", f"'{result['name']}' was created but scatters 0 items (about "
                          f"{0 if estimate is None else round(estimate, 2)} expected for {result['surface_area_m2']} "
                          f"m2 of surface). {advice}. The object stays in the scene; delete it if unwanted",
                          details=result)
    return result
