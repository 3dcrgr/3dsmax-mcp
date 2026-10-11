"""Shared Forest Pack plumbing: the MAXScript helper library, response parsing, validation.

Every fact used here was measured live in 3ds Max 2026 with Forest Pack Pro 9 (version 900);
docs/fork/forest-pack.md lists them. The tool modules (tools/forest.py, tools/scattering.py)
send the scripts themselves, so tests can patch their ``client``.
"""
from __future__ import annotations

import base64
import math
import re
from typing import Any, Iterable

# Items per units_x * units_y tile with the default density map (dense.bmp, threshold 50).
# Measured: 1437 on a 10 m plane at 1000 cm units, 16023 at 300 cm.
ITEMS_PER_TILE = 1437

MODE_NAMES = {0: "procedural", 1: "custom_edit"}

# namelist is the master: Forest Pack pads every other item list to namelist.count on a rebuild.
ITEM_LISTS_CORE = ("cobjlist", "geomlist", "problist", "widthlist", "heightlist")
ITEM_LISTS_AUX = ("conamelist", "matList", "coloridlist", "usemeshdimlist", "centerlist", "ScaleList",
                  "zoffsetlist", "specidlist", "tempidlist", "tempnamelist", "nongeomlist",
                  "includechildlist", "keepgrouplist", "radiuslist")
ITEM_LISTS = ("namelist",) + ITEM_LISTS_CORE + ITEM_LISTS_AUX

# The 30 area lists and the values a new spline area gets (verified when appending an area).
AREA_DEFAULTS = (
    ("aridlist", "undefined"), ("pf_aractivelist", "false"), ("arnamelist", '""'), ("arnodelist", "undefined"),
    ("arnodenamelist", '""'), ("artypelist", "0"), ("arincexclist", "0"), ("arresollist", "512"),
    ("arslicelist", "false"), ("arslicetoplist", "10.0"), ("arwidthlist", "10.0"), ("arforceopenlist", "false"),
    ("armaplist", "undefined"), ("arscalelist", "100.0"), ("arthresholdlist", "100.0"), ("arsurfidlist", '""'),
    ("arflafdenslist", "100.0"), ("arflafscalist", "100.0"), ("arflinvlist", "false"), ("arselspeclist", "false"),
    ("arspeclist", '""'), ("arpaintlist", "undefined"), ("arboundchecklist", "0"), ("arprojectlist", "2"),
    ("arshapelist", "0"), ("arobscalelist", "100.0"), ("arlinkidlist", "-1"), ("arscalemin", "100.0"),
    ("arscalemax", "100.0"), ("arzoffset", "0.0"),
)
AREA_LISTS = tuple(name for name, _ in AREA_DEFAULTS)

ERROR_CODES = frozenset({
    "AMBIGUOUS", "BAD_GEOM_ID", "BAD_INDEX", "BAD_SIZE", "BAD_SPLINE", "BAD_SURFACE", "BAD_GEOMETRY",
    "CONFIRM_DISCARD_REQUIRED", "CRASH_RISK", "CUSTOM_EDIT_REQUIRED", "FILE_NOT_FOUND", "FREEZE_MISMATCH",
    "MERGE_FAILED", "NOT_FOREST", "NOT_FOUND", "OBJECT_NOT_IN_FILE", "PLUGIN_MISSING", "STALE",
    "UNSUPPORTED_ITEM", "UNSUPPORTED_MODE", "USER_BUSY",
})
_RETRYABLE = {"USER_BUSY", "STALE"}


class ForestError(RuntimeError):
    """A Forest Pack tool failure with a machine-readable code (and optional details)."""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message if message.startswith(code) else f"{code}: {message}")
        self.code = code
        self.retryable = code in _RETRYABLE
        self.details = details


# --------------------------------------------------------------------------------------------
# MAXScript helper library (prepended to every script)
# --------------------------------------------------------------------------------------------

_AR_LISTS_MXS = "#(" + ", ".join(f'"{name}"' for name, _ in AREA_DEFAULTS) + ")"
_AR_DEFAULTS_MXS = "#(" + ", ".join(f'#("{name}", {value})' for name, value in AREA_DEFAULTS) + ")"

