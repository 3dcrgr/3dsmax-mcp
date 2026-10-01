"""Cosmos orchestration; network I/O remains in the external MCP process."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .. import process_health
from ..max_client import (HANG_ADVICE, MaxClient, MaxHealthError, RequestOutcomeUnknown, mark_settling,
                          release_settling)

from .cosmos_client import Cosmos, CosmosError, asset_id as normalize_id

_KIND_TAGS = {"all": (), "model": (1,), "material": (38,), "hdri": (427,)}
_STATES = {1: "unavailable", 2: "not_downloaded", 3: "ready",
           4: "downloading", 5: "unavailable", 6: "ready", 7: "creating"}
_EXPECTED = {"model": "nodes", "material": "materials", "hdri": "maps"}
_import_lock = threading.Lock()
_download_jobs: dict[tuple[str, str], float] = {}
_download_lock = threading.Lock()

SETTLE_SECONDS_DEFAULT = 90
SETTLE_SECONDS_MAX = 300
_DETECT_TIMEOUT_S = 30.0
_POLL_TIMEOUT_S = 10.0       # a light poll is read-only: dropped (probe) at this deadline
_POLL_GATE_MS = 300          # WM_NULL round trip to the main window before each poll
_SETTLE_CHECKS = 3
_SETTLE_INTERVAL_S = 1.0
_SETTLE_MIN_SPAN_S = 6.0     # IsHungAppWindow only reports hung after ~5 s without pumping
_SETTLE_FLOOR_S = 8          # settle_seconds below this still wait this long
_SETTLE_CPU_GRACE_S = 10.0   # windows answer but CPU busy: stop waiting after this
_SETTLE_MESSAGE_TIMEOUT_MS = 500
_SETTLING_GUARD_S = 900.0
_BROWSER_WAIT_S = 15.0       # after the browser action: wait this long for a main-thread browser
_BROWSER_INTERVAL_S = 0.5
_BROWSER_CHECKS = 2          # consecutive responsive checks (WM_NULL catches a fresh stall at once)
_BROWSER_MESSAGE_TIMEOUT_MS = 500
# True when the Compact or the Slate Material Editor is open (both render slots with renderers.medit).
_EDITOR_OPEN = "((try(MatEditor.isOpen())catch(true)) or (try(SME.isOpen())catch(false)))"
_WAIT_ADVICE = HANG_ADVICE + " Do not open or close the Material Editor meanwhile."


def _max(client, command, cmd_type="maxscript", timeout=None, probe=False):
    if probe:  # read-only: may be dropped at its deadline instead of waiting out a stall
        response = client.send_command(command, cmd_type=cmd_type, timeout=timeout, probe=True)
    else:
        response = client.send_command(command, cmd_type=cmd_type, timeout=timeout)
    result = response.get("result", {})
    return json.loads(result) if isinstance(result, str) else result


def _compact(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _search_context(client, renderer):
    """Resolve the importer from Cosmos alone, so search works while Max is busy.

    The target PID comes from the client without binding or sending. Max is only
    asked for its current renderer when that PID has several Cosmos importers.
    Also returns where the renderer came from: explicit, only_importer or scene.
    """
    if renderer not in ("current", "corona", "vray"):
        raise ValueError("renderer must be current, corona or vray")
    source = "explicit" if renderer != "current" else "only_importer"
    try:
        target = client.resolve_target()
    except Exception:
        target = {}
    pid = target.get("target_pid") if target.get("available") else None
    if pid:
        service = Cosmos(timeout=15)
        matches = [i for i in service.importers() if i["pid"] == pid
                   and (renderer == "current" or _compact(i["renderer"]) == renderer)]
        if len(matches) == 1:
            service.importer_id = matches[0]["id"]
            return service, matches[0], source
    return (*_context(client, renderer), "explicit" if renderer != "current" else "scene")


def _context(client, renderer):
    if renderer not in ("current", "corona", "vray"):
        raise ValueError("renderer must be current, corona or vray")
    # Also binds the existing MaxClient to its normal selected/default instance.
    current = _max(client, '(local s=classof renderers.current as string; '
                   '"{\\"renderer\\":\\"" + (MCP_Server.escapeJsonString s) + "\\"}")')
    target = client.get_selected_max_instance()
    if not target.get("available") or not target.get("target_pid"):
        raise CosmosError("Select a running Max instance first.", "COSMOS_NO_IMPORTER")
    selected_renderer = re.sub(r"[^a-z0-9]", "", current["renderer"].lower())
    wanted = renderer
    if renderer == "current":
        wanted = ("corona" if selected_renderer.startswith("corona") else
                  "vray" if selected_renderer.startswith("vray") else "")
    if not wanted:
        raise CosmosError("Current renderer has no Cosmos integration; choose corona or vray.",
                          "COSMOS_NO_IMPORTER")
    service = Cosmos(timeout=15)
    matches = [i for i in service.importers()
               if i["pid"] == target["target_pid"]
               and re.sub(r"[^a-z0-9]", "", i["renderer"].lower()) == wanted]
    if len(matches) != 1:
        raise CosmosError("Expected one %s Cosmos importer for Max PID %s; open Cosmos in that renderer."
                          % (wanted, target["target_pid"]), "COSMOS_NO_IMPORTER")
    importer = matches[0]
    service.importer_id = importer["id"]
    return service, importer


def _summary(service, asset):
    state = _STATES.get(asset["availability"], "unknown")
    return {"asset_id": asset["id"], "name": asset["name"], "kind": asset["kind"],
            "revision": asset["revision"], "state": state, "size_bytes": asset["size"],
            "thumbnail": (service.base_url + "/api/v1/package" + asset["preview_path"]
                          if asset.get("preview_path") else None)}


def search(client, query, kind, downloaded, limit, offset, renderer):
    if kind not in _KIND_TAGS:
        raise ValueError("kind must be all, model, material or hdri")
    if len(query) > 500:
        raise ValueError("query must be at most 500 characters")
    service, importer, source = _search_context(client, renderer)
    result = service.search(query, limit=limit, offset=offset,
                            tag_ids=_KIND_TAGS[kind], downloaded=downloaded)
    items = [_summary(service, a) for a in result["items"]]
    # only_importer: the scene renderer was not checked; cosmos_import with "current" still checks it.
    return {"renderer": importer["renderer"], "renderer_source": source, "max_pid": importer["pid"], "assets": items,
            "offset": offset, "next_offset": offset + len(items) if len(items) == limit else None}


def _download(service, package_id, wait_seconds):
    if not isinstance(wait_seconds, int) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 60:
        raise ValueError("wait_seconds must be an integer from 0 to 60")
    package_id = normalize_id(package_id)
    asset = service.asset(package_id)
    key = (service.importer_id, package_id)
    if asset["availability"] in (3, 6):
        with _download_lock:
            _download_jobs.pop(key, None)
        return asset
    if asset["availability"] in (1, 5):
        raise CosmosError("This asset is unavailable to the current Cosmos account/importer.",
                          "COSMOS_ASSET_UNAVAILABLE")
    if asset["availability"] not in (2, 4):
        raise CosmosError("Asset is not downloadable in its current state.", "COSMOS_ASSET_UNAVAILABLE")
    with _download_lock:
        submitted = _download_jobs.get(key)
        if submitted is not None and time.monotonic() - submitted > 180:
            _download_jobs.pop(key, None)
            raise CosmosError("Cosmos did not finish the download within 180 seconds. Check the asset in Cosmos.",
                              "COSMOS_DOWNLOAD_FAILED")
        # Do not enqueue another copy while a prior request is being scheduled.
        if asset["availability"] == 2 and submitted is None:
            service.download(package_id, asset["revision"])
            submitted = _download_jobs[key] = time.monotonic()
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        asset = service.asset(package_id)
        if asset["availability"] in (3, 6):
            with _download_lock:
                _download_jobs.pop(key, None)
            return asset
        if asset["availability"] in (1, 5):
            with _download_lock:
                _download_jobs.pop(key, None)
            service.require_sign_in()
            raise CosmosError("Cosmos download did not complete. Check this asset's download error in Cosmos.",
                              "COSMOS_DOWNLOAD_FAILED")
    return asset


def download(client, package_id, wait_seconds, renderer):
    service, importer = _context(client, renderer)
    asset = _download(service, package_id, wait_seconds)
    result = _summary(service, asset)
    if result["state"] not in ("ready", "unavailable"):
        result["state"] = "queued" if asset["availability"] == 2 else "downloading"
        result["next"] = "Call cosmos_download with this asset_id to continue waiting."
    return result


# __MAT_CLASSES__ / __SCAN_MAPS__ select a full scan (every material class
# across all animatables, plus texture maps) or a light poll (nodes by
# cosmosAssetId, the 24 Material Editor slots, maps only for HDRIs) used while
# the native importer is still working on Max's main thread.
# __RECORD__ (pre-dispatch only) also returns the handles of every material (and,
# for HDRIs, bitmap/HDRI map) seen as "known". Later scans get those handles back
# (sorted, __KNOWN_*__) and also report any material/map NOT among them, whatever
# its name: package materials are often named differently from the asset.
_ASSET_SNAPSHOT = r"""(
 fn compactName value = (
  (dotNetClass "System.Text.RegularExpressions.Regex").Replace (toLower(value as string)) "[^a-z0-9]" ""
 )
 local key="__KEY__"
 local aid="__ASSET__"
 local record=__RECORD__
 local recordMaps=__RECORD_MAPS__
 local knownMats=__KNOWN_MATS__
 local knownMaps=__KNOWN_MAPS__
 local diffMats=__DIFF_MATS__
 local diffMaps=__DIFF_MAPS__
 fn matchesName value key = ((findString (compactName value) key)!=undefined)
 fn handleOf value = ((getHandleByAnim value) as integer64)
 fn isKnown h known = (
  local lo=1, hi=known.count, found=false
  while not found and lo<=hi do (
   local mid=(lo+hi)/2
   if known[mid]==h then found=true else if known[mid]<h then lo=mid+1 else hi=mid-1
  )
  found
 )
 fn isNew value known diff = (diff and (try(not (isKnown (handleOf value) known))catch(false)))
 fn remember value known = (try(append known (handleOf value))catch())
 fn slotOf value = (
  local s=0
  try(for i=1 to meditMaterials.count while s==0 do if meditMaterials[i]==value do s=i)catch()
  s
 )
 fn fileOf value = (
  local f=try(value.filename as string)catch("")
  if f=="" do f=try(value.HDRIMapName as string)catch("")
  f
 )
 fn quoteJSON value = ("\"" + (MCP_Server.escapeJsonString(value as string)) + "\"")
 fn refJSON value isNode slot:undefined isSub:false = (
  local h=if isNode then value.handle as string else formattedPrint (handleOf value) format:"d"
  "{\"handle\":"+(quoteJSON h)+",\"name\":"+(quoteJSON(try(value.name)catch("")))+",\"class\":"+(quoteJSON(classof value))+",\"filename\":"+(quoteJSON(fileOf value))+(if slot==undefined then "" else ",\"slot\":"+(slot as string))+(if isSub then ",\"sub\":true" else "")+"}"
 )
 fn collectSubs value subs depth = (
  if depth<12 do for i=1 to (try(getNumSubMtls value)catch(0)) do (
   local s=try(getSubMtl value i)catch(undefined)
   if s!=undefined and findItem subs s==0 do (append subs s; collectSubs s subs (depth+1))
  )
 )
 fn joinJSON values = (
  local s="["
  for i=1 to values.count do (if i>1 do s+=","; s+=values[i])
  s+"]"
 )
 fn joinHandles values = (
  local ss=stringStream ""
  for i=1 to values.count do (if i>1 do format "," to:ss; format "%" (formattedPrint values[i] format:"d") to:ss)
  "["+(ss as string)+"]"
 )
 local nodes=for n in objects where (try(n.cosmosAssetId==aid)catch(false)) collect n
 local mats=#()
 for n in nodes where n.material!=undefined do appendIfUnique mats n.material
 try(
  for m in meditMaterials where m!=undefined do (
   if record do remember m knownMats
   if (matchesName m.name key) or (isNew m knownMats diffMats) do appendIfUnique mats m
  )
 )catch()
 for c in __MAT_CLASSES__ do try(
  for m in (getClassInstances c processAllAnimatables:true) do (
   if record do remember m knownMats
   if (matchesName m.name key) or (isNew m knownMats diffMats) do appendIfUnique mats m
  )
 )catch()
 local maps=#()
 fn visitMaps value maps visited depth = (
  if value!=undefined and depth<12 and findItem visited value==0 do (
   append visited value
   if superclassof value==textureMap do appendIfUnique maps value
   for i=1 to (try(getNumSubTexmaps value)catch(0)) do visitMaps (getSubTexmap value i) maps visited (depth+1)
   for i=1 to (try(getNumSubMtls value)catch(0)) do visitMaps (getSubMtl value i) maps visited (depth+1)
  )
 )
 local visited=#()
 if __SCAN_MAPS__ do (
  for m in mats do visitMaps m maps visited 0
  for c in textureMap.classes where (matchPattern (c as string) pattern:"*bitmap*" or matchPattern (c as string) pattern:"*hdri*") do try(
   for m in (getClassInstances c processAllAnimatables:true) do (
    if recordMaps do remember m knownMaps
    if (matchesName m.name key) or (matchesName (fileOf m) key) or (isNew m knownMaps diffMaps) do appendIfUnique maps m
   )
  )catch()
 )
 local knownJSON=if record then (",\"known\":{\"materials\":"+(joinHandles knownMats)+",\"maps\":"+(if recordMaps then joinHandles knownMaps else "null")+"}") else ""
 local subs=#()
 for m in mats do collectSubs m subs 0
 "{\"nodes\":"+(joinJSON(for n in nodes collect(refJSON n true)))+",\"materials\":"+(joinJSON(for m in mats collect(refJSON m false slot:(slotOf m) isSub:((findItem subs m)>0))))+",\"maps\":"+(joinJSON(for m in maps collect(refJSON m false)))+knownJSON+"}"
)"""

_RENDERER_MATERIAL_CLASSES = (
    '(for c in material.classes where (matchPattern (c as string) pattern:"__PREFIX__*" '
    'or (c as string)=="Multimaterial") collect c)')


def _handle_ints(values):
    out = set()
    for value in values or ():
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            pass
    return out


def _mxs_handles(values):
    """Sorted MAXScript Integer64 array for isKnown's binary search (same type as handleOf)."""
    return "#(" + ",".join("%dL" % h for h in sorted(values)) + ")"


