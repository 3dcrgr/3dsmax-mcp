"""Live iToo API inventory and guarded RailClone slot edits.

Pointer-taking renderer callbacks are listed, never invoked. Slot edits use the
vendor's published parallel arrays, with a token and one checked undo hold.
"""
from __future__ import annotations

import math
import os
import hashlib
from pathlib import Path
import re
import uuid
import xml.etree.ElementTree as ET
from typing import Any

from ..server import mcp
from ..coerce import StrList
from ..helpers.railclone_lib import RailCloneError
from . import railclone as rc


BASE_FIELDS = {"banode": "node", "bafull": "bool", "bastart": "number", "balength": "number"}
SEGMENT_FIELDS = {
    "sobjnode": "node", "spos": "point", "srot": "point", "ssca": "point",
    "sxalign": "align", "syalign": "align", "szalign": "align", "slicesrc": "slice",
    "spadin": "number", "spadout": "number", "spadtop": "number", "spadbottom": "number",
    "sfixedsize": "point", "sSizeScale": "bool", "sinstance": "bool", "sbend": "bool",
    "sslice": "bool", "snesting": "bool", "srenderable": "bool", "sexportforest": "bool",
    "szdeform": "int", "ssurfconform": "bool", "ssurfnormal": "bool", "sslopefix": "bool",
    "sflattop": "number", "sflatbottom": "number", "srandtrans": "bool", "srandrot": "bool",
    "srandscale": "bool", "srandmat": "bool", "srt1": "point", "srt2": "point",
    "srr1": "point", "srr2": "point", "srs1": "point", "srs2": "point",
    "smaterial": "int", "smatrange": "int", "smapping": "bool", "smapreal": "bool",
    "smapseamless": "bool", "smapchans": "channels", "smapsize": "point", "smapoff": "point",
    "smaprotx": "number", "smaproty": "number", "smaprotz": "number",
}
# Names come from showInterfaces on RailClone 7.3.5 / Forest Pack 9.4.3.
# Known effects are tracked explicitly; undiscovered methods never become callable.
COVERAGE = {
    "railclone": {
        "getXMLStyle": "get_railclone_style", "setXMLStyle": "set_railclone_style",
        "getXMLOutput": "get_railclone_output", "segmentsUpdate": "railclone_inspect(reload_segments=true)",
        "loadLibraryItemByPath": "railclone_from_library", "getStyleDesc": "railclone_inspect(rebuild=true)",
        "setStyleDesc": "railclone_metadata", "setProxyMode": "railclone_proxy",
        "Instantiate": "railclone_instances", "InstantiateEnable": "railclone_instances",
        "InstantiateDelete": "railclone_instances", "version": "itoo_api_inventory",
        "resetCreatedVersion": "railclone_metadata(reset_compatibility=true)",
    },
    "forest": {
        "create": "forest_edit_items", "delete": "forest_edit_items", "edit": "forest_edit_items",
        "move": "forest_edit_items", "setPosition": "forest_edit_items(move)",
        "setRotation": "forest_edit_items", "setWidth": "forest_edit_items",
        "setHeight": "forest_edit_items", "setSize": "forest_edit_items(set_size)",
        "setGeomID": "forest_edit_items", "setSeed": "forest_edit_items", "count": "forest_items",
        "getPosition": "forest_items", "getFullTransform": "forest_items", "getGeomID": "forest_items",
        "getWidth": "forest_items(size)", "getHeight": "forest_items(size)", "getSize": "forest_items",
        "getRotation": "forest_items(rotation_z)", "getSeed": "forest_items", "update": "forest_items",
        "version": "itoo_api_inventory", "getSelected": "forest_inspect",
    },
}
INTERNAL = {"RegisterEngine", "registerEngine", "SetEngineFeatures", "setEngineFeatures",
            "getRenderID", "getRenderNode", "getRenderNodes", "clearRenderNodes", "getRenderData",
            "getRenderDataRaw", "clearRenderDataRaw", "setNodesCache", "setCreatedVersion", "upgradeFromVersion"}
UI = {"openBrowser", "closeBrowser", "refresh", "openLister", "setRollup", "update_ui", "setOverlay"}
CATALOG_READS = {"getMacroCount", "getMacro", "getSelItemName", "getSelItemProp", "getSelItemCustomProp"}


def parse_interfaces(text: str, plugin: str) -> list[dict[str, Any]]:
    interface = ""
    methods = []
    for line in text.splitlines():
        m = re.match(r"\s*Interface:\s*(\w+)", line)
        if m:
            interface = m[1]
        m = re.match(r"\s*<([^>]+)>(\w+)(.*)", line)
        if not m:
            continue
        method = m[2]
        tool = "itoo_catalog" if method in CATALOG_READS else COVERAGE[plugin].get(method)
        status = "tool" if tool else "renderer_internal" if method in INTERNAL else "ui" if method in UI else "missing"
        methods.append({"interface": interface, "method": method, "signature": line.strip(),
                        "returns": m[1], "coverage": status, "tool": tool})
    return methods


@mcp.tool()
def itoo_api_inventory(plugin: str = "railclone") -> dict[str, Any]:
    """List the installed iToo methods, signatures and dedicated MCP coverage, without creating scene nodes.

    plugin is railclone or forest. Includes properties, API and product versions.
    'missing' means no dedicated callable wrapper; generic plugin inspection and
    plugin_patch can still handle properties. Renderer pointer APIs and browser
    UI methods are classified separately. This never calls the listed methods,
    opens a browser, rebuilds geometry or reads getStyleDesc.
    """
    if plugin not in COVERAGE:
        raise ValueError("plugin must be railclone or forest")
    cls = "RailClone_Pro" if plugin == "railclone" else "Forest_Pro"
    pattern = "railclone*.dlo" if plugin == "railclone" else "forestpack*.dlo"
    raw = rc._send(f'''
if {cls} == undefined do throw "PLUGIN_MISSING: {cls} is unavailable"
local b = createInstance {cls}
local ss = stringStream "", interfaces = stringStream ""
showInterfaces b to:interfaces
format "ITOOINVENTORY\\n" to:ss
rcRow ss #("I", rcEncode (interfaces as string))
rcRow ss #("V", rcV (try ({cls}.version()) catch undefined))
local mods = ((dotNetClass "System.Diagnostics.Process").GetCurrentProcess()).Modules
for i = 0 to mods.Count - 1 do (
    local md = mods.Item[i]
    if matchPattern md.ModuleName pattern:"{pattern}" do rcRow ss #("M", rcV md.ModuleName, rcV md.FileVersionInfo.FileVersion)
)
for p in getPropNames b do rcRow ss #("P", rcV (p as string), rcV ((classOf (getProperty b p)) as string))
format "END\\n" to:ss
ss as string
''')
    result: dict[str, Any] = {"plugin": plugin, "read_only": True, "properties": [], "modules": [], "api_version": None}
    for row in rc._records(raw, "ITOOINVENTORY"):
        if row[0] == "I":
            result["methods"] = parse_interfaces(rc._unb64(row[1]), plugin)
        elif row[0] == "V":
            result["api_version"] = rc._dv(row[1])
        elif row[0] == "M":
            result["modules"].append({"module": rc._dv(row[1]), "product_version": rc._dv(row[2])})
        elif row[0] == "P":
            result["properties"].append({"name": rc._dv(row[1]), "type": rc._dv(row[2])})
    result["summary"] = {k: sum(m["coverage"] == k for m in result.get("methods", []))
                         for k in ("tool", "missing", "ui", "renderer_internal")}
    return result


@mcp.tool()
def itoo_catalog(plugin: str = "railclone", macro: str = "", item_property: str = "",
                  custom_property: str = "") -> dict[str, Any]:
    """Read iToo's Library Browser catalog metadata and stored macro text without opening it.

    Returns macro count and current selected browser item name. Optional macro,
    item_property and custom_property read exact keys from the installed scatalog
    interface. An unopened browser can return empty values. Does not evaluate
    macros (which can run scripts), change catalog settings or open/close the UI.
    """
    if plugin not in COVERAGE:
        raise ValueError("plugin must be railclone or forest")
    for value in (macro, item_property, custom_property):
        if not isinstance(value, str) or "\x00" in value or len(value) > 256:
            raise ValueError("catalog keys must be strings without NUL, up to 256 characters")
    cls = "RailClone_Pro" if plugin == "railclone" else "Forest_Pro"
    body = f'''if {cls}==undefined do throw "PLUGIN_MISSING: plugin unavailable"
local api=getInterface {cls} #scatalog
if api==undefined do throw "API_UNAVAILABLE: no scatalog interface"
local ss=stringStream ""
format "ITOOCATALOG\\n" to:ss
rcRow ss #("K", "macro_count", rcV (api.getMacroCount()))
rcRow ss #("K", "selected_item", rcV (api.getSelItemName()))
'''
    for key, method, value in (("macro", "getMacro", macro), ("item_property", "getSelItemProp", item_property),
                               ("custom_property", "getSelItemCustomProp", custom_property)):
        if value:
            body += f'local key_{key}=rcDecode "{rc._b64(value)}"\n'
            body += f'if not isProperty api #{method} do throw "API_UNAVAILABLE: {method}"\n'
            body += f'rcRow ss #("K", "{key}", rcV (api.{method} key_{key}))\n'
    body += 'format "END\\n" to:ss\nss as string\n'
    result: dict[str, Any] = {"plugin": plugin, "read_only": True}
    for row in rc._records(rc._send(body), "ITOOCATALOG"):
        if row[0] == "K":
            result[row[1]] = rc._dv(row[2])
    return result


