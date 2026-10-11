"""itoo_vantage_check: a read-only check of how ready Forest Pack and RailClone objects are for Chaos Vantage.

Fork issue #25. Facts come from the iToo docs (knowledge-pack digests A, C2 and F) and from live probes in
Max 2027.2 with RailClone 7.3.5 and Forest Pack 9.4.3 (see docs/fork/itoo-vantage-check.md). Each finding
carries a basis: "doc" (iToo or Chaos documentation), "live" (measured in a scratch Max), "inf" (inferred,
not verified) or "memory" (the user's own Vantage logs). Vantage itself was never rendered by this tool.

The MAXScript only reads: property reads, sub-material/texmap walks, getXMLOutput/getXMLStyle, trees.count()
and trees.getGeomID. It never rebuilds (no segmentsUpdate, seed = seed, trees.update()), never writes a
property, material or selection, never renders or exports, and never calls RailClone's getStyleDesc() (it
empties the output export until a rebuild). tests/test_itoo_vantage.py lints the script for writes.
"""
from __future__ import annotations

import base64
import math
import re
from typing import Any

from ..coerce import StrList
from ..server import client, mcp

MAX_NAMES = 500
MAX_VISITS = 4000
DEFAULT_BUDGET_S = 30.0
NEAR_CAP = 0.8
ITOO_CLASSES = ("Forest_Pro", "RailClone_Pro", "ForestIvy")
# Class names compared in lower case: classOf prints some as lower case (Output is "output").
BITMAP_CLASSES = frozenset({"bitmaptexture", "vraybitmap", "vrayhdri"})
SOLID_CLASSES = frozenset({"vraycolor"})
# Wrappers that output a constant colour when nothing is plugged into them.
WRAPPER_CLASSES = frozenset({"color_correction", "output", "rgb_tint", "rgb_multiply", "mix", "mask",
                             "compositetexturemap", "vraycomptex"})
COLOR_MAP_CLASSES = frozenset({"forest_color", "railclone_color"})
TINT_MAP_MODES = {0: "Random Values", 1: "As Texture on Surface"}  # [inf] UI order (probe F-2.5)

BASIS = {
    "doc": "iToo or Chaos documentation",
    "live": "measured in a scratch 3ds Max 2027.2 (RailClone 7.3.5, Forest Pack 9.4.3)",
    "inf": "inferred, not verified",
    "memory": "the user's own Vantage 3.4 logs and notes",
}
EXPORT_NOTE = ("RailClone counts come from its last build (getXMLOutput), read as it is and never rebuilt here: a "
               "moved or copied RailClone keeps a stale export, and so does one with autoUpdate off.")

FALLBACK = {
    "railclone": ("If it still fails in Vantage, RailClone Tools can convert it to plain Max instances, which every "
                  "renderer handles: RailClone_Pro.Instantiate mode layer autoDelete separatedMeshes forceInstances "
                  "disableAtEnd (it works on the selection). Use forceInstances=false: true ignores the Material and "
                  "UVW operators. It changes the scene, so ask the user first. Never export a .vrscene (it crashed Max)."),
    "forest": ("If it still fails in Vantage, Forest Tools > Instantiate (UI only) converts the items to Max "
               "instances, which keep their source material IDs (so ID-driven maps work again). It changes the "
               "scene, so ask the user first. Never export a .vrscene (it crashed Max)."),
    "ivy": ("If it still fails in Vantage, turn ForestIvy render instancing off (one mesh) or convert the ivy to a "
            "mesh. Ask the user first. Never export a .vrscene."),
}


class ItooVantageError(RuntimeError):
    """A failed check with a machine-readable code."""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message if message.startswith(code) else f"{code}: {message}")
        self.code = code
        self.retryable = code in {"MAX_BUSY"}
        self.details = details


# --------------------------------------------------------------------------------------------
# MAXScript (read-only). Rows: TAG|field|...; strings are base64; typed values use ivV:
# u undefined, x no such property, b0/b1, i<int>, f<float>, s<b64>, o<b64 class>:<b64 name>.
# --------------------------------------------------------------------------------------------