def _snapshot_script(asset, light=False, renderer="", scan_classes=False, known=None, record=False):
    """known: the pre-dispatch "known" handles; materials/maps outside it are reported
    whatever their name (a light poll diffs only the asset's own resource kind).
    record: return those handles instead (the pre-dispatch snapshot)."""
    # Match this asset only. Do not snapshot unrelated scene/material metadata.
    key = _compact(asset["name"])
    if len(key) < 4:
        raise CosmosError("Asset has no usable identity for import verification.")
    classes, scan_maps = "material.classes", "true"
    diff_kinds = ("materials", "maps")
    if light:
        prefix = "Corona" if _compact(renderer).startswith("corona") else "VRay"
        classes = (_RENDERER_MATERIAL_CLASSES.replace("__PREFIX__", prefix)
                   if scan_classes and asset["kind"] == "material" else "#()")
        scan_maps = "true" if asset["kind"] == "hdri" else "false"
        diff_kinds = (_EXPECTED.get(asset["kind"]),)
    known = {} if record else (known or {})
    diff = {kind: kind in diff_kinds and known.get(kind) is not None for kind in ("materials", "maps")}
    return (_ASSET_SNAPSHOT.replace("__KEY__", key).replace("__ASSET__", normalize_id(asset["id"]))
            .replace("__MAT_CLASSES__", classes).replace("__SCAN_MAPS__", scan_maps)
            .replace("__RECORD__", "true" if record else "false")
            .replace("__RECORD_MAPS__", "true" if record and asset["kind"] == "hdri" else "false")
            .replace("__KNOWN_MATS__", _mxs_handles(_handle_ints(known.get("materials")) if diff["materials"] else ()))
            .replace("__KNOWN_MAPS__", _mxs_handles(_handle_ints(known.get("maps")) if diff["maps"] else ()))
            .replace("__DIFF_MATS__", "true" if diff["materials"] else "false")
            .replace("__DIFF_MAPS__", "true" if diff["maps"] else "false"))