@mcp.tool()
def forest_inspect(name: str, max_entries: int = 1000) -> dict[str, Any]:
    """Read Forest Pack settings and parallel arrays without building or editing items.

    Includes geometry, surfaces, ordered areas, distribution, transforms, effects
    and their parameter arrays. Values are typed; node references carry handles.
    max_entries bounds each array and reports omissions. Does not call trees.update
    or count, which can build lazily. getSelected reports the current sub-item index.
    Product version is available separately from itoo_api_inventory.
    """
    from ..helpers import forest as fh
    fh.node_name(name, "name")
    if type(max_entries) is not int or not 1 <= max_entries <= 5000:
        raise ValueError("max_entries must be 1..5000")
    raw = rc._send(fh.MXS_LIB + f'''
local obj=fpForest "{rc._b64(name)}"
local base=obj.baseObject, ss=stringStream ""
format "ITOOFOREST\\n" to:ss
rcRow ss #("I", rcH obj, rcEncode obj.name)
rcRow ss #("SELECTED", rcV (try (base.trees.getSelected()) catch undefined))
for p in getPropNames base do (
    local v=getProperty base p
    if isKindOf v ArrayParameter or isKindOf v Array then (
        rcRow ss #("A", p as string, v.count as string)
        for i=1 to (amin #(v.count,{max_entries})) do rcRow ss #("V", p as string, i as string, rcV v[i])
    ) else rcRow ss #("P", p as string, rcV v)
)
format "END\\n" to:ss
ss as string
''')
    result: dict[str, Any] = {"read_only": True, "rebuilt": False, "settings": {}, "arrays": {}}
    for row in rc._records(raw, "ITOOFOREST"):
        if row[0] == "I":
            result.update(handle=int(row[1]), name=rc._unb64(row[2]))
        elif row[0] == "SELECTED":
            result["selected_item"] = rc._dv(row[1])
        elif row[0] == "P":
            result["settings"][row[1]] = rc._dv(row[2])
        elif row[0] == "A":
            n = int(row[2])
            result["arrays"][row[1]] = {"count": n, "values": [], "truncated": n > max_entries}
        elif row[0] == "V":
            result["arrays"][row[1]]["values"].append(rc._dv(row[3]))
    return result


def effect_xml(code: str, title: str) -> str:
    """Build a parameter-free effect using the native .eff format, not MAXScript."""
    if not isinstance(code, str) or not code.strip() or len(code) > 100000 or "\x00" in code:
        raise ValueError("code must be nonempty effect-language text, up to 100000 characters")
    executable = re.sub(r"#.*", "", code)
    if re.search(r"[+*/^\-]=|\+\+|--", executable):
        raise ValueError("Forest Effects compound assignments/increments are unsupported; use x = x + value")
    if not isinstance(title, str) or not title or len(title) > 256 or "\x00" in title:
        raise ValueError("effect_name must be nonempty text, up to 256 characters")
    root = ET.Element("effect", {"version": "500", "title": title, "description": "Created through MCP"})
    ET.SubElement(root, "expression").text = code
    return ET.tostring(root, encoding="unicode")


_EFFECT_LIB = r'''
fn iaPreserveEffectReferences base staged = (
    for p in #(#efpainode,#efpaspline,#efpatexmap,#efpacontref) do (
        if isProperty base p and isProperty staged p do setProperty staged p ((getProperty base p) as array)
    )
)
fn iaEffectsProps base = (
    local props=#()
    for p in getPropNames base where matchPattern (p as string) pattern:"ef*" do (
        local a=getProperty base p
        if isKindOf a ArrayParameter or isKindOf a Array do append props p
    )
    props
)
fn iaEffectValue v = (
    if v!=undefined and isKindOf v TextureMap then (
        "t"+(rcH v)+":"+(rcEncode v.name)+":"+(rcEncode ((classOf v) as string))
    ) else rcV v
)
fn iaControllerIdentity base i = (
    local owner=base.efpacontref[i], sub=base.efpacontanim[i]+1
    if owner==undefined then "u" else (
        local ctrl=try ((getSubAnim owner sub).controller) catch undefined
        (rcH owner)+":"+(rcEncode ((classOf owner) as string))+":"+(sub as string)+":"+
        (if ctrl==undefined then "0" else rcH ctrl)+":"+(rcEncode ((classOf ctrl) as string))
    )
)
fn iaEffectsToken obj base = (
    local ss=stringStream ""
    format "%|%|" (rcH obj) (rcH base) to:ss
    for p in iaEffectsProps base do (
        format "%|" p to:ss
        local a=getProperty base p
        for v in a do format "%|" (iaEffectValue v) to:ss
    )
    for i=1 to base.efpaid.count where base.efpatype[i]==2 do format "%|" (iaControllerIdentity base i) to:ss
    rcHash (ss as string)
)
fn iaEffectsReport obj base = (
    local ss=stringStream ""
    format "ITOOEFFECTS\n" to:ss
    rcRow ss #("T", iaEffectsToken obj base)
    for p in iaEffectsProps base do (
        local a=getProperty base p
        rcRow ss #("A", p as string, a.count as string)
        for i=1 to a.count do rcRow ss #("V", p as string, i as string, iaEffectValue a[i])
    )
    for i=1 to base.efpaid.count where base.efpatype[i]==2 do rcRow ss #("C",i as string,iaControllerIdentity base i)
    format "END\n" to:ss
    ss as string
)
'''

NUMERIC_KINDS = {"int": (0, "efpaint", "int"), "real": (1, "efpafloat", "number"),
                 "percentage": (2, "efpafloat", "number"), "unit": (3, "efpaunit", "number")}
PARAM_DEFAULTS = {
    "efpaid": '""', "efpaeffid": '""', "efpatype": "0", "efpaname": '""',
    "efpalimit": "false", "efpadesc": '""', "efpanumtype": "0",
    **{f"efpaint{s}": "0" for s in ("val", "min", "max", "def")},
    **{f"efpa{t}{s}": "0.0" for t in ("float", "unit") for s in ("val", "min", "max", "def")},
    "efpainode": "undefined", "efpaspline": "undefined", "efpacontref": "undefined",
    "efpacontanim": "0", "efpacontype": "0", "efpatexmap": "undefined", "efpacurve": '""',
}


def parameter_definitions(document: ET.Element, allow_references: bool = False) -> list[dict[str, Any]]:
    """Validate measured numeric and optionally node/spline .eff definitions."""
    definitions = document.findall(".//parameter")
    if definitions != document.findall("parameter"):
        raise ValueError("EFFECT_FORMAT_UNSUPPORTED: parameters must be direct effect children")
    if len(definitions) > 100:
        raise ValueError("effect exceeds 100 parameters")
    rows, names, ids = [], set(), set()
    for definition in definitions:
        kind, name = definition.get("numtype"), definition.get("name", "")
        reference = definition.get("type") in ("node", "spline", "texture", "controller")
        if not ((reference and allow_references) or (definition.get("type") == "numerical" and kind in NUMERIC_KINDS)):
            raise ValueError("PARAMETER_TYPE_UNSUPPORTED: only measured numeric/node/spline/texture parameters are supported")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) or name.lower() in names:
            raise ValueError("parameter names must be unique identifiers")
        ident = "{" + str(uuid.UUID(definition.get("id", ""))) + "}"
        if ident in ids:
            raise ValueError("parameter IDs must be unique")
        names.add(name.lower()); ids.add(ident)
        if reference:
            if definition.get("type") == "controller" and set(definition.attrib) - {"id", "name", "description", "type"}:
                raise ValueError("CONTROLLER_SCHEMA_UNSUPPORTED: unmeasured controller definition attributes")
            rows.append({"id": ident, "name": name, "kind": definition.get("type"),
                         "description": definition.get("description", "")})
            continue
        values = {}
        for field in ("min", "max", "default"):
            value = float(definition.get(field, "0"))
            if not math.isfinite(value) or (kind == "int" and (not value.is_integer() or abs(value) > 2**31-1)):
                raise ValueError("parameter bounds/default must be finite and match the numeric type")
            values[field] = int(value) if kind == "int" else value
        limited = definition.get("limits", "0")
        if limited not in ("0", "1"):
            raise ValueError("parameter limits must be 0 or 1")
        if limited == "1" and not values["min"] <= values["default"] <= values["max"]:
            raise ValueError("parameter default is outside its declared limits")
        rows.append({"id": ident, "name": name, "kind": kind, "limited": limited == "1",
                     "description": definition.get("description", ""), **values})
    return rows


def numeric_definitions(document: ET.Element) -> list[dict[str, Any]]:
    return parameter_definitions(document)


def bind_definitions(rows: list[dict[str, Any]], bindings: dict[str, Any] | None) -> None:
    """Require an explicit binding, including explicit null, for every reference."""
    refs = [r for r in rows if r["kind"] in ("node", "spline", "texture", "controller")]
    if bindings is None:
        bindings = {}
    if not isinstance(bindings, dict):
        raise ValueError("bindings must map exact parameter IDs or names to node references")
    touched = set()
    for key, value in bindings.items():
        if not isinstance(key, str):
            raise ValueError("binding keys must be parameter IDs or names")
        try:
            ident = "{" + str(uuid.UUID(key)) + "}"
        except ValueError:
            ident = key
        matches = [r for r in refs if r["id"] == ident] or [r for r in refs if r["name"] == key]
        if len(matches) != 1:
            raise ValueError("binding must identify exactly one reference parameter ID or name")
        row = matches[0]
        if row["id"] in touched:
            raise ValueError("duplicate reference binding")
        touched.add(row["id"])
        row["binding_literal"] = binding_literal(value, row["kind"])
    if len(touched) != len(refs):
        raise ValueError("REFERENCE_BINDINGS_REQUIRED: explicitly bind every node/spline/texture parameter, or use null")


REFERENCE_KINDS = {1: ("node", "efpainode"), 5: ("spline", "efpaspline"), 3: ("texture", "efpatexmap")}


def binding_literal(value: Any, kind: str) -> str:
    if kind == "texture":
        return texture_literal(value)
    if kind == "controller":
        return controller_literal(value)
    return reference_literal(value)