MXS_LIB = r'''
fn fpEnc text = (dotNetClass "System.Convert").ToBase64String ((dotNetClass "System.Text.Encoding").UTF8.GetBytes (text as string))
fn fpDec text = (dotNetClass "System.Text.Encoding").UTF8.GetString ((dotNetClass "System.Convert").FromBase64String text)
fn fpF v = formattedPrint (v as float) format:".9g"
fn fpP p = (fpF p.x) + "," + (fpF p.y) + "," + (fpF p.z)
fn fpTM m = (fpP m.row1) + "," + (fpP m.row2) + "," + (fpP m.row3) + "," + (fpP m.row4)
fn fpB v = (if v == true then "1" else "0")
fn fpVal v = (
    if v == undefined then ("u") else if (isKindOf v Number) then (fpF v) else if (isKindOf v BooleanClass) then (if v then "1" else "0") else ("?")
)
fn fpState v = (if v == undefined then "u" else if isValidNode v then "n" else "x")
-- The source object's object-offset (pivot offset) TM. getFullTransform maps the source's
-- object-space vertices, so inverse(fpOffTM src) * itemTM places the source's pivot.
fn fpOffTM src = (scaleMatrix src.objectOffsetScale) * (src.objectOffsetRot as matrix3) * (transMatrix src.objectOffsetPos)
fn fpBusy = (theHold.Holding() or theHold.IsSuspended() or theHold.SuperLevel() != 0)
fn fpNode b64 = (
    local wanted = fpDec b64
    local hits = getNodeByName wanted exact:true all:true
    if hits.count == 0 do throw ("NOT_FOUND: no object is named '" + wanted + "'")
    if hits.count > 1 do throw ("AMBIGUOUS: " + (hits.count as string) + " objects are named '" + wanted + "'; rename them first")
    hits[1]
)
fn fpHasForest = (Forest_Pro != undefined)
fn fpForest b64 = (
    if not (fpHasForest()) do throw "PLUGIN_MISSING: Forest Pack Pro (Forest_Pro) is not installed"
    local nd = fpNode b64
    if classOf nd.baseObject != Forest_Pro do throw ("NOT_FOREST: '" + nd.name + "' is a " + ((classOf nd.baseObject) as string) + ", not a Forest Pack object")
    nd
)
-- geomlist: 0 = nothing, 1 = billboard, 2 = custom object (cobjlist). 3 crashed Max (access violation)
-- on a rebuild in testing, so nothing rebuilds a forest that holds anything but 0, 1 or 2.
fn fpGeomBad nd = (
    local gl = nd.geomlist as array
    local bad = 0
    for i = 1 to gl.count while bad == 0 do (
        local v = gl[i]
        if not ((isKindOf v Number) and v >= 0 and v <= 2 and (v as integer) == v) do bad = i
    )
    bad
)
fn fpGuard nd = (
    local bad = fpGeomBad nd
    if bad > 0 do throw ("CRASH_RISK: geomlist[" + (bad as string) + "] is " + (nd.geomlist[bad] as string) + "; only 0, 1 and 2 rebuild safely (3 crashed Max in testing). Run forest_validate.")
)
fn fpMark = (
    local h = (getHandleByAnim (bezier_float())) as integer64
    for o in objects do (local x = (getHandleByAnim o) as integer64; if x > h do h = x)
    h
)
fn fpNewNodes mark = (for o in objects where ((getHandleByAnim o) as integer64) > mark collect o)
fn fpUnique wanted = (if (getNodeByName wanted exact:true) == undefined then wanted else uniqueName wanted)
-- Upward-facing area projected on XY (scene units squared); the bounding box above maxFaces.
fn fpXYArea nd maxFaces = (
    local a = 0.0
    local nf = (getPolygonCount nd)[1]
    if nf > 0 and nf <= maxFaces do (
        local m = snapshotAsMesh nd
        for i = 1 to m.numfaces do (
            local fc = getFace m i
            local p1 = getVert m fc.x
            local c = cross ((getVert m fc.y) - p1) ((getVert m fc.z) - p1)
            if c.z > 0 do a += c.z * 0.5
        )
        delete m
    )
    if a <= 0.0 do (local bb = nodeGetBoundingBox nd (matrix3 1); a = (bb[2].x - bb[1].x) * (bb[2].y - bb[1].y))
    a
)
fn fpKeepAreas nd keep = (
    for p in @@AR_LISTS@@ do (
        local v = (getProperty nd p) as array
        -- Collect into a local first: a for-collect written into a call's arguments that reads the
        -- caller's locals fails in MAXScript ("Bad free thunk local member index", seen live)
        local kept = for k in keep where k <= v.count collect v[k]
        setProperty nd p kept
    )
)
fn fpAppendArea nd shp atype incexc isOn = (
    for d in @@AR_DEFAULTS@@ do (
        local v = (getProperty nd d[1]) as array
        local val = d[2]
        case d[1] of (
            "aridlist": (val = 1; for x in v where isKindOf x Number do val = amax val (x + 1))
            "pf_aractivelist": val = isOn
            "arnamelist": val = shp.name
            "arnodelist": val = shp
            "artypelist": val = atype
            "arincexclist": val = incexc
        )
        append v val
        setProperty nd d[1] v
    )
)
'''.replace("@@AR_LISTS@@", _AR_LISTS_MXS).replace("@@AR_DEFAULTS@@", _AR_DEFAULTS_MXS)