def _asset_snapshot(client, asset, light=False, renderer="", scan_classes=False, timeout=None, probe=False,
                    known=None):
    """Full scan by default. light=True checks nodes, Material Editor slots and
    (for HDRIs) maps; scan_classes adds the renderer's own material classes.
    known (the pre-dispatch handles) also reports new materials/maps by handle."""
    return _max(client, _snapshot_script(asset, light, renderer, scan_classes, known), timeout=timeout,
                probe=probe)


# Run later only when the Material Editor is closed: restoring re-renders its
# slots with the recorded (V-Ray) renderer, which is what stalled Max.
# Backup = #(previous renderer, previous locked, the Scanline instance we set).
# "stale" when medit is no longer that instance (scene reset/opened, or the
# user picked another renderer): nothing is overwritten. When it was locked,
# re-locking is enough (medit follows production again).
_MEDIT_RESTORE = (
    '(global mcp_cosmosMeditBackup; local b=mcp_cosmosMeditBackup; '
    'if b==undefined then "nothing_to_restore" '
    'else if not (try(b[3]==renderers.medit)catch(false)) then (mcp_cosmosMeditBackup=undefined; "stale") '
    'else if ' + _EDITOR_OPEN + ' then "editor_open" '
    'else (try(if b[2]==true then renderers.medit_locked=true '
    'else (if b[1]!=undefined do renderers.medit=b[1]; if b[2]==false do renderers.medit_locked=false); '
    'mcp_cosmosMeditBackup=undefined; "restored")catch("failed: "+(getCurrentException()))))')

# Without the swap: only read whether an earlier swapped import left medit at our Scanline.
_MEDIT_LEFTOVER = "leftover=try(mcp_cosmosMeditBackup[3]==renderers.medit)catch(false)"

# Inserted into _PREPARE only when the Scanline swap is wanted.
_MEDIT_SWAP = r"""if scanline!=undefined do (
  if (try(classof renderers.medit)catch(undefined))==scanline then (
   pending=try(mcp_cosmosMeditBackup[3]==renderers.medit)catch(false)
   if not pending do mcp_cosmosMeditBackup=undefined
  ) else (
   try(
    sl=scanline()
    mcp_cosmosMeditBackup=#(renderers.medit, locked, sl)
    pending=true
    renderers.medit_locked=false
    renderers.medit=sl
    swapped=true
   )catch(
    swapError=getCurrentException()
    if not (try(renderers.medit==sl)catch(false)) do (
     try(if locked==true do renderers.medit_locked=true)catch()
     mcp_cosmosMeditBackup=undefined
     pending=false
    )
   )
  )
 )"""

# Issue #10: the native importer writes into the ACTIVE Compact Material Editor slot.
# A slot is "free" (safe to overwrite) only when its material is an untouched default:
# named "NN - Default", no sub-materials, no maps, on no scene node and every property
# equal to a new instance of its class (an edited but unnamed slot is not free; the new
# instance is never put in a slot). Anything else in the active slot is worth keeping.
# Never throws; any doubt means "not free".
_SLOT_FNS = r"""fn mcpLooksFree m = (
  local ok=false
  try(
   if m!=undefined and (matchPattern (m.name as string) pattern:"?? - Default") and (getNumSubMtls m)==0 and (refs.dependentNodes m).count==0 do (
    ok=true
    for j=1 to (getNumSubTexmaps m) while ok do if (getSubTexmap m j)!=undefined do ok=false
    if ok do (
     local d=(classof m)()
     for p in (getPropNames m) while ok do if ((try(getProperty m p)catch(#mcpErr)) as string)!=((try(getProperty d p)catch(#mcpErr)) as string) do ok=false
    )
   )
  )catch(ok=false)
  ok
 )
 fn mcpFreeSlot skip = (
  local s=0
  try(for i=1 to meditMaterials.count while s==0 do if (findItem skip i)==0 and (mcpLooksFree meditMaterials[i]) do s=i)catch(s=0)
  s
 )
 fn mcpSlotRef m = (
  if m==undefined then "null" else "{\"handle\":\""+(try(formattedPrint ((getHandleByAnim m) as integer64) format:"d")catch(""))+"\",\"name\":\""+(MCP_Server.escapeJsonString(try(m.name as string)catch("")))+"\"}"
 )"""

# In _PREPARE (after the baseline snapshot): if the active slot holds a material
# worth keeping, remember it (the global's reference also keeps it alive if the
# importer displaces it) and make a free slot active so the importer writes there.
# An earlier import's unfinished record (stale) is kept alive and its switch undone.
# Changing activeMeditSlot can make an open Compact editor render that slot's sample.
_SLOT_PREPARE = r"""try(
  if mcp_cosmosMeditSlot!=undefined do (
   slotStale=true
   local old=mcp_cosmosMeditSlot
   if mcp_cosmosMeditDisplaced==undefined do mcp_cosmosMeditDisplaced=#()
   try(appendIfUnique mcp_cosmosMeditDisplaced old[2])catch()
   try(if old[3]>0 and activeMeditSlot==old[3] do activeMeditSlot=old[1])catch()
   mcp_cosmosMeditSlot=undefined
  )
  slotA=activeMeditSlot
  slotMat=meditMaterials[slotA]
  slotKeep=slotMat!=undefined and not (mcpLooksFree slotMat)
  if slotKeep do (
   mcp_cosmosMeditSlot=#(slotA, slotMat, 0, maxFilePath+maxFileName)
   slotFree=mcpFreeSlot #(slotA)
   if slotFree>0 do (
    mcp_cosmosMeditSlot[3]=slotFree
    activeMeditSlot=slotFree
    slotSwitched=(activeMeditSlot==slotFree)
   )
  )
 )catch(slotErr=getCurrentException())"""

# In _FINALIZE (only after the OS-only settle was quiet): if the kept material was
# displaced anyway by a material newer than every pre-dispatch handle (the import),
# move that one to a free slot and put the kept one back; else keep the kept one
# alive in mcp_cosmosMeditDisplaced. Then make the user's slot active again.
# With no free slot before dispatch, moving the import needs a slot freed meanwhile;
# usually the result is the warning. An open undo transaction (busy) leaves the record
# for pending_restore; a deleted material or another scene file (stale) leaves the
# slots as they are. Slot assignments can make the editor render sample slots (V-Ray:
# on the main thread), as when the user does it by hand. Never throws.
_SLOT_RESTORE = r"""(
  local out="null"
  try(
   local b=mcp_cosmosMeditSlot
   if b!=undefined and theHold.Holding() then (out="{\"busy\":true}") else if b!=undefined and ((try(isDeleted b[2])catch(true)) or (try(b[4]!=(maxFilePath+maxFileName))catch(true))) then (
    mcp_cosmosMeditSlot=undefined
    local kept=false
    if not (try(isDeleted b[2])catch(true)) do (
     if mcp_cosmosMeditDisplaced==undefined do mcp_cosmosMeditDisplaced=#()
     try(appendIfUnique mcp_cosmosMeditDisplaced b[2]; kept=true)catch()
    )
    out="{\"stale\":true,\"original\":"+(try(b[1] as string)catch("0"))+",\"material\":"+(if kept then mcpSlotRef b[2] else "null")+"}"
   ) else if b!=undefined do (
    local a=b[1], m=b[2], f=b[3]
    local cur=try(meditMaterials[a])catch(undefined)
    local displaced=(cur!=undefined and cur!=m)
    local fresh=displaced and (__FRESH_TEST__)
    local movedTo=0, restored=false, keptAlive=false, activeRestored=undefined, err=""
    if fresh do (
     movedTo=if f>0 and (mcpLooksFree (try(meditMaterials[f])catch(undefined))) then f else (mcpFreeSlot #(a))
     if movedTo>0 do try(
      meditMaterials[movedTo]=cur
      meditMaterials[a]=m
      restored=(meditMaterials[a]==m)
     )catch(err=getCurrentException())
    )
    if displaced and not restored do (
     if mcp_cosmosMeditDisplaced==undefined do mcp_cosmosMeditDisplaced=#()
     try(appendIfUnique mcp_cosmosMeditDisplaced m; keptAlive=true)catch()
    )
    if f>0 do try(
     if activeMeditSlot!=a do activeMeditSlot=a
     activeRestored=(activeMeditSlot==a)
    )catch(activeRestored=false; if err=="" do err=getCurrentException())
    mcp_cosmosMeditSlot=undefined
    out="{\"original\":"+(a as string)+",\"switched_to\":"+(f as string)+",\"displaced\":"+(if displaced then mcpSlotRef m else "null")+",\"occupant\":"+(if displaced then mcpSlotRef cur else "null")+",\"imported_occupant\":"+(if fresh then "true" else "false")+",\"moved_to\":"+(movedTo as string)+",\"restored\":"+(if restored then "true" else "false")+",\"kept_alive\":"+(if keptAlive then "true" else "false")+",\"active_restored\":"+(if activeRestored==true then "true" else if activeRestored==false then "false" else "null")+",\"error\":\""+(MCP_Server.escapeJsonString(err as string))+"\"}"
   )
  )catch(out="{\"error\":\""+(MCP_Server.escapeJsonString(getCurrentException() as string))+"\"}")
  out
 )"""