def controller_literal(value: Any) -> str:
    if value is None:
        return "#(undefined,0)"
    keys = {"owner_handle", "owner_class", "subanim", "controller_handle", "controller_class"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("controller binding requires canonical owner/track/controller identity or null")
    for key in ("owner_handle", "controller_handle", "subanim"):
        if type(value[key]) is not int or not 0 < value[key] < 2**63:
            raise ValueError("controller handles/subanim must be positive integers")
    if value["subanim"] > 100000:
        raise ValueError("controller subanim exceeds supported range")
    for key in ("owner_class", "controller_class"):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 1024 or "\x00" in value[key]:
            raise ValueError("controller classes must be nonempty bounded strings")
    return f'(iaEffectController {value["owner_handle"]} "{rc._b64(value["owner_class"])}" {value["subanim"]} {value["controller_handle"]} "{rc._b64(value["controller_class"])}")'


def effect_value(value: str) -> Any:
    if value.startswith("t"):
        handle, name, cls = value[1:].split(":", 2)
        return {"handle": int(handle), "name": rc._unb64(name), "class": rc._unb64(cls)}
    return rc._dv(value)


def texture_literal(value: Any) -> str:
    if value is None:
        return "undefined"
    if not isinstance(value, dict) or set(value) != {"name", "handle", "class"}:
        raise ValueError("texture binding requires {name, handle, class} identity or null")
    handle = value["handle"]
    if isinstance(handle, bool) or not isinstance(handle, int) or not 0 < handle < 2**63:
        raise ValueError("texture handle must be a positive integer")
    for key in ("name", "class"):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 1024 or "\x00" in value[key]:
            raise ValueError("texture name/class must be nonempty bounded strings")
    return f'(iaEffectMap "{rc._b64(value["name"])}" {handle} "{rc._b64(value["class"])}")'


def reference_literal(value: Any) -> str:
    """Resolve an exact unique name or a guarded scene node identity."""
    if value is None:
        return "undefined"
    handle = 0
    if isinstance(value, dict):
        if set(value) != {"name", "handle"}:
            raise ValueError("reference identity requires name and handle only")
        handle = value["handle"]
        if isinstance(handle, bool) or not isinstance(handle, int) or not 0 < handle < 2**63:
            raise ValueError("reference handle must be a positive integer")
        value = value["name"]
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise ValueError("reference requires an exact node name, name/handle identity, or null")
    return f'(iaEffectNode "{rc._b64(value)}" {handle})'


_REFERENCE_LIB = '''
fn iaEffectController handle cls sub ctrlHandle ctrlCls = (
    local owner=getAnimByHandle handle
    if owner==undefined or ((classOf owner) as string)!=(rcDecode cls) do throw "STALE_CONTROLLER_REFERENCE: owner changed"
    if sub>owner.numSubs do throw "STALE_CONTROLLER_REFERENCE: track no longer exists"
    local ctrl=try ((getSubAnim owner sub).controller) catch undefined
    if ctrl==undefined do throw "CONTROLLER_NOT_BOUND: track has no controller"
    if (rcH ctrl)!=(ctrlHandle as string) or ((classOf ctrl) as string)!=(rcDecode ctrlCls) do throw "STALE_CONTROLLER_REFERENCE: controller changed"
    if superClassOf ctrl!=FloatController do throw "CONTROLLER_TYPE_UNSUPPORTED: only verified scalar float controllers are supported"
    #(owner,sub-1)
)
fn iaEffectMap text handle cls = (
    local m=getAnimByHandle handle
    if m==undefined do throw "STALE_MAP_REFERENCE: map no longer exists"
    if not isKindOf m TextureMap do throw "NOT_TEXTURE_MAP: binding requires a texture map"
    if m.name!=(rcDecode text) or ((classOf m) as string)!=(rcDecode cls) do throw "STALE_MAP_REFERENCE: refresh map identity"
    m
)
fn iaEffectNode text handle = (
    local wanted=rcDecode text
    if handle!=0 then (
        local n=getAnimByHandle handle
        if not isValidNode n or n.name!=wanted do throw "STALE_NODE_REFERENCE: refresh node identity"
        n
    ) else (
        local nodes=getNodeByName wanted exact:true all:true
        if nodes==undefined or nodes.count!=1 do throw "NODE_NOT_UNIQUE: use a name/handle identity"
        nodes[1]
    )
)
'''


def parameter_rows(state: dict[str, Any], effect: str = "") -> list[dict[str, Any]]:
    arrays = {p.lower(): a["values"] for p, a in state["arrays"].items()}
    effects = state["effects"]
    if effect:
        matches = [e for e in effects if e["id"] == effect] or [e for e in effects if e["name"] == effect]
        if len(matches) != 1:
            raise RailCloneError("EFFECT_NOT_UNIQUE", "use an exact effect ID")
        effects = matches
    by_id = {e["id"]: e for e in effects}; rows = []
    count = len(arrays.get("efpaid", []))
    for p in ("efpaeffid", "efpatype", "efpaname"):
        if len(arrays.get(p, [])) != count:
            raise RailCloneError("EFFECT_PARAMETER_LIST_MISMATCH", f"incomplete {p}")
    def at(prop: str, i: int) -> Any:
        values = arrays.get(prop, [])
        if len(values) != count:
            raise RailCloneError("EFFECT_PARAMETER_LIST_MISMATCH", f"incomplete {prop}")
        return values[i]
    for i, ident in enumerate(arrays.get("efpaid", [])):
        owner = arrays["efpaeffid"][i]
        if owner not in by_id:
            if effect:
                continue
            raise RailCloneError("ORPHAN_EFFECT_PARAMETER", "parameter refers to a missing effect")
        row = {"id": ident, "effect_id": owner, "name": arrays["efpaname"][i],
               "native_type": arrays["efpatype"][i], "index": i + 1, "writable": False}
        if row["native_type"] == 0:
            code = at("efpanumtype", i)
            kinds = [k for k, data in NUMERIC_KINDS.items() if data[0] == code]
            if not kinds:
                raise RailCloneError("PARAMETER_TYPE_UNSUPPORTED", "unknown native numeric subtype")
            kind = kinds[0]; _, prefix, _ = NUMERIC_KINDS[kind]
            definition = next((p for p in parse_effect_xml(by_id[owner]["xml"]).findall("parameter")
                               if p.get("id", "").lower() == str(ident).lower()), None)
            if definition is None or definition.get("name") != row["name"] or definition.get("type") != "numerical" or definition.get("numtype") != kind:
                raise RailCloneError("PARAMETER_SCHEMA_MISMATCH", "XML and native parameter definition differ")
            row.update(kind=kind, value=at(prefix+"val", i), min=at(prefix+"min", i),
                       max=at(prefix+"max", i), default=at(prefix+"def", i), limited=at("efpalimit", i),
                       writable=True)
        elif row["native_type"] == 2:
            definition = next((p for p in parse_effect_xml(by_id[owner]["xml"]).findall("parameter")
                               if p.get("id", "").lower() == str(ident).lower()), None)
            if definition is None or definition.get("name") != row["name"] or definition.get("type") != "controller":
                raise RailCloneError("PARAMETER_SCHEMA_MISMATCH", "XML and native controller definition differ")
            subtype = at("efpacontype", i)
            bindings = state.get("controller_bindings", {})
            if i+1 not in bindings:
                raise RailCloneError("INVALID_READBACK", "controller identity is missing")
            value = bindings[i+1]
            row.update(kind="controller", value=value, native_controller_type=subtype,
                       bound=value is not None and value["controller_handle"] != 0,
                       writable=subtype == 0 and (value is None or value["controller_handle"] != 0))
        elif row["native_type"] in REFERENCE_KINDS:
            kind, prop = REFERENCE_KINDS[row["native_type"]]
            definition = next((p for p in parse_effect_xml(by_id[owner]["xml"]).findall("parameter")
                               if p.get("id", "").lower() == str(ident).lower()), None)
            if definition is None or definition.get("name") != row["name"] or definition.get("type") != kind:
                raise RailCloneError("PARAMETER_SCHEMA_MISMATCH", "XML and native reference definition differ")
            value = at(prop, i)
            if value is not None and (not isinstance(value, dict) or not isinstance(value.get("handle"), int)
                                      or not isinstance(value.get("name"), str)):
                raise RailCloneError("INVALID_REFERENCE", "native reference is not a scene node")
            if kind == "texture" and value is not None and not isinstance(value.get("class"), str):
                raise RailCloneError("INVALID_REFERENCE", "native texture identity is incomplete")
            row.update(kind=kind, value=value, bound=value is not None, writable=True)
        rows.append(row)
    return rows


def append_numeric_parameters(rows: list[dict[str, Any]], owner: str) -> str:
    if not rows:
        return ""
    script = 'local pc=staged.efpaid.count\n'
    for i, row in enumerate(rows):
        if row["kind"] in ("node", "spline", "texture", "controller"):
            var = f'import_reference_{i}'
            script += f'local {var}={row["binding_literal"]}\n'
            source = f'{var}[1]' if row["kind"] == "controller" else var
            script += f'if {source}!=undefined and ({source}==obj or (refs.dependencyLoopTest {source} obj) or (refs.dependencyLoopTest {source} base)) do throw "DEPENDENCY_CYCLE: source depends on target"\n'
            if row["kind"] == "spline":
                script += f'if {var}!=undefined and superClassOf {var}.baseObject!=Shape do throw "NOT_SPLINE: binding requires a shape"\n'
    for prop in ("efpaeffid", "efpatype", "efpaname"):
        script += f'if staged.{prop}.count!=pc do throw "EFFECT_PARAMETER_LIST_MISMATCH: {prop}"\n'
    for prop, default in PARAM_DEFAULTS.items():
        script += f'''local next_{prop}=staged.{prop} as array
if next_{prop}.count==0 do next_{prop}=for j=1 to pc collect {default}
if next_{prop}.count!=pc do throw "EFFECT_PARAMETER_LIST_MISMATCH: {prop}"
'''
        for i, row in enumerate(rows):
            reference = row["kind"] in ("node", "spline", "texture", "controller")
            subtype, prefix, typ = (0, "", "") if reference else NUMERIC_KINDS[row["kind"]]
            string_fields = {"efpaid": row["id"], "efpaeffid": owner,
                             "efpaname": row["name"], "efpadesc": row["description"]}
            if prop in string_fields:
                value = f'(rcDecode "{rc._b64(string_fields[prop])}")'
            elif reference and prop == "efpatype":
                value = str({"node": 1, "spline": 5, "texture": 3, "controller": 2}[row["kind"]])
            elif row["kind"] == "controller" and prop == "efpacontref":
                value = f'import_reference_{i}[1]'
            elif row["kind"] == "controller" and prop == "efpacontanim":
                value = f'import_reference_{i}[2]'
            elif reference and prop == {"node": "efpainode", "spline": "efpaspline", "texture": "efpatexmap", "controller": "efpacontref"}[row["kind"]]:
                value = f'import_reference_{i}'
            elif prop == "efpanumtype":
                value = str(subtype)
            elif prop == "efpalimit":
                value = rc._mxs_bool(row.get("limited", False))
            elif not reference and prop.startswith(prefix) and prop[len(prefix):] in ("val", "min", "max", "def"):
                suffix = prop[len(prefix):]
                value = _literal(row[{"val": "default", "def": "default"}.get(suffix, suffix)], typ)
            else:
                value = default
            script += f'append next_{prop} {value}\n'
        script += f'staged.{prop}=next_{prop}\n'
    return script


@mcp.tool()
def forest_effect_parameters(name: str, effect: str = "", action: str = "inspect",
                             values: dict[str, Any] | None = None, expected_effects: str = "") -> dict[str, Any]:
    """Inspect Forest effect parameters or set existing numeric/reference values in one staged undo step.

    Select by exact parameter ID or unique name, optionally scoped to an effect ID.
    set requires effects_token from inspection. Integer, real, percentage and
    scene-unit values are supported; enabled native limits are enforced. Percentage
    values use the effect's declared range, not an assumed 0..100 cap. Object and
    spline bindings accept exact unique node names, {name, handle} identities, or
    null to clear. Spline bindings require shapes; cycles and stale identities are
    refused. Texture bindings require {name, handle, class} identities or null;
    handles resolve existing texture maps only and never create a map. Controller
    bindings require the canonical owner/track/controller identity from inspection;
    subanim is 1-based, native storage is 0-based. Only measured scalar float tracks
    with native controller type 0 are writable. Source keys/values are not edited.
    Animated evaluation is unverified: live trees.update returned frame-zero values
    after a time change. Do not infer animated output from binding readback.
    Curve writes and other controller subtypes are refused. XML/native schema must agree.
    Does not build or compile-check; verify using forest_items(refresh_surfaces=true).
    """
    if action not in ("inspect", "set"):
        raise ValueError("action must be inspect or set")
    if action == "set" and (not isinstance(expected_effects, str) or not rc.TOKEN.fullmatch(expected_effects)):
        raise ValueError("set requires expected_effects token")
    if action == "set" and (not isinstance(values, dict) or not values or len(values) > 100):
        raise ValueError("set requires 1..100 parameter values")
    if action == "inspect" and values:
        raise ValueError("inspect does not accept values")
    state = forest_effects(name)
    rows = parameter_rows(state, effect)
    if action == "set":
        if state["effects_token"] != expected_effects:
            raise RailCloneError("STALE_EFFECTS", "read parameters again")
        writes, touched = [], set()
        for key, value in values.items():
            matches = [p for p in rows if p["id"] == key] or [p for p in rows if p["name"] == key]
            if len(matches) != 1:
                raise ValueError("parameter must identify exactly one ID or unique name")
            row = matches[0]
            if row["index"] in touched:
                raise ValueError("duplicate parameter target")
            touched.add(row["index"])
            if not row["writable"]:
                raise ValueError("PARAMETER_TYPE_UNSUPPORTED: this parameter type is not supported")
            if row["native_type"] in REFERENCE_KINDS or row["native_type"] == 2:
                kind, prop = ("controller", "efpacontref") if row["native_type"] == 2 else REFERENCE_KINDS[row["native_type"]]
                literal = binding_literal(value, kind)
                var = f'next_reference_{row["index"]}'
                writes.append(f'local {var}={literal}')
                source = f'{var}[1]' if kind == "controller" else var
                writes.append(f'if {source}!=undefined and ({source}==obj or (refs.dependencyLoopTest {source} obj) or (refs.dependencyLoopTest {source} base)) do throw "DEPENDENCY_CYCLE: source depends on the target"')
                if kind == "spline":
                    writes.append(f'if {var}!=undefined and superClassOf {var}.baseObject!=Shape do throw "NOT_SPLINE: reference requires a shape"')
                writes.append(f'staged.{prop}[{row["index"]}]={source}')
                if kind == "controller":
                    writes.append(f'staged.efpacontanim[{row["index"]}]={var}[2]')
                continue
            _, prefix, typ = NUMERIC_KINDS[row["kind"]]
            literal = _literal(value, typ)
            if row["limited"] and not row["min"] <= value <= row["max"]:
                raise ValueError("parameter value is outside native limits")
            writes.append(f'staged.{prefix}val[{row["index"]}]={literal}')
        from ..helpers import forest as fh
        body = fh.MXS_LIB + _EFFECT_LIB + _REFERENCE_LIB + f'\nlocal obj=fpForest "{rc._b64(name)}"\nlocal base=obj.baseObject\n'
        body += f'''if fpBusy() do throw "USER_BUSY: an undo operation is active"
if (rcShared obj).count>0 do throw "SHARED_BASE: make the Forest base unique first"
if base.mode!=0 do throw "CUSTOM_EDIT_UNSUPPORTED: requires procedural mode"
if iaEffectsToken obj base!="{expected_effects}" do throw "STALE_EFFECTS: read again"
local staged=undefined
with undo off (staged=copy base
iaPreserveEffectReferences base staged
{chr(10).join(writes)})
if iaEffectsToken obj base!="{expected_effects}" do throw "STALE_EFFECTS: state changed while staging"
if fpBusy() do throw "USER_BUSY: an undo operation became active"
local owns=false
try (theHold.Begin(); owns=true; obj.baseObject=staged; theHold.Accept "MCP Forest effect parameters"; owns=false)
catch (if owns do theHold.Cancel(); throw())
"PARAMETERS_WRITTEN"
'''
        if rc._send(body).strip() != "PARAMETERS_WRITTEN":
            raise RailCloneError("INVALID_READBACK", "parameter write did not acknowledge")
        state = forest_effects(name); rows = parameter_rows(state, effect)
    return {"action": action, "parameters": rows, "effects_token": state["effects_token"],
            "read_only": action == "inspect", "undoable": action == "set", "rebuilt": False,
            "refresh_required": action == "set", "compile_verified": False,
            "controller_animation_verified": False if any(r.get("kind") == "controller" for r in rows) else None}


@mcp.tool()
def forest_effects(name: str, action: str = "list", effect: str = "", code: str = "",
                   effect_name: str = "", enabled: bool = True, expected_effects: str = "",
                   confirm_delete: bool = False, position: int = 0,
                   path: str = "", expected_file_hash: str = "",
                   bindings: dict[str, Any] | None = None,
                   export_reference_definitions: bool = False) -> dict[str, Any]:
    """Read, add, replace code, toggle or remove Forest Pack effects with guarded staged undo.

    action: list, get, add, load, export, move, set_code, toggle, remove. Read returns full effect XML,
    parameter arrays and effects_token. Mutations require expected_effects from a
    current read; effect identifies an exact ID or unique name (not an index).
    add creates a parameter-free effect from Forest Effects language, e.g.
    'fpItem.position.z = fpItem.position.z + 10;'. This is not MAXScript. Existing parameter
    definitions/values are preserved when setting code or toggling. Removing an effect also removes its parameter rows; unknown parameter arrays are refused.
    remove additionally requires confirm_delete=true. Shared bases are refused.
    Edits are staged on a copied Forest base and adopted in one undo step.
    Does not rebuild the scatter, change procedural/custom mode or compile-check
    effect-language code. Readback confirms storage; verify using
    forest_items(refresh_surfaces=true), which forces evaluation (a normal read
    can return the cached distribution). Mutations return refresh_required=true.

    move uses a 1-based destination position and reorders all six effect rows,
    preserving parameter associations by ID. load appends a .eff
    definition, preserving metadata; expected_file_hash must match a library read.
    Numeric parameters and node/spline/texture references are initialized with fresh IDs.
    load requires bindings for every reference by exact parameter ID or name;
    values accept exact unique names, {name, handle} identities or explicit null.
    Texture values require {name, handle, class} identities or explicit null;
    existing maps are reused, not recreated or serialized into the preset.
    export of references requires export_reference_definitions=true: the file
    contains definitions only, not scene bindings. required_bindings lists the
    parameters that must be bound explicitly on the next load. Controller bindings
    require canonical owner/track/controller identities for measured scalar float
    tracks (native type 0); sources are reused and never keyed/edited. Curve files
    and unmeasured controller definition attributes are refused. No source nodes
    are created or serialized.
    export writes an effect with current numeric values as defaults to a new absolute .eff path; existing
    files are never overwritten. Export does not edit Max and is not undoable.
    """
    from ..helpers import forest as fh
    fh.node_name(name, "name")
    if action not in ("list", "get", "add", "load", "export", "move", "set_code", "toggle", "remove"):
        raise ValueError("unknown effect action")
    if type(position) is not int or (action == "move" and position < 1) or (action != "move" and position != 0):
        raise ValueError("move requires a positive 1-based position; other actions use position=0")
    if type(enabled) is not bool or type(confirm_delete) is not bool:
        raise ValueError("flags must be boolean")
    if type(export_reference_definitions) is not bool:
        raise ValueError("export_reference_definitions must be boolean")
    if bindings is not None and action != "load":
        raise ValueError("bindings are accepted only for load")
    if export_reference_definitions and action != "export":
        raise ValueError("export_reference_definitions is accepted only for export")
    if action not in ("list", "add", "load") and (not isinstance(effect, str) or not effect or "\x00" in effect):
        raise ValueError("effect must be an exact ID or unique name")
    if action == "remove" and not confirm_delete:
        raise ValueError("remove requires confirm_delete=true")
    imported_text = ""
    imported_parameters: list[dict[str, Any]] = []
    file_hash = ""
    if action in ("load", "export"):
        if not isinstance(path, str) or "\x00" in path or not Path(path).is_absolute() or Path(path).suffix.lower() != ".eff":
            raise ValueError("load/export requires an absolute .eff path")
        if action == "load":
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_file_hash):
                raise ValueError("load requires expected_file_hash from a current library read")
            info = read_effect_file(Path(path), include_xml=True)
            if info["sha256"].lower() != expected_file_hash.lower():
                raise ValueError("STALE_EFFECT_FILE: file changed; read the library again")
            if not info["parameter_free"]:
                document = parse_effect_xml(info["xml"])
                imported_parameters = parameter_definitions(document, allow_references=True)
                bind_definitions(imported_parameters, bindings)
                # Repeated imports must not share parameter identities.
                for definition, row in zip(document.findall("parameter"), imported_parameters):
                    row["id"] = "{" + str(uuid.uuid4()) + "}"
                    definition.set("id", row["id"])
                info["xml"] = ET.tostring(document, encoding="unicode")
            else:
                bind_definitions([], bindings)
            effect_name = effect_name or info["title"] or info["name"]
            effect_xml(info["code"], effect_name)
            imported_text, file_hash = info["xml"], info["sha256"]
        elif not Path(path).parent.is_dir():
            raise ValueError("export parent directory must exist")
        elif Path(path).exists():
            raise ValueError("EFFECT_FILE_EXISTS: export never overwrites existing files")
    body = fh.MXS_LIB + _EFFECT_LIB + _REFERENCE_LIB + f'\nlocal obj=fpForest "{rc._b64(name)}"\nlocal base=obj.baseObject\n'
    if action not in ("list", "get", "export"):
        if not isinstance(expected_effects, str) or rc.TOKEN.fullmatch(expected_effects) is None:
            raise ValueError("mutation requires effects_token as expected_effects")
        body += f'''if fpBusy() do throw "USER_BUSY: an undo operation is active"
if (rcShared obj).count>0 do throw "SHARED_BASE: make the Forest base unique first"
if base.mode!=0 do throw "CUSTOM_EDIT_UNSUPPORTED: effect edits currently require procedural mode"
if iaEffectsToken obj base!="{expected_effects}" do throw "STALE_EFFECTS: effects changed; read again"
local staged=undefined
with undo off (staged=copy base; iaPreserveEffectReferences base staged)
local rowProps=#(#efidlist,#efnamelist,#efxmllist,#efenablelist)
local n=base.efidlist.count
for p in rowProps do if (getProperty base p).count!=n do throw "EFFECT_LIST_MISMATCH: effect arrays have unequal lengths"
'''
        if action in ("add", "load"):
            text = imported_text or effect_xml(code, effect_name)
            new_id = "{" + str(uuid.uuid4()) + "}"
            body += append_numeric_parameters(imported_parameters, new_id)
            body += f'''
local ids=staged.efidlist as array, labels=staged.efnamelist as array
local xmls=staged.efxmllist as array, flags=staged.efenablelist as array
local selections=staged.efselspeclist as array, specs=staged.efspeclist as array
if selections.count==0 do selections=for i=1 to n collect false
if specs.count==0 do specs=for i=1 to n collect ""
if selections.count!=n or specs.count!=n do throw "EFFECT_LIST_MISMATCH: optional effect arrays have unequal lengths"
append ids "{new_id}"; append labels (rcDecode "{rc._b64(effect_name)}")
append xmls (rcDecode "{rc._b64(text)}"); append flags {rc._mxs_bool(enabled)}
append selections false; append specs ""
staged.efidlist=ids; staged.efnamelist=labels; staged.efxmllist=xmls; staged.efenablelist=flags
staged.efselspeclist=selections; staged.efspeclist=specs
'''
        else:
            body += f'''
local wanted=rcDecode "{rc._b64(effect)}", matches=#()
for i=1 to n do if base.efidlist[i]==wanted do append matches i
if matches.count==0 do for i=1 to n do if base.efnamelist[i]==wanted do append matches i
if matches.count!=1 do throw "EFFECT_NOT_UNIQUE: use a valid effect ID"
local idx=matches[1]
'''
            if action == "set_code":
                effect_xml(code, "validate")
                body += f'''
local doc=dotNetObject "System.Xml.XmlDocument"
doc.XmlResolver=undefined; doc.LoadXml staged.efxmllist[idx]
local ex=doc.SelectSingleNode "/effect/expression"
if ex==undefined do throw "EFFECT_FORMAT_UNSUPPORTED: no expression element"
ex.InnerText=rcDecode "{rc._b64(code)}"
staged.efxmllist[idx]=doc.OuterXml
'''
            elif action == "toggle":
                body += f'staged.efenablelist[idx]={rc._mxs_bool(enabled)}\n'
            elif action == "move":
                body += f'''if {position}>n do throw "BAD_POSITION: destination exceeds effect count"
for p in join rowProps #(#efselspeclist,#efspeclist) do (
    local a=getProperty staged p as array
    if a.count!=0 and a.count!=n do throw "EFFECT_LIST_MISMATCH: optional effect arrays have unequal lengths"
    if a.count==n do (
        local item=a[idx]
        deleteItem a idx
        insertItem item a {position}
        setProperty staged p a
    )
)
'''
            else:
                parameter_props = ",".join("#" + prop for prop in PARAM_DEFAULTS)
                body += f'''local paramProps=#({parameter_props})
local pc=base.efpaid.count, dead=#()
if base.efpaeffid.count!=pc do throw "EFFECT_PARAMETER_LIST_MISMATCH: effect owner array"
for j=1 to pc where base.efpaeffid[j]==base.efidlist[idx] do append dead j
if dead.count>0 do (
    for p in iaEffectsProps staged where matchPattern (p as string) pattern:"efpa*" do (
        if findItem paramProps p==0 do throw "PARAMETER_SCHEMA_UNSUPPORTED: unknown parameter array"
    )
    for p in paramProps do (
        local a=getProperty staged p as array
        if a.count!=0 and a.count!=pc do throw "EFFECT_PARAMETER_LIST_MISMATCH: incomplete parameter array"
        if a.count==pc do (
            for j=dead.count to 1 by -1 do deleteItem a dead[j]
            setProperty staged p a
        )
    )
)
for p in join rowProps #(#efselspeclist,#efspeclist) do (
    local a=getProperty staged p as array
    if a.count!=0 and a.count!=n do throw "EFFECT_LIST_MISMATCH: optional effect arrays have unequal lengths"
    if a.count==n do (deleteItem a idx; setProperty staged p a)
)
'''
        body += f'''
if iaEffectsToken obj base!="{expected_effects}" do throw "STALE_EFFECTS: effects changed while staging"
if fpBusy() do throw "USER_BUSY: an undo operation became active"
local owns=false
try (theHold.Begin(); owns=true; obj.baseObject=staged; theHold.Accept "MCP Forest effects"; owns=false)
catch (if owns do theHold.Cancel(); throw())
base=obj.baseObject
'''
    body += 'iaEffectsReport obj base\n'
    result: dict[str, Any] = {"action": action, "read_only": action in ("list", "get"),
                             "scene_read_only": action in ("list", "get", "export"),
                             "undoable": action not in ("list", "get", "export"), "rebuilt": False,
                             "refresh_required": action not in ("list", "get", "export"),
                             "compile_verified": False,
                             "arrays": {}, "effects": []}
    for row in rc._records(rc._send(body), "ITOOEFFECTS"):
        if row[0] == "T":
            result["effects_token"] = row[1]
        elif row[0] == "A":
            result["arrays"][row[1]] = {"count": int(row[2]), "values": []}
        elif row[0] == "V":
            result["arrays"][row[1]]["values"].append(effect_value(row[3]))
        elif row[0] == "C":
            value = None
            if row[2] != "u":
                owner_handle, owner_class, subanim, ctrl_handle, ctrl_class = row[2].split(":", 4)
                value = {"owner_handle": int(owner_handle), "owner_class": rc._unb64(owner_class),
                         "subanim": int(subanim), "controller_handle": int(ctrl_handle),
                         "controller_class": rc._unb64(ctrl_class)}
            result.setdefault("controller_bindings", {})[int(row[1])] = value
    arrays = {p: a["values"] for p, a in result["arrays"].items()}
    for i, ident in enumerate(arrays.get("efidlist", [])):
        e = {"id": ident, "index": i + 1}
        for prop, label in (("efnamelist", "name"), ("efxmllist", "xml"), ("efenablelist", "enabled")):
            a = arrays.get(prop, [])
            e[label] = a[i] if i < len(a) else None
        result["effects"].append(e)
    if action in ("get", "export"):
        matches = [e for e in result["effects"] if e["id"] == effect]
        if not matches:
            matches = [e for e in result["effects"] if e["name"] == effect]
        if len(matches) != 1:
            raise RailCloneError("EFFECT_NOT_UNIQUE", "use a valid effect ID")
        result["effect"] = matches[0]
    if action == "load":
        result.update(imported_path=path, imported_sha256=file_hash, initialized_parameters=len(imported_parameters))
    if action == "export":
        chosen = result["effect"]
        document = parse_effect_xml(chosen["xml"])
        if document.findall(".//parameter") or chosen["id"] in arrays.get("efpaeffid", []):
            parameter_definitions(document, allow_references=True)
            parameters = parameter_rows(result, chosen["id"])
            definitions = document.findall("parameter")
            if len(parameters) != len(definitions):
                raise ValueError("PARAMETER_SCHEMA_MISMATCH: native parameters and XML differ")
            for definition in definitions:
                matches = [p for p in parameters if str(p["id"]).lower() == definition.get("id", "").lower()]
                if len(matches) != 1 or not matches[0]["writable"]:
                    raise ValueError("PARAMETER_TYPE_UNSUPPORTED: parameter cannot be exported")
                row = matches[0]
                if row["kind"] in ("node", "spline", "texture", "controller"):
                    if not export_reference_definitions:
                        raise ValueError("REFERENCE_EXPORT_CONFIRMATION_REQUIRED: definitions omit scene bindings; set export_reference_definitions=true")
                    result.setdefault("required_bindings", []).append({"id": row["id"], "name": row["name"], "kind": row["kind"]})
                    continue
                definition.set("default", str(row["value"]))
                definition.set("min", str(row["min"])); definition.set("max", str(row["max"]))
                definition.set("limits", "1" if row["limited"] else "0")
            payload = ET.tostring(document, encoding="utf-8")
        else:
            payload = chosen["xml"].encode("utf-8")
        with Path(path).open("xb") as output:
            output.write(payload)
        result.update(exported_path=path, exported_sha256=hashlib.sha256(payload).hexdigest())
        if result.get("required_bindings"):
            result["scene_bindings_exported"] = False
    return result