MXS_LIB = r'''
fn ivB64 s = (dotNetClass "System.Convert").ToBase64String ((dotNetClass "System.Text.Encoding").UTF8.GetBytes (s as string))
fn ivDec s = (dotNetClass "System.Text.Encoding").UTF8.GetString ((dotNetClass "System.Convert").FromBase64String s)
fn ivH n = formattedPrint ((getHandleByAnim n) as integer64) format:"d"
fn ivName v = (
    local r = ""
    if v != undefined do r = try (v.name as string) catch ""
    r
)
fn ivCls v = if v == undefined then "" else ((classOf v) as string)
fn ivV v = (
    if v == undefined then "u"
    else if isKindOf v BooleanClass then (if v then "b1" else "b0")
    else if isKindOf v Integer or isKindOf v Integer64 then ("i" + (formattedPrint v format:"d"))
    else if isKindOf v IntegerPtr then ("i" + (formattedPrint (v as integer64) format:"d"))
    else if isKindOf v Float or isKindOf v Double then ("f" + (formattedPrint v format:".9g"))
    else if isKindOf v String then ("s" + (ivB64 v))
    else if isKindOf v Name then ("s" + (ivB64 (v as string)))
    else ("o" + (ivB64 ((classOf v) as string)) + ":" + (ivB64 (try (v.name as string) catch (v as string))))
)
fn ivP obj prop = if obj != undefined and isProperty obj prop then (ivV (getProperty obj prop)) else "x"
fn ivGet obj prop = if obj != undefined and isProperty obj prop then (getProperty obj prop) else undefined
fn ivArr obj prop = (
    local r = #()
    if obj != undefined and isProperty obj prop do (
        local v = getProperty obj prop
        if v != undefined do r = v as array
    )
    r
)
fn ivAt arr k = if k >= 1 and k <= arr.count then arr[k] else undefined
fn ivNow t0 = (
    local n = timeStamp()
    if n < t0 then (n + 86400000) else n
)
fn ivBudget ctx = (
    ctx[2] += 1
    if ctx[2] > ctx[3] or (ivNow ctx[6]) > ctx[5] do ctx[4] = true
    not ctx[4]
)
fn ivSubsSet t = (
    local c = 0
    local n = try (getNumSubTexmaps t) catch 0
    for i = 1 to n do if (try (getSubTexmap t i) catch undefined) != undefined do c += 1
    c
)
fn ivFile t = (
    local f = undefined
    if isProperty t #filename then f = t.filename
    else if isProperty t #HDRIMapName do f = t.HDRIMapName
    if isKindOf f String then (filenameFromPath f) else ""
)
fn ivColorMap ss t cls path = (
    local s1 = ivGet t #mapbase
    local s1Subs = 0
    local s1File = ""
    if s1 != undefined do (
        s1Subs = ivSubsSet s1
        s1File = ivFile s1
    )
    local ml = ivArr t #maplist
    local mo = ivArr t #maponlist
    local assigned = 0
    local enabled = 0
    for k = 1 to ml.count do (
        if ml[k] != undefined do (
            assigned += 1
            if (ivAt mo k) == true do enabled += 1
        )
    )
    format "CM|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%|%\n" (ivB64 cls) (ivB64 path) (ivB64 (ivName t)) (ivB64 (ivCls s1)) (ivB64 (ivName s1)) s1Subs (ivB64 s1File) assigned enabled ml.count (ivP t #mapidmode) (ivP t #tintmode) (ivP t #tintmapmode) (ivP t #tintmin) (ivP t #tintmax) (ivP t #override) (ivP t #tintvariation) (ivP t #tintmap) to:ss
)
fn ivWalkTex ss ctx t path depth = (
    if t != undefined and depth <= 32 and (findItem ctx[1] t) == 0 and (ivBudget ctx) do (
        append ctx[1] t
        local cls = (classOf t) as string
        if cls == "Forest_Color" or cls == "RailClone_Color" do ivColorMap ss t cls path
        if cls == "VRayMultiSubTex" do format "MS|%|%|%|%\n" (ivB64 path) (ivB64 (ivName t)) (ivP t #from_id) (ivP t #random_by_face_mtl_id) to:ss
        if cls == "MultiTexture" then format "IDM|%|%|%|rnd_by|%\n" (ivB64 cls) (ivB64 path) (ivB64 (ivName t)) (ivP t #rnd_by) to:ss
        else if isProperty t #randByMat do format "IDM|%|%|%|randbymat|%\n" (ivB64 cls) (ivB64 path) (ivB64 (ivName t)) (ivP t #randByMat) to:ss
        local n = try (getNumSubTexmaps t) catch 0
        for i = 1 to n do (
            local sub = try (getSubTexmap t i) catch undefined
            if sub != undefined do ivWalkTex ss ctx sub (path + "/" + (try ((getSubTexmapSlotName t i) as string) catch "?")) (depth + 1)
        )
    )
    ok
)
fn ivWalkMtl ss ctx m path depth = (
    if m != undefined and depth <= 32 and (findItem ctx[1] m) == 0 and (ivBudget ctx) do (
        append ctx[1] m
        local cls = (classOf m) as string
        local nm = ivName m
        local here = path + "/" + nm
        if cls == "Standardmaterial" and matchPattern nm pattern:"FPColor ID*" then format "FPC|%\n" (ivB64 here) to:ss
        else (
            if cls == "Forest_Material" do format "FM|%|%\n" (ivB64 here) (ivB64 nm) to:ss
            local nt = try (getNumSubTexmaps m) catch 0
            for i = 1 to nt do (
                local tx = try (getSubTexmap m i) catch undefined
                if tx != undefined do (
                    local sn = try ((getSubTexmapSlotName m i) as string) catch "?"
                    if matchPattern sn pattern:"*opacity*" or matchPattern sn pattern:"*cutout*" do format "OP|%|%|%|%|%\n" (ivB64 here) (ivB64 cls) (ivB64 sn) (ivB64 ((classOf tx) as string)) (ivB64 (ivName tx)) to:ss
                    ivWalkTex ss ctx tx (here + "/" + sn) (depth + 1)
                )
            )
            local nsub = try (getNumSubMtls m) catch 0
            for i = 1 to nsub do (
                local sm = try (getSubMtl m i) catch undefined
                if sm != undefined do ivWalkMtl ss ctx sm (here + "[" + (i as string) + "]") (depth + 1)
            )
        )
    )
    ok
)
fn ivForest ss nd ctx = (
    local b = nd.baseobject
    format "N|forest|%|%\n" (ivH nd) (ivB64 nd.name) to:ss
    for p in #(#disabled, #vmaxitems, #rmaxitems, #maxFaces, #rendermode, #rmesh, #vmesh, #rskip, #usefakeshadows, #consmat, #tintmode, #tintmapmode, #tintmin, #tintmax, #mode, #manualupdate) do format "K|%|%\n" (p as string) (ivP b p) to:ss
    local cnt = try (nd.trees.count()) catch undefined
    format "K|count|%\n" (ivV cnt) to:ss
    format "K|material|%\n" (ivV nd.material) to:ss
    local nl = ivArr b #namelist
    local gl = ivArr b #geomlist
    local pl = ivArr b #problist
    local cl = ivArr b #cobjlist
    local ml = ivArr b #matList
    local nI = amin nl.count 500
    format "K|item_types|%\n" (ivV nl.count) to:ss
    local rmode = ivGet b #rendermode
    local meshMode = rmode != undefined and rmode != 0
    for k = 1 to nI do (
        local src = ivAt cl k
        local srcOk = isValidNode src
        local srcCls = if srcOk then ((classOf src.baseobject) as string) else ""
        local faces = -1
        -- A V-Ray proxy node's mesh is its viewport preview, not what renders: leave its faces unknown.
        if srcOk and meshMode and srcCls != "VRayProxy" do faces = try (src.mesh.numfaces) catch -1
        local ovr = (ivAt ml k) != undefined
        format "IT|%|%|%|%|%|%|%|%\n" k (ivV (ivAt gl k)) (ivV (ivAt pl k)) (ivB64 ((ivAt nl k) as string)) (ivB64 srcCls) (ivB64 (if srcOk then src.name else "")) faces (ivV ovr) to:ss
    )
    local aa = ivArr b #pf_aractivelist
    local atype = ivArr b #artypelist
    local ainc = ivArr b #arincexclist
    local abound = ivArr b #arboundchecklist
    if not (isProperty b #arboundchecklist) do format "U|boundary_check|%\n" (ivB64 "arboundchecklist is not a property on this build") to:ss
    local nA = amin (amax #(aa.count, atype.count, abound.count)) 200
    for k = 1 to nA do format "AR|%|%|%|%|%\n" k (ivV (ivAt aa k)) (ivV (ivAt atype k)) (ivV (ivAt ainc k)) (ivV (ivAt abound k)) to:ss
    if meshMode and cnt != undefined and cnt > 0 do (
        if cnt > 200000 then format "U|histogram|%\n" (ivB64 "over 200000 items: items per type were not counted") to:ss
        else (
            local hist = #()
            for g = 1 to nI do append hist 0
            local trs = nd.trees
            local stopped = false
            for i = 0 to cnt - 1 while not stopped do (
                local gid = trs.getGeomID i
                if gid >= 1 and gid <= nI do hist[gid] += 1
                if (mod i 20000) == 0 and (ivNow ctx[6]) > ctx[5] do stopped = true
            )
            if stopped then format "U|histogram|%\n" (ivB64 "the time budget ran out while counting items per type") to:ss
            else for g = 1 to nI do format "HIST|%|%\n" g hist[g] to:ss
        )
    )
    for k = 1 to (amin ml.count nI) do (
        if ml[k] != undefined do ivWalkMtl ss ctx ml[k] ("matList[" + (k as string) + "]") 0
    )
    -- A matList override replaces the item's source material inside forest_automat_*: that source material
    -- doesn't render through this forest, so it isn't walked.
    for k = 1 to (amin cl.count nI) do (
        if isValidNode cl[k] and (ivAt ml k) == undefined do ivWalkMtl ss ctx cl[k].material ("item " + (k as string) + " " + cl[k].name) 0
    )
    ivWalkMtl ss ctx nd.material "node" 0
)
fn ivRail ss nd ctx = (
    local b = nd.baseobject
    format "N|railclone|%|%\n" (ivH nd) (ivB64 nd.name) to:ss
    for p in #(#disabled, #rendermode, #rmesh, #vmesh, #nodemat, #proxymode, #autoUpdate, #maxseg, #maxFaces, #createdversion, #proxyfile) do format "K|%|%\n" (p as string) (ivP b p) to:ss
    local pf = ivGet b #proxyfile
    if isKindOf pf String and pf != "" do format "K|proxyfile_exists|%\n" (ivV (doesFileExist pf)) to:ss
    format "K|material|%\n" (ivV nd.material) to:ss
    local nMods = nd.modifiers.count
    for i = 1 to nMods do (
        local md = nd.modifiers[i]
        format "MD|%|%|%|%|%\n" i (ivB64 ((classOf md) as string)) (ivB64 md.name) (ivP md #enabled) (ivP md #enabledInRenders) to:ss
    )
    local api = getInterface b #railclone
    local rx = dotNetClass "System.Text.RegularExpressions.Regex"
    if api == undefined then format "U|interface|%\n" (ivB64 "the object has no railclone interface") to:ss
    else (
        if isProperty api #getXMLOutput then (
            local ox = api.getXMLOutput()
            if ox == undefined do ox = ""
            local total = (rx.Matches ox "<item[ />]").Count
            local segs = rx.Matches ox " source_segment=\"([^\"]*)\""
            format "OUT|%|%|%\n" ox.count total segs.Count to:ss
            local keys = #()
            local counts = #()
            for k = 0 to segs.Count - 1 do (
                local key = (segs.Item k).Groups.Item[1].Value
                local j = findItem keys key
                if j == 0 then (append keys key; append counts 1) else counts[j] += 1
            )
            for j = 1 to keys.count do format "OS|%|%\n" (ivB64 keys[j]) counts[j] to:ss
        ) else format "U|export|%\n" (ivB64 "this RailClone build has no getXMLOutput") to:ss
        if isProperty api #getXMLStyle then (
            local sx = api.getXMLStyle()
            if sx == undefined do sx = ""
            local mops = rx.Matches sx "<node [^>]* class=\"Material\"[^>]*>"
            local onOps = 0
            local offOps = 0
            for k = 0 to mops.Count - 1 do (
                local tag = (mops.Item k).Value
                if (findString tag "baseClass=\"Operator\"") != undefined do (
                    if (findString tag " on=\"0\"") != undefined then offOps += 1 else onOps += 1
                )
            )
            local uvw = (rx.Matches sx "<node [^>]* class=\"UVW\"[^>]*>").Count
            format "XS|%|%|%|%\n" sx.count onOps offOps uvw to:ss
        ) else format "U|style|%\n" (ivB64 "this RailClone build has no getXMLStyle") to:ss
    )
    local sid = ivArr b #sid
    local snm = ivArr b #sname
    local sref = ivArr b #sobjref
    local snode = ivArr b #sobjnode
    local smtl = ivArr b #sobjmtl
    local smap = ivArr b #smapping
    local nS = amin sid.count 500
    for k = 1 to nS do (
        local rr = ivAt sref k
        local srcNode = ivAt snode k
        local srcOk = isValidNode srcNode
        local rCls = ivCls rr
        local nCls = if srcOk then ((classOf srcNode.baseobject) as string) else ""
        local faces = -1
        if rCls != "VRayProxy" and nCls != "VRayProxy" do (
            faces = try ((rr.mesh).numfaces) catch -1
            if faces < 0 and srcOk do faces = try (srcNode.mesh.numfaces) catch -1
        )
        if (rCls == "VRayProxy" or nCls == "VRayProxy") and (ivGet b #rendermode) == false do ctx[7][1] = true
        local sm = ivAt smtl k
        local smSubs = if sm != undefined then (try (getNumSubMtls sm) catch -1) else 0
        format "SG|%|%|%|%|%|%|%|%|%\n" k (ivB64 ((ivAt snm k) as string)) (ivB64 rCls) (ivB64 nCls) (ivB64 (if srcOk then srcNode.name else "")) faces (ivV (ivAt smap k)) (ivB64 (ivCls sm)) smSubs to:ss
    )
    for k = 1 to (amin smtl.count nS) do (
        if smtl[k] != undefined do ivWalkMtl ss ctx smtl[k] ("sobjmtl[" + (k as string) + "]") 0
    )
    if (ivGet b #nodemat) == true do (
        for k = 1 to (amin snode.count nS) do (
            if isValidNode snode[k] do ivWalkMtl ss ctx snode[k].material ("segment " + (k as string) + " source " + snode[k].name) 0
        )
    )
    ivWalkMtl ss ctx nd.material "node" 0
)
fn ivIvy ss nd ctx = (
    local b = nd.baseobject
    format "N|ivy|%|%\n" (ivH nd) (ivB64 nd.name) to:ss
    for p in #(#renderinstancing, #maxbranches, #maxleaves) do format "K|%|%\n" (p as string) (ivP b p) to:ss
    format "K|material|%\n" (ivV nd.material) to:ss
    ivWalkMtl ss ctx nd.material "node" 0
)
'''