# Undo only the pre-dispatch slot switch (prepare lost; nothing was dispatched).
_SLOT_ACTIVE_RESTORE = ('(global mcp_cosmosMeditSlot; local b=mcp_cosmosMeditSlot; if b==undefined then '
                        '"nothing_to_restore" else (try(if b[3]>0 and activeMeditSlot!=b[1] do activeMeditSlot=b[1])'
                        'catch(); mcp_cosmosMeditSlot=undefined; "restored"))')

# ONE bridge call before dispatch: baseline snapshot, selection, Material Editor
# renderer state, then (only with swap) the swap to Scanline (V-Ray rendering the
# imported material's slot preview blocked Max's main thread for minutes), then
# the active-slot guard (#10).
_PREPARE = r"""(
 global mcp_cosmosMeditBackup
 global mcp_cosmosMeditSlot, mcp_cosmosMeditDisplaced
 if theHold.Holding() do throw "USER_BUSY"
 local snap=__SNAPSHOT__
 __SLOT_FNS__
 fn jsonBool v = (if v==true then "true" else if v==false then "false" else "null")
 fn jsonStr v = ("\"" + (MCP_Server.escapeJsonString(v as string)) + "\"")
 local sel=selection as array
 local selJSON="["
 for i=1 to sel.count do (if i>1 do selJSON+=","; selJSON+=(sel[i].handle as string))
 selJSON+="]"
 local meditClass=try((classof renderers.medit) as string)catch("undefined")
 local meditName=try(renderers.medit as string)catch("undefined")
 local locked=try(renderers.medit_locked)catch(undefined)
 local editorOpen=try(__EDITOR_OPEN__)catch(undefined)
 local scanline=Default_Scanline_Renderer
 local sl=undefined
 local swapped=false
 local pending=false
 local swapError=""
 local leftover=false
 __SWAP__
 local slotA=0, slotMat=undefined, slotKeep=false, slotFree=0, slotSwitched=false, slotStale=false, slotErr=""
 local slotMode=try((MatEditor.mode) as string)catch("")
 __SLOT__
 clearSelection()
 "{\"before\":"+snap+",\"selection\":"+selJSON+",\"medit\":{\"class\":"+(jsonStr meditClass)+",\"instance\":"+(jsonStr meditName)+",\"locked\":"+(jsonBool locked)+",\"editor_open\":"+(jsonBool editorOpen)+",\"scanline_available\":"+(jsonBool (scanline!=undefined))+",\"swapped\":"+(jsonBool swapped)+",\"backup_pending\":"+(jsonBool pending)+",\"leftover_backup\":"+(jsonBool leftover)+",\"error\":"+(jsonStr swapError)+"},\"medit_slot\":{\"active\":"+(slotA as string)+",\"material\":"+(mcpSlotRef slotMat)+",\"keep\":"+(jsonBool slotKeep)+",\"free_slot\":"+(slotFree as string)+",\"switched\":"+(jsonBool slotSwitched)+",\"mode\":"+(jsonStr slotMode)+",\"stale\":"+(jsonBool slotStale)+",\"error\":"+(jsonStr slotErr)+"}}"
)"""

# After Max is quiet: restore the selection, then (only if the Material
# Editor is closed) the recorded Material Editor renderer, then the user's
# Material Editor slot (#10). Never opens, closes or activates any window.
# Safe to run twice.
_FINALIZE = r"""(
 global mcp_cosmosMeditBackup
 global mcp_cosmosMeditSlot, mcp_cosmosMeditDisplaced
 __SLOT_FNS__
 local restoredSelection=false
 if not theHold.Holding() do (
  select (for h in #(__HANDLES__) where (maxOps.getNodeByHandle h)!=undefined collect (maxOps.getNodeByHandle h))
  restoredSelection=true
 )
 local medit=if __RESTORE__ then __MEDIT_RESTORE__ else "not_requested"
 local slotJSON=__SLOT_RESTORE__
 local editorOpen=try(__EDITOR_OPEN__)catch(undefined)
 "{\"selection_restored\":"+(if restoredSelection then "true" else "false")+",\"medit\":\""+(MCP_Server.escapeJsonString(medit as string))+"\",\"editor_open\":"+(if editorOpen==true then "true" else if editorOpen==false then "false" else "null")+",\"medit_slot\":"+slotJSON+"}"
)"""


# ONE bridge call when no responsive main-thread Cosmos browser exists: find the
# renderer's "Cosmos browser" action by search (never a fixed table/item index)
# and run it. Opened this way the browser lives on Max's main thread; the
# importer's own hidden browser on another thread is what stalled/deadlocked Max.
# getActionTable/getActionItem take 1-based <index> arguments (0 would read index -1).
_OPEN_BROWSER = r"""(
 fn jsonStr v = ("\"" + (MCP_Server.escapeJsonString(v as string)) + "\"")
 local found=false, tableName="", desc="", executed=false, err="", tid=undefined, aid=undefined
 try(
  for i=1 to actionMan.numActionTables while not found do (
   local t=try(actionMan.getActionTable i)catch(undefined)
   local n=if t==undefined then "" else (try(t.name as string)catch(""))
   if t!=undefined and (__TABLE_TEST__) do (
    for j=1 to (try(t.numActionItems)catch(0)) while not found do (
     local a=try(t.getActionItem j)catch(undefined)
     if a!=undefined do (
      local d=""
      try(a.getDescriptionText &d)catch()
      if not (matchPattern d pattern:"*cosmos browser*") do (d=""; try(a.getButtonText &d)catch())
      if matchPattern d pattern:"*cosmos browser*" do (found=true; tableName=n; desc=d; tid=t.id; aid=a.id)
     )
    )
   )
  )
  if found do executed=((actionMan.executeAction tid (aid as string))==true)
 )catch(err=getCurrentException())
 "{\"found\":"+(if found then "true" else "false")+",\"table\":"+(jsonStr tableName)+",\"description\":"+(jsonStr desc)+",\"executed\":"+(if executed then "true" else "false")+",\"error\":"+(jsonStr err)+"}"
)"""
_BROWSER_TABLES = {"vray": '(matchPattern n pattern:"*v-ray*" or matchPattern n pattern:"*vray*")',
                   "corona": 'matchPattern n pattern:"*corona*"'}


def _open_browser_script(renderer):
    table = _BROWSER_TABLES.get("corona" if _compact(renderer).startswith("corona") else "vray")
    return _OPEN_BROWSER.replace("__TABLE_TEST__", table)