_PLACEHOLDER = re.compile(r"@@([A-Z0-9_]+)@@")


def fill(template: str, **values: Any) -> str:
    """Substitute @@KEY@@ placeholders in one pass, so substituted text is never rescanned."""
    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"no value for placeholder {key}")
        return str(values[key])
    return _PLACEHOLDER.sub(repl, template)


def wrap(body: str, lib: str = MXS_LIB) -> str:
    """The full script: helper library, then the body inside try/catch. The body returns a string."""
    return "(\n" + lib + "\ntry (\n" + body + '\n) catch ("FPERR|" + (getCurrentException() as string))\n)'


def lines(response: Any) -> list[str]:
    """The FPOK report lines of a bridge response, or a ForestError."""
    if not isinstance(response, dict):
        raise ForestError("BRIDGE_ERROR", f"Unexpected bridge response: {str(response)[:200]}")
    if response.get("error") or response.get("ok") is False:
        error = response.get("error") or {}
        if isinstance(error, dict):
            raise ForestError(str(error.get("code") or "BRIDGE_ERROR"), str(error.get("message") or error))
        raise ForestError("BRIDGE_ERROR", str(error))
    raw = response.get("result")
    if isinstance(raw, str) and raw.startswith("__MCP_MS_ERR__:"):
        raise ForestError("MAXSCRIPT_ERROR", raw[len("__MCP_MS_ERR__:"):].strip()[:2000])
    if isinstance(raw, str) and raw.startswith("FPERR|"):
        message = raw.split("|", 1)[1].strip()
        code = "FOREST_ERROR"
        for match in re.finditer(r"\b([A-Z][A-Z_]+):", message):
            if match.group(1) in ERROR_CODES:
                code = match.group(1)
                message = message[match.start():]
                break
        raise ForestError(code, message[:2000])
    if not isinstance(raw, str) or not raw.startswith("FPOK"):
        raise ForestError("INVALID_READBACK", f"Forest Pack script returned no report: {str(raw)[:250]}")
    return [line.rstrip("\r") for line in raw.split("\n")[1:] if line.strip()]


# --------------------------------------------------------------------------------------------
# Literals and parsing
# --------------------------------------------------------------------------------------------

def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def unb64(text: str) -> str:
    return base64.b64decode(text, validate=True).decode("utf-8") if text else ""


def mxs_b64_array(names: Iterable[str]) -> str:
    return "#(" + ", ".join(f'"{b64(n)}"' for n in names) + ")"


def mxs_float(value: float) -> str:
    return f"{float(value):.6f}"


def mxs_point(values: Iterable[float]) -> str:
    return "[" + ", ".join(mxs_float(v) for v in values) + "]"


