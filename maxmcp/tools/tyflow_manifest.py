"""tyFlow operator manifest: probe-harvested catalog of operator types and params.

Operator display names are the only type identity tyFlow exposes at runtime, and
PB2 param names are non-obvious (`shape_type_tab`, `type_3d_ID_tab`, ...). The
manifest gives agents exact type names, availability on this installation, and
per-type property names/defaults. Harvest probes a scratch flow (one operator of
each type added, introspected, removed) and caches per tyFlow version.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ..helpers.maxscript import NODE_LOOKUP_FNS, safe_string
from ..helpers.tyflow_catalog import EXECUTABLE_OPERATOR_NAMES, TYFLOW_OPERATOR_NAMES

from ..coerce import StrList
from ..max_client import CALL_STATE_ERRORS
from ..server import client, mcp
from ._tyflow_core import CORE_HELPERS, MAXSCRIPT_ERROR

_PROBE_FLOW = "zzz_mcp_manifest_probe"
_BATCH_SIZE = 40


def _cache_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = Path(root) / "3dsmax-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(version: int) -> Path:
    return _cache_dir() / f"tyflow_manifest_{version}.json"


def _latest_cache() -> Path | None:
    candidates = sorted(
        _cache_dir().glob("tyflow_manifest_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("operators"), list) else None


def _tyflow_version() -> int | dict[str, Any]:
    maxscript = """(
if tyFlow == undefined then "__ERROR__|tyFlow plugin is not available"
else ((tyFlow_version()) as string)
)"""
    # Transport and call-state exceptions propagate with their code and hint.
    response = client.send_command(maxscript)
    raw = str(response.get("result", "")).strip()
    if raw.startswith("__ERROR__|"):
        return {"error": raw.split("|", 1)[1]}
    try:
        return int(raw)
    except ValueError:
        return {"error": f"Could not read tyFlow version: {raw[:100]}"}


def _probe_batch(names: list[str]) -> dict[str, Any] | list[dict[str, Any]]:
    array_literal = "#(" + ", ".join(f'"{safe_string(item)}"' for item in names) + ")"
    maxscript = f"""(
{CORE_HELPERS}
{NODE_LOOKUP_FNS}
if tyFlow == undefined then (
    "__ERROR__|tyFlow plugin is not available"
) else (
    local out = stringstream ""
    -- Batches reuse the scratch flow; several left by an interrupted run are replaced.
    local flow = undefined
    local probeFlows = mcpNamedNodes "{_PROBE_FLOW}"
    if probeFlows.count == 1 then flow = probeFlows[1] else (for f in probeFlows do (try (delete f) catch ()))
    if flow == undefined do flow = tyFlow name:"{_PROBE_FLOW}" pos:[0,0,-100000]
    local eh = flow.tyFlow.addEvent()
    local ev = eh.Event
    for opName in {array_literal} do (
        local op = undefined
        try (op = ev.addOperator opName -1) catch ()
        if op == undefined then (
            format "OPX|%|unavailable|0\\n" (mcpTyClean opName) to:out
        ) else (
            local pNames = #()
            try (pNames = getPropNames op) catch ()
            format "OPX|%|available|%\\n" (mcpTyClean opName) (pNames.count as string) to:out
            for p in pNames do (
                local v = "<unreadable>"
                local cls = "?"
                try (
                    local pv = getProperty op p
                    v = pv as string
                    cls = (classof pv) as string
                ) catch ()
                if v.count > 120 do v = (substring v 1 120) + "..."
                format "PP|%|%|%|%\\n" (mcpTyClean opName) (mcpTyClean (p as string)) (mcpTyClean v) (mcpTyClean cls) to:out
            )
            try (op.remove()) catch ()
        )
    )
    out as string
)
)"""
    try:
        response = client.send_command(maxscript, timeout=300)
    except CALL_STATE_ERRORS:
        raise  # the probe may still run; never report it as an ordinary failure
    except Exception as exc:
        return {"error": str(exc)}
    raw = str(response.get("result", ""))
    if raw.startswith("__ERROR__|"):
        return {"error": raw.split("|", 1)[1]}
    if raw.startswith(MAXSCRIPT_ERROR):
        return {"error": "MAXScript error: " + raw[len(MAXSCRIPT_ERROR):].strip()}

    def _decode(token: str) -> str:
        return token.replace("<pipe>", "|")

    entries: dict[str, dict[str, Any]] = {}
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) >= 4 and parts[0] == "OPX":
            type_name = _decode(parts[1])
            entries[type_name] = {
                "type": type_name,
                "available": parts[2] == "available",
                "executable": type_name in EXECUTABLE_OPERATOR_NAMES,
                "propertyCount": int(parts[3]) if parts[3].isdigit() else 0,
                "properties": [],
            }
        elif len(parts) >= 5 and parts[0] == "PP":
            entry = entries.get(_decode(parts[1]))
            if entry is not None:
                entry["properties"].append(
                    {
                        "name": _decode(parts[2]),
                        "default": _decode(parts[3]),
                        "valueClass": _decode(parts[4]),
                    }
                )
    return list(entries.values())


def _cleanup_probe_flow() -> str | None:
    """Delete the scratch flow; return why it may still be in the scene."""
    maxscript = f"""(
{NODE_LOOKUP_FNS}
local flows = mcpNamedNodes "{_PROBE_FLOW}"
for flow in flows do (try (delete flow) catch ())
if (for flow in flows where isValidNode flow collect flow).count > 0 then "LEFT" else "OK"
)"""
    try:
        raw = str(client.send_command(maxscript).get("result", "")).strip()
    except Exception as exc:
        return str(exc)
    return None if raw == "OK" else f"delete was refused ({raw[:120]})"


@mcp.tool()
def harvest_tyflow_manifest(
    refresh: bool = False,
    operator_names: StrList | None = None,
    batch_size: int = _BATCH_SIZE,
) -> str:
    """Probe-harvest the tyFlow operator manifest and cache it per tyFlow version.

    Without `refresh`, an existing cache is returned untouched (no Max traffic).
    `operator_names` limits the probe to specific types and merges results into
    the existing cache. Probing adds each operator (inert) to a hidden scratch
    flow, reads its property names/defaults, then removes it; the scratch flow
    is deleted afterwards.
    """
    version = _tyflow_version()
    if isinstance(version, dict):
        return json.dumps(version)
    cache = _cache_path(version)
    existing = _read_manifest(cache)
    if existing and not refresh and not operator_names:
        return json.dumps(
            {
                "cached": True,
                "tyflowVersion": version,
                "operatorCount": len(existing["operators"]),
                "availableCount": sum(1 for op in existing["operators"] if op.get("available")),
                "cachePath": str(cache),
            }
        )

    names = [str(item) for item in (operator_names or TYFLOW_OPERATOR_NAMES)]
    harvested: list[dict[str, Any]] = []
    errors: list[str] = []
    step = max(1, int(batch_size))
    cleanup_error: str | None = None
    try:
        for start in range(0, len(names), step):
            batch = names[start : start + step]
            outcome = _probe_batch(batch)
            if isinstance(outcome, dict):
                errors.append(str(outcome.get("error", "probe failed")))
                break
            harvested.extend(outcome)
    finally:
        cleanup_error = _cleanup_probe_flow()

    if not harvested:
        return json.dumps({"error": "Manifest probe produced no entries", "details": errors})

    probed = {op["type"] for op in harvested}
    unprobed = [item for item in names if item not in probed]
    # A probe that stopped early merges into the cache instead of replacing it,
    # so operators it never reached keep their earlier entries.
    if (operator_names or errors) and existing:
        merged = {op["type"]: op for op in existing["operators"]}
        for op in harvested:
            merged[op["type"]] = op
        operators = list(merged.values())
    else:
        operators = harvested
    manifest = {
        "tyflowVersion": version,
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "operators": operators,
    }
    cache.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    payload: dict[str, Any] = {
        "cached": False,
        "tyflowVersion": version,
        "operatorCount": len(operators),
        "availableCount": sum(1 for op in operators if op.get("available")),
        "unavailable": [op["type"] for op in operators if not op.get("available")],
        "errors": errors,
        "cachePath": str(cache),
    }
    warnings: list[str] = []
    if errors:
        payload["unprobed"] = unprobed
        warnings.append(
            f"The probe stopped early; {len(unprobed)} operator type(s) were not probed: "
            + "; ".join(errors)
        )
    if cleanup_error:
        warnings.append(f"Scratch flow {_PROBE_FLOW} may remain in the scene: {cleanup_error}")
    if warnings:
        payload["status"] = "partial"
        payload["warnings"] = warnings
    return json.dumps(payload)


@mcp.tool()
def list_tyflow_operators(
    query: str = "",
    include_properties: bool = False,
    executable_only: bool = False,
    limit: int = 40,
) -> str:
    """Query the cached tyFlow operator manifest (no Max traffic).

    Matches query tokens against operator type names and property names
    (case-insensitive substrings). Each entry's `available` reports whether this
    installation could add that type when probed. Run harvest_tyflow_manifest
    first if no cache exists.
    """
    path = _latest_cache()
    manifest = _read_manifest(path) if path else None
    if not manifest:
        return json.dumps(
            {
                "error": "No tyFlow operator manifest cached yet.",
                "hint": {"suggested_tools": ["harvest_tyflow_manifest"]},
            }
        )
    tokens = [token.casefold() for token in query.split() if token.strip()]
    query_key = query.strip().casefold()
    exact_matches: list[dict[str, Any]] = []
    name_matches: list[dict[str, Any]] = []
    prop_matches: list[dict[str, Any]] = []
    for op in manifest["operators"]:
        if executable_only and not op.get("executable"):
            continue
        bucket = name_matches
        if tokens:
            type_key = op["type"].casefold()
            if query_key == type_key:
                bucket = exact_matches
            elif query_key in type_key or all(token in type_key for token in tokens):
                bucket = name_matches
            else:
                prop_names = " ".join(p["name"].casefold() for p in op.get("properties", []))
                if all(token in type_key or token in prop_names for token in tokens):
                    bucket = prop_matches
                else:
                    continue
        entry = {
            "type": op["type"],
            "available": op.get("available", False),
            "executable": op.get("executable", False),
            "propertyCount": op.get("propertyCount", 0),
        }
        if include_properties:
            entry["properties"] = op.get("properties", [])
        bucket.append(entry)
    matches = exact_matches + name_matches + prop_matches
    truncated = len(matches) > max(1, int(limit))
    return json.dumps(
        {
            "tyflowVersion": manifest.get("tyflowVersion"),
            "matchCount": len(matches),
            "truncated": truncated,
            "operators": matches[: max(1, int(limit))],
        }
    )