def _prepare_script(asset, swap=True):
    return (_PREPARE.replace("__SWAP__", _MEDIT_SWAP if swap else _MEDIT_LEFTOVER)
            .replace("__SNAPSHOT__", _snapshot_script(asset, record=True)).replace("__EDITOR_OPEN__", _EDITOR_OPEN)
            .replace("__SLOT_FNS__", _SLOT_FNS).replace("__SLOT__", _SLOT_PREPARE))


def _newest_known(before):
    """Highest pre-dispatch material handle (-1: none recorded). Handles only grow, and
    the record scan gave every existing material one, so a higher handle is new."""
    return max(_handle_ints(((before or {}).get("known") or {}).get("materials")), default=-1)


def _finalize_script(handles, restore_medit, newest_known=-1):
    """newest_known: a slot occupant above this handle counts as the import's (-1: none
    does, so a displaced slot material is kept alive and reported, not swapped back)."""
    values = ",".join(str(int(h)) for h in handles)
    fresh = ("false" if int(newest_known) < 0
             else "try(((getHandleByAnim cur) as integer64)>(%dL))catch(false)" % int(newest_known))
    return (_FINALIZE.replace("__HANDLES__", values)
            .replace("__RESTORE__", "true" if restore_medit else "false")
            .replace("__MEDIT_RESTORE__", _MEDIT_RESTORE)
            .replace("__SLOT_FNS__", _SLOT_FNS)
            .replace("__SLOT_RESTORE__", _SLOT_RESTORE.replace("__FRESH_TEST__", fresh))
            .replace("__EDITOR_OPEN__", _EDITOR_OPEN))


def _browser_state(pid):
    """OS-level only: "Chaos Cosmos Browser" windows (hidden included) with owning thread and hung state."""
    return process_health.thread_windows(pid, process_health.COSMOS_BROWSER_TITLE, _BROWSER_MESSAGE_TIMEOUT_MS)


def _main_browser_ready(state):
    return any(w["main_thread"] and not w["hung"] for w in state.get("windows") or [])


def _hung_elsewhere(state):
    """Hung "Chaos Cosmos Browser" windows NOT owned by Max's main thread: main-thread
    work activating one of those is the cross-thread deadlock."""
    return [w for w in state.get("windows") or [] if w["hung"] and not w["main_thread"]]


def _pre_dispatch_state(pid, browser):
    """OS-level only (nothing sent): is Max's main window, or a "Chaos Cosmos
    Browser" (hidden included) on a separate thread, already hung before this import?
    A hung main-thread browser only means Max's main thread is busy."""
    windows = browser.get("windows") or []
    return {"main_hung": process_health.quick_hung_check(pid), "browser_windows": len(windows),
            "browser_hung": len(_hung_elsewhere(browser))}


def _browser_record(state, opened, warning=None, **extra):
    mains = state.get("main_threads") or []
    return {"ensured": _main_browser_ready(state), "opened": opened, "main_thread": mains[0] if mains else None,
            "windows": [{k: w.get(k) for k in ("thread", "main_thread", "visible", "hung")}
                        for w in state.get("windows") or []], "warning": warning, **extra}


def _browser_evidence(state):
    return ", ".join("'%s'%s on %s thread %s %s" % (
        process_health.COSMOS_BROWSER_TITLE, "" if w.get("visible") else " (hidden)",
        "Max's main" if w["main_thread"] else "a separate", w.get("thread"), "hung" if w["hung"] else "responding")
        for w in state.get("windows") or []) or "no 'Chaos Cosmos Browser' window"


def _wait_main_browser(pid):
    """OS-level only, bounded: wait for a responsive main-thread Cosmos browser."""
    started, streak = time.monotonic(), 0
    while True:
        state = _browser_state(pid)
        streak = streak + 1 if _main_browser_ready(state) else 0
        if streak >= _BROWSER_CHECKS or time.monotonic() - started >= _BROWSER_WAIT_S:
            return state
        time.sleep(_BROWSER_INTERVAL_S)


def _on_main(state):
    return any(w["main_thread"] for w in state.get("windows") or [])


def _ensure_browser(client, pid, renderer, state):
    """Make sure a responsive "Chaos Cosmos Browser" is owned by Max's main thread.

    No bridge call when one already is, or when one exists there but Max's main
    thread is busy (OS-level wait instead). Otherwise a fresh OS check, then ONE
    call runs the renderer's Cosmos browser action, then an OS-level wait.
    Returns (record, warnings, state); record["refused"] when the fresh check
    found a hung separate-thread browser (no action sent). Lost calls and transport
    errors (MaxHealthError, RequestOutcomeUnknown, OSError) propagate.
    """
    had_other = any(not w["main_thread"] for w in state.get("windows") or [])
    action = None
    if not _main_browser_ready(state) and not _on_main(state):
        state = _browser_state(pid)  # the first check predates selecting and guarding this instance
        if _hung_elsewhere(state):
            return _browser_record(state, False, refused=True), [], state
    if _main_browser_ready(state):
        pass
    elif _on_main(state):  # there, but Max's main thread is busy: running the action would only queue
        state = _wait_main_browser(pid)
    else:
        try:
            action = _max(client, _open_browser_script(renderer))
        except (MaxHealthError, RequestOutcomeUnknown, OSError):
            raise
        except Exception as exc:
            action = {"found": False, "executed": False, "error": str(exc)}
        if action.get("found"):  # some actions report not executed after doing their work
            state = _wait_main_browser(pid)
    warnings = []
    if had_other:
        warnings.append("A 'Chaos Cosmos Browser' is also open on a separate (non-main) thread; the importer "
                        "may use it, the path that stalled and deadlocked Max.")
    extra = {} if action is None else {"action": action}
    record = _browser_record(state, bool(action and action.get("executed")), **extra)
    if not record["ensured"]:
        if action is None:
            why = "the browser on Max's main thread did not respond within %g s" % _BROWSER_WAIT_S
        elif action.get("error"):
            why = "its action failed: %s" % action["error"]
        elif action.get("found"):
            why = "its action %s, but no responsive browser appeared on Max's main thread within %g s" % (
                "ran" if action.get("executed") else "'%s' in table '%s' reported not executed" % (
                    action.get("description"), action.get("table")), _BROWSER_WAIT_S)
        else:
            why = "no Cosmos browser action found for this renderer"
        warnings.append("Could not open the Cosmos browser on Max's main thread (%s). The importer uses a hidden "
                        "browser on its own thread, the path that stalled and deadlocked Max; the Material Editor "
                        "renderer is switched to Scanline for this import as a mitigation." % why)
    record["warning"] = " ".join(warnings) or None
    return record, warnings, state


def _dispatch_gate(pid):
    """OS-level only, bounded, right before _PREPARE (earlier checks predate the
    browser action and its wait). Returns (browser state, main_busy) as soon as a
    separate-thread browser is hung or Max's main window answers."""
    started = time.monotonic()
    while True:
        state = _browser_state(pid)
        busy = _main_window_busy(pid)
        if _hung_elsewhere(state) or not busy or time.monotonic() - started >= _BROWSER_WAIT_S:
            return state, busy
        time.sleep(_BROWSER_INTERVAL_S)


def _busy_refusal(base, pid, state, pre, record, main_busy=False):
    """Nothing dispatched: a separate-thread Cosmos browser is hung, or Max's main
    window does not answer. Guards the PID (requests refused, nothing sent) while
    those windows stay hung."""
    hung = _hung_elsewhere(state)
    windows = {w["hwnd"]: process_health.COSMOS_BROWSER_TITLE for w in hung if w.get("hwnd")}
    for hwnd in process_health.main_windows(pid):
        windows.setdefault(hwnd, "")
    evidence = ("Max's main window does not respond; " if main_busy else "") + _browser_evidence(state)
    if windows:
        mark_settling(pid, windows, "a hung Cosmos browser" if hung else "a Cosmos import", evidence,
                      _SETTLING_GUARD_S)
    sent = "the Cosmos browser action was sent, nothing else" if "action" in record else "nothing was sent"
    if hung:
        advice = ("Do not open the Cosmos browser or open/close any window now (activating a hung window on "
                  "another thread is the deadlock). " + HANG_ADVICE)
    else:
        advice = _WAIT_ADVICE
    return {**base, "state": "browser_hung" if hung else "not_imported", "code": "IMPORT_SETTLING",
            "dispatched": False, "safe_to_edit": False, "retryable": True, "evidence": evidence,
            "import_timing": {"pre_dispatch": pre}, "cosmos_browser": record,
            "next": "Nothing was imported (%s): %s. Requests to this Max are refused with IMPORT_SETTLING (nothing "
                    "sent) while it stays hung. %s Then retry." % (sent, evidence, advice)}