def num(text: str) -> float | None:
    """A float from a report field; None for undefined (u), missing (-) or non-numeric (?)."""
    if text in ("", "u", "-", "?"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def floats(text: str) -> list[float]:
    return [float(x) for x in text.split(",")] if text else []


def mode_name(mode: int) -> str:
    return MODE_NAMES.get(mode, f"unknown_{mode}")


def node_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 1024:
        raise ValueError(f"{label} must be a non-empty object name")
    return value


def name_list(value: Any, label: str, *, allow_empty: bool = False, limit: int = 500) -> list[str]:
    """A de-duplicated list of object names; a single string or comma list is accepted."""
    if value is None:
        value = []
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")] if "," in value else [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of object names")
    out: list[str] = []
    for v in value:
        node_name(v, label)
        if v not in out:
            out.append(v)
    if not out and not allow_empty:
        raise ValueError(f"{label} must name at least one object")
    if len(out) > limit:
        raise ValueError(f"{label} may name at most {limit} objects")
    return out


def finite(value: Any, label: str, low: float = -math.inf, high: float = math.inf, *, low_open: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    value = float(value)
    if value < low or value > high or (low_open and value <= low):
        bound = f"above {low:g}" if low_open else f"from {low:g}"
        raise ValueError(f"{label} must be {bound} to {high:g}")
    return value


def integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} to {high}")
    return value


def estimate_count(area_m2: float, units_x_m: float, units_y_m: float) -> float | None:
    """Expected items for the default density map: 1437 per units_x * units_y tile."""
    if units_x_m <= 0 or units_y_m <= 0:
        return None
    return ITEMS_PER_TILE * area_m2 / (units_x_m * units_y_m)


def units_for_items_per_m2(items_per_m2: float) -> float:
    """Tile size in centimetres that gives items_per_m2 with the default density map."""
    return 100.0 * math.sqrt(ITEMS_PER_TILE / items_per_m2)


# --------------------------------------------------------------------------------------------
# forest_validate: the collected state and its analysis (pure Python, unit tested)
# --------------------------------------------------------------------------------------------

COLLECT_FN = r'''
fn fpCollect nd ss doBuild = (
    local upm = units.decodeValue "1m"
    format "N|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%\n" (fpEnc nd.name) (nd.mode as string) (fpF nd.units_x) (fpF nd.units_y) (fpB nd.lock_ratio) (fpEnc (nd.mapname as string)) (nd.threshold as string) (fpB nd.densityMap) (nd.distmode as string) (fpB nd.disabled) (fpB nd.manualupdate) (nd.vmesh as string) (fpB nd.surfanim) (nd.direction as string) (fpF upm) to:ss
    for p in @@ITEM_LISTS@@ do format "L|%|%\n" p ((getProperty nd p).count) to:ss
    local nA = 0
    for p in @@AR_LISTS@@ do (
        local c = (getProperty nd p).count
        nA = amax nA c
        format "R|%|%\n" p c to:ss
    )
    local nl = nd.namelist as array, cl = nd.cobjlist as array, gl = nd.geomlist as array, pl = nd.problist as array
    local wl = nd.widthlist as array, hl = nd.heightlist as array, cn = nd.conamelist as array
    local nI = amax #(nl.count, cl.count, gl.count, pl.count, wl.count, hl.count, cn.count)
    if nI > 2000 do nI = 2000
    for i = 1 to nI do (
        local src = if i <= cl.count then cl[i] else undefined
        local sw = "", sh = "", sg = "0"
        if isValidNode src do (
            local bb = nodeGetBoundingBox src src.transform
            local d = bb[2] - bb[1]
            sw = fpF (amax d.x d.y); sh = fpF d.z
            if superclassOf src == GeometryClass do sg = "1"
        )
        format "I|%|%|%|%|%|%|%|%|%|%|%|%\n" i (if i <= gl.count then fpVal gl[i] else "-") (if i <= pl.count then fpVal pl[i] else "-") (if i <= wl.count then fpVal wl[i] else "-") (if i <= hl.count then fpVal hl[i] else "-") (if i <= cl.count then fpState cl[i] else "-") (if isValidNode src then fpEnc src.name else "") sg (if i <= nl.count then fpEnc (nl[i] as string) else "-") (if i <= cn.count then fpEnc (cn[i] as string) else "-") sw sh to:ss
    )
    local atype = nd.artypelist as array, aa = nd.pf_aractivelist as array, ai = nd.arincexclist as array
    local an = nd.arnodelist as array, anm = nd.arnamelist as array
    for i = 1 to nA do (
        local ar = if i <= an.count then an[i] else undefined
        format "A|%|%|%|%|%|%|%\n" i (if i <= atype.count then fpVal atype[i] else "-") (if i <= aa.count then fpVal aa[i] else "-") (if i <= ai.count then fpVal ai[i] else "-") (if i <= an.count then fpState an[i] else "-") (if isValidNode ar then fpEnc ar.name else "") (if i <= anm.count then fpEnc (anm[i] as string) else "-") to:ss
    )
    local sl = nd.surflist as array
    for i = 1 to sl.count do (
        local s = sl[i]
        format "S|%|%|%|%\n" i (fpState s) (if isValidNode s then fpEnc s.name else "") (if isValidNode s then fpF (fpXYArea s 50000) else "0") to:ss
    )
    local bad = fpGeomBad nd
    if bad > 0 then (format "C|-1|0|%\n" bad to:ss) else if doBuild then (nd.trees.update(); format "C|%|1|0\n" (nd.trees.count()) to:ss) else (format "C|-1|0|0\n" to:ss)
)
'''.replace("@@ITEM_LISTS@@", "#(" + ", ".join(f'"{n}"' for n in ITEM_LISTS) + ")").replace("@@AR_LISTS@@", _AR_LISTS_MXS)