def _fields(kind: str) -> dict[str, str]:
    if kind not in ("base", "segment"):
        raise ValueError("kind must be base or segment")
    return BASE_FIELDS if kind == "base" else SEGMENT_FIELDS


def _literal(value: Any, typ: str) -> str:
    if typ == "channels":
        if not isinstance(value, str) or re.fullmatch(r"\d{1,2}(?:[ ,;]+\d{1,2})*", value) is None or len(value) > 512:
            raise ValueError("mapping channels must be a string of 0..99 channel IDs")
        return f'(rcDecode "{rc._b64(value)}")'
    if typ == "node":
        if value is None:
            return "undefined"
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("node references must be exact nonempty names or null")
        return f'(iaNode "{rc._b64(value)}")'
    if typ == "bool":
        if type(value) is not bool:
            raise ValueError("Boolean property requires true or false")
        return rc._mxs_bool(value)
    if typ == "point":
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("Point property requires three finite numbers")
        return "[" + ",".join(_literal(x, "number") for x in value) + "]"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Numeric property requires a finite number")
    if typ in ("int", "align", "slice") and (not isinstance(value, int) or abs(value) > 2**31 - 1):
        raise ValueError("Integer property requires a 32-bit integer")
    if typ == "align" and not 0 <= value <= 4:
        raise ValueError("Alignment must be 0..4")
    if typ == "slice" and not 0 <= value <= 20:
        raise ValueError("Slice output must be 0..20")
    return repr(float(value)) if typ == "number" else str(value)