def _main_window_busy(pid):
    """OS-level gate before a poll. IsHungAppWindow lags ~5 s, so a short WM_NULL
    round trip to the main window(s) also catches a stall that just started."""
    if process_health.quick_hung_check(pid) is True:
        return True
    for hwnd in process_health.main_windows(pid):
        state = process_health.window_responsive(hwnd, _POLL_GATE_MS)
        if state.get("exists") and state.get("responds") is False:
            return True
    return False


def _wait_import(client, asset, before, renderer, pid, timeout=_DETECT_TIMEOUT_S):
    """Detect the new resource with light, backed-off, read-only polls.

    Cosmos acknowledges dispatch before the host finishes creating resources.
    No poll is sent while Max's main window is hung or not answering; the loop
    waits instead. Polls are dropped at their deadline; the first dropped poll
    ends detection (error in the result) so requests never pile up on Max.
    A material or map counts by handle (absent before dispatch), whatever its name.
    """
    expected = _EXPECTED.get(asset["kind"])
    timing = {"detected": False, "after_s": 0.0, "polls": 0, "skipped_hung": 0, "probe": None}
    if not expected:
        return timing
    old = _known(before, expected)
    started = time.monotonic()
    interval = 0.5
    while True:
        time.sleep(interval)
        elapsed = time.monotonic() - started
        timing["after_s"] = round(elapsed, 1)
        if _main_window_busy(pid):
            timing["skipped_hung"] += 1
        else:
            timing["polls"] += 1
            try:
                # Renderer material classes every other poll after ~4 s (Slate mode misses slots).
                probe = _asset_snapshot(client, asset, light=True, renderer=renderer,
                                        scan_classes=elapsed >= 4 and timing["polls"] % 2 == 0,
                                        timeout=_POLL_TIMEOUT_S, probe=True, known=before.get("known"))
            except (MaxHealthError, RequestOutcomeUnknown) as exc:
                timing["error"] = exc
                timing["after_s"] = round(time.monotonic() - started, 1)
                return timing
            timing["probe"] = probe
            if any(item["handle"] not in old for item in probe.get(expected, [])):
                timing["detected"] = True
                return timing
        if time.monotonic() - started >= timeout:
            return timing
        interval = min(interval * 1.5, 3.0)


def _settle(pid, seconds, baseline_cpu):
    """OS-level only (no bridge calls): wait until Max's main window and every
    "Chaos Cosmos Browser" window (hidden included) respond for several checks."""
    threshold = None if baseline_cpu is None else round(max(0.5, baseline_cpu * 1.5 + 0.25), 2)
    return process_health.wait_responsive(
        pid, max(seconds, _SETTLE_FLOOR_S), titles=(process_health.COSMOS_BROWSER_TITLE,), checks=_SETTLE_CHECKS,
        interval=_SETTLE_INTERVAL_S, timeout_ms=_SETTLE_MESSAGE_TIMEOUT_MS, cpu_threshold=threshold,
        min_span_s=_SETTLE_MIN_SPAN_S, cpu_grace_s=_SETTLE_CPU_GRACE_S)


def _settle_summary(settle):
    return {key: settle.get(key) for key in ("quiet", "windows_quiet", "waited_s", "checks", "streak",
                                             "responsive_streak", "required_checks", "cpu_cores", "cpu_threshold",
                                             "state")} | {"evidence": process_health.describe_windows(settle)}


def _guard_windows(pid, settle):
    """{hwnd: title} for the settling guard: the settle windows plus the PID's
    current main and Cosmos browser windows (OS-level only)."""
    windows = {s["hwnd"]: ("" if s.get("role") == "main" else (s.get("title") or ""))
               for s in (settle or {}).get("windows") or [] if s.get("exists")}
    for hwnd in process_health.main_windows(pid):
        windows.setdefault(hwnd, "")
    for hwnd in process_health.find_windows(pid, process_health.COSMOS_BROWSER_TITLE):
        windows.setdefault(hwnd, process_health.COSMOS_BROWSER_TITLE)
    return windows


def _known(before, kind):
    """Handles that existed before dispatch: the reported items plus the recorded handle set."""
    handles = {str(x["handle"]) for x in before.get(kind, [])}
    handles.update(str(h) for h in (before.get("known") or {}).get(kind) or ())
    return handles


def _resources(before, after):
    resources = {}
    for kind in ("nodes", "materials", "maps"):
        old = _known(before, kind)
        resources[kind] = []
        for item in after.get(kind, []):
            item = dict(item)
            item["created"] = str(item["handle"]) not in old
            slot, sub = item.pop("slot", None), item.pop("sub", False)
            if kind == "materials":
                item["medit_slot"] = slot or None
                item["sub_material"] = bool(sub)  # a sub-material of another listed material
            if kind == "nodes":
                item["node_ref"] = {"handle": int(item["handle"]), "name": item["name"]}
            filename = item.get("filename")
            if filename:
                item["file_exists"] = Path(filename).is_file()
            else:
                item.pop("filename", None)
            resources[kind].append(item)
    return resources


def _primary(response, asset, probe=None):
    """Name the main created material/map. Preference: named like the asset, then seen by the
    detecting poll (probe; created during the import itself, not later by hand), then not a
    sub-material, then in a Material Editor slot, then the first. Every created one stays
    listed; nothing is renamed."""
    expected = _EXPECTED.get(asset["kind"])
    if expected not in ("materials", "maps"):
        return
    response["asset_name"] = asset["name"]
    created = [item for item in response.get(expected) or [] if item.get("created")]
    if not created:
        return
    key, label = _compact(asset["name"]), expected[:-1]
    early = {str(i.get("handle")) for i in (probe or {}).get(expected) or ()}
    reasons = (("name", lambda i: key in _compact(i.get("name", ""))),
               ("detected_during_import", lambda i: str(i.get("handle")) in early),
               ("top_level", lambda i: not i.get("sub_material")),
               ("medit_slot", lambda i: bool(i.get("medit_slot"))))
    primary = min(created, key=lambda i: tuple(not test(i) for _, test in reasons))
    reason = ("only_new" if len(created) == 1
              else next((name for name, test in reasons if test(primary) and not all(test(i) for i in created)),
                        "first"))
    response["primary_" + label] = {k: primary[k] for k in ("handle", "name", "class", "medit_slot") if k in primary}
    response["primary_reason"] = reason
    response[label + "_name"] = primary.get("name")
    if key not in _compact(primary.get("name", "")):
        response["note"] = "The package %s is named '%s', not '%s' (not renamed)." % (
            label, primary.get("name"), asset["name"])
        top = [i for i in created if not i.get("sub_material")]
        if len(top) > 1:  # a name match needs no guess; sub-materials do not count
            response["note"] += (" %d new top-level %s appeared since the import started (some may be made by "
                                 "hand meanwhile); picked by %s, all are listed in %s." % (
                                     len(top), expected, reason, expected))


def _set_slot(response, handle, slot):
    """Correct the reported medit_slot of the listed material with this handle."""
    for item in (response.get("materials") or []) + [response.get("primary_material") or {}]:
        if handle is not None and str(item.get("handle")) == str(handle):
            item["medit_slot"] = slot or None


