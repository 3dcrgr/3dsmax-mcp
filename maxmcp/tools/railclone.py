"""RailClone tools: style XML, evaluated output, inspection, exposed parameters and the library.

The XML methods (getXMLStyle/setXMLStyle/getXMLOutput) exist in RailClone 7.x;
they were verified on RailClone Pro 7.1.1 (Max 2026) and upstream on 7.3.5.
The library tools read the library browser roots from the registry.
"""
from __future__ import annotations

import base64
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from ..coerce import StrList
from ..helpers import railclone_lib as rclib
from ..helpers.maxscript import NODE_LOOKUP_FNS, name_in_use
from ..helpers.mesh import integer
from ..helpers.precheck import PrecheckBlocked
from ..helpers.railclone_lib import RailCloneError
from ..max_client import DialogBlocked
from ..server import client, mcp


_NODE_LOOKUP_FNS = NODE_LOOKUP_FNS


MAX_XML_CHARS = 4_000_000
TOKEN = re.compile(r"[0-9A-F]{2}(?:-[0-9A-F]{2}){31}")
MS_ERROR = "__MCP_MS_ERR__:"
UNDO_PARAMS = "MCP RailClone params"
UNDO_LIBRARY = "MCP RailClone from library"
UNDO_CLEANUP = "MCP RailClone cleanup"
_AUTO_UPDATE_NOTE = ("AUTO_UPDATE_OFF: autoUpdate is off (manual mode); force_rebuild=true switched it on for the "
                     "read and back off (undo off), so the RailClone was rebuilt and keeps the new geometry, which "
                     "can't be undone")
_AUTO_UPDATE_KEPT = ("AUTO_UPDATE_OFF: autoUpdate is off (manual mode), so the RailClone was left alone and this is "
                     "its last build: it can be stale after moves or edits, and empty after a script called "
                     "getStyleDesc(). force_rebuild=true builds it (autoUpdate on for the read and back off, undo "
                     "off); the new geometry is kept, can't be undone, and a heavy RailClone blocks Max while it "
                     "builds")
_RELOAD_NOTE = ("SEGMENTS_RELOADED: segmentsUpdate ran (reload_segments). It renamed every linked segment (sname) "
                "after its source node, so sname can differ from the segment's name in the style XML, and it "
                "isn't undoable. It has no geometry effect on RailClone 7.3.5: use force_rebuild to pick up "
                "source edits made while autoUpdate was off")
_RELOAD_SKIPPED = ("RELOAD_SKIPPED: nothing was rebuilt (autoUpdate is off), so segmentsUpdate didn't run; "
                   "force_rebuild=true runs it")


def _proxy_note(proxymode: Any, by_box: bool = True) -> str:
    placed = ("an item counts as placed unless its box marks an empty slot" if by_box else
              "placed and placeholders are left out")
    return (f"PROXY_CACHE: Proxy Cache is on (proxymode {proxymode}). Its output lists the items without "
            f"source_segment, so item counts by segment are unknown (by_segment is empty; {placed}), and a Forest "
            "Pack object that uses this RailClone's items (Use RailClone Items) gets none")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _mxs_bool(value: bool) -> str:
    return "true" if value else "false"