MXS_MAIN = r'''
local ss = stringStream ""
local t0 = timeStamp()
local deadline = t0 + @@BUDGET_MS@@
format "IVC1\n" to:ss
format "S|save_required_before|%\n" (ivV (getSaveRequired())) to:ss
format "S|renderer|%\n" (ivV ((classOf renderers.current) as string)) to:ss
format "S|max_version|%\n" (ivV ((maxVersion())[1])) to:ss
local needVer = #(false)
for l in lights do (
    if isProperty l #shadowGenerator do (
        local sg = l.shadowGenerator
        if sg != undefined and ((classOf sg) as string) == "XShadow" do format "XL|%|%\n" (ivH l) (ivB64 l.name) to:ss
    )
)
local wanted = @@NAMES@@
local targets = #()
if wanted.count == 0 then (
    for o in objects do (
        local c = (classOf o.baseobject) as string
        if c == "Forest_Pro" or c == "RailClone_Pro" or c == "ForestIvy" do append targets o
    )
) else (
    for w in wanted do (
        local found = getNodeByName (ivDec w) exact:true all:true
        if found.count == 0 do format "MISS|%\n" w to:ss
        for o in found do (
            local c = (classOf o.baseobject) as string
            if c == "Forest_Pro" or c == "RailClone_Pro" or c == "ForestIvy" then appendIfUnique targets o
            else format "NI|%|%\n" (ivB64 o.name) (ivB64 c) to:ss
        )
    )
)
format "S|targets|%\n" (ivV targets.count) to:ss
for i = 1 to targets.count do (
    local nd = targets[i]
    if (ivNow t0) > deadline then format "SKIP|%|%\n" (ivH nd) (ivB64 nd.name) to:ss
    else (
        local ctx = #(#(), 0, @@MAX_VISITS@@, false, deadline, t0, needVer)
        local c = (classOf nd.baseobject) as string
        local n0 = ivNow t0
        try (
            if c == "Forest_Pro" then ivForest ss nd ctx
            else if c == "RailClone_Pro" then ivRail ss nd ctx
            else ivIvy ss nd ctx
        ) catch (
            format "NERR|%\n" (ivB64 (getCurrentException())) to:ss
        )
        if ctx[4] do format "WT|%\n" ctx[2] to:ss
        format "NE|%\n" ((ivNow t0) - n0) to:ss
    )
)
if needVer[1] do (
    local proc = (dotNetClass "System.Diagnostics.Process").GetCurrentProcess()
    local mods = proc.Modules
    local rcVer = undefined
    for i = 0 to mods.Count - 1 while rcVer == undefined do (
        local md = mods.Item[i]
        if matchPattern md.ModuleName pattern:"railclone*.dlo" do rcVer = md.FileVersionInfo.FileVersion
    )
    format "S|railclone_version|%\n" (ivV rcVer) to:ss
)
format "S|save_required_after|%\n" (ivV (getSaveRequired())) to:ss
format "S|elapsed_ms|%\n" (ivV ((ivNow t0) - t0)) to:ss
format "END\n" to:ss
ss as string
'''


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _unb64(text: str) -> str:
    return base64.b64decode(text, validate=True).decode("utf-8") if text else ""


def build_script(names: list[str], budget_ms: int, max_visits: int = MAX_VISITS) -> str:
    """The whole read-only MAXScript: helpers, then the scan inside try/catch. Names travel as base64."""
    wanted = "#(" + ", ".join(f'"{_b64(n)}"' for n in names) + ")"
    body = (MXS_MAIN.replace("@@NAMES@@", wanted).replace("@@BUDGET_MS@@", str(int(budget_ms)))
            .replace("@@MAX_VISITS@@", str(int(max_visits))))
    return "(\n" + MXS_LIB + "\ntry (\n" + body + '\n) catch ("IVERR|" + (getCurrentException() as string))\n)'


# --------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------

class _Missing:
    """A property that does not exist on this build (row value "x")."""

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


def _dv(field: str) -> Any:
    tag, body = field[:1], field[1:]
    if tag == "u":
        return None
    if tag == "x":
        return MISSING
    if tag == "b":
        return body == "1"
    if tag == "i":
        return int(body)
    if tag == "f":
        try:
            value = float(body)
        except ValueError:
            return None
        return value if math.isfinite(value) else None
    if tag == "s":
        return _unb64(body)
    if tag == "o":
        cls, _, name = body.partition(":")
        return {"class": _unb64(cls), "name": _unb64(name)}
    raise ValueError(f"unknown value {field[:20]!r}")


def _new_object(kind: str, handle: int, name: str) -> dict[str, Any]:
    return {"kind": kind, "handle": handle, "name": name, "k": {}, "items": [], "areas": [], "hist": {},
            "mods": [], "out": None, "out_segs": {}, "style": None, "segs": [], "cm": [], "op": [], "fm": [],
            "ms": [], "idm": [], "fpc": [], "u": [], "walk_truncated": None, "error": None, "elapsed_ms": None}


def parse_report(raw: Any) -> dict[str, Any]:
    """The IVC1 report as facts: scene values, missing names, skipped nodes and one record per object."""
    if not isinstance(raw, str):
        raise ItooVantageError("INVALID_READBACK", f"the check returned no report: {str(raw)[:200]}")
    if raw.startswith("IVERR|"):
        raise ItooVantageError("MAXSCRIPT_ERROR", raw.split("|", 1)[1].strip()[:2000])
    if raw.startswith("__MCP_MS_ERR__:"):
        raise ItooVantageError("MAXSCRIPT_ERROR", raw[len("__MCP_MS_ERR__:"):].strip()[:2000])
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "IVC1":
        raise ItooVantageError("INVALID_READBACK", f"expected an IVC1 report, got: {raw[:200]}")
    if "END" not in lines:
        raise ItooVantageError("INVALID_READBACK", "the report is incomplete (no END line)")
    facts: dict[str, Any] = {"scene": {}, "missing": [], "not_itoo": [], "skipped": [], "xshadow_lights": [],
                             "objects": []}
    current: dict[str, Any] | None = None
    try:
        for line in lines[1:lines.index("END")]:
            if not line:
                continue
            f = line.split("|")
            tag = f[0]
            if tag == "S":
                facts["scene"][f[1]] = _dv(f[2])
            elif tag == "MISS":
                facts["missing"].append(_unb64(f[1]))
            elif tag == "NI":
                facts["not_itoo"].append({"name": _unb64(f[1]), "class": _unb64(f[2])})
            elif tag == "SKIP":
                facts["skipped"].append({"handle": int(f[1]), "name": _unb64(f[2])})
            elif tag == "XL":
                facts["xshadow_lights"].append({"handle": int(f[1]), "name": _unb64(f[2])})
            elif tag == "N":
                current = _new_object(f[1], int(f[2]), _unb64(f[3]))
                facts["objects"].append(current)
            elif current is None:
                raise ValueError(f"{tag} row before any object")
            elif tag == "K":
                current["k"][f[1].lower()] = _dv(f[2])
            elif tag == "IT":
                current["items"].append({"index": int(f[1]), "geom": _dv(f[2]), "prob": _dv(f[3]),
                                         "name": _unb64(f[4]), "source_class": _unb64(f[5]) or None,
                                         "source": _unb64(f[6]) or None, "faces": int(f[7]),
                                         # older reports have no override field
                                         "override": _dv(f[8]) if len(f) > 8 else None})
            elif tag == "AR":
                current["areas"].append({"index": int(f[1]), "active": _dv(f[2]), "type": _dv(f[3]),
                                         "incexc": _dv(f[4]), "boundary": _dv(f[5])})
            elif tag == "HIST":
                current["hist"][int(f[1])] = int(f[2])
            elif tag == "MD":
                current["mods"].append({"index": int(f[1]), "class": _unb64(f[2]), "name": _unb64(f[3]),
                                        "enabled": _dv(f[4]), "in_renders": _dv(f[5])})
            elif tag == "OUT":
                current["out"] = {"chars": int(f[1]), "items": int(f[2]), "sourced": int(f[3])}
            elif tag == "OS":
                current["out_segs"][_unb64(f[1])] = int(f[2])
            elif tag == "XS":
                current["style"] = {"chars": int(f[1]), "material_ops": int(f[2]), "material_ops_off": int(f[3]),
                                    "uvw_ops": int(f[4])}
            elif tag == "SG":
                current["segs"].append({"index": int(f[1]), "name": _unb64(f[2]), "ref_class": _unb64(f[3]) or None,
                                        "source_class": _unb64(f[4]) or None, "source": _unb64(f[5]) or None,
                                        "faces": int(f[6]), "mapping": _dv(f[7]),
                                        "sobjmtl_class": _unb64(f[8]) or None, "sobjmtl_subs": int(f[9])})
            elif tag == "CM":
                current["cm"].append({
                    "class": _unb64(f[1]), "path": _unb64(f[2]), "name": _unb64(f[3]),
                    "slot1_class": _unb64(f[4]) or None, "slot1_name": _unb64(f[5]) or None,
                    "slot1_inputs": int(f[6]), "slot1_file": _unb64(f[7]) or None,
                    "maps_assigned": int(f[8]), "maps_enabled": int(f[9]), "map_slots": int(f[10]),
                    "mapidmode": _dv(f[11]), "tintmode": _dv(f[12]), "tintmapmode": _dv(f[13]),
                    "tintmin": _dv(f[14]), "tintmax": _dv(f[15]), "override": _dv(f[16]),
                    "tintvariation": _dv(f[17]), "tintmap": _dv(f[18])})
            elif tag == "OP":
                current["op"].append({"path": _unb64(f[1]), "material_class": _unb64(f[2]), "slot": _unb64(f[3]),
                                      "map_class": _unb64(f[4]), "map_name": _unb64(f[5])})
            elif tag == "FM":
                current["fm"].append({"path": _unb64(f[1]), "name": _unb64(f[2])})
            elif tag == "MS":
                current["ms"].append({"path": _unb64(f[1]), "name": _unb64(f[2]), "from_id": _dv(f[3]),
                                      # older reports have no random_by_face_mtl_id field
                                      "by_face_id": _dv(f[4]) if len(f) > 4 else MISSING})
            elif tag == "IDM":
                current["idm"].append({"class": _unb64(f[1]), "path": _unb64(f[2]), "name": _unb64(f[3]),
                                       "prop": f[4], "value": _dv(f[5])})
            elif tag == "FPC":
                current["fpc"].append(_unb64(f[1]))
            elif tag == "U":
                current["u"].append({"check": f[1], "reason": _unb64(f[2])})
            elif tag == "WT":
                current["walk_truncated"] = int(f[1])
            elif tag == "NERR":
                current["error"] = _unb64(f[1])
            elif tag == "NE":
                current["elapsed_ms"] = int(float(f[1]))
            else:
                raise ValueError(f"unknown row {tag!r}")
    except (ValueError, IndexError, UnicodeError) as exc:
        raise ItooVantageError("INVALID_READBACK", f"malformed check report: {exc}") from exc
    return facts