def parse_state(report: list[str]) -> dict[str, Any]:
    """The fpCollect report as a dict."""
    state: dict[str, Any] = {"lengths": {}, "area_lengths": {}, "items": [], "areas": [], "surfaces": [],
                             "count": None, "built": False, "crash_index": 0}
    for line in report:
        f = line.split("|")
        tag = f[0]
        if tag == "N":
            state.update(name=unb64(f[1]), mode=int(float(f[2])), units_x=float(f[3]), units_y=float(f[4]),
                         lock_ratio=f[5] == "1", mapname=unb64(f[6]), threshold=int(float(f[7])),
                         density_map=f[8] == "1", distmode=int(float(f[9])), disabled=f[10] == "1",
                         manual_update=f[11] == "1", vmesh=int(float(f[12])), surfanim=f[13] == "1",
                         direction=int(float(f[14])), upm=float(f[15]))
        elif tag == "L":
            state["lengths"][f[1]] = int(f[2])
        elif tag == "R":
            state["area_lengths"][f[1]] = int(f[2])
        elif tag == "I":
            state["items"].append({
                "index": int(f[1]), "geom_raw": f[2], "geom": num(f[2]), "prob": num(f[3]), "width": num(f[4]),
                "height": num(f[5]), "cobj": f[6], "cobj_name": unb64(f[7]), "cobj_geometry": f[8] == "1",
                "label": None if f[9] == "-" else unb64(f[9]), "coname": None if f[10] == "-" else unb64(f[10]),
                "src_width": num(f[11]), "src_height": num(f[12]),
            })
        elif tag == "A":
            state["areas"].append({
                "index": int(f[1]), "type": num(f[2]), "active": f[3] == "1", "incexc": num(f[4]),
                "node": f[5], "node_name": unb64(f[6]), "name": None if f[7] == "-" else unb64(f[7]),
            })
        elif tag == "S":
            state["surfaces"].append({"index": int(f[1]), "state": f[2], "name": unb64(f[3]), "xy_area": float(f[4])})
        elif tag == "C":
            count = int(f[1])
            state.update(count=None if count < 0 else count, built=f[2] == "1", crash_index=int(f[3]))
    return state


def _issue(code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "detail": detail, **extra}


