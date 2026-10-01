"""Cosmos orchestration; network I/O remains in the external MCP process."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .. import process_health
from ..max_client import MaxClient, MaxHealthError, RequestOutcomeUnknown, mark_settling, release_settling

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
# True when the Compact or the Slate Material Editor is open (both render slots with renderers.medit).
_EDITOR_OPEN = "((try(MatEditor.isOpen())catch(true)) or (try(SME.isOpen())catch(false)))"
_WAIT_ADVICE = ("Stalls like this (V-Ray rendering the Material Editor preview, or the Cosmos importer) "
                "cleared on their own after 5-8 min: wait, check with get_bridge_status, and do not end Max. "
                "Do not open or close the Material Editor meanwhile.")


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
_ASSET_SNAPSHOT = r"""(
 fn compactName value = (
  (dotNetClass "System.Text.RegularExpressions.Regex").Replace (toLower(value as string)) "[^a-z0-9]" ""
 )
 local key="__KEY__"
 local aid="__ASSET__"
 fn matchesName value key = ((findString (compactName value) key)!=undefined)
 fn fileOf value = (
  local f=try(value.filename as string)catch("")
  if f=="" do f=try(value.HDRIMapName as string)catch("")
  f
 )
 fn quoteJSON value = ("\"" + (MCP_Server.escapeJsonString(value as string)) + "\"")
 fn refJSON value isNode = (
  local h=if isNode then value.handle as string else (getHandleByAnim value) as string
  "{\"handle\":"+(quoteJSON h)+",\"name\":"+(quoteJSON(try(value.name)catch("")))+",\"class\":"+(quoteJSON(classof value))+",\"filename\":"+(quoteJSON(fileOf value))+"}"
 )
 fn joinJSON values = (
  local s="["
  for i=1 to values.count do (if i>1 do s+=","; s+=values[i])
  s+"]"
 )
 local nodes=for n in objects where (try(n.cosmosAssetId==aid)catch(false)) collect n
 local mats=#()
 for n in nodes where n.material!=undefined do appendIfUnique mats n.material
 try(
  for m in meditMaterials where m!=undefined and (matchesName m.name key) do appendIfUnique mats m
 )catch()
 for c in __MAT_CLASSES__ do try(
  for m in (getClassInstances c processAllAnimatables:true) where (matchesName m.name key) do appendIfUnique mats m
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
   for m in (getClassInstances c processAllAnimatables:true) where ((matchesName m.name key) or (matchesName (fileOf m) key)) do appendIfUnique maps m
  )catch()
 )
 "{\"nodes\":"+(joinJSON(for n in nodes collect(refJSON n true)))+",\"materials\":"+(joinJSON(for m in mats collect(refJSON m false)))+",\"maps\":"+(joinJSON(for m in maps collect(refJSON m false)))+"}"
)"""

_RENDERER_MATERIAL_CLASSES = (
    '(for c in material.classes where (matchPattern (c as string) pattern:"__PREFIX__*" '
    'or (c as string)=="Multimaterial") collect c)')


def _snapshot_script(asset, light=False, renderer="", scan_classes=False):
    # Match this asset only. Do not snapshot unrelated scene/material metadata.
    key = _compact(asset["name"])
    if len(key) < 4:
        raise CosmosError("Asset has no usable identity for import verification.")
    classes, scan_maps = "material.classes", "true"
    if light:
        prefix = "Corona" if _compact(renderer).startswith("corona") else "VRay"
        classes = (_RENDERER_MATERIAL_CLASSES.replace("__PREFIX__", prefix)
                   if scan_classes and asset["kind"] == "material" else "#()")
        scan_maps = "true" if asset["kind"] == "hdri" else "false"
    return (_ASSET_SNAPSHOT.replace("__KEY__", key).replace("__ASSET__", normalize_id(asset["id"]))
            .replace("__MAT_CLASSES__", classes).replace("__SCAN_MAPS__", scan_maps))


def _asset_snapshot(client, asset, light=False, renderer="", scan_classes=False, timeout=None, probe=False):
    """Full scan by default. light=True checks nodes, Material Editor slots and
    (for HDRIs) maps; scan_classes adds the renderer's own material classes."""
    return _max(client, _snapshot_script(asset, light, renderer, scan_classes), timeout=timeout, probe=probe)


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