# --------------------------------------------------------------------------------------------
# Analysis (pure Python, unit tested)
# --------------------------------------------------------------------------------------------

# code: (severity, basis, message, fix)
CODES: dict[str, tuple[str, str, str, str | None]] = {
    # 1. Colour maps
    "COLORMAP_SLOT1_EMPTY": (
        "error", "doc+live",
        "Vantage doesn't support Forest Color / RailClone Color: it bypasses the map and renders input slot 1 (the "
        "Default Map, mapbase), which is empty, so the texture is lost.",
        "Put the intended texture in the map's Default Map slot (slot 1)."),
    "COLORMAP_SLOT1_FLAT": (
        "error", "doc+live",
        "Vantage bypasses the colour map and renders input slot 1, which holds a flat colour (a solid colour map, or "
        "a wrapper with nothing plugged in), so the texture is lost.",
        "Put the intended texture in the Default Map slot (slot 1)."),
    "COLORMAP_SLOT1": (
        "info", "doc+live", "Vantage bypasses the colour map and renders what input slot 1 (Default Map) holds.", None),
    "COLORMAP_SLOT1_NESTED": (
        "info", "inf", "Slot 1 holds another iToo colour map, which Vantage bypasses too; its own slot 1 is checked "
        "separately.", None),
    "COLORMAP_VARIATION_LOST": (
        "warn", "doc",
        "Vantage bypasses Forest Color / RailClone Color, so its tint and its random per-item map variation are "
        "always lost.",
        "If the variation matters, use VRayMultiSubTex random by node handle / instance ID instead."),
    "FORESTCOLOR_GPU_TINT_FROM_MAP": (
        "warn", "doc+inf",
        "Forest Color tints from a map ('Get color from map', tintmode 1 [inf]): V-Ray GPU doesn't support that in "
        "'Random Values' or 'As Texture on Surface' mode.",
        "Use the gradient tint or the map slots instead."),
    # 2. Opacity
    "OPACITY_MAPPED": (
        "info", "doc",
        "Opacity-mapped material: Vantage has Opacity Maps off by default. Turn on Renderer > Advanced > Opacity Maps "
        "(Clip Mode) in Vantage (it can't be set from a script).",
        "Prefer mesh leaves over alpha cards."),
    "OPACITY_MAP_NOT_BITMAP": (
        "warn", "memory",
        "The opacity slot holds a procedural or wrapper map; Vantage's docs list those as not supported in opacity "
        "mapping.",
        "Bake one repeat to a bitmap and use a VRayBitmap with the same real-world size."),
    # 3. CPU-only features
    "CPU_ONLY_FOREST_MATERIAL": (
        "warn", "doc",
        "Forest Material is documented for V-Ray (CPU) and Scanline only; assume it doesn't work in V-Ray GPU or "
        "Vantage.", "Use the item's own material, with Forest Color for variation."),
    "CPU_ONLY_BILLBOARD": (
        "warn", "doc+live",
        "2D billboard item (geomlist 1): the 2D library is documented for Scanline, Mental Ray and V-Ray (CPU) only.",
        "Use 3D meshes."),
    "XSHADOW_BILLBOARDS": (
        "warn", "doc+memory",
        "Billboards with Forest's fake (XShadow) shadows, which don't work in V-Ray: assign the main light in the "
        "Forest Shadows rollout instead.", None),
    "EDGE_MODE_ON_PROXIES": (
        "warn", "doc+inf",
        "An active area uses Edge boundary mode (arboundchecklist 2 [inf: 0 Point, 1 Size, 2 Edge; not confirmed "
        "live]) with V-Ray proxy items: Forest Pack 9 documents that as V-Ray CPU only.",
        "Use Point mode or tighter areas."),
    "EDGE_MODE_UNVERIFIED": (
        "info", "doc+inf",
        "An active area uses Edge boundary mode (arboundchecklist 2 [inf]); the docs don't say how it renders in "
        "V-Ray GPU or Vantage: check it in the Vantage view.", None),
    "IVY_RENDER_INSTANCING": (
        "warn", "doc",
        "ForestIvy render instancing is documented for V-Ray CPU and Arnold only: on V-Ray GPU / Vantage expect one "
        "big mesh. Check the branch and leaf limits.", None),
    "XSHADOW_LIGHT": (
        "warn", "doc+inf",
        "A light uses Forest Pack's XShadow shadow generator (fake shadows for billboards); assume V-Ray GPU and "
        "Vantage ignore it.", "Use the light's V-Ray shadows."),
    # 4. Material IDs
    "ID_MAP_IN_FOREST": (
        "warn", "doc+live",
        "A map keyed on face material ID on a Forest item (VRayMultiSubTex by face material ID, from_id 0, or random "
        "with 'By face MtlID'; BerconTile / BerconGradient random by material ID): Forest consolidates the item "
        "materials into forest_automat_* and renumbers the face IDs, so the map picks other entries or loses its "
        "variation.",
        "Use Forest Color or VRayMultiSubTex random by node/instance, or turn Consolidate Materials off and assign a "
        "hand-built Multi/Sub to the Forest node."),
    "ID_MAP_UNCONSOLIDATED": (
        "info", "doc+live",
        "A map keyed on face material ID with Consolidate Materials off: the faces keep the source IDs; check them "
        "against the node material.", None),
    "FPCOLOR_HARMLESS": (
        "info", "memory",
        "'FPColor ID n' Standard materials in forest_automat_* only drive the viewport Color ID display; rendered "
        "faces never use them and Forest rebuilds them. Leave them as they are.", None),
    "FOREST_NO_AUTOMATERIAL": (
        "info", "live", "The Forest node has no material yet: forest_automat_* is created at the first build.", None),
    "FOREST_NODE_MATERIAL": (
        "info", "doc+live",
        "Consolidate Materials is off and the node has its own material; check that its IDs match the source "
        "objects' face IDs.", None),
    "FOREST_NODE_MATERIAL_REPLACED": (
        "warn", "doc",
        "The Forest node's material isn't forest_automat_*: a manual assignment overrides Forest's consolidated "
        "material.", "Change materials on the source objects or with matList, never on the Forest node."),
    # 5. RailClone settings
    "RC_DISABLED": (
        "info", "live",
        "The RailClone object is disabled: 0 faces and an empty output, so it renders nothing in Max or Vantage.",
        None),
    "RC_MODIFIERS": (
        "warn", "doc",
        "Modifiers on a RailClone object are incompatible with its native instancing (memory and Vantage load grow).",
        "Remove them; deform the spline instead (RC_Spline / RC_Slice go on the spline)."),
    "RC_MODIFIERS_OFF": (
        "info", "inf", "The node's modifiers are off in renders, so they shouldn't affect the render.", None),
    "RC_INSTANCING_OFF": (
        "info", "doc+live",
        "Use Instancing Engine is off: RailClone renders one mesh (more memory). Toggling it changed nothing in the "
        "viewport or the export in testing, so its render-time effect is unverified.", None),
    "RC_RENDER_BOXES": (
        "warn", "doc+live",
        "Render mode is Boxes (rmesh 1): the object renders as boxes (render-time only: the viewport didn't change in "
        "testing).", "Set Render to Mesh."),
    "RC_SEGMENT_MATERIAL": (
        "warn", "doc",
        "Use Segment Material is on: a render-time feature documented for V-Ray and Arnold only; Vantage is "
        "unverified: check it in the Vantage view and log.", None),
    "RC_MATERIAL_OPERATOR": (
        "warn", "doc+live",
        "Material operator in the style: under the instancing engine it works in V-Ray only per the docs; Vantage is "
        "unverified: check it in the Vantage view and log.", None),
    "RC_MATERIAL_OPERATOR_MESH": (
        "info", "doc", "Material operator in the style; with the instancing engine off it works in every renderer.",
        None),
    "RC_SEGMENT_MAPPING": (
        "warn", "doc+live",
        "Segment Deform > Mapping (smapping): under the instancing engine it works in V-Ray only per the docs; Vantage "
        "is unverified: check it in the Vantage view and log.", None),
    "RC_SEGMENT_MAPPING_MESH": (
        "info", "doc", "Segment Deform > Mapping (smapping); with the instancing engine off it works in every renderer.",
        None),
    "RC_PROXY_CACHE_EMBEDDED": (
        "info", "doc",
        "Proxy cache (Embedded) is on: the cached result depends on the renderer that was active when it was baked; "
        "re-bake after switching renderer.", None),
    "RC_PROXY_CACHE_FILE": (
        "info", "doc",
        "Proxy cache (External file) is on: the result depends on the renderer active at bake time, and Use Segment "
        "Material and lights survive only in Embedded mode.", None),
    "RC_PROXY_FILE_MISSING": ("error", "live", "The external proxy cache file doesn't exist.", None),
    "RC_PROXY_MESH_REVERTABLE": (
        "info", "doc",
        "V-Ray proxy segments with the instancing engine off: since RailClone 7, V-Ray proxies no longer need full "
        "mesh mode for V-Ray GPU and Vantage, so instancing can go back on.", None),
    "RC_SOBJMTL_NOT_ONE_SLOT_MULTI": (
        "warn", "memory+live",
        "Segment materials (sobjmtl) that aren't one-slot Multimaterials: Vantage 3.4 logged 'Unsupported nested "
        "MtlSingleBRDF' for them, and wrapping each in a one-slot Multimaterial fixed it. (Writing sobjmtl changed no "
        "face IDs in testing; the node material drives the look.)",
        "Put each segment material in a one-slot Multimaterial and assign that to sobjmtl."),
    # 6. Caps
    "RC_EXPORT_EMPTY": (
        "warn", "live",
        "RailClone's export has no items: nothing was generated (no spline; the maxseg or maxFaces cap was exceeded, "
        "which gives 0 items; or a script cleared the export), or it was never built.",
        "A rebuild refreshes it (get_railclone_output rebuilds); this check never rebuilds."),
    "RC_AUTOUPDATE_OFF": (
        "info", "live",
        "autoUpdate is off: no scripted call rebuilds, so the export and these counts may predate the last change.",
        None),
    "RC_MAXSEG_UNLIMITED": ("info", "doc", "The segment limit (maxseg) is 0: no limit.", None),
    "RC_MAXSEG_EXCEEDED": (
        "error", "live",
        "The export holds more segments than maxseg: over the cap RailClone generates nothing (0 items), so this "
        "export predates the cap.", None),
    "RC_MAXSEG_NEAR": (
        "warn", "live", "The segment count is close to maxseg: over it RailClone generates nothing at all.", None),
    "RC_MAXFACES_UNLIMITED": ("info", "doc", "The face limit (maxFaces) is 0: no limit.", None),
    "RC_MAXFACES_EXCEEDED": (
        "error", "live",
        "The estimated faces (items x segment faces; instanced items count too) are over maxFaces: over the cap "
        "RailClone generates nothing.", None),
    "RC_MAXFACES_NEAR": (
        "warn", "live",
        "The estimated faces (items x segment faces; instanced items count too) are close to maxFaces: over it "
        "RailClone generates nothing.", None),
    "FOREST_DISABLED": ("info", "doc", "The Forest object is disabled: it renders nothing in Max or Vantage.", None),
    "FOREST_NO_ITEMS": (
        "warn", "live", "trees.count() is 0: nothing to render, or the forest isn't built yet (this check never "
        "builds it).", "Run forest_validate."),
    "FOREST_VIEWPORT_THINNING": (
        "warn", "live",
        "trees.count() may be thinned by the viewport Max Items limit (vmaxitems): when the full count N is over it, "
        "Forest keeps every k-th grid cell (k = ceil(sqrt(N / vmaxitems))), so the render count can be about 4x (or "
        "9x...) the viewport count.",
        "To read the real count, set vmaxitems to 0 and rebuild (that writes; this check doesn't)."),
    "FOREST_RENDER_MAX_ITEMS_UNLIMITED": (
        "info", "doc", "Max Items (render) is 0, no limit: huge counts can slow down or crash Max.", None),
    "FOREST_RENDER_MAX_ITEMS_EXCEEDED": (
        "error", "doc",
        "The item count is over Max Items (render): Forest then builds an 'F' icon instead of the items, so none of "
        "them render.", "Raise rmaxitems or lower the density."),
    "FOREST_RENDER_MAX_ITEMS_RISK": (
        "warn", "doc+live",
        "If the viewport count is thinned, the render count (about 4x or more) is over Max Items (render), and Forest "
        "would render an 'F' icon instead of the items.", None),
    "FOREST_RENDER_MAX_ITEMS_NEAR": (
        "warn", "doc", "The item count is close to Max Items (render); over it Forest renders an 'F' icon instead.",
        None),
    "FOREST_MESH_RENDER_MODE": (
        "warn", "doc+inf",
        "Render Mode isn't Automatic (rendermode 0, the default; other values are Meshes [inf]): Forest builds one mesh "
        "with no instancing, and Max Faces applies.", "Set Render Mode to Automatic."),
    "FOREST_MAX_FACES_EXCEEDED": (
        "error", "doc",
        "In Meshes render mode the estimated faces are over Max Faces: Forest then renders an 'F' icon instead of "
        "the items.", None),
    "FOREST_MAX_FACES_NEAR": ("warn", "doc", "In Meshes render mode the estimated faces are close to Max Faces.", None),
    "FOREST_RENDER_DISPLAY": (
        "warn", "doc+inf",
        "The render display mode isn't Mesh (rmesh 0, the default): items may render as proxies (planes, boxes...).",
        None),
    "FOREST_RENDER_SIMPLIFICATION": (
        "info", "doc", "Render Simplification is on: fewer items render than are built.", None),
    "GEOMLIST_CRASH_RISK": ("error", "live", "geomlist 3 crashed Max on a rebuild in testing.", None),
    "GEOMLIST_UNKNOWN": ("warn", "live", "Unknown geomlist value (0 gap, 1 billboard, 2 custom object).", None),
    # Scene
    "RENDERER_NOT_VRAY": (
        "info", "doc", "The current renderer isn't V-Ray: Vantage Live Link needs V-Ray GPU, and some findings assume "
        "V-Ray.", None),
    "FALLBACK": (
        "info", "doc+memory",
        "Objects with a feature Vantage may not translate (colour map slot 1, CPU-only features, ID-driven maps) have "
        "a 'fallback': convert them to plain instances (RailClone Tools Instantiate or Forest Tools) when they can't "
        "be fixed. Never export a .vrscene.", None),
}
# Findings that a conversion to plain instances can work around: Vantage / V-Ray GPU translation problems. A
# disabled object, an exceeded cap or a missing proxy file is fixed in Max, and converting wouldn't help.
FALLBACK_CODES = frozenset({
    "COLORMAP_SLOT1_EMPTY", "COLORMAP_SLOT1_FLAT", "CPU_ONLY_FOREST_MATERIAL", "CPU_ONLY_BILLBOARD",
    "EDGE_MODE_ON_PROXIES", "IVY_RENDER_INSTANCING", "ID_MAP_IN_FOREST",
})