def analyze(state: dict[str, Any]) -> dict[str, Any]:
    """Errors, warnings and the density estimate for a collected forest."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    lengths = state["lengths"]
    n = lengths.get("namelist", 0)
    if n == 0:
        errors.append(_issue("NO_ITEM_TYPES", "namelist is empty, so the forest has no item types"))
    for name in ITEM_LISTS_CORE:
        length = lengths.get(name, 0)
        if length < n:
            errors.append(_issue("ITEM_LIST_SHORT", f"{name} has {length} entries for {n} item types; Forest Pack "
                                 "pads it with values that produce nothing or empty items", list=name,
                                 length=length, expected=n))
        elif length > n:
            warnings.append(_issue("ITEM_LIST_LONG", f"{name} has {length} entries for {n} item types; the extra "
                                   "entries are ignored (is namelist missing an entry?)", list=name, length=length,
                                   expected=n))
    for name in ITEM_LISTS_AUX:
        length = lengths.get(name, 0)
        if 0 < length < n:
            warnings.append(_issue("AUX_LIST_SHORT", f"{name} has {length} entries for {n} item types; Forest Pack "
                                   "pads it with its own defaults", list=name, length=length, expected=n))
        elif length > n:
            warnings.append(_issue("ITEM_LIST_LONG", f"{name} has {length} entries for {n} item types; the extra "
                                   "entries are ignored", list=name, length=length, expected=n))
    for item in state["items"]:
        i = item["index"]
        if i > n:
            continue
        label = item["label"] or f"item {i}"
        geom = item["geom"]
        if item["geom_raw"] != "-":
            if geom == 3:
                errors.append(_issue("CRASH_RISK", f"{label}: geomlist is 3, which crashed Max on a rebuild in "
                                     "testing (no proxy); the forest is not rebuilt", index=i))
            elif geom not in (0.0, 1.0, 2.0):
                errors.append(_issue("GEOM_INVALID", f"{label}: geomlist is {item['geom_raw']} (valid: 1 billboard, "
                                     "2 custom object); the forest is not rebuilt", index=i))
            elif geom == 0:
                errors.append(_issue("GEOM_NONE", f"{label}: geomlist is 0, so this item type produces nothing",
                                     index=i))
            elif geom == 2 and item["cobj"] != "n":
                errors.append(_issue("SOURCE_MISSING", f"{label}: custom object type without a source object "
                                     "(cobjlist entry undefined); its items are empty", index=i))
        if item["prob"] is not None and item["prob"] <= 0:
            errors.append(_issue("PROB_NONPOSITIVE", f"{label}: probability {item['prob']:g} never picks it",
                                 index=i))
        for key in ("width", "height"):
            if item[key] is not None and item[key] <= 0:
                errors.append(_issue("SIZE_NONPOSITIVE", f"{label}: {key} is {item[key]:g}", index=i))
        if item["cobj"] == "n" and item["coname"] not in (None, "") and item["coname"] != item["cobj_name"]:
            warnings.append(_issue("CONAME_MISMATCH", f"{label}: conamelist says '{item['coname']}' but the source is "
                                   f"'{item['cobj_name']}' (renamed after linking)", index=i))
    area_lengths = state["area_lengths"]
    if len(set(area_lengths.values())) > 1:
        errors.append(_issue("AREA_LIST_LENGTH", "the area lists differ in length: " + ", ".join(
            f"{k}={v}" for k, v in sorted(area_lengths.items(), key=lambda kv: (-kv[1], kv[0]))
            if v != max(area_lengths.values()))))
    areas = state["areas"]
    surface_areas = [a for a in areas if a["type"] == 3]
    if not surface_areas:
        warnings.append(_issue("SURFACE_AREA_MISSING", "no Surface Area entry; Forest Pack appends an inactive one "
                               "on the next rebuild"))
    elif len(surface_areas) > 1:
        errors.append(_issue("SURFACE_AREA_COUNT", f"{len(surface_areas)} Surface Area entries (type 3); expected 1"))
    active = [a for a in areas if a["active"]]
    if not active:
        errors.append(_issue("NO_ACTIVE_AREA", "no area is active (stock presets ship their Surface Area inactive); "
                             "set pf_aractivelist for the Surface Area or a spline area"))
    for a in active:
        if a["type"] == 0 and a["node"] != "n":
            errors.append(_issue("AREA_NODE_MISSING", f"active spline area {a['index']} ('{a['name']}') has no "
                                 "spline", index=a["index"]))
        if a["type"] == 3 and not state["surfaces"]:
            errors.append(_issue("SURFACE_LIST_EMPTY", "the Surface Area is active but surflist is empty",
                                 index=a["index"]))
    for s in state["surfaces"]:
        if s["state"] != "n":
            errors.append(_issue("SURFACE_MISSING", f"surflist entry {s['index']} is undefined or deleted",
                                 index=s["index"]))
    mode = state.get("mode", 0)
    if mode == 1:
        warnings.append(_issue("CUSTOM_EDIT_MODE", "custom edit mode: items are frozen, so units, seed and item list "
                               "changes don't regenerate them; switching to procedural discards every edit"))
    elif mode not in (0, 1):
        warnings.append(_issue("MODE_UNKNOWN", f"mode is {mode} (0 procedural and 1 custom edit are known)"))
    if state.get("disabled"):
        warnings.append(_issue("DISABLED", "the forest is disabled"))
    if state.get("manual_update"):
        warnings.append(_issue("MANUAL_UPDATE", "manual update is on; the viewport may show stale items"))

    estimate = None
    estimate_note = None
    default_map = (state.get("mapname", "").lower() == "dense.bmp" and not state.get("density_map")
                   and state.get("distmode") == 0 and state.get("threshold") == 50)
    upm = state.get("upm") or 0.0
    surface_active = any(a["active"] and a["type"] == 3 for a in areas)
    if not default_map:
        estimate_note = "not estimated: the forest doesn't use the default density map (dense.bmp, threshold 50)"
    elif not surface_active:
        estimate_note = "not estimated: only the Surface Area is estimated, and it is inactive"
    elif upm > 0:
        area_m2 = sum(s["xy_area"] for s in state["surfaces"] if s["state"] == "n") / (upm * upm)
        value = estimate_count(area_m2, state["units_x"] / upm, state["units_y"] / upm)
        if value is not None:
            estimate = round(value, 1)
            if value < 1:
                warnings.append(_issue("UNITS_TOO_LARGE", f"about {value:.2f} items expected: the density tile "
                                       f"({state['units_x']:g} x {state['units_y']:g} scene units) is large for "
                                       f"{area_m2:.2f} m2 of surface; lower units_x/units_y"))
    if state.get("crash_index"):
        errors.append(_issue("NOT_REBUILT", "the forest was not rebuilt because geomlist holds an unsafe value"))
    elif state.get("built") and state.get("count") == 0:
        errors.append(_issue("NO_ITEMS", "the forest builds 0 items"))
    return {"errors": errors, "warnings": warnings, "estimated_count": estimate, "estimate_note": estimate_note}


def fix_plan(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Conservative repairs: pad short lists, relink sources by unique name. Never truncates or writes geomlist 3."""
    lengths = state["lengths"]
    n = lengths.get("namelist", 0)
    plan: list[dict[str, Any]] = []
    if n == 0:
        return plan
    items = {item["index"]: item for item in state["items"]}
    relink = [i for i in range(1, min(n, lengths.get("cobjlist", 0)) + 1)
              if items.get(i, {}).get("cobj") != "n" and items.get(i, {}).get("geom") == 2]
    if lengths.get("cobjlist", 0) < n or relink:
        plan.append({"list": "cobjlist", "action": "pad_relink", "from": lengths.get("cobjlist", 0), "to": n,
                     "relink": relink})
    upm = state.get("upm") or 100.0
    for name in ("geomlist", "problist", "widthlist", "heightlist", "conamelist"):
        length = lengths.get(name, 0)
        if length >= n or (name == "conamelist" and length == 0 and lengths.get("cobjlist", 0) == 0):
            continue
        step: dict[str, Any] = {"list": name, "action": "pad", "from": length, "to": n}
        if name in ("widthlist", "heightlist"):
            key = "width" if name == "widthlist" else "height"
            existing = [it[key] for it in state["items"] if it[key] is not None and it[key] > 0]
            step["fallback"] = existing[0] if existing else 0.1 * upm  # 10 cm
        plan.append(step)
    area_lengths = state["area_lengths"]
    if area_lengths and len(set(area_lengths.values())) > 1:
        target = max(area_lengths.values())
        for name, _ in AREA_DEFAULTS:
            length = area_lengths.get(name, 0)
            if length < target:
                plan.append({"list": name, "action": "pad_area", "from": length, "to": target})
    return plan