_SLOT_LIB = r'''
fn iaNode text = (
    local hits = getNodeByName (rcDecode text) exact:true all:true
    if hits.count != 1 do throw "NODE_NOT_UNIQUE: source must exist with a unique name"
    hits[1]
)
fn iaSlotToken obj base props = (
    local ss=stringStream ""
    format "%|%|%|" (rcH obj) (rcH base) ((rcApi base #getXMLStyle).getXMLStyle()) to:ss
    for p in props do (
        format "%|" p to:ss
        for v in (rcArr base p) do (
            format "%|" (rcV v) to:ss
            if isKindOf v MAXWrapper do format "%|" (rcH v) to:ss
        )
    )
    rcHash (ss as string)
)
fn iaWritable obj base = (
    if theHold.Holding() or theHold.IsSuspended() or theHold.SuperLevel() != 0 do throw "USER_BUSY: an undo operation is active"
    if (rcShared obj).count > 0 do throw "SHARED_BASE: make the base unique before editing"
    if isProperty base #stylelink and base.stylelink != undefined do throw "SHARED_STYLE: target uses a style master"
    for n in objects where n != obj and isProperty n.baseObject #stylelink do (
        if n.baseObject.stylelink == obj do throw "SHARED_STYLE: target is a style master"
    )
)
'''