def _slot_guard(response, prep, fin, warnings, ran=True):
    """Issue #10: report the active Material Editor slot guard and the primary material's
    final medit_slot. prep/fin: the medit_slot records of _PREPARE/_FINALIZE; ran=False
    when finalize has not run yet."""
    _slot_record(response, prep, fin, warnings, ran)
    primary = response.get("primary_material")
    if primary:
        response["medit_slot"] = primary.get("medit_slot")


def _slot_record(response, prep, fin, warnings, ran):
    if not prep:
        return
    switched = prep.get("free_slot") if prep.get("switched") else None
    record = {"original": prep.get("active") or None, "material": prep.get("material"),
              "kept": bool(prep.get("keep")), "switched_to": switched, "mode": prep.get("mode") or None}
    response["medit_active_slot"] = record
    if prep.get("error"):
        warnings.append("Could not check the active Material Editor slot: %s" % prep["error"])
    if prep.get("stale"):
        warnings.append("An earlier import's Material Editor slot record was never finalized; its slot switch was "
                        "undone and its material kept alive in the MAXScript global mcp_cosmosMeditDisplaced.")
    if not prep.get("keep"):
        record["state"] = "not_needed"  # the active slot held an unused default material
        return
    if not ran:
        record["state"] = "pending"
        return
    if not fin:
        record["state"] = "no_record"
        if switched:
            warnings.append("The active Material Editor slot may still be %s; set it back with execute_maxscript: "
                            "activeMeditSlot = %s" % (switched, prep.get("active")))
        return
    if fin.get("busy"):
        record["state"] = "pending"  # an undo transaction was open; the record is kept
        warnings.append("The Material Editor slot was not restored yet (an undo transaction was open): run "
                        "pending_restore.maxscript once with execute_maxscript.")
        return
    if fin.get("stale"):
        record["state"] = "stale"
        kept = fin.get("material")
        warnings.append("The scene changed since the import (reset or another file), so the Material Editor slots "
                        "were left as they are%s." % (
                            "; '%s' is kept alive in the MAXScript global mcp_cosmosMeditDisplaced" % kept.get("name")
                            if kept else ""))
        return
    if "original" not in fin:
        record["state"] = "error"
        warnings.append("Could not restore the Material Editor slot (%s)%s." % (
            fin.get("error"), "; set the active slot back with execute_maxscript: activeMeditSlot = %s"
            % prep.get("active") if switched else ""))
        return
    original, displaced = fin["original"], fin.get("displaced")
    record["active_restored"] = fin.get("active_restored")
    error = fin.get("error") or ""
    record["state"] = "kept"
    if displaced:
        response["displaced_material"] = {**displaced, "slot": original}
        response["medit_slot_restored"] = bool(fin.get("restored"))
        occupant = fin.get("occupant") or {}
        if fin.get("restored"):
            record["state"] = "restored"
            _set_slot(response, occupant.get("handle"), fin.get("moved_to"))
            _set_slot(response, displaced.get("handle"), original)
        else:
            record["state"] = "displaced"
            why = (error or ("no free (unused default) slot for the imported material" if fin.get("imported_occupant")
                             else "the slot's new material is not recognised as this import's"))
            handle = str(displaced.get("handle") or "")
            how = ("meditMaterials[%s] = getAnimByHandle %sL" % (original, handle) if handle.isdigit()
                   else "meditMaterials[%s] = <its entry in mcp_cosmosMeditDisplaced>" % original)
            warnings.append(
                "The importer replaced '%s' in Material Editor slot %s with '%s'; it was not put back (%s). It is kept "
                "alive in the MAXScript global mcp_cosmosMeditDisplaced. To restore it, first assign or re-slot '%s' "
                "(an unassigned material removed from every slot can be deleted), then run with execute_maxscript: "
                "%s" % (displaced.get("name"), original, occupant.get("name"), why, occupant.get("name"), how))
    if fin.get("switched_to") and fin.get("active_restored") is False:
        warnings.append("Could not make Material Editor slot %s active again%s; run with execute_maxscript: "
                        "activeMeditSlot = %s" % (original, " (%s)" % error if error else "", original))
    elif error and record["state"] != "displaced":
        warnings.append("Material Editor slot restore reported: %s" % error)


def _health_failure(exc):
    details = getattr(exc, "details", None) or {}
    return {"code": getattr(exc, "code", None), "message": str(exc),
            "process": details.get("process"), "request_sent": details.get("request_sent")}


def _prepare_failed(base, pid, exc, restore_medit_renderer, swap=True, step="preparation"):
    """A pre-dispatch call was sent but its result was lost: nothing was dispatched.
    preparation: the selection may be cleared (and, with swap, medit switched to
    Scanline). browser: only the Cosmos browser action may have run."""
    lost = isinstance(exc, (MaxHealthError, RequestOutcomeUnknown))
    if lost:
        windows = _guard_windows(pid, None)
        if windows:
            mark_settling(pid, windows, "a Cosmos import", "%s call lost" % step, _SETTLING_GUARD_S)
    restore_medit_renderer = restore_medit_renderer and swap and step == "preparation"
    if step == "browser":
        warning = "The Cosmos browser action may have run; nothing else was changed."
    else:
        warning = ("The preparation call may have run: the selection may be cleared%s and the active Material "
                   "Editor slot switched (undo that once with execute_maxscript: %s). %sInspect the scene before "
                   "retrying the import." % (
                       " and the Material Editor renderer switched to Scanline" if swap else "",
                       _SLOT_ACTIVE_RESTORE,
                       "Once Max responds, run pending_restore.maxscript once (it reports nothing_to_restore "
                       "if nothing was switched). " if restore_medit_renderer else ""))
    response = {**base, "state": "not_imported", "dispatched": False, "safe_to_edit": not lost,
                "message": str(exc), "warnings": [warning],
                "next": "Nothing was imported. " + (_WAIT_ADVICE if lost else "Retry the import.")}
    if lost:
        response["health"] = _health_failure(exc)
    if restore_medit_renderer:
        response["pending_restore"] = {"selection": None, "restore_medit_renderer": True,
                                       "maxscript": _MEDIT_RESTORE}
    return response