def _short(path: str, limit: int = 160) -> str:
    return path if len(path) <= limit else path[:40] + " ... " + path[-(limit - 45):]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value is MISSING:
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _version(text: Any) -> list[int] | None:
    if not isinstance(text, str):
        return None
    parts = [int(p) for p in re.findall(r"\d+", text)]
    return parts or None


def _cls(name: Any) -> str:
    return name.lower() if isinstance(name, str) else ""


class _Report:
    """Findings of one object (or the scene), aggregated by code. Messages live in CODES."""

    def __init__(self, max_paths: int):
        self.max_paths = max_paths
        self.findings: list[dict[str, Any]] = []
        self.unknown: list[dict[str, str]] = []
        self._by_code: dict[str, dict[str, Any]] = {}

    def add(self, code: str, where: str | None = None) -> None:
        found = self._by_code.get(code)
        if found is None:
            found = {"severity": CODES[code][0], "code": code, "count": 0}
            self._by_code[code] = found
            self.findings.append(found)
        found["count"] += 1
        if where:
            places = found.setdefault("where", [])
            if len(places) < self.max_paths:
                places.append(_short(where))
            else:
                found["where_truncated"] = True

    def unknown_check(self, check: str, reason: str) -> None:
        entry = {"check": check, "reason": reason}
        if entry not in self.unknown:
            self.unknown.append(entry)

    def status(self) -> str:
        severities = {f["severity"] for f in self.findings}
        return "error" if "error" in severities else "warn" if "warn" in severities else "ok"

    def sorted_findings(self) -> list[dict[str, Any]]:
        order = {"error": 0, "warn": 1, "info": 2}
        return sorted(self.findings, key=lambda f: order.get(f["severity"], 3))


def _slot1_text(cm: dict[str, Any]) -> str:
    if not cm["slot1_class"]:
        return "empty"
    text = f"{cm['slot1_class']} '{cm['slot1_name'] or ''}'"
    if cm["slot1_file"]:
        text += f" ({cm['slot1_file']})"
    return text


def _is_flat(cm: dict[str, Any]) -> bool:
    cls = _cls(cm["slot1_class"])
    return cls in SOLID_CLASSES or (cls in WRAPPER_CLASSES and cm["slot1_inputs"] == 0)