# ONE bridge call before dispatch: baseline snapshot, selection, Material Editor
# renderer state, then the swap to Scanline (V-Ray rendering the imported
# material's slot preview blocked Max's main thread for minutes).
_PREPARE = r"""(
 global mcp_cosmosMeditBackup
 if theHold.Holding() do throw "USER_BUSY"
 local snap=__SNAPSHOT__
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
 if scanline!=undefined do (
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
 )
 clearSelection()
 "{\"before\":"+snap+",\"selection\":"+selJSON+",\"medit\":{\"class\":"+(jsonStr meditClass)+",\"instance\":"+(jsonStr meditName)+",\"locked\":"+(jsonBool locked)+",\"editor_open\":"+(jsonBool editorOpen)+",\"scanline_available\":"+(jsonBool (scanline!=undefined))+",\"swapped\":"+(jsonBool swapped)+",\"backup_pending\":"+(jsonBool pending)+",\"error\":"+(jsonStr swapError)+"}}"
)"""

# After Max is quiet: restore the selection, then (only if the Material
# Editor is closed) the recorded Material Editor renderer. Never opens,
# closes or activates any window. Safe to run twice.
_FINALIZE = r"""(
 global mcp_cosmosMeditBackup
 local restoredSelection=false
 if not theHold.Holding() do (
  select (for h in #(__HANDLES__) where (maxOps.getNodeByHandle h)!=undefined collect (maxOps.getNodeByHandle h))
  restoredSelection=true
 )
 local medit=if __RESTORE__ then __MEDIT_RESTORE__ else "not_requested"
 local editorOpen=try(__EDITOR_OPEN__)catch(undefined)
 "{\"selection_restored\":"+(if restoredSelection then "true" else "false")+",\"medit\":\""+(MCP_Server.escapeJsonString(medit as string))+"\",\"editor_open\":"+(if editorOpen==true then "true" else if editorOpen==false then "false" else "null")+"}"
)"""


def _prepare_script(asset):
    return _PREPARE.replace("__SNAPSHOT__", _snapshot_script(asset)).replace("__EDITOR_OPEN__", _EDITOR_OPEN)


def _finalize_script(handles, restore_medit):
    values = ",".join(str(int(h)) for h in handles)
    return (_FINALIZE.replace("__HANDLES__", values)
            .replace("__RESTORE__", "true" if restore_medit else "false")
            .replace("__MEDIT_RESTORE__", _MEDIT_RESTORE)
            .replace("__EDITOR_OPEN__", _EDITOR_OPEN))


def _pre_dispatch_state(pid):
    """OS-level only (nothing sent): is Max's main window or a "Chaos Cosmos
    Browser" window (hidden included) already hung before this import?"""
    browser = process_health.find_windows(pid, process_health.COSMOS_BROWSER_TITLE)
    return {"main_hung": process_health.quick_hung_check(pid), "browser_windows": len(browser),
            "browser_hung": sum(1 for h in browser if process_health.window_hung(h) is True)}