def import_asset(client, package_id, wait_seconds, renderer, settle_seconds=SETTLE_SECONDS_DEFAULT,
                 restore_medit_renderer=True, swap_medit_renderer=False):
    if (not isinstance(settle_seconds, int) or isinstance(settle_seconds, bool)
            or not 0 <= settle_seconds <= SETTLE_SECONDS_MAX):
        raise ValueError("settle_seconds must be an integer from 0 to %d" % SETTLE_SECONDS_MAX)
    if not _import_lock.acquire(blocking=False):
        raise CosmosError("A Cosmos import is already in progress.", "USER_BUSY", True)
    operation_client = import_guard = pid = None
    try:
        service, importer = _context(client, renderer)
        asset = _download(service, package_id, wait_seconds)
        result = _summary(service, asset)
        if result["state"] != "ready":
            result.update(state="queued" if asset["availability"] == 2 else "downloading", next="Call cosmos_import with this asset_id when ready.")
            return result
        pid = importer["pid"]
        _snapshot_script(asset)  # validate the asset identity before touching Max
        browser_state = _browser_state(pid)
        pre = _pre_dispatch_state(pid, browser_state)
        if pre["main_hung"]:
            raise CosmosError("Max (PID %s) is not responding: its main window hung. Nothing was sent and nothing "
                              "was imported. %s" % (pid, _WAIT_ADVICE), "IMPORT_SETTLING", True)
        if pre["browser_hung"]:
            # A browser thread still blocked by an earlier import plus main-thread work
            # (e.g. the browser action activating it) is the cross-thread deadlock shape.
            return _busy_refusal({**result, "renderer": importer["renderer"], "max_pid": pid}, pid, browser_state,
                                 pre, _browser_record(browser_state, False))
        operation_client = MaxClient()
        operation_client.select_max_instance(pid)
        client = operation_client
        if client.get_selected_max_instance().get("target_pid") != pid:
            raise CosmosError("Selected Max instance changed before import.", "COSMOS_TARGET_CHANGED")
        # Until this returns, other requests to this Max are refused (IMPORT_SETTLING, nothing sent).
        import_guard = mark_settling(pid, {}, "a Cosmos import", None, _SETTLING_GUARD_S, owner=client)
        base = {**result, "renderer": importer["renderer"], "max_pid": pid, "repeat_safe": False}
        try:
            browser, warnings, ensured_state = _ensure_browser(client, pid, importer["renderer"], browser_state)
        except (MaxHealthError, RequestOutcomeUnknown) as exc:
            if (getattr(exc, "details", None) or {}).get("request_sent") is False:
                raise  # nothing was sent, so nothing changed
            return {**_prepare_failed(base, pid, exc, restore_medit_renderer, step="browser"),
                    "cosmos_browser": _browser_record(browser_state, False, warning="browser action call lost")}
        if browser.get("refused"):
            return _busy_refusal(base, pid, ensured_state, pre, browser)
        gate, main_busy = _dispatch_gate(pid)  # the checks above predate the action and its wait
        if _hung_elsewhere(gate) or main_busy:
            return _busy_refusal(base, pid, gate, pre, browser, main_busy)
        # Not ensured means the hidden-browser path: Scanline avoids the V-Ray slot-preview stall there.
        swap = bool(swap_medit_renderer) or not browser["ensured"]
        swap_mode = "requested" if swap_medit_renderer else ("fallback" if swap else "off")
        try:
            prepared = _max(client, _prepare_script(asset, swap))
        except (MaxHealthError, RequestOutcomeUnknown, ValueError) as exc:
            if (getattr(exc, "details", None) or {}).get("request_sent") is False:
                raise  # nothing was sent, so nothing changed
            return {**_prepare_failed(base, pid, exc, restore_medit_renderer, swap), "cosmos_browser": browser}
        before, selected, medit = prepared["before"], prepared["selection"], prepared["medit"]
        slot_prep = prepared.get("medit_slot") or {}
        failure, health = None, None
        if swap and not medit.get("scanline_available"):
            warnings.append("Default_Scanline_Renderer is unavailable, so the Material Editor renderer was not "
                            "switched; the importer's slot preview may stall Max with V-Ray.")
        elif swap and medit.get("error"):
            warnings.append("Could not switch the Material Editor renderer to Scanline: %s" % medit["error"])
        if not swap and medit.get("leftover_backup"):
            warnings.append("An earlier import left the Material Editor renderer at Scanline. Once the Material "
                            "Editor is closed, restore it with execute_maxscript: %s" % _MEDIT_RESTORE)
        restore_pending = swap and bool(medit.get("backup_pending"))
        baseline_cpu = process_health.cpu_cores(pid, 0.5)
        detection = {"detected": False, "probe": None}
        try:
            # Native Chaos importer owns its normal host undo/import behavior.
            # Never hold Max's main thread or a theHold transaction across this RPC.
            service.timeout = 30
            service.import_asset(asset["id"], asset["revision"])
            detection = _wait_import(client, asset, before, importer["renderer"], pid)
        except MaxHealthError as exc:
            failure, health = str(exc), _health_failure(exc)
        except Exception as exc:
            failure = str(exc)
        poll_error = detection.pop("error", None)
        if poll_error is not None:
            failure, health = str(poll_error), _health_failure(poll_error)
        settle = _settle(pid, settle_seconds, baseline_cpu)
        restore_medit = restore_medit_renderer and restore_pending
        pending = {"selection": selected, "restore_medit_renderer": restore_medit,
                   "maxscript": _finalize_script(selected, restore_medit, _newest_known(before))}
        also = ((" and Material Editor renderer" if restore_medit else "")
                + (" and Material Editor slot" if slot_prep.get("keep") else ""))
        quiet = bool(settle.get("quiet") or settle.get("windows_quiet"))
        after = finished = lost = None
        if quiet:
            if not settle.get("quiet"):
                warnings.append("Max's windows respond, but its CPU was still busy (%s cores) after %s s; "
                                "textures may still be loading." % (settle.get("cpu_cores"), settle.get("waited_s")))
            step = "confirm"
            try:
                after = _asset_snapshot(client, asset, known=before.get("known"))
                step = "restore"
                finished = _max(client, pending["maxscript"])
            except (MaxHealthError, RequestOutcomeUnknown) as exc:  # Max stopped responding again
                health, failure, lost, quiet = _health_failure(exc), str(exc), step, False
            except Exception as exc:
                warnings.append("Could not confirm the import or restore the selection: %s" % exc)
        timing = {k: v for k, v in detection.items() if k != "probe"}
        response = {**base, "import_timing": {**timing, "pre_dispatch": pre, "settle": _settle_summary(settle)},
                    "medit_renderer": {k: medit.get(k) for k in ("class", "locked", "editor_open", "swapped")}
                    | {"swap": swap_mode},
                    "cosmos_browser": browser}
        if health:
            response["health"] = health
        if failure:
            response["message"] = failure
        if not quiet:
            # Refuse further requests (nothing sent) while a remembered window stays hung.
            windows = _guard_windows(pid, settle)
            if windows:
                mark_settling(pid, windows, "a Cosmos import", process_health.describe_windows(settle),
                              _SETTLING_GUARD_S)
            seen = after if after is not None else detection.get("probe")
            if seen:
                response.update(_resources(before, seen))
                _primary(response, asset, detection.get("probe"))
            _slot_guard(response, slot_prep, None, warnings, ran=False)
            warnings.append("Selection%s not restored yet: once get_bridge_status reports Max responding, run "
                            "pending_restore.maxscript once with execute_maxscript." % also)
            why = ("Max is still busy after the import (%s)." % process_health.describe_windows(settle)
                   if lost is None else "Max stopped responding while %s." % (
                       "confirming the import" if lost == "confirm" else
                       "restoring the selection/Material Editor renderer and slot; that restore may already "
                       "have run (running pending_restore once more is harmless)"))
            created = any(i.get("created") for i in response.get(_EXPECTED.get(asset["kind"]) or "", []))
            response.update(state="settling", detected=bool(detection.get("detected")) or created,
                            safe_to_edit=False, pending_restore=pending, warnings=warnings,
                            next="%s Do not edit the scene yet; requests to this Max are refused with "
                                 "IMPORT_SETTLING (nothing sent) while a window stays hung. %s" % (why, _WAIT_ADVICE))
            return response
        expected = _EXPECTED.get(asset["kind"])
        observed = False
        if after is not None:
            response.update(_resources(before, after))
            _primary(response, asset, detection.get("probe"))
            observed = bool(expected) and any(item["created"] for item in response[expected])
        if finished is None:
            response["pending_restore"] = pending
            warnings.append("Selection%s not restored: run pending_restore.maxscript once with execute_maxscript."
                            % also)
        else:
            if not finished.get("selection_restored"):
                warnings.append("The previous selection was not restored (an undo transaction was open).")
            medit_state = finished.get("medit")
            if restore_pending and medit_state != "restored":
                if medit_state == "stale":
                    warnings.append("The Material Editor renderer was left as is: it changed since the import "
                                    "(another scene, or set by hand).")
                else:
                    reason = {"not_requested": "restore_medit_renderer is false",
                              "editor_open": "a Material Editor is open; restoring would re-render its slots"
                              }.get(medit_state, medit_state)
                    warnings.append("The Material Editor renderer is still Scanline (%s). Once the Material Editor "
                                    "is closed, restore it with execute_maxscript: %s" % (reason, _MEDIT_RESTORE))
        slot_fin = None if finished is None else finished.get("medit_slot")
        _slot_guard(response, slot_prep, slot_fin, warnings, ran=finished is not None)
        if (slot_fin or {}).get("busy"):
            response["pending_restore"] = pending
        response.update(state="imported" if observed else ("import_unknown" if failure or after is None
                                                           else "imported_unverified"),
                        safe_to_edit=True)
        if not observed and (failure or after is None):
            response["next"] = "Inspect the requested asset in Max before retrying."
        if warnings:
            response["warnings"] = warnings
        return response
    finally:
        try:
            release_settling(pid, import_guard)
            if operation_client is not None:
                operation_client.release_max_instance()
        finally:
            _import_lock.release()