def _color_maps(rep: _Report, obj: dict[str, Any], forest_tint: dict[str, Any] | None) -> None:
    for cm in obj["cm"]:
        where = f"{cm['path']}: {cm['class']} '{cm['name']}', slot 1 = {_slot1_text(cm)}"
        if not cm["slot1_class"]:
            rep.add("COLORMAP_SLOT1_EMPTY", where)
        elif _is_flat(cm):
            rep.add("COLORMAP_SLOT1_FLAT", where)
        else:
            rep.add("COLORMAP_SLOT1", where)
            if _cls(cm["slot1_class"]) in COLOR_MAP_CLASSES:
                rep.add("COLORMAP_SLOT1_NESTED", where)
        rep.add("COLORMAP_VARIATION_LOST",
                f"{cm['path']}: {cm['maps_assigned']} of {cm['map_slots']} map slots set ({cm['maps_enabled']} "
                f"enabled), Get ID from {cm['mapidmode']}, tintmode {cm['tintmode']}, tint min/max "
                f"{cm['tintmin']}/{cm['tintmax']}, override {cm['override']}")
        if _cls(cm["class"]) == "forest_color" and forest_tint is not None:
            source = cm if cm["override"] is True else forest_tint
            mode, map_mode = source.get("tintmode"), source.get("tintmapmode")
            tmin, tmax = _num(source.get("tintmin")), _num(source.get("tintmax"))
            if mode is MISSING or mode is None or map_mode is MISSING:
                rep.unknown_check("forest_color_gpu_mode", "the tint mode properties could not be read")
            elif mode == 1 and not (tmin == 0 and tmax == 0):
                owner = "the map (override on)" if source is cm else "the Forest object (override off)"
                rep.add("FORESTCOLOR_GPU_TINT_FROM_MAP",
                        f"{cm['path']}: tint from {owner}, tintmapmode {map_mode} "
                        f"({TINT_MAP_MODES.get(map_mode, 'unknown')} [inf])")


def _opacity(rep: _Report, obj: dict[str, Any]) -> None:
    for op in obj["op"]:
        where = f"{op['path']} [{op['slot']}] = {op['map_class']} '{op['map_name']}'"
        rep.add("OPACITY_MAPPED", where)
        if _cls(op["map_class"]) not in BITMAP_CLASSES:
            rep.add("OPACITY_MAP_NOT_BITMAP", where)


def _materials(rep: _Report, obj: dict[str, Any], forest_tint: dict[str, Any] | None) -> None:
    _color_maps(rep, obj, forest_tint)
    _opacity(rep, obj)
    for fm in obj["fm"]:
        rep.add("CPU_ONLY_FOREST_MATERIAL", fm["path"])
    for ms in obj["ms"]:
        if ms["from_id"] is MISSING or ms["from_id"] is None:
            rep.unknown_check("multisubtex_mode", f"{ms['path']}: VRayMultiSubTex mode (from_id) not readable")


def _common(rep: _Report, obj: dict[str, Any]) -> None:
    for u in obj["u"]:
        rep.unknown_check(u["check"], u["reason"])
    if obj["walk_truncated"] is not None:
        rep.unknown_check("materials", f"the material walk stopped after {obj['walk_truncated']} materials and maps "
                                       "(visit cap or time budget); later ones were not checked")
    if obj["error"]:
        rep.unknown_check("read", f"reading the object failed part-way: {obj['error'][:300]}")
    for key, value in obj["k"].items():
        if value is MISSING:
            rep.unknown_check(key, "no such property on this build")


def analyze_forest(obj: dict[str, Any], max_paths: int, details: bool = False) -> dict[str, Any]:
    rep = _Report(max_paths)
    k = obj["k"]
    _common(rep, obj)
    count = k.get("count") if isinstance(k.get("count"), int) and not isinstance(k.get("count"), bool) else None
    vmax, rmax = _num(k.get("vmaxitems")), _num(k.get("rmaxitems"))
    rendermode = k.get("rendermode")
    consolidate = k.get("consmat")
    if k.get("disabled") is True:
        rep.add("FOREST_DISABLED")
    elif count == 0:
        rep.add("FOREST_NO_ITEMS")
    if count is None:
        rep.unknown_check("count", "trees.count() could not be read")

    # Caps: the viewport Max Items limit thins trees.count(); the render caps apply to the full count.
    thinning = "unknown"
    if count is not None and vmax is not None:
        if vmax == 0:
            thinning = "none"
        elif count * 4 <= vmax:
            thinning = "unlikely"
        else:
            thinning = "possible"
            rep.add("FOREST_VIEWPORT_THINNING", f"trees.count() = {count}, vmaxitems = {vmax:g}")
            rep.unknown_check("render_item_count", "trees.count() follows the viewport Max Items limit; the full "
                                                   "count needs vmaxitems = 0 and a rebuild, which this check never "
                                                   "does")
    if count is not None and rmax is not None:
        if rmax == 0:
            rep.add("FOREST_RENDER_MAX_ITEMS_UNLIMITED")
        elif count > rmax:
            rep.add("FOREST_RENDER_MAX_ITEMS_EXCEEDED", f"count {count} > rmaxitems {rmax:g}")
        elif thinning == "possible" and count * 4 > rmax:
            rep.add("FOREST_RENDER_MAX_ITEMS_RISK", f"count {count} (x4 = {count * 4}) vs rmaxitems {rmax:g}")
        elif count >= NEAR_CAP * rmax:
            rep.add("FOREST_RENDER_MAX_ITEMS_NEAR", f"count {count} vs rmaxitems {rmax:g}")
    faces, faces_partial = None, False
    if isinstance(rendermode, int) and not isinstance(rendermode, bool) and rendermode != 0:
        rep.add("FOREST_MESH_RENDER_MODE", f"rendermode {rendermode}")
        faces, faces_partial = _forest_faces(rep, obj, thinning)
    if isinstance(k.get("rmesh"), int) and k["rmesh"] != 0:
        rep.add("FOREST_RENDER_DISPLAY", f"rmesh {k['rmesh']}")
    if isinstance(k.get("rskip"), int) and k["rskip"] > 0:
        rep.add("FOREST_RENDER_SIMPLIFICATION", f"rskip {k['rskip']}")

    # Items: billboards, unknown geomlist values, proxies
    proxies = [it for it in obj["items"] if it["source_class"] == "VRayProxy"]
    billboards = [it for it in obj["items"] if it["geom"] == 1]
    for it in billboards:
        rep.add("CPU_ONLY_BILLBOARD", f"item {it['index']} '{it['name']}'")
    for it in obj["items"]:
        if it["geom"] == 3:
            rep.add("GEOMLIST_CRASH_RISK", f"item {it['index']} '{it['name']}'")
        elif it["geom"] not in (0, 1, 2, None):
            rep.add("GEOMLIST_UNKNOWN", f"item {it['index']} '{it['name']}': {it['geom']}")
    if billboards and k.get("usefakeshadows") is True:
        rep.add("XSHADOW_BILLBOARDS", f"{len(billboards)} billboard item(s), usefakeshadows on")

    # Areas: Edge mode (boundary check 2 [inf]) with V-Ray proxy items is CPU-only.
    edge = [a["index"] for a in obj["areas"] if a["active"] is True and a["boundary"] == 2]
    if any(a["boundary"] is None for a in obj["areas"]):
        rep.unknown_check("boundary_check", "some area boundary-check values could not be read")
    if edge and proxies:
        rep.add("EDGE_MODE_ON_PROXIES", f"areas {edge}, {len(proxies)} proxy item(s)")
    elif edge:
        rep.add("EDGE_MODE_UNVERIFIED", f"areas {edge}")
    if proxies:
        rep.unknown_check("proxy_items", "V-Ray proxy items are found by class only (VRayProxy); whether a "
                                         "UI-picked proxy uses another geomlist value wasn't probed")

    # Materials
    material = k.get("material")
    mat_name = material.get("name", "") if isinstance(material, dict) else ""
    if material is None:
        rep.add("FOREST_NO_AUTOMATERIAL")
    elif not mat_name.lower().startswith("forest_automat"):
        where = f"node material {material.get('class') if isinstance(material, dict) else material} '{mat_name}'"
        rep.add("FOREST_NODE_MATERIAL" if consolidate is False else "FOREST_NODE_MATERIAL_REPLACED", where)
    if obj["fpc"]:
        rep.add("FPCOLOR_HARMLESS", f"{len(obj['fpc'])} FPColor material(s)")
    _id_maps(rep, obj, consolidate)
    _materials(rep, obj, {key: k.get(key) for key in ("tintmode", "tintmapmode", "tintmin", "tintmax")})
    state: dict[str, Any] = {
        "viewport_count": count, "vmaxitems": k.get("vmaxitems"), "viewport_thinning": thinning,
        "rmaxitems": k.get("rmaxitems"), "max_faces_millions": k.get("maxfaces"), "render_mode": rendermode,
        "rmesh": k.get("rmesh"), "vmesh": k.get("vmesh"), "rskip": k.get("rskip"), "disabled": k.get("disabled"),
        "consolidate_materials": consolidate, "item_types": k.get("item_types"), "billboards": len(billboards),
        "proxy_items": len(proxies), "material": material, "color_maps": len(obj["cm"]),
        "opacity_maps": len(obj["op"]),
    }
    if faces is not None:
        state["estimated_faces"] = faces
        if faces_partial:
            state["estimated_faces_partial"] = True
    if details:
        state["items"] = [{"index": it["index"], "name": it["name"], "geom": it["geom"], "probability": it["prob"],
                           "source": it["source"], "source_class": it["source_class"],
                           "material_override": it["override"],
                           "count": obj["hist"].get(it["index"]) if obj["hist"] else None} for it in obj["items"]]
        state["areas"] = obj["areas"]
    return _object_result(obj, rep, state)


