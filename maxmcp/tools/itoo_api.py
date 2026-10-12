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
fn iaEffectsProps base = (
    local props=#()
    for p in getPropNames base where matchPattern (p as string) pattern:"ef*" do (
        local a=getProperty base p
        if isKindOf a ArrayParameter or isKindOf a Array do append props p
    )
    props
)
fn iaEffectsToken obj base = (
    local ss=stringStream ""
    format "%|%|" (rcH obj) (rcH base) to:ss
    for p in iaEffectsProps base do (
        format "%|" p to:ss
        local a=getProperty base p
        for v in a do format "%|" (rcV v) to:ss
    )
    rcHash (ss as string)
)
fn iaEffectsReport obj base = (
    local ss=stringStream ""
    format "ITOOEFFECTS\n" to:ss
    rcRow ss #("T", iaEffectsToken obj base)
    for p in iaEffectsProps base do (
        local a=getProperty base p
        rcRow ss #("A", p as string, a.count as string)
        for i=1 to a.count do rcRow ss #("V", p as string, i as string, rcV a[i])
    )
    format "END\n" to:ss
    ss as string
)
'''


@mcp.tool()
def forest_effects(name: str, action: str = "list", effect: str = "", code: str = "",
                   effect_name: str = "", enabled: bool = True, expected_effects: str = "",
                   confirm_delete: bool = False) -> dict[str, Any]:
    """Read, add, replace code, toggle or remove Forest Pack effects with guarded staged undo.

    action: list, get, add, set_code, toggle, remove. Read returns full effect XML,
    parameter arrays and effects_token. Mutations require expected_effects from a
    current read; effect identifies an exact ID or unique name (not an index).
    add creates a parameter-free effect from Forest Effects language, e.g.
    'fpItem.position.z = fpItem.position.z + 10;'. This is not MAXScript. Existing parameter
    definitions/values are preserved when setting code or toggling. Removing a
    parameterized effect is refused until parameter-row cleanup is implemented.
    remove additionally requires confirm_delete=true. Shared bases are refused.
    Edits are staged on a copied Forest base and adopted in one undo step.
    Does not rebuild the scatter, change procedural/custom mode or compile-check
    effect-language code. Readback confirms storage; verify using
    forest_items(refresh_surfaces=true), which forces evaluation (a normal read
    can return the cached distribution). Mutations return refresh_required=true.
    """
    from ..helpers import forest as fh
    fh.node_name(name, "name")
    if action not in ("list", "get", "add", "set_code", "toggle", "remove"):
        raise ValueError("unknown effect action")
    if type(enabled) is not bool or type(confirm_delete) is not bool:
        raise ValueError("flags must be boolean")
    if action not in ("list", "add") and (not isinstance(effect, str) or not effect or "\x00" in effect):
        raise ValueError("effect must be an exact ID or unique name")
    if action == "remove" and not confirm_delete:
        raise ValueError("remove requires confirm_delete=true")
    body = fh.MXS_LIB + _EFFECT_LIB + f'\nlocal obj=fpForest "{rc._b64(name)}"\nlocal base=obj.baseObject\n'
    if action not in ("list", "get"):
        if not isinstance(expected_effects, str) or rc.TOKEN.fullmatch(expected_effects) is None:
            raise ValueError("mutation requires effects_token as expected_effects")
        body += f'''if fpBusy() do throw "USER_BUSY: an undo operation is active"
if (rcShared obj).count>0 do throw "SHARED_BASE: make the Forest base unique first"
if base.mode!=0 do throw "CUSTOM_EDIT_UNSUPPORTED: effect edits currently require procedural mode"
if iaEffectsToken obj base!="{expected_effects}" do throw "STALE_EFFECTS: effects changed; read again"
local staged=undefined
with undo off (staged=copy base)
local rowProps=#(#efidlist,#efnamelist,#efxmllist,#efenablelist)
local n=base.efidlist.count
for p in rowProps do if (getProperty base p).count!=n do throw "EFFECT_LIST_MISMATCH: effect arrays have unequal lengths"
'''
        if action == "add":
            text = effect_xml(code, effect_name)
            new_id = "{" + str(uuid.uuid4()) + "}"
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
            else:
                body += '''if findItem (base.efpaeffid as array) base.efidlist[idx]>0 do throw "PARAMETERIZED_EFFECT: parameter cleanup is not yet supported"
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
                             "undoable": action not in ("list", "get"), "rebuilt": False,
                             "refresh_required": action not in ("list", "get"),
                             "compile_verified": False,
                             "arrays": {}, "effects": []}
    for row in rc._records(rc._send(body), "ITOOEFFECTS"):
        if row[0] == "T":
            result["effects_token"] = row[1]
        elif row[0] == "A":
            result["arrays"][row[1]] = {"count": int(row[2]), "values": []}
        elif row[0] == "V":
            result["arrays"][row[1]]["values"].append(rc._dv(row[3]))
    arrays = {p: a["values"] for p, a in result["arrays"].items()}
    for i, ident in enumerate(arrays.get("efidlist", [])):
        e = {"id": ident, "index": i + 1}
        for prop, label in (("efnamelist", "name"), ("efxmllist", "xml"), ("efenablelist", "enabled")):
            a = arrays.get(prop, [])
            e[label] = a[i] if i < len(a) else None
        result["effects"].append(e)
    if action == "get":
        matches = [e for e in result["effects"] if e["id"] == effect]
        if not matches:
            matches = [e for e in result["effects"] if e["name"] == effect]
        if len(matches) != 1:
            raise RailCloneError("EFFECT_NOT_UNIQUE", "use a valid effect ID")
        result["effect"] = matches[0]
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
format "END\\n" to:ss
ss as string
'''
    result: dict[str, Any] = {"plugin": plugin, "action": action, "settings": {}, "field_types": fields,
                             "read_only": action == "inspect", "undoable": action == "set",
                             "geometry_undoable": False, "forced_rebuild": False}
    for row in rc._records(rc._send(body), "ITOOSETTINGS"):
        if row[0] == "T":
            result["settings_token"] = row[1]
        elif row[0] == "P":
            canonical = next((p for p in fields if p.lower() == row[1].lower()), row[1])
            result["settings"][canonical] = rc._dv(row[2])
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


def read_effect_file(path: Path) -> dict[str, Any]:
    if path.suffix.lower() != ".eff" or not path.is_file():
        raise ValueError("path must be an existing .eff file")
    if path.stat().st_size > 2_000_000:
        raise ValueError("effect file exceeds 2 MB")
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError("effect XML must be UTF-8 or UTF-16 with a BOM")
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("effect DTDs/entities are unsupported")
    root = ET.fromstring(text)
    if root.tag != "effect":
        raise ValueError("expected an effect XML root")
    params = [dict(p.attrib) for p in root.findall("parameter")]
    return {"path": str(path), "name": path.stem, "title": root.get("title", "")[:256],
            "description": root.get("description", "")[:2000], "version": root.get("version"),
            "sha256": hashlib.sha256(data).hexdigest(), "parameters": params,
            "parameter_free": not params, "code": root.findtext("expression", default="")}


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