@mcp.tool()
def railclone_slots(name: str = "", handle: int = 0, kind: str = "segment", action: str = "inspect",
                    slot: str = "", values: dict[str, Any] | None = None, expected_slots: str = "") -> dict[str, Any]:
    """Inspect or edit RailClone base/segment slots by exact GUID or unique slot name.

    Inspect returns all writable fields, source references and slots_token.
    Edit requires that token, slot, and values mapping published property names
    to typed values. Node bindings use exact names or null. Setting sobjnode
    also updates sobjref and sobjmtl; unrelated fields remain intact. Base nodes
    must match batype (spline/surface). Includes segment transform, alignment,
    padding, deformation, mapping, material IDs and Export to Forest.
    All changes validate before writing, use one undo step, and return readback.
    Does not toggle autoUpdate or force a rebuild. Numeric lengths are scene units;
    srot/srand rotation fields follow the plugin's native units. Read enums first.
    """
    fields = _fields(kind)
    if action not in ("inspect", "edit"):
        raise ValueError("action must be inspect or edit")
    idprop, nameprop = ("baid", "baname") if kind == "base" else ("sid", "sname")
    observed = [idprop, nameprop] + (["batype"] if kind == "base" else ["sobjref", "sobjmtl"]) + list(fields)
    props = "#(" + ",".join("#" + x for x in observed) + ")"
    body = rc._target(name, handle) + _SLOT_LIB + f'\nlocal base=obj.baseObject\nrcApi base #getXMLStyle\nlocal props={props}\n'
    if action == "edit":
        if not isinstance(slot, str) or not slot:
            raise ValueError("edit requires an exact slot GUID/name")
        if not isinstance(expected_slots, str) or rc.TOKEN.fullmatch(expected_slots) is None:
            raise ValueError("edit requires slots_token as expected_slots")
        if not isinstance(values, dict) or not values or any(p not in fields for p in values):
            raise ValueError("values must contain supported slot properties")
        literals = {p: _literal(v, fields[p]) for p, v in values.items()}
        body += f'''
iaWritable obj base
if iaSlotToken obj base props != "{expected_slots}" do throw "STALE_SLOTS: read slots again"
local wanted=rcDecode "{rc._b64(slot)}", hits=#()
local ids=rcArr base #{idprop}, labels=rcArr base #{nameprop}
for i=1 to ids.count do if (ids[i] as string)==wanted do append hits i
if hits.count==0 do for i=1 to labels.count do if (labels[i] as string)==wanted do append hits i
if hits.count!=1 do throw "SLOT_NOT_UNIQUE: slot must identify exactly one input"
local idx=hits[1]
'''
        writes = []
        for p, literal in literals.items():
            body += f'if not isProperty base #{p} or (rcArr base #{p}).count < idx do throw "PROPERTY_UNAVAILABLE: {p}"\n'
            body += f'local next_{p}={literal}\n'
            if fields[p] != "node":
                body += f'if classOf next_{p}!=classOf base.{p}[idx] do throw "PROPERTY_TYPE_MISMATCH: {p} differs in this plugin build"\n'
            if fields[p] == "node":
                body += f'if next_{p}!=undefined and (next_{p}==obj or (refs.dependencyLoopTest next_{p} base)) do throw "DEPENDENCY_CYCLE: source depends on the target"\n'
                if kind == "base":
                    body += f'''if next_{p} != undefined do (
if base.batype[idx]==0 and superClassOf next_{p}.baseObject!=Shape do throw "BAD_SPLINE: base requires a shape"
if base.batype[idx]==1 and superClassOf next_{p}.baseObject!=GeometryClass do throw "BAD_SURFACE: base requires geometry"
)
'''
                else:
                    body += f'''if next_{p} != undefined and superClassOf next_{p}.baseObject!=GeometryClass do throw "BAD_GEOMETRY: segment requires geometry"
if base.sobjref.count<idx or base.sobjmtl.count<idx do throw "SLOT_LIST_MISMATCH: source reference arrays are incomplete"
'''
                    writes.extend([f'base.sobjref[idx]=if next_{p}==undefined then undefined else next_{p}.baseObject',
                                   f'base.sobjmtl[idx]=if next_{p}==undefined then undefined else next_{p}.material'])
            writes.append(f'base.{p}[idx]=next_{p}')
        body += 'local owns=false\ntry (theHold.Begin(); owns=true\n' + "\n".join(writes)
        body += '\ntheHold.Accept "MCP RailClone slots"; owns=false) catch (if owns do theHold.Cancel(); throw())\n'
    body += f'''
local ss=stringStream ""
format "ITOOSLOTS\\n" to:ss
rcRow ss #("T", iaSlotToken obj base props)
for i=1 to (rcArr base #{idprop}).count do (
    for p in props do rcRow ss #("S", i as string, p as string, rcV (rcAt (rcArr base p) i))
)
format "END\\n" to:ss
ss as string
'''
    raw = rc._send(body)
    slots: dict[int, dict[str, Any]] = {}
    result: dict[str, Any] = {"kind": kind, "action": action, "read_only": action == "inspect",
                             "undoable": action == "edit", "field_types": fields, "slots": []}
    for row in rc._records(raw, "ITOOSLOTS"):
        if row[0] == "T":
            result["slots_token"] = row[1]
        elif row[0] == "S":
            slots.setdefault(int(row[1]), {"index": int(row[1])})[row[2]] = rc._dv(row[3])
    result["slots"] = list(slots.values())
    return result


@mcp.tool()
def railclone_metadata(name: str = "", handle: int = 0, description: str | None = None,
                       reset_compatibility: bool = False, confirm_change: bool = False) -> dict[str, Any]:
    """Set a RailClone description or explicitly upgrade style compatibility, with staged undo.

    getStyleDesc is not called: it clears RailClone's cached output in 7.3.5.
    description is stored through setStyleDesc and read from style XML. A reset
    invokes resetCreatedVersion and can change geometry; it requires
    confirm_change=true. Shared bases/style links are refused. Target's transform,
    material and modifiers stay intact; base identity changes in one undo step.
    """
    if description is not None and (not isinstance(description, str) or len(description) > 65536 or "\x00" in description):
        raise ValueError("description must be a string without NUL, up to 65536 characters")
    if type(reset_compatibility) is not bool or type(confirm_change) is not bool:
        raise ValueError("confirmation flags must be boolean")
    if reset_compatibility and not confirm_change:
        raise ValueError("reset_compatibility changes old style behavior; confirm_change is required")
    body = rc._target(name, handle) + _SLOT_LIB + '\nlocal base=obj.baseObject\n'
    if description is not None or reset_compatibility:
        operation = ''
        if description is not None:
            operation += f'local d=rcDecode "{rc._b64(description)}"\n(rcApi staged #setStyleDesc).setStyleDesc d\n'
        if reset_compatibility:
            operation += '(rcApi staged #resetCreatedVersion).resetCreatedVersion()\n'
        body += f'''iaWritable obj base
if base.autoUpdate==false do throw "AUTO_UPDATE_OFF: metadata changes clear cached output; enable updates explicitly first"
local staged=undefined
with undo off (staged=copy base
{operation})
local owns=false
try (theHold.Begin(); owns=true; obj.baseObject=staged; theHold.Accept "MCP RailClone metadata"; owns=false)
catch (if owns do theHold.Cancel(); throw())
base=obj.baseObject
'''
    body += '''local xml=(rcApi base #getXMLStyle).getXMLStyle()
rcResult obj xml (rcToken obj xml)
'''
    result = rc._style_result(rc._run(body))
    result.update(undoable=description is not None or reset_compatibility,
                  compatibility_reset=reset_compatibility)
    return result