def _parse_xml(text: str, root: str) -> tuple[ET.Element, int, int]:
    """Parse a de-duplicated copy; returns (element, attributes removed, conflicting removals)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("XML must be a nonempty string")
    if len(text) > MAX_XML_CHARS:
        raise ValueError(f"XML exceeds {MAX_XML_CHARS} characters; partial styles are not accepted")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("XML DTDs and entity declarations are not supported")
    fixed, removed, conflicts = rclib.sanitize_xml(text)
    try:
        element = ET.fromstring(fixed)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML: {exc}") from exc
    if element.tag != root:
        raise ValueError(f"Expected <{root}> XML, got <{element.tag}>")
    return element, removed, conflicts


def _xml(text: str, root: str) -> ET.Element:
    return _parse_xml(text, root)[0]


_FUNCTIONS = r'''
fn rcEncode text = (dotNetClass "System.Convert").ToBase64String ((dotNetClass "System.Text.Encoding").UTF8.GetBytes text)
fn rcDecode text = (dotNetClass "System.Text.Encoding").UTF8.GetString ((dotNetClass "System.Convert").FromBase64String text)
fn rcHash text = (
    local sha = (dotNetClass "System.Security.Cryptography.SHA256").Create()
    local digest = (dotNetClass "System.BitConverter").ToString (sha.ComputeHash ((dotNetClass "System.Text.Encoding").UTF8.GetBytes text))
    sha.Dispose(); digest as string
)
fn rcApi base method = (
    local api = getInterface base #railclone
    if api == undefined do throw "NOT_RAILCLONE: target has no RailClone interface"
    if not isProperty api method do (
        local code = if matchPattern (method as string) pattern:"*XML*" then "XML_API_UNAVAILABLE" else "API_UNAVAILABLE"
        throw (code + ": this RailClone build has no railclone." + (method as string) + " method")
    )
    api
)
fn rcToken obj xml = rcHash ((formattedPrint ((getHandleByAnim obj) as integer64) format:"d") + "|" + (formattedPrint ((getHandleByAnim obj.baseObject) as integer64) format:"d") + "|" + xml)
fn rcResult obj xml token = (
    "RCXML|" + (formattedPrint ((getHandleByAnim obj) as integer64) format:"d") + "|" + (rcEncode obj.name) + "|" + token + "|" + (rcEncode xml)
)
fn rcSanitize text = (dotNetClass "System.Text.RegularExpressions.Regex").Replace text (rcDecode "@@DUP@@") "$1"
fn rcH n = formattedPrint ((getHandleByAnim n) as integer64) format:"d"
fn rcG v = formattedPrint v format:".9g"
fn rcJoin parts sep = (
    local s = ""
    for i = 1 to parts.count do (
        if i > 1 do s += sep
        s += parts[i]
    )
    s
)
fn rcRow ss parts = format "%\n" (rcJoin parts "|") to:ss
fn rcV v = (
    if v == undefined then "u"
    else if isKindOf v BooleanClass then (if v then "b1" else "b0")
    else if isKindOf v Integer or isKindOf v Integer64 then ("i" + (formattedPrint v format:"d"))
    else if isKindOf v IntegerPtr then ("i" + (formattedPrint (v as integer64) format:"d"))
    else if isKindOf v Float or isKindOf v Double then ("f" + (rcG v))
    else if isKindOf v String then ("s" + (rcEncode v))
    else if isKindOf v Name then ("s" + (rcEncode (v as string)))
    else if isValidNode v then ("n" + (rcH v) + ":" + (rcEncode v.name))
    else if isKindOf v Point3 then ("p" + (rcG v.x) + "," + (rcG v.y) + "," + (rcG v.z))
    else if isKindOf v Material then ("m" + (rcEncode ((classOf v) as string)) + ":" + (rcEncode v.name))
    else ("o" + (rcEncode ((classOf v) as string)) + ":" + (rcEncode (v as string)))
)
fn rcArr base prop = if isProperty base prop then (getProperty base prop) else #()
fn rcAt arr i = if arr != undefined and i <= arr.count then arr[i] else undefined
-- The running RailClone version as a number (major, minor and patch digits): a new instance's
-- createdversion; no node is created (live).
-- RailClone_Pro.version() is an API number (400), not the product version.
fn rcRuntime = (try ((createInstance RailClone_Pro).createdversion) catch undefined)
-- A `for ... collect` written straight into a call's arguments must not read the caller's
-- locals: MAXScript then fails with "Bad free thunk local member index" or reads the wrong
-- variable (both seen live). Collect into a local first, then pass it.
fn rcTm m = (
    local parts = for r in #(m.row1, m.row2, m.row3, m.row4) collect ((rcG r.x) + "," + (rcG r.y) + "," + (rcG r.z))
    rcJoin parts ","
)
fn rcShared obj = (
    local base = obj.baseObject
    local deps = try (refs.dependentNodes base baseObjectOnly:true) catch undefined
    if deps == undefined do deps = for n in objects where n.baseObject == base collect n
    for n in deps where isValidNode n and n != obj collect n
)
fn rcRestoreSel prevSel = (
    local keep = for n in prevSel where isValidNode n collect n
    if keep.count == 0 then clearSelection() else select keep
)
-- Rebuild, then return the output export. Moves and copies do not re-evaluate a RailClone, and neither
-- do notifyDependents or reading .mesh. Live (RailClone 7.3): seed = seed rebuilds without touching the
-- segments. segmentsUpdate changes no geometry there, but renames every linked segment (sname) to
-- its source node's name, so it runs only when reloadSegs is true. With autoUpdate off no scripted
-- call rebuilds and the build is lazy: autoUpdate goes on, the export is read, then it goes back
-- off (the rebuilt output is kept). All with undo off. The read tools call it on a RailClone with
-- autoUpdate off only when asked to (force_rebuild): it replaces the geometry the user froze.
fn rcRebuildOutput base reloadSegs = (
    local api = rcApi base #getXMLOutput
    local xml = undefined
    local wasOff = (isProperty base #autoUpdate) and base.autoUpdate == false
    with undo off (
        if wasOff do base.autoUpdate = true
        try (
            if reloadSegs then (
                local n = (rcArr base #sid).count
                if n > 0 then ((rcApi base #segmentsUpdate).segmentsUpdate 1 n) else (base.seed = base.seed)
            ) else (base.seed = base.seed)
            xml = api.getXMLOutput()
        ) catch (
            if wasOff do base.autoUpdate = false
            throw()
        )
        if wasOff do base.autoUpdate = false
    )
    xml
)
-- The role of each base (baid order) from the style XML (live, RailClone 7.3): the connectors whose
-- target_id is the base id. A generator input names it (spline, spline_x, spline_y: path;
-- clipping_area: clip; base_surface: surface). A macro input "{macro}{input}" takes its InputMacro's
-- type (0 segment, 2 surface, 3 clipping area; 1 spline is followed inside the macro), and an
-- operator is followed to what uses it. batype is 0 for paths and clipping areas alike, and names
-- are no guide. railclone_lib.base_roles is the same walk in Python.
fn rcRoleOf found = (
    local hasPath = (findItem found "path") > 0
    local hasClip = (findItem found "clip") > 0
    if found.count == 0 then "unused"
    else if hasPath and hasClip then "mixed"
    else if hasPath then "path"
    else if hasClip then "clip"
    else if (findItem found "surface") > 0 then "surface"
    else if found.count == 1 and found[1] == "segment" then "segment"
    else "unknown"
)
fn rcBaseRoles base = (
    local ids = for v in (rcArr base #baid) collect (toLower (v as string))
    local roles = for v in ids collect "unknown"
    local api = getInterface base #railclone
    local doc = undefined
    if api != undefined and isProperty api #getXMLStyle do (
        doc = dotNetObject "System.Xml.XmlDocument"
        doc.XmlResolver = undefined
        try (doc.LoadXml (rcSanitize (api.getXMLStyle()))) catch (doc = undefined)
    )
    if doc != undefined do (
        local cIds = #(), cTargets = #(), cOwners = #(), cKinds = #()
        local conns = doc.GetElementsByTagName "connector"
        for i = 0 to conns.Count - 1 do (
            local c = conns.Item i
            local own = c.ParentNode
            -- A connector in a <groupConnectors> list (an operator's variable inputs, such as Basic Ops'
            -- Splines) is an input of the node around the list
            if own != undefined and own.Name == "groupConnectors" do own = own.ParentNode
            local t = toLower (c.GetAttribute "target_id")
            if t != "" and own != undefined and own.Name == "node" do (
                append cIds (toLower (c.GetAttribute "id"))
                append cTargets t
                append cOwners (toLower (own.GetAttribute "id"))
                append cKinds (own.GetAttribute "baseClass")
            )
        )
        local mIds = #(), mTypes = #()
        local nodes = doc.GetElementsByTagName "node"
        for i = 0 to nodes.Count - 1 do (
            local nd = nodes.Item i
            if (nd.GetAttribute "class") == "InputMacro" do (
                local ty = -1
                local kids = nd.ChildNodes
                for j = 0 to kids.Count - 1 do (
                    local ch = kids.Item j
                    if ch.Name == "parameter" and (ch.GetAttribute "id") == "type" do (
                        local tv = (ch.GetAttribute "value") as integer
                        ty = if tv == undefined then -1 else tv
                    )
                )
                append mIds (toLower (nd.GetAttribute "id"))
                append mTypes ty
            )
        )
        for b = 1 to ids.count do (
            local found = #()
            local queue = #(ids[b])
            local seen = #()
            local steps = 0
            while queue.count > 0 and steps < 500 do (
                steps += 1
                local tgt = queue[queue.count]
                deleteItem queue queue.count
                if (findItem seen tgt) == 0 do (
                    append seen tgt
                    for k = 1 to cTargets.count where cTargets[k] == tgt do (
                        local cid = cIds[k]
                        if cid == "spline" or cid == "spline_x" or cid == "spline_y" then appendIfUnique found "path"
                        else if cid == "clipping_area" then appendIfUnique found "clip"
                        else if cid == "base_surface" then appendIfUnique found "surface"
                        else (
                            if not (matchPattern cid pattern:"_exp_*") do (
                                local cut = findString cid "}{"
                                local m = 0
                                local inner = ""
                                if cut != undefined and cid[1] == "{" do (
                                    inner = substring cid (cut + 1) -1
                                    m = findItem mIds inner
                                )
                                if m > 0 then (
                                    local mt = mTypes[m]
                                    if mt == 3 then appendIfUnique found "clip"
                                    else if mt == 2 then appendIfUnique found "surface"
                                    else if mt == 0 then appendIfUnique found "segment"
                                    else append queue inner
                                )
                                else if cKinds[k] == "Operator" then append queue cOwners[k]
                                else appendIfUnique found "unknown"
                            )
                        )
                    )
                )
            )
            roles[b] = rcRoleOf found
        )
    )
    roles
)
fn rcOutStats xml = (
    local rx = dotNetClass "System.Text.RegularExpressions.Regex"
    local total = (rx.Matches xml "<item[ />]").Count
    local segs = rx.Matches xml " source_segment=\"([^\"]*)\""
    local keys = #()
    local counts = #()
    for k = 0 to segs.Count - 1 do (
        local key = (segs.Item k).Groups.Item[1].Value
        local j = findItem keys key
        if j == 0 then (append keys key; append counts 1) else counts[j] += 1
    )
    local parts = for j = 1 to keys.count collect ((rcEncode keys[j]) + ":" + (counts[j] as string))
    #((total as string), (segs.Count as string), (rcJoin parts ","))
)
'''.replace("@@DUP@@", _b64(rclib.DUP_ATTR_PATTERN))


def _target(name: str, handle: int) -> str:
    if not isinstance(name, str) or "\x00" in name:
        raise ValueError("name must be a string without NUL characters")
    integer(handle, "handle", low=0, high=2**63 - 1)
    # Keep Unicode, quotes and newlines out of MAXScript syntax.
    encoded = base64.b64encode(name.encode("utf-8")).decode("ascii")
    code = f'local wantedName = rcDecode "{encoded}"\nlocal obj = undefined\n'
    if handle:
        code += f'obj = getAnimByHandle {handle}\n'
        code += 'if not isValidNode obj do throw "NOT_FOUND: node handle is no longer valid"\n'
        if name:
            code += 'if obj.name != wantedName do throw "NODE_REF_MISMATCH: handle and name disagree"\n'
    elif name:
        code += 'local matches = mcpNamedNodes wantedName\n'
        code += 'if matches.count == 0 do throw "NOT_FOUND: node not found"\n'
        code += 'if matches.count != 1 do throw "AMBIGUOUS: use a handle for duplicate node names"\nobj = matches[1]\n'
    else:
        raise ValueError("Provide a RailClone node name or handle")
    return code


def _send(body: str) -> str:
    response = client.send_command('(' + _FUNCTIONS + _NODE_LOOKUP_FNS + '\ntry (\n' + body +
                                   '\n) catch ("RCERROR|" + (getCurrentException() as string))\n)')
    if response.get("error") or response.get("ok") is False:
        error = response.get("error") or {}
        if isinstance(error, dict):
            raise RailCloneError(error.get("code", "BRIDGE_ERROR"), error.get("message", str(error)))
        raise RailCloneError("BRIDGE_ERROR", str(error))
    raw = response.get("result")
    if isinstance(raw, str) and raw.startswith("RCERROR|"):
        message = raw.split("|", 1)[1]
        match = re.search(r"\b([A-Z][A-Z_]+):", message)
        raise RailCloneError(match[1] if match else "RAILCLONE_ERROR", message)
    if isinstance(raw, str) and raw.startswith(MS_ERROR):
        raise RailCloneError("MAXSCRIPT_ERROR", raw[len(MS_ERROR):].strip())
    if not isinstance(raw, str):
        raise RailCloneError("INVALID_READBACK", f"RailClone returned no readback: {str(raw)[:250]}")
    return raw


def _run(body: str) -> dict[str, Any]:
    raw = _send(body)
    if not raw.startswith("RCXML|"):
        raise RailCloneError("INVALID_READBACK", f"RailClone returned no XML readback: {raw[:250]}")
    try:
        _, handle, name, token, xml = raw.split("|", 4)
        return {"handle": int(handle), "name": base64.b64decode(name, validate=True).decode("utf-8"),
                "style_token": token, "xml": base64.b64decode(xml, validate=True).decode("utf-8")}
    except (ValueError, UnicodeError) as exc:
        raise RailCloneError("INVALID_READBACK", "Incomplete XML response; inspect before retrying") from exc


def _unb64(text: str) -> str:
    return base64.b64decode(text, validate=True).decode("utf-8")


def _dv(field: str) -> Any:
    """Decode one rcV field: u, b0/b1, i<int>, f<float>, s<b64>, n<handle>:<b64>, p<x,y,z>, m/o<b64>:<b64>."""
    tag, body = field[:1], field[1:]
    if tag == "u":
        return None
    if tag == "b":
        return body == "1"
    if tag == "i":
        return int(body)
    if tag == "f":
        try:
            value = float(body)
        except ValueError:  # MSVC prints NaN as e.g. "-nan(ind)"
            return None
        return value if value == value and abs(value) != float("inf") else None
    if tag == "s":
        return _unb64(body)
    if tag == "n":
        handle, name = body.split(":", 1)
        return {"name": _unb64(name), "handle": int(handle)}
    if tag == "p":
        return [float(v) for v in body.split(",")]
    if tag == "m":
        cls, name = body.split(":", 1)
        return {"class": _unb64(cls), "name": _unb64(name)}
    if tag == "o":
        cls, text = body.split(":", 1)
        return {"class": _unb64(cls), "text": _unb64(text)}
    raise ValueError(f"unknown field {field[:20]!r}")


def _records(raw: str, head: str) -> list[list[str]]:
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != head:
        raise RailCloneError("INVALID_READBACK", f"expected a {head} report, got: {raw[:200]}")
    try:
        end = lines.index("END")
    except ValueError:
        raise RailCloneError("INVALID_READBACK", f"incomplete {head} report; inspect before retrying") from None
    return [line.split("|") for line in lines[1:end] if line]


def _stats(row: list[str]) -> dict[str, Any]:
    """An O row: O|<label>|total|placed|b64name:n,b64name:n."""
    total, placed = int(row[2]), int(row[3])
    by_segment: dict[str, int] = {}
    for pair in filter(None, row[4].split(",") if len(row) > 4 else []):
        key, count = pair.rsplit(":", 1)
        by_segment[_unb64(key)] = by_segment.get(_unb64(key), 0) + int(count)
    return {"total": total, "placed": placed, "placeholders": total - placed,
            "by_segment": dict(sorted(by_segment.items()))}


def _style_result(result: dict[str, Any]) -> dict[str, Any]:
    root, removed, conflicts = _parse_xml(result["xml"], "scene")
    result.update(schema=dict(root.attrib), node_count=sum(1 for _ in root.iter("node")),
                  duplicate_attributes_removed=removed)
    if conflicts:
        result["warnings"] = [f"DUPLICATE_ATTRIBUTE_CONFLICT: {conflicts} repeated attributes had different "
                              "values; the first one was kept for parsing"]
    return result


@mcp.tool()
def get_railclone_style(name: str = "", handle: int = 0) -> dict[str, Any]:
    """Read complete RailClone style XML and a token for guarded replacement.

    Target by unique name or handle; both are cross-checked. XML is verbatim,
    including nested graphs, unknown fields and the repeated attributes RailClone
    7.x writes on some nodes (y="..." twice); parsing uses a de-duplicated copy
    (duplicate_attributes_removed). Pass style_token as set_railclone_style's
    expected_style. Exposed parameter values are not in the XML: use
    railclone_inspect and railclone_set_param for them. Style XML is graph data,
    not a portable geometry/material asset package.
    """
    result = _run(_target(name, handle) + f'''
local api = rcApi obj.baseObject #getXMLStyle
local xml = api.getXMLStyle()
if xml.count > {MAX_XML_CHARS} do throw "XML_TOO_LARGE: style exceeds the XML response limit"
rcResult obj xml (rcToken obj xml)
''')
    return _style_result(result)


@mcp.tool()
def set_railclone_style(
    xml: str,
    expected_style: str,
    name: str = "",
    handle: int = 0,
) -> dict[str, Any]:
    """Replace a RailClone graph using setXMLStyle and return actual XML readback.

    Read first; expected_style is the returned style_token, checked in Max before
    applying. The token changes after every set, even for identical XML. Requires
    well-formed <scene> XML; repeated attributes are reduced to their first value
    before sending. RailClone's Ok means XML acceptance, not graph/geometry
    correctness; verify with get_railclone_output. setXMLStyle itself is not
    undoable, so the edit is staged on a copy of the RailClone base and adopted in
    one undo step. Node identity, transform, material and modifier stack stay
    intact; base identity changes. Instanced bases and master/slave style links are
    refused. Source geometry is not created here. Maximum XML: 4 million chars.
    """
    _, removed, conflicts = _parse_xml(xml, "scene")
    if not isinstance(expected_style, str) or TOKEN.fullmatch(expected_style) is None:
        raise ValueError("expected_style must be the style_token from get_railclone_style")
    target = _target(name, handle)
    # RailClone accepted the de-duplicated XML live (7.1.1); a strict parser would not accept the raw one.
    sent = rclib.sanitize_xml(xml)[0]
    encoded = base64.b64encode(sent.encode("utf-8")).decode("ascii")
    result = _run(target + f'''
if theHold.Holding() or theHold.IsSuspended() or theHold.SuperLevel() != 0 do throw "USER_BUSY: an undo operation is active"
local base = obj.baseObject
local api = rcApi base #setXMLStyle
rcApi base #getXMLStyle
local before = api.getXMLStyle()
if rcToken obj before != "{expected_style}" do throw "STALE_STYLE: RailClone changed; read the style again"
for n in objects where n != obj do (
    if n.baseObject == base do throw "SHARED_BASE: make the RailClone base unique before replacing its style"
    if isProperty n.baseObject #stylelink do (
        if n.baseObject.stylelink == obj do throw "SHARED_STYLE: this RailClone is a style master"
    )
)
if isProperty base #stylelink do (
    if base.stylelink != undefined do throw "SHARED_STYLE: this RailClone uses a linked master style"
)
local staged = undefined
local readback = undefined
with undo off (
    staged = copy base
    local stagedApi = rcApi staged #setXMLStyle
    local inputXml = rcDecode "{encoded}"
    local status = stagedApi.setXMLStyle inputXml
    if status != "Ok" do throw ("STYLE_REJECTED: " + (status as string))
    readback = stagedApi.getXMLStyle()
)
if readback == undefined or readback.count == 0 do throw "INVALID_READBACK: RailClone returned an empty style"
if readback.count > {MAX_XML_CHARS} do throw "XML_TOO_LARGE: resulting style exceeds the XML response limit"
local doc = dotNetObject "System.Xml.XmlDocument"
doc.XmlResolver = undefined
-- RailClone 7.x repeats attributes (y twice); .NET rejects that, so check a de-duplicated copy.
doc.LoadXml (rcSanitize readback)
if doc.DocumentElement.Name != "scene" do throw "INVALID_READBACK: expected scene XML"
if theHold.Holding() or theHold.IsSuspended() do throw "USER_BUSY: an undo operation is active"
if obj.baseObject != base or rcToken obj (api.getXMLStyle()) != "{expected_style}" do throw "STALE_STYLE: RailClone changed during staging"
local ownsHold = false
local result = undefined
try (
    theHold.Begin(); ownsHold = true
    obj.baseObject = staged
    -- Recompile with the owning node's transform. A detached base evaluates
    -- spline references against identity and otherwise double-applies placement.
    -- Only the new base is changed; cancelling still restores the original base.
    local adoptedApi = rcApi obj.baseObject #setXMLStyle
    local adoptedXml = readback
    local adoptedStatus = adoptedApi.setXMLStyle adoptedXml
    if adoptedStatus != "Ok" do throw ("STYLE_REJECTED: " + (adoptedStatus as string))
    local actual = (rcApi obj.baseObject #getXMLStyle).getXMLStyle()
    if actual != readback do throw "READBACK_MISMATCH: adopted style differs from staged style"
    result = rcResult obj actual (rcToken obj actual)
    theHold.Accept "MCP RailClone XML style"; ownsHold = false
) catch (
    if ownsHold do theHold.Cancel()
    throw()
)
result
''')
    result = _style_result(result)
    result.update(status="Ok", undoable=True, base_replaced=True, sent_duplicates_removed=removed)
    if conflicts:
        result.setdefault("warnings", []).append(
            f"DUPLICATE_ATTRIBUTE_CONFLICT: the input repeated {conflicts} attributes with different values; "
            "the first value was sent")
    return result


@mcp.tool()
def get_railclone_output(
    name: str = "",
    handle: int = 0,
    offset: int = 0,
    limit: int = 100,
    include_xml: bool = False,
    rebuild: bool = True,
    reload_segments: bool = False,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Inspect RailClone getXMLOutput at the current frame, without rendering.

    rebuild (default) re-evaluates first with seed = seed, undo off: moving or
    copying a RailClone does not, so cached output goes stale. It doesn't touch the
    segments. A RailClone with autoUpdate off (manual mode) is left alone and its
    last build returned (AUTO_UPDATE_OFF), unless force_rebuild=true: that switches
    autoUpdate on for the read and back off, and keeps the new geometry (not
    undoable; a heavy RailClone blocks Max while it builds).
    reload_segments=true also runs segmentsUpdate: it renames linked segments
    after their sources, with no geometry effect on RailClone 7.3.5; use
    force_rebuild to pick up source edits made while autoUpdate was off. Items keep
    RailClone's attributes verbatim (index, source_segment, instanced, tm, box,
    tags) plus placeholder=true for empty slots (no source_segment, inverted
    sentinel box). Under Proxy Cache (proxymode != 0) no item has a source_segment:
    the box alone marks placeholders, by_segment is empty, and PROXY_CACHE says so.
    tm and box are in the RailClone object's local space; node_tm and bounds_world
    give world placement. total/placed/placeholders/by_segment/bounds cover every
    item, not only the page. offset is a zero-based row offset. include_xml adds
    the full XML. Each call re-evaluates; pages are not a snapshot. Max 4M chars.
    """
    integer(offset, "offset", low=0, high=2**31 - 1)
    integer(limit, "limit", high=10000)
    if type(include_xml) is not bool:
        raise ValueError("include_xml must be boolean")
    if type(rebuild) is not bool or type(reload_segments) is not bool or type(force_rebuild) is not bool:
        raise ValueError("rebuild, reload_segments and force_rebuild must be boolean")
    if (reload_segments or force_rebuild) and not rebuild:
        raise ValueError("reload_segments and force_rebuild rebuild the RailClone; they can't be combined with "
                         "rebuild=false")
    if rebuild:
        # autoUpdate off is the user's manual mode: rebuilding it replaces the geometry they froze
        read_code = (f"rebuilt = {_mxs_bool(force_rebuild)} or not autoOff\n"
                     f"xml = if rebuilt then (rcRebuildOutput base {_mxs_bool(reload_segments)}) "
                     "else (api.getXMLOutput())\n")
    else:
        read_code = "xml = api.getXMLOutput()\n"
    raw = _send(_target(name, handle) + f'''
local base = obj.baseObject
local api = rcApi base #getXMLOutput
local rebuilt = false
local autoOff = (isProperty base #autoUpdate) and base.autoUpdate == false
local xml = undefined
{read_code}if xml.count > {MAX_XML_CHARS} do throw "XML_TOO_LARGE: output exceeds the XML response limit"
local shared = for n in (rcShared obj) collect (rcEncode n.name)
local proxy = if isProperty base #proxymode then base.proxymode else 0
"RCOUT|" + (rcH obj) + "|" + (rcEncode obj.name) + "|" + (rcTm obj.objectTransform) + "|" + (rcJoin shared ",") + "|" + (if rebuilt then "1" else "0") + "|" + (if autoOff then "1" else "0") + "|" + (proxy as string) + "|" + (rcEncode xml)
''')
    parts = raw.split("|", 8)
    if len(parts) != 9 or parts[0] != "RCOUT":
        raise RailCloneError("INVALID_READBACK", f"RailClone returned no output readback: {raw[:250]}")
    try:
        xml = _unb64(parts[8])
        result: dict[str, Any] = {"handle": int(parts[1]), "name": _unb64(parts[2])}
        shared = [_unb64(s) for s in parts[4].split(",") if s]
        proxymode = int(parts[7])
    except (ValueError, UnicodeError) as exc:
        raise RailCloneError("INVALID_READBACK", "Incomplete output response; inspect before retrying") from exc
    auto_off = parts[6] == "1"
    rebuilt = parts[5] == "1"
    reloaded = reload_segments and rebuilt
    proxy = proxymode != 0
    node_rows = rclib.parse_matrix(parts[3])
    root = _xml(xml, "RailClone")
    items = [dict(item.attrib) for item in root.findall("item")]
    for item in items:
        item["placeholder"] = rclib.is_placeholder(item, proxy)
    summary = rclib.summarize_items(items, node_rows, proxy)
    end = min(offset + limit, len(items))
    warnings = []
    if shared:
        warnings.append(f"SHARED_BASE: this RailClone shares its base with {shared}; an instanced base anchors "
                        "its output to one node only, so positions may belong to another instance")
    if not rebuild:
        warnings.append("NOT_REBUILT: this is RailClone's last export; it can be stale after moves and copies, "
                        "and empty after a script calls getStyleDesc() (it clears the export until a rebuild)"
                        + ("; autoUpdate is off, so it is stale until a rebuild" if auto_off else ""))
    elif auto_off:
        warnings.append(_AUTO_UPDATE_NOTE if rebuilt else _AUTO_UPDATE_KEPT)
    if reloaded:
        warnings.append(_RELOAD_NOTE)
    elif reload_segments:
        warnings.append(_RELOAD_SKIPPED)
    if proxy:
        warnings.append(_proxy_note(proxymode))
    if summary["placed"] == 0:
        warnings.append("EMPTY_OUTPUT: no placed items (no spline assigned, or the style produces nothing)")
    result.update(
        schema=dict(root.attrib), total=len(items), offset=offset, items=items[offset:end],
        truncated=end < len(items), next_offset=end if end < len(items) else None,
        rebuilt=rebuilt, segments_reloaded=reloaded, auto_update=not auto_off, proxymode=proxymode,
        proxy_cache=proxy, space="object", node_tm=node_rows,
        placed=summary["placed"], placeholders=summary["placeholders"], by_segment=summary["by_segment"],
        bounds_object=summary["bounds_object"], bounds_world=summary["bounds_world"],
        shared_base_with=shared, warnings=warnings,
    )
    if include_xml:
        result["xml"] = xml
    return result