def _id_maps(rep: _Report, obj: dict[str, Any], consolidate: Any) -> None:
    """Maps keyed on face material ID, whose IDs Forest renumbers when it consolidates the item materials."""
    code = "ID_MAP_IN_FOREST" if consolidate is not False else "ID_MAP_UNCONSOLIDATED"
    for ms in obj["ms"]:
        from_id = ms["from_id"]
        if isinstance(from_id, bool) or not isinstance(from_id, int):
            continue  # unreadable: _materials lists it under unknown
        where = f"{ms['path']}: VRayMultiSubTex '{ms['name']}'"
        if from_id == 0:
            rep.add(code, f"{where} from_id 0 (face material ID)")
        elif from_id == 12 and ms["by_face_id"] is True:
            rep.add(code, f"{where} from_id 12, random by face MtlID")
        elif from_id == 12 and ms["by_face_id"] is not False:
            rep.unknown_check("multisubtex_mode", f"{where}: random mode, but 'By face MtlID' "
                                                  "(random_by_face_mtl_id) isn't readable")
    multitex = []
    for idm in obj["idm"]:
        where = f"{idm['path']}: {idm['class']} '{idm['name']}'"
        if idm["prop"] == "randbymat":
            if idm["value"] is True:
                rep.add(code, f"{where} random by material ID")
            elif idm["value"] is not False:
                rep.unknown_check("id_map", f"{where}: 'Random by material id' (randByMat) isn't readable")
        elif idm["prop"] == "rnd_by":
            multitex.append(f"{where} rnd_by {idm['value']}")
    if multitex and consolidate is not False:
        rep.unknown_check("id_map", "MultiTexture 'Randomize by' (rnd_by): which value means Material ID isn't "
                                    "confirmed, so whether these randomise by material ID (Forest renumbers the face "
                                    f"IDs, which would change their variation) is unknown: {multitex[:5]}")


def _forest_faces(rep: _Report, obj: dict[str, Any], thinning: str) -> tuple[int | None, bool]:
    """Faces in Meshes render mode: items per type x the source's faces, and whether some types had no count.
    (None, False) when the items per type are unknown."""
    if not obj["hist"]:
        rep.unknown_check("faces", "the items per type weren't counted, so the face count is unknown")
        return None, False
    total, unknown = 0, []
    for it in obj["items"]:
        n = obj["hist"].get(it["index"], 0)
        # a V-Ray proxy node's mesh is its viewport preview (12 faces live for a box preview), not what renders
        if n and (it["faces"] < 0 or it["source_class"] == "VRayProxy"):
            unknown.append(it["name"])
        elif n:
            total += n * it["faces"]
    if unknown:
        rep.unknown_check("faces", f"no face count for {len(unknown)} item type(s) (billboard, V-Ray proxy or no "
                                   f"mesh): {unknown[:5]}; the face estimate is partial")
    cap = _num(obj["k"].get("maxfaces"))
    if cap and cap > 0:
        why = [w for w, on in (("trees.count() may be thinned", thinning == "possible"),
                               ("some item types have no face count", bool(unknown))) if on]
        note = f" (a lower bound: {'; '.join(why)})" if why else ""
        if total > cap * 1e6:
            rep.add("FOREST_MAX_FACES_EXCEEDED", f"about {total} faces{note} > {cap:g} million")
        elif total >= NEAR_CAP * cap * 1e6:
            rep.add("FOREST_MAX_FACES_NEAR", f"about {total} faces{note} vs {cap:g} million")
    return total, bool(unknown)


def analyze_railclone(obj: dict[str, Any], max_paths: int, rc_version: list[int] | None,
                      details: bool = False) -> dict[str, Any]:
    rep = _Report(max_paths)
    k = obj["k"]
    _common(rep, obj)
    instancing = k.get("rendermode")
    disabled = k.get("disabled") is True
    if disabled:
        rep.add("RC_DISABLED")

    # Modifiers break native instancing.
    active = [m for m in obj["mods"] if m["enabled"] is not False and m["in_renders"] is not False]
    if active:
        rep.add("RC_MODIFIERS", ", ".join(f"[{m['index']}] {m['class']} '{m['name']}'" for m in active))
    elif obj["mods"]:
        rep.add("RC_MODIFIERS_OFF", ", ".join(f"[{m['index']}] {m['class']}" for m in obj["mods"]))

    # Render settings
    if instancing is False:
        rep.add("RC_INSTANCING_OFF")
    rmesh = k.get("rmesh")
    if rmesh == 1 and rmesh is not True:
        rep.add("RC_RENDER_BOXES")
    elif isinstance(rmesh, int) and not isinstance(rmesh, bool) and rmesh not in (0, 1):
        rep.unknown_check("rmesh", f"rmesh {rmesh} isn't a documented value (0 Mesh, 1 Boxes)")
    if k.get("nodemat") is True:
        rep.add("RC_SEGMENT_MATERIAL")
    style = obj["style"]
    if style is not None and style["material_ops"]:
        rep.add("RC_MATERIAL_OPERATOR" if instancing is not False else "RC_MATERIAL_OPERATOR_MESH",
                f"{style['material_ops']} enabled Material operator node(s)")
    if style is None:
        rep.unknown_check("material_operator", "the style XML couldn't be read")
    mapped = [s for s in obj["segs"] if s["mapping"] is True]
    if mapped:
        rep.add("RC_SEGMENT_MAPPING" if instancing is not False else "RC_SEGMENT_MAPPING_MESH",
                ", ".join(f"[{s['index']}] {s['name']}" for s in mapped))

    # Proxy cache
    mode = k.get("proxymode")
    if mode == 1 and mode is not True:
        rep.add("RC_PROXY_CACHE_EMBEDDED")
    elif mode == 2:
        rep.add("RC_PROXY_CACHE_FILE", str(k.get("proxyfile")))
        if k.get("proxyfile_exists") is False:
            rep.add("RC_PROXY_FILE_MISSING", str(k.get("proxyfile")))
    elif isinstance(mode, int) and not isinstance(mode, bool) and mode not in (0, 1, 2):
        rep.unknown_check("proxymode", f"proxymode {mode} isn't a documented value (0 off, 1 embedded, 2 file)")

    # Segments: V-Ray proxies under Mesh mode, sobjmtl wrapping
    proxy_segs = [s for s in obj["segs"] if "VRayProxy" in (s["ref_class"], s["source_class"])]
    if proxy_segs and instancing is False:
        if rc_version is None:
            rep.unknown_check("railclone_version", "the RailClone plugin version couldn't be read, so whether V-Ray "
                                                   "proxy segments still need Mesh mode is unknown")
        elif rc_version[0] >= 7:
            rep.add("RC_PROXY_MESH_REVERTABLE", ", ".join(f"[{s['index']}] {s['name']}" for s in proxy_segs))
    bad = [s for s in obj["segs"]
           if s["sobjmtl_class"] and not (_cls(s["sobjmtl_class"]) == "multimaterial" and s["sobjmtl_subs"] == 1)]
    if bad:
        rep.add("RC_SOBJMTL_NOT_ONE_SLOT_MULTI",
                ", ".join(f"[{s['index']}] {s['name']}: {s['sobjmtl_class']} ({s['sobjmtl_subs']} subs)" for s in bad))

    export = _rc_export(rep, obj, disabled)
    _materials(rep, obj, None)
    state: dict[str, Any] = {
        "disabled": k.get("disabled"), "instancing": instancing, "rmesh": rmesh, "vmesh": k.get("vmesh"),
        "use_segment_material": k.get("nodemat"), "auto_update": k.get("autoupdate"), "proxy_mode": mode,
        "created_version": k.get("createdversion"), "material": k.get("material"), "modifiers": len(obj["mods"]),
        "segments": len(obj["segs"]), "material_operators": style["material_ops"] if style else None,
        "uvw_operators": style["uvw_ops"] if style else None, "export": export, "color_maps": len(obj["cm"]),
        "opacity_maps": len(obj["op"]),
    }
    if details:
        state["proxy_file"] = k.get("proxyfile") or None
        state["proxy_file_exists"] = k.get("proxyfile_exists")
        state["material_operators_off"] = style["material_ops_off"] if style else None
        state["modifier_list"] = obj["mods"]
        state["segment_list"] = [
            {"index": s["index"], "name": s["name"], "source": s["source"], "class": s["source_class"] or s["ref_class"],
             "faces": s["faces"] if s["faces"] >= 0 else None, "mapping": s["mapping"], "sobjmtl": s["sobjmtl_class"],
             "items": obj["out_segs"].get(s["name"])} for s in obj["segs"]]
    return _object_result(obj, rep, state)