@mcp.tool()
def railclone_proxy(name: str = "", handle: int = 0, action: str = "inspect", mode: str = "disabled",
                    path: str = "", confirm_change: bool = False, overwrite: bool = False) -> dict[str, Any]:
    """Inspect or set RailClone Proxy Cache (disabled, embedded, external).

    set calls the real setProxyMode and requires confirm_change: cache generation
    freezes output, can block Max, and is not claimed undoable. External paths
    must be absolute .rcproxy paths with an existing parent directory. Existing
    files require overwrite=true, checked inside Max immediately before calling.
    Does not alter Use Instancing Engine or the fullMeshProxy registry setting.
    Forest Pack cannot distribute this RailClone's items while Proxy Cache is on.
    Vantage cache compatibility is unverified. Returns native status and readback.
    """
    if action not in ("inspect", "set") or mode not in ("disabled", "embedded", "external"):
        raise ValueError("invalid action or mode")
    if type(confirm_change) is not bool or type(overwrite) is not bool:
        raise ValueError("confirmation flags must be boolean")
    body = rc._target(name, handle) + _SLOT_LIB + '\nlocal base=obj.baseObject\nrcApi base #setProxyMode\nlocal status=""\n'
    if action == "set":
        if not confirm_change:
            raise ValueError("set requires confirm_change=true; cache writes are not undoable")
        if not isinstance(path, str) or "\x00" in path:
            raise ValueError("path must be a string without NUL")
        if mode == "external":
            from pathlib import PureWindowsPath
            p = PureWindowsPath(path)
            if not p.is_absolute() or p.suffix.lower() != ".rcproxy":
                raise ValueError("external cache needs an absolute .rcproxy path")
        elif path:
            raise ValueError("disabled/embedded modes use an empty path")
        body += 'iaWritable obj base\n' + f'local cacheFile=rcDecode "{rc._b64(path)}"\n'
        if mode == "external":
            body += '''if not (doesDirectoryExist (getFilenamePath cacheFile)) do throw "DIRECTORY_MISSING: cache parent is missing"
'''
            if not overwrite:
                body += 'if doesFileExist cacheFile do throw "OVERWRITE_REQUIRED: external cache already exists"\n'
        code = {"disabled": 0, "embedded": 1, "external": 2}[mode]
        body += f'status=(rcApi base #setProxyMode).setProxyMode {code} cacheFile\n'
        if mode == "external":
            body += 'if not doesFileExist cacheFile do throw ("CACHE_WRITE_FAILED: " + status)\n'
    body += '''local ss=stringStream ""
format "ITOOPROXY\\n" to:ss
rcRow ss #("P", rcV base.proxymode, rcV base.proxyfile, rcV status)
format "END\\n" to:ss
ss as string
'''
    rows = rc._records(rc._send(body), "ITOOPROXY")
    if len(rows) != 1 or rows[0][0] != "P":
        raise RailCloneError("INVALID_READBACK", "missing proxy status")
    row = rows[0]
    return {"proxymode": rc._dv(row[1]), "proxyfile": rc._dv(row[2]), "native_status": rc._dv(row[3]),
            "read_only": action == "inspect", "undoable": False,
            "warnings": [] if action == "inspect" or mode == "disabled" else ["PROXY_CACHE: Forest Pack item distribution is unavailable; Vantage compatibility unverified"]}


@mcp.tool()
def railclone_instances(name: str = "", handle: int = 0, action: str = "create", layer: str = "",
                        max_items: int = 1000, separated_meshes: bool = True,
                        disable_source: bool = False, confirm_delete: bool = False) -> dict[str, Any]:
    """Create standalone RailClone instances, enable source, or delete prior vendor-generated instances.

    Explicit target only; previous selection is restored even on failure. Create
    uses the global Instantiate API (no auto-delete or forced material/UVW bypass).
    Refuses cached/proxy, manual-update, empty or over-max_items output before
    converting. max_items caps the native output items, not a promise of exact
    node count. Native conversion/deletion is not claimed undoable; create returns
    exact created node handles for cleanup. Delete is vendor name-based and needs
    confirm_delete=true; renaming source/instances can break that association.
    """
    if action not in ("create", "delete", "enable"):
        raise ValueError("action must be create, delete or enable")
    if type(max_items) is not int or not 1 <= max_items <= 100000:
        raise ValueError("max_items must be 1..100000")
    if any(type(v) is not bool for v in (separated_meshes, disable_source, confirm_delete)):
        raise ValueError("flags must be boolean")
    if not isinstance(layer, str) or "\x00" in layer or len(layer) > 256:
        raise ValueError("layer must be a string without NUL, up to 256 characters")
    if action == "delete" and not confirm_delete:
        raise ValueError("delete requires confirm_delete=true; deletion is name-based")
    body = rc._target(name, handle) + _SLOT_LIB + '\nlocal base=obj.baseObject\niaWritable obj base\n'
    if action == "create":
        body += f'''if base.disabled do throw "SOURCE_DISABLED: enable the source first"
if base.autoUpdate==false do throw "AUTO_UPDATE_OFF: enable updates explicitly before conversion"
if base.proxymode!=0 do throw "PROXY_CACHE: output sources are unavailable"
local ox=rcRebuildOutput base false
local counts=rcOutStats ox
    local n=counts[1] as integer
if n==0 do throw "EMPTY_OUTPUT: no source items to instantiate"
if n>{max_items} do throw "ITEM_LIMIT: output exceeds max_items"
'''
        mode = 2 if layer else 0
        op = f'local targetLayer=rcDecode "{rc._b64(layer)}"\nRailClone_Pro.global.Instantiate {mode} targetLayer false {rc._mxs_bool(separated_meshes)} false {rc._mxs_bool(disable_source)}'
    else:
        op = 'RailClone_Pro.global.InstantiateDelete()' if action == "delete" else 'RailClone_Pro.global.InstantiateEnable()'
    body += f'''local prev=selection as array, before=objects as array
try (select obj
{op}
rcRestoreSel prev) catch (rcRestoreSel prev; throw())
local ss=stringStream ""
format "ITOOINSTANCES\\n" to:ss
for nd in objects where findItem before nd==0 do rcRow ss #("N", rcH nd, rcEncode nd.name)
for nd in before where not isValidNode nd do rcRow ss #("D", "deleted")
rcRow ss #("S", rcV base.disabled)
format "END\\n" to:ss
ss as string
'''
    result: dict[str, Any] = {"action": action, "created": [], "deleted_count": 0, "undoable": False}
    for row in rc._records(rc._send(body), "ITOOINSTANCES"):
        if row[0] == "N":
            result["created"].append({"handle": int(row[1]), "name": rc._unb64(row[2])})
        elif row[0] == "D":
            result["deleted_count"] += 1
        elif row[0] == "S":
            result["source_disabled"] = rc._dv(row[1])
    if action == "create" and not result["created"]:
        raise RailCloneError("EMPTY_OUTPUT", "native Instantiate created no scene nodes", result)
    return result


RC_SETTINGS = {
    "disabled": "bool", "autoUpdate": "bool", "seed": "int", "gscale": "number",
    "iconSize": "number", "curvesteps": "int", "maxseg": "int", "maxfaces": "number",
    "vmesh": "int", "rmesh": "int", "rendermode": "bool",
}
FP_SETTINGS = {
    "disabled": "bool", "manualupdate": "bool", "seed": "int", "units_x": "number",
    "units_y": "number", "lock_ratio": "bool", "direction": "int", "maxdensity": "int",
    "vmaxitems": "int", "rmaxitems": "int", "vmesh": "int", "rmesh": "int",
    "pf_efonlyrender": "bool", "applyRotation": "bool", "applyScale": "bool", "applyTranslation": "bool",
}


def settings_values(settings: Any, fields: dict[str, str]) -> dict[str, str]:
    if not isinstance(settings, dict) or not settings or any(k not in fields for k in settings):
        raise ValueError("settings must be a nonempty mapping of supported field names")
    literals = {k: _literal(v, fields[k]) for k, v in settings.items()}
    for k, v in settings.items():
        if k in {"gscale", "units_x", "units_y"} and v <= 0:
            raise ValueError(k + " must be positive")
        if k in {"seed", "iconSize", "curvesteps", "maxseg", "maxfaces", "maxdensity", "vmaxitems", "rmaxitems"} and v < 0:
            raise ValueError(k + " must be nonnegative")
        if k == "direction" and not -100 <= v <= 100:
            raise ValueError("direction must be -100..100")
        if fields is RC_SETTINGS and k == "vmesh" and not 0 <= v <= 4:
            raise ValueError("RailClone vmesh must be 0..4")
        if fields is RC_SETTINGS and k == "rmesh" and not 0 <= v <= 1:
            raise ValueError("RailClone rmesh must be 0..1")
    return literals