def _rows(tag: str, fields: tuple[str, ...]) -> str:
    # An explicit inner loop: in a nested `collect` inside the call's arguments, MAXScript
    # resolved the loop variable to the stringStream (live: No "<=" function for StringStream).
    arrays = "#(" + ", ".join("#" + f for f in fields) + ")"
    return ('(\n'
            f'    local fa = for f in {arrays} collect (rcArr base f)\n'
            '    for k = 1 to fa[1].count do (\n'
            f'        local row = #("{tag}", (k as string))\n'
            '        for a in fa do append row (rcV (rcAt a k))\n'
            '        rcRow ss row\n'
            '    )\n'
            ')\n')


def _inspect_body(*, graph: bool, output: bool, bases: bool, segments: bool, reload: bool = False,
                  rebuild: bool = True, force: bool = False) -> str:
    body = '''
local base = obj.baseObject
local api = getInterface base #railclone
if api == undefined do throw "NOT_RAILCLONE: target has no RailClone interface"
local ss = stringStream ""
format "RCINSPECT\\n" to:ss
rcRow ss #("K", "handle", rcV ((getHandleByAnim obj) as integer64))
rcRow ss #("K", "name", rcV obj.name)
rcRow ss #("K", "class", rcV ((classOf base) as string))
rcRow ss #("K", "base_handle", rcV (rcH base))
rcRow ss #("K", "upm", rcV (units.decodeValue "1mm"))
rcRow ss #("K", "units", rcV (units.SystemType as string))
rcRow ss #("K", "api_version", rcV (try (RailClone_Pro.version()) catch undefined))
rcRow ss #("K", "rc_runtime", rcV (rcRuntime()))
for p in #(#style, #savedversion, #createdversion, #proxymode, #disabled, #seed, #gscale, #vmesh, #rmesh, #rendermode, #autoUpdate, #nodemat, #stylelink, #maxseg, #maxfaces, #maxtime) do rcRow ss #("K", (p as string), rcV (if isProperty base p then (getProperty base p) else undefined))
rcRow ss #("K", "node_tm", rcV (rcTm obj.objectTransform))
rcRow ss #("K", "layer", rcV obj.layer.name)
local mtl = obj.material
rcRow ss #("K", "material", rcV mtl)
if mtl != undefined and isProperty mtl #materialIDList do (
    local ids = for id in mtl.materialIDList collect (id as string)
    rcRow ss #("K", "material_ids", rcV (rcJoin ids ","))
)
for n in (rcShared obj) do rcRow ss #("D", rcV n)
'''
    if bases:
        body += _rows("B", rclib.BASE_FIELDS)
    body += _rows("P", rclib.PARAM_FIELDS)
    if graph:
        body += f'''if isProperty api #getXMLStyle then (
    local sx = api.getXMLStyle()
    if sx.count > {MAX_XML_CHARS} then rcRow ss #("W", rcEncode "XML_TOO_LARGE: style XML over the limit; graph skipped") else rcRow ss #("X", rcEncode sx)
) else rcRow ss #("W", rcEncode "XML_API_UNAVAILABLE: no getXMLStyle; graph skipped")
'''
    if output:
        # A RailClone with autoUpdate off (manual mode) is rebuilt only on request (force): rebuilding replaces
        # the geometry the user froze. getStyleDesc() empties RailClone's output export until the next rebuild
        # (live), so it is read only when this call rebuilds, before the rebuild; the export is full again
        # afterwards. Without a rebuild (or without the output) it isn't read at all.
        body += (f'local rcBuild = {_mxs_bool(rebuild)} and ({_mxs_bool(force)} or not ((isProperty base '
                 '#autoUpdate) and base.autoUpdate == false))\n')
        body += 'rcRow ss #("K", "rebuilt", rcV rcBuild)\n'
        body += 'if rcBuild do rcRow ss #("K", "style_desc", rcV (try (api.getStyleDesc()) catch undefined))\n'
        body += f'''if isProperty api #getXMLOutput then (
    local ox = if rcBuild then (rcRebuildOutput base {_mxs_bool(reload)}) else (api.getXMLOutput())
    if ox.count > {MAX_XML_CHARS} then rcRow ss #("W", rcEncode "XML_TOO_LARGE: output XML over the limit; output skipped") else rcRow ss #("O", rcEncode ox)
) else rcRow ss #("W", rcEncode "XML_API_UNAVAILABLE: no getXMLOutput; output skipped")
'''
    if segments:
        # After the rebuild: segmentsUpdate (reload) renames linked segments, and the output's source_segment
        # with them (live), so the names read here match the output's
        body += _rows("S", rclib.SEGMENT_FIELDS)
    return body + 'format "END\\n" to:ss\nss as string\n'