def _before_dispatch(pid, prepared):
    """PRE-DISPATCH HOOK (intentionally a no-op).

    The place for an OS-level step between the preparation call and the Cosmos
    import RPC, e.g. the untested idea of showing the hidden "Chaos Cosmos
    Browser" so its throttled Qt page does not stall the import handshake.
    Must not send bridge calls. Returns an optional note for the result.
    """
    return None


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
    """
    expected = _EXPECTED.get(asset["kind"])
    timing = {"detected": False, "after_s": 0.0, "polls": 0, "skipped_hung": 0, "probe": None}
    if not expected:
        return timing
    old = {item["handle"] for item in before.get(expected, [])}
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
                                        timeout=_POLL_TIMEOUT_S, probe=True)
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


def _resources(before, after):
    resources = {}
    for kind in ("nodes", "materials", "maps"):
        old = {x["handle"] for x in before.get(kind, [])}
        resources[kind] = []
        for item in after.get(kind, []):
            item = dict(item)
            item["created"] = item["handle"] not in old
            if kind == "nodes":
                item["node_ref"] = {"handle": int(item["handle"]), "name": item["name"]}
            filename = item.get("filename")
            if filename:
                item["file_exists"] = Path(filename).is_file()
            else:
                item.pop("filename", None)
            resources[kind].append(item)
    return resources


def _health_failure(exc):
    details = getattr(exc, "details", None) or {}
    return {"code": getattr(exc, "code", None), "message": str(exc),
            "process": details.get("process"), "request_sent": details.get("request_sent")}


def _prepare_failed(base, pid, exc, restore_medit_renderer):
    """The one pre-dispatch call was sent but its result was lost: nothing was
    dispatched, yet the selection may be cleared and medit switched to Scanline."""
    lost = isinstance(exc, (MaxHealthError, RequestOutcomeUnknown))
    if lost:
        windows = _guard_windows(pid, None)
        if windows:
            mark_settling(pid, windows, "a Cosmos import", "preparation call lost", _SETTLING_GUARD_S)
    response = {**base, "state": "not_imported", "dispatched": False, "safe_to_edit": not lost,
                "message": str(exc),
                "warnings": ["The preparation call may have run: the selection may be cleared and the Material "
                             "Editor renderer switched to Scanline. %sInspect the scene before retrying the import."
                             % ("Once Max responds, run pending_restore.maxscript once (it reports nothing_to_restore "
                                "if nothing was switched). " if restore_medit_renderer else "")],
                "next": "Nothing was imported. " + (_WAIT_ADVICE if lost else "Retry the import.")}
    if lost:
        response["health"] = _health_failure(exc)
    if restore_medit_renderer:
        response["pending_restore"] = {"selection": None, "restore_medit_renderer": True,
                                       "maxscript": _MEDIT_RESTORE}
    return response


def import_asset(client, package_id, wait_seconds, renderer, settle_seconds=SETTLE_SECONDS_DEFAULT,
                 restore_medit_renderer=True):
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
        pre = _pre_dispatch_state(pid)
        if pre["main_hung"] or pre["browser_hung"]:
            # A browser thread still blocked by an earlier import plus new main-thread
            # work is the cross-thread deadlock shape: do not start another import.
            raise CosmosError("Max (PID %s) is not responding: %s hung. Nothing was sent and nothing was imported. %s"
                              % (pid, "its main window" if pre["main_hung"] else "its 'Chaos Cosmos Browser' window",
                                 _WAIT_ADVICE), "IMPORT_SETTLING", True)
        operation_client = MaxClient()
        operation_client.select_max_instance(pid)
        client = operation_client
        if client.get_selected_max_instance().get("target_pid") != pid:
            raise CosmosError("Selected Max instance changed before import.", "COSMOS_TARGET_CHANGED")
        # Until this returns, other requests to this Max are refused (IMPORT_SETTLING, nothing sent).
        import_guard = mark_settling(pid, {}, "a Cosmos import", None, _SETTLING_GUARD_S, owner=client)
        base = {**result, "renderer": importer["renderer"], "max_pid": pid, "repeat_safe": False}
        try:
            prepared = _max(client, _prepare_script(asset))
        except (MaxHealthError, RequestOutcomeUnknown, ValueError) as exc:
            if (getattr(exc, "details", None) or {}).get("request_sent") is False:
                raise  # nothing was sent, so nothing changed
            return _prepare_failed(base, pid, exc, restore_medit_renderer)
        before, selected, medit = prepared["before"], prepared["selection"], prepared["medit"]
        warnings, failure, health = [], None, None
        if not medit.get("scanline_available"):
            warnings.append("Default_Scanline_Renderer is unavailable, so the Material Editor renderer was not "
                            "switched; the importer's slot preview may stall Max with V-Ray.")
        elif medit.get("error"):
            warnings.append("Could not switch the Material Editor renderer to Scanline: %s" % medit["error"])
        restore_pending = bool(medit.get("backup_pending"))
        hook_note = _before_dispatch(pid, prepared)
        if hook_note:
            warnings.append(hook_note)
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
                   "maxscript": _finalize_script(selected, restore_medit)}
        quiet = bool(settle.get("quiet") or settle.get("windows_quiet"))
        after = finished = lost = None
        if quiet:
            if not settle.get("quiet"):
                warnings.append("Max's windows respond, but its CPU was still busy (%s cores) after %s s; "
                                "textures may still be loading." % (settle.get("cpu_cores"), settle.get("waited_s")))
            step = "confirm"
            try:
                after = _asset_snapshot(client, asset)
                step = "restore"
                finished = _max(client, pending["maxscript"])
            except (MaxHealthError, RequestOutcomeUnknown) as exc:  # Max stopped responding again
                health, failure, lost, quiet = _health_failure(exc), str(exc), step, False
            except Exception as exc:
                warnings.append("Could not confirm the import or restore the selection: %s" % exc)
        timing = {k: v for k, v in detection.items() if k != "probe"}
        response = {**base, "import_timing": {**timing, "pre_dispatch": pre, "settle": _settle_summary(settle)},
                    "medit_renderer": {k: medit.get(k) for k in ("class", "locked", "editor_open", "swapped")}}
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
            warnings.append("Selection%s not restored yet: once get_bridge_status reports Max responding, run "
                            "pending_restore.maxscript once with execute_maxscript."
                            % (" and Material Editor renderer" if restore_medit else ""))
            why = ("Max is still busy after the import (%s)." % process_health.describe_windows(settle)
                   if lost is None else "Max stopped responding while %s." % (
                       "confirming the import" if lost == "confirm" else
                       "restoring the selection/Material Editor renderer; that restore may already have run "
                       "(running pending_restore once more is harmless)"))
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
            observed = bool(expected) and any(item["created"] for item in response[expected])
        if finished is None:
            response["pending_restore"] = pending
            warnings.append("Selection%s not restored: run pending_restore.maxscript once with execute_maxscript."
                            % (" and Material Editor renderer" if restore_medit else ""))
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