def _settings(plugin: str, name: str, action: str, settings: dict[str, Any] | None,
              expected_settings: str, confirm_rebuild: bool) -> dict[str, Any]:
    from ..helpers import forest as fh
    if action not in ("inspect", "set") or type(confirm_rebuild) is not bool:
        raise ValueError("action must be inspect/set; confirm_rebuild must be boolean")
    fields = RC_SETTINGS if plugin == "railclone" else FP_SETTINGS
    if plugin == "forest":
        fh.node_name(name, "name")
        body = fh.MXS_LIB + f'local obj=fpForest "{rc._b64(name)}"\n'
    else:
        body = rc._target(name, 0)
    body += _SLOT_LIB + '\nlocal base=obj.baseObject\n'
    if plugin == "railclone":
        body += 'if getInterface base #railclone==undefined do throw "NOT_RAILCLONE: target has no RailClone interface"\n'
    props = "#(" + ",".join("#" + p for p in fields) + ")"
    body += f'''local props={props}
fn iaSettingsToken obj base props = (
    local ss=stringStream ""
    format "%|%|" (rcH obj) (rcH base) to:ss
    for p in props do format "%|%|" p (rcV (if isProperty base p then getProperty base p else undefined)) to:ss
    rcHash (ss as string)
)
'''
    if action == "set":
        if not isinstance(expected_settings, str) or rc.TOKEN.fullmatch(expected_settings) is None:
            raise ValueError("set requires settings_token as expected_settings")
        literals = settings_values(settings, fields)
        body += f'iaWritable obj base\nif iaSettingsToken obj base props!="{expected_settings}" do throw "STALE_SETTINGS: settings changed; read again"\n'
        if plugin == "forest":
            body += 'if base.mode!=0 do throw "CUSTOM_EDIT_UNSUPPORTED: configure procedural forests only"\n'
        for p, literal in literals.items():
            body += f'if not isProperty base #{p} do throw "PROPERTY_UNAVAILABLE: {p}"\nlocal next_{p}={literal}\n'
            body += f'if classOf next_{p}!=classOf (getProperty base #{p}) do throw "PROPERTY_TYPE_MISMATCH: {p} differs in this plugin build"\n'
        if not confirm_rebuild:
            if plugin == "railclone" and settings.get("autoUpdate") is True:
                body += 'if base.autoUpdate==false do throw "REBUILD_CONFIRMATION_REQUIRED: enabling updates replaces frozen output"\n'
            if plugin == "forest" and settings.get("manualupdate") is False:
                body += 'if base.manualupdate==true do throw "REBUILD_CONFIRMATION_REQUIRED: enabling updates replaces cached output"\n'
        body += 'local owns=false\ntry (theHold.Begin(); owns=true\n'
        body += '\n'.join(f'setProperty base #{p} next_{p}' for p in literals)
        body += '\ntheHold.Accept "MCP iToo settings"; owns=false) catch (if owns do theHold.Cancel(); throw())\n'
    body += '''local ss=stringStream ""
format "ITOOSETTINGS\\n" to:ss
rcRow ss #("T", iaSettingsToken obj base props)
for p in props do rcRow ss #("P", p as string, rcV (if isProperty base p then getProperty base p else undefined))
-- Forest exposes its translation extents on the Forest object; checking the
-- base object reports them as unavailable in FP 9.4.3 even though they are readable.
for p in #(#transxmin, #transxmax, #transymin, #transymax, #transzmin, #transzmax) do
    rcRow ss #("X", p as string, rcV (if isProperty obj p then getProperty obj p else undefined))
format "END\\n" to:ss
ss as string
'''
    result: dict[str, Any] = {"plugin": plugin, "action": action, "settings": {}, "field_types": fields,
                             "read_only": action == "inspect", "undoable": action == "set",
                             "geometry_undoable": False, "forced_rebuild": False}
    if plugin == "forest":
        result["translation_extents"] = {"x_min": None, "x_max": None, "y_min": None, "y_max": None,
                                         "z_min": None, "z_max": None}
    for row in rc._records(rc._send(body), "ITOOSETTINGS"):
        if row[0] == "T":
            result["settings_token"] = row[1]
        elif row[0] == "P":
            canonical = next((p for p in fields if p.lower() == row[1].lower()), row[1])
            result["settings"][canonical] = rc._dv(row[2])
        elif row[0] == "X" and plugin == "forest":
            names = {"transxmin": "x_min", "transxmax": "x_max", "transymin": "y_min",
                     "transymax": "y_max", "transzmin": "z_min", "transzmax": "z_max"}
            key = names.get(row[1].lstrip("#").lower())
            if key:
                value = rc._dv(row[2])
                result["translation_extents"][key] = int(value) if isinstance(value, (int, float)) else None
    return result


@mcp.tool()
def railclone_settings(name: str, action: str = "inspect", settings: dict[str, Any] | None = None,
                       expected_settings: str = "", confirm_rebuild: bool = False) -> dict[str, Any]:
    """Inspect/edit RailClone global, display and instancing settings with a state token and one undo hold.

    Supported: disabled, autoUpdate, seed, gscale, iconSize, curvesteps, maxseg,
    maxfaces, vmesh (0..4), rmesh (0..1), rendermode (Use Instancing Engine).
    set requires settings_token and typed settings. Enabling autoUpdate from
    manual mode requires confirm_rebuild=true: regenerated geometry cannot be
    undone by restoring the flag. No forced rebuild or registry change occurs.
    Shared bases/style links, stale state and mismatched native types are refused.
    """
    return _settings("railclone", name, action, settings, expected_settings, confirm_rebuild)


@mcp.tool()
def forest_settings(name: str, action: str = "inspect", settings: dict[str, Any] | None = None,
                    expected_settings: str = "", confirm_rebuild: bool = False) -> dict[str, Any]:
    """Inspect/edit Forest distribution, update, cap, transform and effect-display settings.

    Uses a fresh settings_token, native type checks and one undo hold. units_x/y
    are positive tile dimensions in scene units; lock_ratio can make Y follow X.
    direction is -100..100. maxdensity is a candidate-cell cap, not density.
    Includes seed, disabled, manualupdate, viewport/render caps/modes,
    applyRotation/Scale/Translation and pf_efonlyrender. Procedural mode only; never
    switches custom edit mode. Disabling manualupdate needs confirm_rebuild=true.
    Does not build: verify with forest_items(refresh_surfaces=true).
    """
    return _settings("forest", name, action, settings, expected_settings, confirm_rebuild)


def parse_effect_xml(text: str) -> ET.Element:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 2_000_000 or "\x00" in text:
        raise ValueError("effect XML must be text without NUL, up to 2 MB")
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("effect DTDs/entities are unsupported")
    root = ET.fromstring(text)
    if root.tag != "effect":
        raise ValueError("expected an effect XML root")
    return root


def read_effect_file(path: Path, include_xml: bool = False) -> dict[str, Any]:
    if path.suffix.lower() != ".eff" or not path.is_file():
        raise ValueError("path must be an existing .eff file")
    if path.stat().st_size > 2_000_000:
        raise ValueError("effect file exceeds 2 MB")
    with path.open("rb") as stream:
        data = stream.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("effect file exceeds 2 MB")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError("effect XML must be UTF-8 or UTF-16 with a BOM")
    root = parse_effect_xml(text)
    params = [dict(p.attrib) for p in root.findall(".//parameter")]
    result = {"path": str(path), "name": path.stem, "title": root.get("title", "")[:256],
            "description": root.get("description", "")[:2000], "version": root.get("version"),
            "sha256": hashlib.sha256(data).hexdigest(), "parameters": params,
            "parameter_free": not params, "code": root.findtext("expression", default="")}
    if include_xml:
        result["xml"] = text
    return result


@mcp.tool()
def forest_effect_library(query: str = "", action: str = "search", path: str = "",
                          roots: StrList | None = None, limit: int = 50, include_code: bool = False) -> dict[str, Any]:
    """Search/read installed or user Forest Effects .eff files, without contacting or editing Max.

    Search matches words in title/name/description. Defaults to LOCALAPPDATA/Itoo
    Software/Forest Pack Pro/effects and Documents/ForestPack/effects. Explicit
    roots can cover custom locations. get reads an exact absolute .eff path.
    Returns definitions, parameter metadata and hashes; code is opt-in. Does not
    import into Max, execute code, initialize parameters or write library files.
    Scans at most 2000 files and reports truncation/errors. XML DTDs are refused.
    """
    if action not in ("search", "get") or not isinstance(query, str) or len(query) > 256:
        raise ValueError("action must be search/get; query up to 256 characters")
    if type(limit) is not int or not 1 <= limit <= 200 or type(include_code) is not bool:
        raise ValueError("limit must be 1..200; include_code must be boolean")
    if action == "get":
        p = Path(path)
        if not p.is_absolute():
            raise ValueError("get requires an absolute path")
        item = read_effect_file(p)
        if not include_code:
            item.pop("code")
        return {"read_only": True, "item": item}
    if roots is None:
        folders = [Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) /
                   "Itoo Software" / "Forest Pack Pro" / "effects", Path.home() / "Documents" / "ForestPack" / "effects"]
    else:
        if not isinstance(roots, list) or not 1 <= len(roots) <= 20 or any(not isinstance(x, str) for x in roots):
            raise ValueError("roots must be a list of 1..20 absolute directories")
        folders = [Path(p) for p in roots]
        if any(not p.is_absolute() for p in folders):
            raise ValueError("roots must be absolute")
    items, errors, seen = [], [], set()
    scanned, scan_complete = 0, True
    for folder in folders:
        if not folder.is_dir():
            continue
        for p in sorted(folder.rglob("*.eff")):
            canonical = str(p.resolve()).lower()
            if canonical in seen:
                continue
            seen.add(canonical)
            if scanned >= 2000:
                scan_complete = False
                break
            scanned += 1
            try:
                item = read_effect_file(p)
            except (OSError, ValueError, ET.ParseError) as exc:
                errors.append({"path": str(p), "error": str(exc)[:300]})
                continue
            text = " ".join(item[k] for k in ("name", "title", "description")).lower()
            if all(word in text for word in query.lower().split()):
                if not include_code:
                    item.pop("code")
                items.append(item)
    return {"read_only": True, "roots": [str(p) for p in folders], "items": items[:limit],
            "matched": len(items), "truncated": len(items) > limit or not scan_complete,
            "scanned": scanned, "scan_complete": scan_complete, "errors": errors}