def _parse_inspect(raw: str) -> dict[str, Any]:
    out: dict[str, Any] = {"info": {}, "shared": [], "bases": [], "params": [], "segments": [],
                           "style": None, "output": None, "warnings": []}
    fields = {"B": rclib.BASE_FIELDS, "P": rclib.PARAM_FIELDS, "S": rclib.SEGMENT_FIELDS}
    lists = {"B": "bases", "P": "params", "S": "segments"}
    try:
        for row in _records(raw, "RCINSPECT"):
            tag = row[0]
            if tag == "K":
                out["info"][row[1]] = _dv(row[2])
            elif tag == "D":
                out["shared"].append(_dv(row[1]))
            elif tag in fields:
                values = [_dv(f) for f in row[2:]]
                if len(values) != len(fields[tag]):
                    raise ValueError(f"{tag} row has {len(values)} fields")
                # Ids are strings ({guid}) or small ints; keep MAXScript's "as string" text otherwise.
                if isinstance(values[0], dict) and "text" in values[0]:
                    values[0] = values[0]["text"]
                out[lists[tag]].append(dict(zip(fields[tag], values)))
            elif tag == "X":
                out["style"] = _unb64(row[1])
            elif tag == "O":
                out["output"] = _unb64(row[1])
            elif tag == "W":
                out["warnings"].append(_unb64(row[1]))
    except (ValueError, IndexError, UnicodeError) as exc:
        raise RailCloneError("INVALID_READBACK", f"malformed RailClone report: {exc}") from exc
    return out


def _node_ref(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) and "handle" in value else None