def _rc_export(rep: _Report, obj: dict[str, Any], disabled: bool) -> dict[str, Any]:
    k, out = obj["k"], obj["out"]
    maxseg, maxfaces = _num(k.get("maxseg")), _num(k.get("maxfaces"))
    export: dict[str, Any] = {"state": "unreadable", "items": None, "estimated_faces": None,
                              "max_segments": k.get("maxseg"), "max_faces_millions": k.get("maxfaces")}
    if out is None:
        rep.unknown_check("caps", "unknown: no current export (getXMLOutput couldn't be read)")
        return export
    export["items"] = out["items"]
    if out["items"] == 0:
        export["state"] = "empty"
        rep.unknown_check("caps", "unknown: no current export (the export has no items)")
        if not disabled:
            rep.add("RC_EXPORT_EMPTY")
        return export
    export["state"] = "present"
    if k.get("autoupdate") is False:
        rep.add("RC_AUTOUPDATE_OFF")
    if maxseg is not None:
        if maxseg == 0:
            rep.add("RC_MAXSEG_UNLIMITED")
        elif out["items"] > maxseg:
            rep.add("RC_MAXSEG_EXCEEDED", f"{out['items']} items > maxseg {maxseg:g}")
        elif out["items"] >= NEAR_CAP * maxseg:
            rep.add("RC_MAXSEG_NEAR", f"{out['items']} items vs maxseg {maxseg:g}")
    if out["sourced"] == 0:
        rep.unknown_check("faces", "the export items name no source segment (a proxy cache exports them that way), so "
                                   "the faces can't be estimated")
        return export
    faces_by_name = {s["name"]: -1 if "VRayProxy" in (s["ref_class"], s["source_class"]) else s["faces"]
                     for s in obj["segs"]}
    total, unknown = 0, []
    for name, n in obj["out_segs"].items():
        faces = faces_by_name.get(name, -1)
        if faces < 0:
            unknown.append(name)
        else:
            total += n * faces
    export["estimated_faces"] = total
    if unknown:
        export["estimated_faces_partial"] = True
        rep.unknown_check("faces", f"no face count for segment(s) {unknown[:5]} (V-Ray proxy, an embedded segment "
                                   "whose source was modified, or no mesh): the face estimate is partial")
    if maxfaces is not None:
        if maxfaces == 0:
            rep.add("RC_MAXFACES_UNLIMITED")
        elif total > maxfaces * 1e6:
            rep.add("RC_MAXFACES_EXCEEDED", f"about {total} faces > {maxfaces:g} million")
        elif total >= NEAR_CAP * maxfaces * 1e6:
            rep.add("RC_MAXFACES_NEAR", f"about {total} faces vs {maxfaces:g} million")
    return export


def analyze_ivy(obj: dict[str, Any], max_paths: int, details: bool = False) -> dict[str, Any]:
    rep = _Report(max_paths)
    k = obj["k"]
    _common(rep, obj)
    if k.get("renderinstancing") is True:
        rep.add("IVY_RENDER_INSTANCING",
                f"maxbranches {k.get('maxbranches')}, maxleaves {k.get('maxleaves')} (thousands)")
    _materials(rep, obj, None)
    state = {"render_instancing": k.get("renderinstancing"), "max_branches": k.get("maxbranches"),
             "max_leaves": k.get("maxleaves"), "material": k.get("material"), "color_maps": len(obj["cm"]),
             "opacity_maps": len(obj["op"])}
    return _object_result(obj, rep, state)


def _object_result(obj: dict[str, Any], rep: _Report, state: dict[str, Any]) -> dict[str, Any]:
    status = rep.status()
    result = {"name": obj["name"], "handle": obj["handle"], "kind": obj["kind"], "status": status,
              "findings": rep.sorted_findings(), "unknown": rep.unknown, "state": state,
              "elapsed_ms": obj["elapsed_ms"]}
    if any(f["code"] in FALLBACK_CODES for f in rep.findings):
        result["fallback"] = obj["kind"]
    return result


def analyze(facts: dict[str, Any], max_paths: int = 5, details: bool = False) -> dict[str, Any]:
    """The report: per-object findings, scene findings, a code legend and a summary."""
    scene = facts["scene"]
    rc_version = _version(scene.get("railclone_version"))
    objects = []
    for obj in facts["objects"]:
        if obj["kind"] == "forest":
            objects.append(analyze_forest(obj, max_paths, details))
        elif obj["kind"] == "railclone":
            objects.append(analyze_railclone(obj, max_paths, rc_version, details))
        else:
            objects.append(analyze_ivy(obj, max_paths, details))
    srep = _Report(max_paths)
    for light in facts["xshadow_lights"]:
        srep.add("XSHADOW_LIGHT", light["name"])
    renderer = scene.get("renderer")
    if isinstance(renderer, str) and not renderer.lower().startswith("v_ray"):
        srep.add("RENDERER_NOT_VRAY", renderer)
    if any(o.get("fallback") for o in objects):
        srep.add("FALLBACK")
    warnings = []
    before, after = scene.get("save_required_before"), scene.get("save_required_after")
    if before is False and after is True:
        warnings.append("SCENE_MARKED_DIRTY: the scene needed no save before this check and needs one after it, so "
                        "something during the read marked it changed. Please report this.")
    not_checked = [s["name"] for s in facts["skipped"]]
    if not_checked:
        warnings.append(f"TIME_BUDGET: {len(not_checked)} object(s) were not checked; call again with their names "
                        "or a larger time_budget_s")
    partial = [o["name"] for o in objects if any(u["check"] in ("read", "materials") for u in o["unknown"])]
    if partial:
        warnings.append(f"PARTIAL_READ: {len(partial)} object(s) were read only in part (see their unknown list): "
                        f"{partial[:10]}")
    used: set[str] = set()
    findings = {"error": 0, "warn": 0, "info": 0}
    for f in [f for o in objects for f in o["findings"]] + srep.findings:
        findings[f["severity"]] += 1
        used.add(f["code"])
    statuses = {"error": 0, "warn": 0, "ok": 0}
    kinds = {"forest": 0, "railclone": 0, "ivy": 0}
    for o in objects:
        statuses[o["status"]] += 1
        kinds[o["kind"]] = kinds.get(o["kind"], 0) + 1
    codes = {}
    for code in sorted(used):
        severity, basis, message, fix = CODES[code]
        codes[code] = {"severity": severity, "basis": basis, "message": message}
        if fix:
            codes[code]["fix"] = fix
    fallbacks = sorted({o["fallback"] for o in objects if o.get("fallback")})
    return {
        "complete": not not_checked and not partial,
        "read_only": True,
        "scene": {
            "renderer": renderer, "max_version": scene.get("max_version"),
            "railclone_version": scene.get("railclone_version"),
            "save_required_before": before, "save_required_after": after, "elapsed_ms": scene.get("elapsed_ms"),
        },
        "summary": {"objects": len(objects), "kinds": kinds, "status": statuses, "findings": findings,
                    "unknown": sum(len(o["unknown"]) for o in objects)},
        "objects": objects,
        "scene_findings": srep.sorted_findings(),
        "codes": codes,
        "fallbacks": {kind: FALLBACK[kind] for kind in fallbacks},
        "basis": BASIS,
        "notes": [EXPORT_NOTE],
        "not_found": facts["missing"],
        "not_itoo": facts["not_itoo"],
        "not_checked": not_checked,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------------------------

def _names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        raise ValueError("names must be a list of node names")
    out: list[str] = []
    for name in value:
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("each name must be a non-empty string without NUL characters")
        if name not in out:
            out.append(name)
    if len(out) > MAX_NAMES:
        raise ValueError(f"at most {MAX_NAMES} names per call")
    return out


@mcp.tool()
def itoo_vantage_check(
    names: StrList | None = None,
    time_budget_s: float = DEFAULT_BUDGET_S,
    max_paths: int = 5,
    details: bool = False,
) -> dict[str, Any]:
    """Read-only check of how ready Forest Pack and RailClone objects are for Chaos Vantage.

    Checks every Forest_Pro, RailClone_Pro and ForestIvy node, or the exact
    names given: Forest/RailClone Color slot 1 (what Vantage renders),
    opacity maps, CPU-only features (Forest Material, billboards, Edge mode on
    proxies, ForestIvy instancing, XShadow), ID-driven maps under
    forest_automat_*, RailClone render settings, modifiers, proxy cache and
    sobjmtl wrapping, and item/segment/face caps. Findings name a code;
    codes{} gives each code's severity (error/warn/info), basis (doc, live,
    inf, memory), message and fix. What can't be read without writing is
    listed under unknown, and objects with a feature Vantage may not
    translate name a fallback. It never
    rebuilds (RailClone caps come from the existing export; Forest counts are
    trees.count() as it is, which the viewport Max Items limit can thin),
    writes, selects, renders or exports. One MAXScript call: time_budget_s
    (1-300) bounds it, and objects it doesn't reach are listed in
    not_checked. max_paths caps the example paths per finding; details=true
    adds the segment, item, area and modifier lists.
    """
    wanted = _names(names)
    if isinstance(time_budget_s, bool) or not isinstance(time_budget_s, (int, float)) \
            or not math.isfinite(time_budget_s) or not 1 <= time_budget_s <= 300:
        raise ValueError("time_budget_s must be a number from 1 to 300")
    if isinstance(max_paths, bool) or not isinstance(max_paths, int) or not 1 <= max_paths <= 100:
        raise ValueError("max_paths must be an integer from 1 to 100")
    if type(details) is not bool:
        raise ValueError("details must be true or false")
    script = build_script(wanted, int(time_budget_s * 1000))
    response = client.send_command(script, timeout=float(time_budget_s) + 60.0, probe=True)
    if not isinstance(response, dict):
        raise ItooVantageError("BRIDGE_ERROR", f"unexpected bridge response: {str(response)[:200]}")
    if response.get("error") or response.get("ok") is False:
        error = response.get("error") or {}
        if isinstance(error, dict):
            raise ItooVantageError(str(error.get("code") or "BRIDGE_ERROR"), str(error.get("message") or error))
        raise ItooVantageError("BRIDGE_ERROR", str(error))
    return analyze(parse_report(response.get("result")), max_paths, details)