def _version_int(value: Any) -> int | None:
    """A version number read from Max (735), or None when RailClone didn't give one."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


@mcp.tool()
def railclone_inspect(name: str = "", handle: int = 0, include_graph: bool = True,
                      reload_segments: bool = False, rebuild: bool = True,
                      force_rebuild: bool = False) -> dict[str, Any]:
    """Read one RailClone: settings, bases, exposed parameters, segments, graph summary and output counts.

    One call. Parameters report the value from the array their type
    uses (0 int, 1 float, 2 percent, 3 distance in scene units plus value_mm,
    4 bool, 5 selector with options, 6 string (read only), 7 separator), their
    limits (RailClone does not enforce them) and, with include_graph, which graph
    inputs use them and each base's role (path, clip, surface) from the graph.
    The output counts come after a rebuild with seed = seed (undo off), which
    doesn't touch the segments. reload_segments=true runs segmentsUpdate instead:
    it renames linked segments after their sources, with no geometry effect on
    RailClone 7.3.5; use force_rebuild to pick up source edits made while
    autoUpdate was off. rebuild=false reads the last export (no style_desc then:
    reading it empties the export until a rebuild). A RailClone with autoUpdate
    off (manual mode) is never rebuilt unless force_rebuild=true, which switches
    autoUpdate on for the read and back off and keeps the new geometry (not
    undoable; a heavy RailClone blocks Max while it builds). Use
    get_railclone_output for positions.

    node gives rc_version, the running RailClone ("7.3.5", and rc_version_int
    735), api_version (RailClone_Pro.version(), 400 on 7.1.1 and 7.3.5 alike: an
    API number, not the product version), the object's createdversion and
    savedversion, and proxymode. Warnings: SELECTOR_INDEX_MISMATCH (a selector's
    option index and value disagree; the engine builds with the value),
    RC_SELECTOR_BUG_VERSION (RailClone older than 7.3.1 and selectors in the
    style), STYLE_MAY_PREDATE_MAPPING_FIX (a library style on an object created or
    saved before 7.1.3) and PROXY_CACHE (Proxy Cache on: the output has no
    source_segment, so counts per segment are unknown).
    """
    for label, value in (("include_graph", include_graph), ("reload_segments", reload_segments),
                         ("rebuild", rebuild), ("force_rebuild", force_rebuild)):
        if type(value) is not bool:
            raise ValueError(f"{label} must be boolean")
    if (reload_segments or force_rebuild) and not rebuild:
        raise ValueError("reload_segments and force_rebuild rebuild the RailClone; they can't be combined with "
                         "rebuild=false")
    raw = _send(_target(name, handle) + _inspect_body(graph=include_graph, output=True, bases=True,
                                                       segments=True, reload=reload_segments, rebuild=rebuild,
                                                       force=force_rebuild))
    data = _parse_inspect(raw)
    info, warnings = data["info"], list(data["warnings"])
    upm = info.get("upm") if isinstance(info.get("upm"), float) and info.get("upm") > 0 else None
    material = info.get("material")
    if isinstance(material, dict) and info.get("material_ids"):
        material = dict(material, ids=[int(v) for v in info["material_ids"].split(",") if v])
    runtime = _version_int(info.get("rc_runtime"))
    proxymode = info.get("proxymode")
    proxy = isinstance(proxymode, int) and proxymode != 0
    node = {
        "class": info.get("class"),
        "style": info.get("style") or "",
        "style_desc": info.get("style_desc"),
        "rc_version": rclib.version_text(runtime),
        "rc_version_int": runtime,
        "api_version": info.get("api_version"),
        "createdversion": info.get("createdversion"),
        "savedversion": info.get("savedversion"),
        "proxymode": proxymode,
        "material": material,
        "layer": info.get("layer"),
        "units": info.get("units"),
        "mm_per_unit": round(1.0 / upm, 9) if upm else None,
        "node_tm": rclib.parse_matrix(info.get("node_tm")),
        "settings": {k: info.get(k) for k in ("disabled", "seed", "gscale", "vmesh", "rmesh", "rendermode",
                                             "autoUpdate", "nodemat")},
        "display_mode": info.get("vmesh"),
        "render_mode": info.get("rmesh"),
        "engine_limits": {k: info.get(k) for k in ("maxseg", "maxfaces", "maxtime")},
        "stylelink": _node_ref(info.get("stylelink")),
        "shared_base_with": [s["name"] for s in data["shared"] if isinstance(s, dict)],
    }
    bases = []
    for i, b in enumerate(data["bases"], 1):
        bases.append({"index": i, "id": b["baid"], "name": b["baname"], "type": b["batype"],
                      "type_name": {0: "spline", 1: "surface"}.get(b["batype"], "unknown"), "role": None,
                      "node": _node_ref(b["banode"]), "start": b["bastart"], "length": b["balength"],
                      "full": b["bafull"], "description": b["badesc"] or None})
    params = [rclib.param_view(p, i, upm) for i, p in enumerate(data["params"], 1)]
    graph = None
    if data["style"] is not None:
        try:
            root, removed, conflicts = _parse_xml(data["style"], "scene")
            graph = rclib.graph_summary(root, [p["paid"] for p in data["params"]])
            graph["duplicate_attributes_removed"] = removed
            for base, role in zip(bases, rclib.base_roles(root, [b["id"] for b in bases])):
                base["role"] = role
            if conflicts:
                warnings.append(f"DUPLICATE_ATTRIBUTE_CONFLICT: {conflicts} repeated style attributes differed")
            for exposed in graph["exposed"]:
                if exposed["param_index"]:
                    params[exposed["param_index"] - 1].setdefault("used_by", []).append(
                        {"node": exposed["node"], "node_type": exposed["node_type"], "input": exposed["input"]})
        except ValueError as exc:
            warnings.append(f"GRAPH_UNREADABLE: {exc}")
    output = None
    rebuilt = info.get("rebuilt") is True
    if data["output"] is not None:
        try:
            items = [dict(it.attrib) for it in _xml(data["output"], "RailClone").findall("item")]
            summary = rclib.summarize_items(items, proxy=proxy)
            output = {k: summary[k] for k in ("total", "placed", "placeholders", "by_segment")}
            output["rebuilt"] = rebuilt
            output["segments_reloaded"] = reload_segments and rebuilt
            output["proxy_cache"] = proxy
        except ValueError as exc:
            warnings.append(f"OUTPUT_UNREADABLE: {exc}")
    if data["output"] is not None:
        if not rebuild:
            warnings.append("NOT_REBUILT: the output counts are RailClone's last export; they can be stale after "
                            "moves and copies, and empty after a script called getStyleDesc(). style_desc is left "
                            "out: reading it empties the export until a rebuild")
        elif info.get("autoUpdate") is False:
            warnings.append(_AUTO_UPDATE_NOTE if rebuilt else
                            _AUTO_UPDATE_KEPT + "; style_desc is left out: reading it empties the export until a "
                            "rebuild")
    if reload_segments:
        warnings.append(_RELOAD_NOTE if rebuilt else _RELOAD_SKIPPED)
    segments = []
    for i, s in enumerate(data["segments"], 1):
        ref, mtl = s["sobjref"], s["sobjmtl"]
        segments.append({
            "index": i, "id": s["sid"], "name": s["sname"],
            "source_node": _node_ref(s["sobjnode"]), "embedded": _node_ref(s["sobjnode"]) is None,
            "source_class": ref.get("class") if isinstance(ref, dict) else None,
            "source_material": mtl.get("name") if isinstance(mtl, dict) else None,
            "instance": s["sinstance"], "slice": s["sslice"], "bend": s["sbend"], "renderable": s["srenderable"],
            # under Proxy Cache the output names no segment, so a per-segment count is unknown, not 0
            "items": output["by_segment"].get(s["sname"], 0) if output and not proxy else None,
        })
    empty = [b["name"] for b in bases if b["type"] == 0 and b["node"] is None]
    if empty and len(empty) == sum(1 for b in bases if b["type"] == 0):
        warnings.append(f"NO_PATH: no spline in the spline base(s) {empty}; assign one (banode) for output")
    if node["shared_base_with"]:
        warnings.append(f"SHARED_BASE: the base is instanced with {node['shared_base_with']}")
    for p in params:
        if p.get("out_of_limits"):
            warnings.append(f"PARAM_OUT_OF_LIMITS: {p['name']!r} = {p.get('value')} is outside its limits")
    for raw_param, p in zip(data["params"], params):
        why = rclib.selector_mismatch(raw_param)
        if why:
            warnings.append(f"SELECTOR_INDEX_MISMATCH: {p['name']!r}: {why}. RailClone builds with the value, so "
                            "the panel shows another option than the one built; railclone_set_param with an "
                            "option label writes both")
    warnings += [w for w in (
        rclib.selector_bug_warning(runtime, sum(1 for p in params if p["type"] == 5)),
        rclib.mapping_predate_warning(node["style"], node["createdversion"], node["savedversion"])) if w]
    if proxy:
        warnings.append(_proxy_note(proxymode))
    if output is not None and output["placed"] == 0:
        warnings.append("EMPTY_OUTPUT: the RailClone places no items")
    return {"handle": info.get("handle"), "name": info.get("name"), "node": node, "bases": bases,
            "params": params, "segments": segments, "graph": graph, "output": output, "warnings": warnings}


def _write_params_body(base_handle: str, plan: list[dict[str, Any]]) -> str:
    checks, before, writes, after = [], [], [], []
    for n, entry in enumerate(plan, 1):
        i = entry["index"]
        id_check = (f'((rcAt pid {i}) as string) != (rcDecode "{_b64(str(entry["id"]))}") or '
                    if entry["id"] is not None else "")
        checks.append(f'if {id_check}(rcAt ptype {i}) != {int(entry["type"])} do throw '
                      f'"STALE_PARAMS: parameter {i} changed since it was read; read again"')
        for array, literal in entry["writes"]:
            if array not in rclib.WRITABLE_ARRAYS:
                raise ValueError(f"internal: refusing to write {array}")
            before.append(f'rcRow ss #("V", "before", "{n}", "{array}", rcV (rcAt (rcArr base #{array}) {i}))')
            writes.append(f'    base.{array}[{i}] = {literal}')
            after.append(f'rcRow ss #("V", "after", "{n}", "{array}", rcV (rcAt (rcArr base #{array}) {i}))')
    return f'''
local base = obj.baseObject
local api = rcApi base #getXMLOutput
if (rcH base) != "{base_handle}" do throw "STALE_PARAMS: the RailClone base changed since it was read; read again"
if theHold.Holding() or theHold.IsSuspended() or theHold.SuperLevel() != 0 do throw "USER_BUSY: an undo operation is active"
local pid = rcArr base #paid
local ptype = rcArr base #patype
{chr(10).join(checks)}
local ss = stringStream ""
format "RCSETP\\n" to:ss
rcRow ss (join #("O", "before") (rcOutStats (rcRebuildOutput base false)))
{chr(10).join(before)}
local ownsHold = false
try (
    theHold.Begin()
    ownsHold = true
{chr(10).join(writes)}
    theHold.Accept "{UNDO_PARAMS}"
    ownsHold = false
) catch (
    if ownsHold do theHold.Cancel()
    throw()
)
local outAfter = rcRebuildOutput base false
{chr(10).join(after)}
rcRow ss (join #("O", "after") (rcOutStats outAfter))
format "END\\n" to:ss
ss as string
'''


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)))
    return a == b


@mcp.tool()
def railclone_set_param(params: dict[str, Any], name: str = "", handle: int = 0) -> dict[str, Any]:
    """Set RailClone exposed parameters by exact name or id, in one undo step, and report the output change.

    params maps an exact parameter name (or its id) to a value: int, float,
    percent, distance (scene units, or a string such as "60cm"), bool, or a
    selector option label/value. Values outside the parameter limits are refused,
    since RailClone does not clamp them; separators and string parameters (type 6,
    never seen in a stock style) are refused. The RailClone is rebuilt with
    seed = seed; before/after values and output item counts are returned.
    """
    if not isinstance(params, dict) or not params:
        raise ValueError("params must be a non-empty object of parameter name or id -> value")
    if len(params) > 100:
        raise ValueError("at most 100 parameters per call")
    target = _target(name, handle)
    try:
        raw = _send(target + _inspect_body(graph=False, output=False, bases=False, segments=False))
    except DialogBlocked as exc:  # only the read-only parameter read is queued; nothing was set
        raise PrecheckBlocked(exc, "railclone_set_param", "No parameter was set") from exc
    data = _parse_inspect(raw)
    info = data["info"]
    upm = info.get("upm") if isinstance(info.get("upm"), float) and info.get("upm") > 0 else None
    plan = rclib.plan_param_writes(data["params"], params, upm)
    node_handle = info.get("handle")
    if not isinstance(node_handle, int) or not info.get("base_handle"):
        raise RailCloneError("INVALID_READBACK", "RailClone report has no handles")
    raw = _send(_target("", node_handle) + _write_params_body(str(info["base_handle"]), plan))
    values: dict[tuple[str, int, str], Any] = {}
    stats: dict[str, dict[str, Any]] = {}
    try:
        for row in _records(raw, "RCSETP"):
            if row[0] == "V":
                values[(row[1], int(row[2]), row[3])] = _dv(row[4])
            elif row[0] == "O":
                stats[row[1]] = _stats(row)
    except (ValueError, IndexError, UnicodeError) as exc:
        raise RailCloneError("INVALID_READBACK", f"malformed parameter report: {exc}") from exc
    changes, warnings = [], []
    for n, entry in enumerate(plan, 1):
        change = {k: entry[k] for k in ("key", "index", "id", "name", "type", "type_name", "array", "requested",
                                        "value")}
        for extra in ("label", "option_index", "value_mm"):
            if extra in entry:
                change[extra] = entry[extra]
        change["before"] = values.get(("before", n, entry["array"]))
        change["after"] = values.get(("after", n, entry["array"]))
        if entry["type"] == 5:
            change["ui_index_before"] = values.get(("before", n, "paintval"))
            change["ui_index_after"] = values.get(("after", n, "paintval"))
        expected = float(entry["value"]) if entry["type"] == 5 else entry["value"]
        if not _same(change["after"], expected):
            warnings.append(f"READBACK_DIFFERS: {entry['key']!r} reads {change['after']!r} after writing "
                            f"{expected!r}")
        changes.append(change)
    before, after = stats.get("before"), stats.get("after")
    proxymode = info.get("proxymode")
    proxy = isinstance(proxymode, int) and proxymode != 0
    if proxy:
        # Under Proxy Cache no output item names its segment (live), so the counts by segment are unknown
        for counts in (before, after):
            if counts:
                counts.update(placed=None, placeholders=None)
        # Live (RailClone 7.3.5): "Pickets Distance" 10 -> 20 cm left a Proxy Cache fence at 176 items and 2098
        # faces; without Proxy Cache the pickets halve
        warnings.append(_proxy_note(proxymode, by_box=False) + ". The output is the cached build: the parameter "
                        "is written, but the geometry can stay as cached until the cache is rebuilt or Proxy Cache "
                        "is off")
    delta = {}
    if before and after and not proxy:
        for seg in sorted(set(before["by_segment"]) | set(after["by_segment"])):
            diff = after["by_segment"].get(seg, 0) - before["by_segment"].get(seg, 0)
            if diff:
                delta[seg] = diff
        if after["placed"] == 0:
            warnings.append("EMPTY_OUTPUT: the RailClone places no items after the change")
    return {"handle": node_handle, "name": info.get("name"), "changes": changes,
            "output": {"before": before, "after": after, "by_segment_delta": delta, "proxy_cache": proxy},
            "rebuilt": True, "undoable": True, "undo": UNDO_PARAMS, "warnings": warnings}


@mcp.tool()
def railclone_library(query: str = "", category: str = "", root: str = "", limit: int = 50) -> dict[str, Any]:
    """Search the RailClone library browser (registry roots and their index.xml files); no 3ds Max call.

    query matches all words against name, category, family, description and
    object; category is a substring of the category path; root names one library
    root. Each item has the route railclone_from_library loads, its .max/.rail
    file, object, material, matlib, uuid and thumbnail. Unreachable roots are
    skipped with a warning. The roots come from this server's registry view;
    railclone_from_library checks them against 3ds Max's own roots.
    """
    for label, value in (("query", query), ("category", category), ("root", root)):
        if not isinstance(value, str) or "\x00" in value or len(value) > 500:
            raise ValueError(f"{label} must be a string of at most 500 characters")
    integer(limit, "limit", high=500)
    roots, warnings = rclib.library_roots()
    if root:
        chosen = [r for r in roots if r["name"].lower() == root.strip().lower()]
        if not chosen:
            raise ValueError(f"unknown library root {root!r}; registered roots: {[r['name'] for r in roots]}")
        roots = chosen
    scan = rclib.scan_library(roots, deadline=time.monotonic() + rclib.DEFAULT_SCAN_SECONDS)
    matches = rclib.match_items(scan["items"], query, category)
    return {"roots": scan["roots"], "total": len(matches),
            "items": [rclib.public_item(it) for it in matches[:limit]],
            "truncated": len(matches) > limit, "complete": scan["complete"],
            "warnings": warnings + scan["warnings"]}


# Read only: the library roots as 3ds Max's own registry lists them. The server can read a stale private
# copy of HKCU (Claude Desktop's package keeps one), and a route RailClone doesn't know opens a modal dialog.
_ROOTS_BODY = r'''
local keyPath = rcDecode "@@KEY@@"
local ss = stringStream ""
format "RCROOTS\n" to:ss
local rk = (dotNetClass "Microsoft.Win32.Registry").CurrentUser.OpenSubKey keyPath
if rk == undefined then rcRow ss #("X", "missing") else (
    local valueNames = rk.GetValueNames()
    for nm in valueNames do (
        local v = rk.GetValue nm
        if isKindOf v String do rcRow ss #("R", rcEncode nm, rcEncode v)
    )
    rk.Close()
)
format "END\n" to:ss
ss as string
'''


def _max_library_roots() -> tuple[list[dict[str, str]] | None, str | None]:
    """The roots in 3ds Max's registry (one read-only call), or None and why they couldn't be read."""
    try:
        raw = _send(_ROOTS_BODY.replace("@@KEY@@", _b64(rclib.LIBRARY_KEY)))
    except DialogBlocked as exc:
        raise PrecheckBlocked(exc, "railclone_from_library", "Nothing was loaded") from exc
    except RailCloneError as exc:
        return None, f"{exc.code}: {exc}"
    roots = []
    try:
        for row in _records(raw, "RCROOTS"):
            if row[0] == "R":
                name, data = _unb64(row[1]), _unb64(row[2])
                if name and data.strip():
                    roots.append({"name": name, "path": os.path.expandvars(data.strip())})
    except (RailCloneError, ValueError, IndexError, UnicodeError) as exc:
        return None, f"unreadable roots report: {exc}"
    return roots, None


def _from_library_roots() -> tuple[list[dict[str, str]], list[str]]:
    """The roots a load resolves against: 3ds Max's own, or the server's view when Max's can't be read."""
    server_roots, server_warnings = rclib.library_roots()
    max_roots, why = _max_library_roots()
    if max_roots is None:
        return server_roots, server_warnings + [
            f"ROOTS_NOT_CHECKED: 3ds Max's library roots could not be read ({why}); the item was checked against "
            "this server's registry view only"]
    if not max_roots:
        raise RailCloneError("NO_LIBRARY_ROOTS", "3ds Max's registry lists no RailClone library roots "
                             f"(HKCU\\{rclib.LIBRARY_KEY}); open RailClone's library browser in 3ds Max once")
    differences = rclib.root_differences(server_roots, max_roots)
    if not differences:
        return max_roots, []
    return max_roots, ["ROOTS_DIFFER: this server's registry view differs from 3ds Max's (it can be a stale "
                       "private copy); this call used 3ds Max's roots, and railclone_library may list stale "
                       "routes: " + "; ".join(differences)]


_FROM_LIBRARY_HEAD = r'''
local methodName = "@@METHOD@@"
local maxFile = rcDecode "@@MAXFILE@@"
local objName = rcDecode "@@OBJECT@@"
local route = rcDecode "@@ROUTE@@"
local wantName = rcDecode "@@NAME@@"
local defaultName = rcDecode "@@DEFAULT@@"
local matTpl = rcDecode "@@MATLIB@@"
local matName = rcDecode "@@MATERIAL@@"
local mapsDir = rcDecode "@@MAPS@@"
local splineNames = #(@@SPLINES@@)
local clipNames = #(@@CLIPS@@)
if not (isKindOf RailClone_Pro MAXClass) do throw "NOT_INSTALLED: RailClone Pro is not loaded in this 3ds Max"
if theHold.Holding() or theHold.IsSuspended() or theHold.SuperLevel() != 0 do throw "USER_BUSY: an undo operation is active"
if wantName != "" and @@WANT_IN_USE@@ do throw ("NAME_IN_USE: a node is already named " + wantName)
local shapes = #()
for nm in (splineNames + clipNames) do (
    -- Exact-case match first, else one differing only in case (mcpNamedNodes).
    local found = mcpNamedNodes nm
    if found.count == 0 do throw ("SPLINE_NOT_FOUND: no node is named " + nm)
    if found.count > 1 do (
        local handles = ""
        for n in found do handles += (if handles == "" then "" else ", ") + (mcpHandleText n)
        throw ("AMBIGUOUS: " + (found.count as string) + " nodes are named " + nm + " (handles " + handles + "); rename them first")
    )
    if (superClassOf found[1]) != Shape do throw ("NOT_A_SPLINE: " + nm + " is not a shape")
    append shapes found[1]
)
local splines = for k = 1 to splineNames.count collect shapes[k]
local clipSplines = for k = 1 to clipNames.count collect shapes[splineNames.count + k]
local baseRoles = #()
local warnings = #()
local renderer = try (RC_getRenderer() as string) catch ""
local matPath = ""
if matTpl != "" do (
    if renderer == "" and ((findString matTpl "$(") != undefined) then append warnings "NO_MATERIAL: RC_getRenderer() returned no renderer for the material library path"
    else (
        matPath = substituteString (substituteString matTpl "$(SELECTEDRENDERER)" renderer) "$(RENDERER)" renderer
        if not (doesFileExist matPath) do (
            append warnings ("NO_MATERIAL: material library not found: " + matPath)
            matPath = ""
        )
    )
)
if methodName == "merge" do (
    if not (doesFileExist maxFile) do throw ("FILE_MISSING: " + maxFile)
    if (findItem (getMAXFileObjectNames maxFile quiet:true) objName) == 0 do throw ("OBJECT_MISSING: " + objName + " is not in " + maxFile)
)
local prevSel = for n in selection collect n
local o = undefined
local created = #()
local loaded = false
local fixed = 0
local missing = #()
'''.replace("@@WANT_IN_USE@@", name_in_use("wantName"))

# Merge: only for .max items, so only inside the undo-off wrapper.
_LOAD_MERGE = r'''
loaded = true
-- #useSceneMtlDups: a merged material never replaces a scene material of the same name.
local ok = mergeMAXFile maxFile #(objName) #autoRenameDups #useSceneMtlDups #neverReparent quiet:true
created = for n in objects where not (HasDictValue seen (rcH n)) collect n
local rcs = for n in created where (classOf n.baseObject) == RailClone_Pro collect n
if ok != true do throw ("MERGE_FAILED: mergeMAXFile returned " + (ok as string))
if rcs.count != 1 do throw ("MERGE_FAILED: expected one new RailClone, found " + (rcs.count as string))
o = rcs[1]
if wantName != "" do o.name = wantName
if matPath != "" do (
    local lib = loadTempMaterialLibrary matPath
    local libMtl = undefined
    if lib != undefined do (for i = 1 to lib.count while libMtl == undefined do (if lib[i].name == matName do libMtl = lib[i]))
    if libMtl != undefined then o.material = libMtl else append warnings ("NO_MATERIAL: " + matName + " is not in " + matPath)
)
'''

_LOAD_ROUTE = r'''
o = RailClone_Pro()
o.name = (if wantName != "" then wantName else (if not @@DEFAULT_IN_USE@@ then defaultName else uniqueName (defaultName + "_")))
append created o
-- loadLibraryItemByPath loads into the first SELECTED RailClone: select only this one.
select o
-- A load that fails shows a modal dialog; never call it unless o is the only selected node.
if not (selection.count == 1 and selection[1] == o) do throw "SELECT_FAILED: the new RailClone could not be selected (hidden or frozen current layer?)"
local api = rcApi o.baseObject #loadLibraryItemByPath
local routeArg = route
loaded = true
local code = api.loadLibraryItemByPath routeArg
rcRestoreSel prevSel
if code != 0 do throw ("LOAD_FAILED: loadLibraryItemByPath returned " + (code as string) + " for " + route)
'''.replace("@@DEFAULT_IN_USE@@", name_in_use("defaultName"))

_SETUP = r'''
local base = o.baseObject
local types = rcArr base #batype
local bnames = rcArr base #baname
-- Splines go by the role each base has in the style graph, never by order: batype is 0 for a path and
-- a clipping area alike (live), so the second spline of a path + clipping item became its clipping area.
baseRoles = rcBaseRoles base
local pathIdx = for i = 1 to types.count where types[i] == 0 and (rcAt baseRoles i) == "path" collect i
local clipIdx = for i = 1 to types.count where types[i] == 0 and (rcAt baseRoles i) == "clip" collect i
local otherIdx = for i = 1 to types.count where types[i] == 0 and (findItem #("path", "clip", "unused") (rcAt baseRoles i)) == 0 collect i
if splines.count > pathIdx.count or clipSplines.count > clipIdx.count do (
    local pathNames = for i in pathIdx collect ((rcAt bnames i) as string)
    local clipBaseNames = for i in clipIdx collect ((rcAt bnames i) as string)
    local otherNames = for i in otherIdx collect (((rcAt bnames i) as string) + " (" + (rcAt baseRoles i) + ")")
    local why = "path bases [" + (rcJoin pathNames ", ") + "], clipping-area bases [" + (rcJoin clipBaseNames ", ") + "], bases of unknown role [" + (rcJoin otherNames ", ") + "]"
    if splines.count > pathIdx.count and otherIdx.count > 0 do throw ("ROLE_UNKNOWN: the item's style graph doesn't show which spline bases are paths; nothing is assigned to a base of unknown role. It has " + why)
    if splines.count > pathIdx.count do throw ("TOO_MANY_SPLINES: " + (splines.count as string) + " spline(s) for " + (pathIdx.count as string) + " path base(s); a clipping area takes clipping_splines. The item has " + why)
    throw ("TOO_MANY_CLIPPING_SPLINES: " + (clipSplines.count as string) + " clipping spline(s) for " + (clipIdx.count as string) + " clipping-area base(s). The item has " + why)
)
for k = 1 to splines.count do base.banode[pathIdx[k]] = splines[k]
for k = 1 to clipSplines.count do base.banode[clipIdx[k]] = clipSplines[k]
if splines.count > 0 then o.pos = splines[1].pos else (if clipSplines.count > 0 do o.pos = clipSplines[1].pos)
if o.material != undefined do (
    local maps = for b in (getClassInstances BitmapTexture target:o.material) collect #(b, #filename)
    if isKindOf VRayBitmap MAXClass do (for b in (getClassInstances VRayBitmap target:o.material) do append maps #(b, #HDRIMapName))
    for pair in maps do (
        local f = try (getProperty pair[1] pair[2]) catch undefined
        if isKindOf f String and f != "" and not (doesFileExist f) do (
            local short = filenameFromPath f
            if (mapPaths.getFullFilePath short) == "" do (
                local cand = if mapsDir != "" then (mapsDir + short) else ""
                if cand != "" and (doesFileExist cand) then (setProperty pair[1] pair[2] cand; fixed += 1) else appendIfUnique missing f
            )
        )
    )
)
'''

# Any item but a .rail route load (live: a .max item clears the undo history, by merge or by route).
_UNDO_OFF = r'''
-- Live facts: loading a .max library item clears 3ds Max's whole undo history, by merge or by
-- route; loadTempMaterialLibrary clears it too, and mergeMAXFile is never recorded in undo. So the
-- whole load runs with undo off, with no copy and nothing deleted; the result lists the new nodes.
local seen = Dictionary #string
for n in objects do PutDictValue seen (rcH n) true
with undo off (
    try (
@@LOAD@@
@@SETUP@@
    ) catch (
        -- Nothing above was recorded, so the new nodes are deleted with undo off. Once the load has
        -- run, the history is cleared again: no record the load made can outlive the deleted nodes.
        local left = for n in objects where not (HasDictValue seen (rcH n)) collect n
        for n in left where isValidNode n do (try (delete n) catch ())
        if loaded do (try (clearUndoBuffer()) catch ())
        try (rcRestoreSel prevSel) catch ()
        throw()
    )
)
created = for n in objects where not (HasDictValue seen (rcH n)) collect n
'''

# A .rail route load: it keeps the undo history and is recorded (live), so it is one undo step.
_UNDO_STEP = r'''
-- Nothing in or after this undo step is deleted with undo off, so no undo record can refer to a
-- node that is gone (a later undo would crash 3ds Max).
local ownsHold = false
try (
    theHold.Begin()
    ownsHold = true
@@LOAD@@
@@SETUP@@
    theHold.Accept "@@UNDO@@"
    ownsHold = false
) catch (
    if ownsHold do (try (theHold.Cancel()) catch ())
    -- The cancel removes the new node. Anything left is deleted in an undo step of its own.
    local left = for n in created where isValidNode n collect n
    if left.count > 0 do undo "@@CLEANUP@@" on (for n in left do (try (delete n) catch ()))
    try (rcRestoreSel prevSel) catch ()
    throw()
)
'''

_FROM_LIBRARY_TAIL = r'''
local extras = for n in created where isValidNode n and n != o collect n
local rcBase = o.baseObject
-- getStyleDesc() empties the output export until the next rebuild (live), so it is read first.
local styleDesc = try ((getInterface rcBase #railclone).getStyleDesc()) catch undefined
-- The node is committed now; report a failed rebuild/readback as a warning, not an error.
local stats = try (
    rcOutStats (rcRebuildOutput rcBase false)
) catch (
    append warnings ("REBUILD_FAILED: " + (getCurrentException() as string))
    #("0", "0", "")
)
local ss = stringStream ""
format "RCLIB\n" to:ss
rcRow ss #("K", "handle", rcV ((getHandleByAnim o) as integer64))
rcRow ss #("K", "name", rcV o.name)
rcRow ss #("K", "style", rcV (if isProperty rcBase #style then rcBase.style else undefined))
rcRow ss #("K", "style_desc", rcV styleDesc)
rcRow ss #("K", "material", rcV o.material)
rcRow ss #("K", "renderer", rcV renderer)
rcRow ss #("K", "matlib", rcV matPath)
rcRow ss #("K", "fixed_maps", rcV fixed)
rcRow ss #("K", "api_version", rcV (try (RailClone_Pro.version()) catch undefined))
rcRow ss #("K", "rc_runtime", rcV (rcRuntime()))
for p in #(#createdversion, #savedversion, #proxymode) do rcRow ss #("K", (p as string), rcV (if isProperty rcBase p then (getProperty rcBase p) else undefined))
(
    local nSel = 0
    for t in (rcArr rcBase #patype) where t == 5 do nSel += 1
    rcRow ss #("K", "selectors", rcV nSel)
)
for n in extras do rcRow ss #("N", rcV n)
(
    local fa = for f in #(#baid, #baname, #batype, #banode) collect (rcArr rcBase f)
    for k = 1 to fa[1].count do (
        local row = #("B", (k as string))
        for a in fa do append row (rcV (rcAt a k))
        append row (rcV (rcAt baseRoles k))
        rcRow ss row
    )
)
rcRow ss (join #("O", "after") stats)
for m in missing do rcRow ss #("M", rcEncode m)
for w in warnings do rcRow ss #("W", rcEncode w)
format "END\n" to:ss
ss as string
'''


def _indent(block: str, depth: int) -> str:
    return "\n".join(("    " * depth + line) if line else line for line in block.strip("\n").split("\n"))


def _from_library_body(entry: dict[str, Any], method: str, splines: list[str], name: str, default_name: str,
                       matlib: str, maps_dir: str, *, undo_step: bool, clipping_splines: list[str] = ()) -> str:
    """The from-library script: an undo step for a .rail route load, otherwise the whole load with undo off."""
    if method == "merge" and undo_step:
        raise ValueError("internal: a merge clears the undo history; it never runs in an undo step")
    load = _LOAD_MERGE if method == "merge" else _LOAD_ROUTE
    wrapper, depth = (_UNDO_STEP, 1) if undo_step else (_UNDO_OFF, 2)
    body = (_FROM_LIBRARY_HEAD
            + wrapper.replace("@@LOAD@@", _indent(load, depth)).replace("@@SETUP@@", _indent(_SETUP, depth))
            + _FROM_LIBRARY_TAIL)
    values = {
        "@@METHOD@@": method,
        "@@MAXFILE@@": _b64(entry.get("maxfile") or ""),
        "@@OBJECT@@": _b64(entry.get("object") or ""),
        "@@ROUTE@@": _b64(entry["route"]),
        "@@NAME@@": _b64(name),
        "@@DEFAULT@@": _b64(default_name),
        "@@MATLIB@@": _b64(matlib),
        "@@MATERIAL@@": _b64(entry.get("material") or ""),
        "@@MAPS@@": _b64(maps_dir),
        "@@SPLINES@@": ", ".join(f'rcDecode "{_b64(s)}"' for s in splines),
        "@@CLIPS@@": ", ".join(f'rcDecode "{_b64(s)}"' for s in clipping_splines),
        "@@UNDO@@": UNDO_LIBRARY,
        "@@CLEANUP@@": UNDO_CLEANUP,
    }
    for key, value in values.items():
        body = body.replace(key, value)
    return body


@mcp.tool()
def railclone_from_library(
    item: str,
    splines: StrList | None = None,
    name: str = "",
    method: str = "auto",
    fix_maps: bool = True,
    allow_undo_flush: bool = False,
    clipping_splines: StrList | None = None,
) -> dict[str, Any]:
    """Create a RailClone from a library item (route or exact name), assign splines and verify its output.

    A .rail item loads by route in one undo step. Loading a .max item clears 3ds
    Max's undo history (RailClone's own browser does too), so it is refused unless
    allow_undo_flush=true, then runs with undo off and can't be undone: delete the
    nodes in remove_with to remove it. The item is validated first, against the
    library roots 3ds Max lists: a wrong route opens a modal dialog. method: auto
    (route for .rail files, .pmat matlibs and items without an object; merge for
    every other .max item), merge or route. A merged item without a .mat matlib
    keeps the materials from its file (NO_MATERIAL warning). Bases get splines by
    their role in the style graph: splines go to the path bases in order,
    clipping_splines to the clipping-area bases (a paving boundary, a window
    outline); a base whose role can't be told gets none (ROLE_UNKNOWN). Returns
    the node, method, material, bases with roles, output counts, remove_with and
    warnings, and rc_version (the running RailClone, e.g. "7.3.5"), api_version,
    createdversion, savedversion and proxymode. RailClone older than 7.1.3 warns
    RC_LIBRARY_MAPPING_BUG_VERSION (library presets get wrong texture mapping), and
    older than 7.3.1 RC_SELECTOR_BUG_VERSION when the style has selectors (they
    can get wrong values on a library load).
    """
    if method not in ("auto", "merge", "route"):
        raise ValueError("method must be 'auto', 'merge' or 'route'")
    if type(fix_maps) is not bool or type(allow_undo_flush) is not bool:
        raise ValueError("fix_maps and allow_undo_flush must be boolean")
    if not isinstance(name, str) or "\x00" in name or len(name) > 255:
        raise ValueError("name must be a string of at most 255 characters")
    if splines is None:
        splines = []
    if clipping_splines is None:
        clipping_splines = []
    for label, value in (("splines", splines), ("clipping_splines", clipping_splines)):
        if not isinstance(value, list) or len(value) > 64 or any(
                not isinstance(s, str) or not s or "\x00" in s for s in value):
            raise ValueError(f"{label} must be a list of at most 64 node names")
    if len(set(splines) | set(clipping_splines)) != len(splines) + len(clipping_splines):
        raise ValueError("splines and clipping_splines list a node twice")
    if not isinstance(item, str) or not item.strip() or "\x00" in item:
        raise ValueError("item must be a library route or an exact item name")
    # One read-only call: 3ds Max's own roots are the authority for the route and the item's folder.
    roots, warnings = _from_library_roots()
    entry, scan_warnings = rclib.resolve_item(item, roots,
                                              deadline=time.monotonic() + rclib.DEFAULT_SCAN_SECONDS)
    warnings += scan_warnings
    chosen = entry["load_method"] if method == "auto" else method
    if chosen == "merge" and (entry["maxfile_ext"] != ".max" or not entry.get("object")):
        raise ValueError(f"{entry['route']!r} is a {entry['maxfile_ext'] or 'file-less'} item without a mergeable "
                         ".max object; use method='route'")
    if not entry.get("maxfile") or not os.path.isfile(entry["maxfile"]):
        raise RailCloneError("FILE_MISSING", f"library file not found: {entry.get('maxfile')}")
    ext = entry["maxfile_ext"]
    # Live: a .rail route load keeps the undo history and is recorded, so it gets an undo step. A .max
    # item clears the history, by merge or by route, so it is loaded with undo off (no copy, nothing
    # deleted). Any other file type is treated like a .max item.
    undo_step = chosen == "route" and ext == ".rail"
    if not undo_step and not allow_undo_flush:
        if ext == ".max":
            why = ("is a .max library item. Loading it clears 3ds Max's undo history (RailClone's library "
                   "browser does the same)")
        else:
            why = (f"is a {ext} library item, and only .rail items are known to keep 3ds Max's undo history. "
                   "Loading it can clear the history")
        raise ValueError(f"{entry['route']!r} {why}, so earlier steps can no longer be undone. Pass "
                         "allow_undo_flush=true to load it anyway: it then runs with undo off and can't be "
                         "undone, and remove_with lists the nodes to delete to remove it. "
                         "Nothing was loaded; only the library roots were read from 3ds Max.")
    matlib = ""
    if chosen == "merge":
        matlib, why = rclib.matlib_template(entry)
        if why:
            warnings.append(f"NO_MATERIAL: {why}")
    maps_dir = os.path.join(entry["category_dir"], "maps")
    maps_dir = maps_dir + "\\" if fix_maps and os.path.isdir(maps_dir) else ""
    default_name = "RC_" + (re.sub(r"\W+", "_", entry["name"]).strip("_") or "RailClone")
    raw = _send(_from_library_body(entry, chosen, splines, name, default_name, matlib, maps_dir,
                                   undo_step=undo_step, clipping_splines=clipping_splines))
    info: dict[str, Any] = {}
    extra, bases, missing, stats = [], [], [], None
    try:
        for row in _records(raw, "RCLIB"):
            tag = row[0]
            if tag == "K":
                info[row[1]] = _dv(row[2])
            elif tag == "N":
                extra.append(_dv(row[1]))
            elif tag == "B":
                bid, bname, btype, bnode = (_dv(f) for f in row[2:6])
                bases.append({"index": int(row[1]), "id": bid, "name": bname, "type": btype,
                              "type_name": {0: "spline", 1: "surface"}.get(btype, "unknown"),
                              "role": _dv(row[6]) if len(row) > 6 else None, "node": _node_ref(bnode)})
            elif tag == "O":
                stats = _stats(row)
            elif tag == "M":
                missing.append(_unb64(row[1]))
            elif tag == "W":
                warnings.append(_unb64(row[1]))
    except (ValueError, IndexError, UnicodeError) as exc:
        raise RailCloneError("INVALID_READBACK", f"malformed library report: {exc}") from exc
    if stats is None or "handle" not in info:
        raise RailCloneError("INVALID_READBACK", "library report is incomplete; inspect the scene before retrying")
    runtime = _version_int(info.get("rc_runtime"))
    proxymode = info.get("proxymode")
    proxy = isinstance(proxymode, int) and proxymode != 0
    if proxy:
        # Under Proxy Cache no output item names its segment (live), so only the item total tells
        stats.update(placed=None, placeholders=None)
        warnings.append(_proxy_note(proxymode, by_box=False))
    verified = stats["total"] > 0 if proxy else stats["placed"] > 0
    warnings += [w for w in (rclib.mapping_bug_warning(runtime),
                             rclib.selector_bug_warning(runtime, info.get("selectors") or 0)) if w]
    if (splines or clipping_splines) and not verified:
        warnings.append("EMPTY_OUTPUT: the RailClone places no items with the given splines")
    if not splines and not clipping_splines:
        paths = [b["name"] for b in bases if b["type"] == 0 and b.get("role") == "path"]
        clips = [b["name"] for b in bases if b["type"] == 0 and b.get("role") == "clip"]
        other = [b["name"] for b in bases if b["type"] == 0 and b.get("role") not in ("path", "clip", "unused")]
        warnings.append(f"NO_SPLINES: assign a spline to the path base(s) {paths} (splines) or the clipping-area "
                        f"base(s) {clips} (clipping_splines) for output"
                        + (f"; the role of {other} couldn't be told" if other else ""))
    if chosen == "route" and info.get("style") != entry["route"]:
        warnings.append(f"STYLE_ROUTE_MISMATCH: style reads {info.get('style')!r}")
    if missing:
        warnings.append(f"MISSING_MAPS: {len(missing)} bitmap file(s) do not resolve")
    item_out = {k: entry.get(k) for k in ("route", "root", "category", "name", "maxfile", "object", "material",
                                          "matlib", "uuid")}
    extra_nodes = [e for e in extra if isinstance(e, dict)]
    return {
        "handle": info["handle"], "name": info.get("name"), "method": chosen, "route": entry["route"],
        "item": item_out, "style": info.get("style") or "", "style_desc": info.get("style_desc"),
        "rc_version": rclib.version_text(runtime), "rc_version_int": runtime,
        "api_version": info.get("api_version"), "createdversion": info.get("createdversion"),
        "savedversion": info.get("savedversion"), "proxymode": proxymode,
        "material": info.get("material"), "renderer": info.get("renderer"), "matlib": info.get("matlib") or None,
        "bases": bases, "item_count": stats["total"] if proxy else stats["placed"], "output": stats,
        "verified": verified,
        "maps": {"fixed": info.get("fixed_maps") or 0, "missing": missing},
        "extra_nodes": [e["name"] for e in extra_nodes],
        "undoable": undo_step, "undo": UNDO_LIBRARY if undo_step else None,
        "undo_history_cleared": not undo_step,
        # the nodes this call created; deleting them removes the load (agents don't run undo through MCP)
        "remove_with": [{"name": info.get("name"), "handle": info["handle"]}] + extra_nodes,
        "warnings": warnings,
    }
