"""Agent-facing tyFlow graph view and wiring tools (shadow-ledger backed).

Wiring cannot be read back from tyFlow (closed internal graph), so connects and
disconnects made here are recorded in a ledger stored on the flow node; see
`_tyflow_core`. `get_tyflow_graph` is the one-call graph view for agents:
structure from live readback, edges from the ledger, staleness from the
structural hash.
"""

from __future__ import annotations

import json
from typing import Any

from ..helpers.maxscript import safe_string

from ..coerce import DictList
from ..server import client, mcp
from ._tyflow_core import (
    MAXSCRIPT_ERROR,
    canon_name,
    graph_payload,
    ledger_update,
    parse_ledger,
    read_graph,
    store_ledger,
)


def _load_json(raw: str, fallback: Any) -> Any:
    if raw.startswith(MAXSCRIPT_ERROR):
        return {"error": "MAXScript error: " + raw[len(MAXSCRIPT_ERROR):].strip()}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return fallback


def _send_json(maxscript: str, fallback: Any) -> Any:
    # Transport and call-state exceptions propagate with their code and hint.
    response = client.send_command(maxscript)
    return _load_json(str(response.get("result", "")), fallback)


@mcp.tool()
def get_tyflow_graph(
    name: str,
    include_properties: bool = False,
    max_operators_per_event: int = 200,
    max_properties_per_operator: int = 200,
) -> str:
    """Full tyFlow graph view: events, operators, properties, ledger edges, staleness.

    `graph_hash` is the optimistic-concurrency token for `tyflow_apply_patch`.
    `ledger_status`: fresh (ledger matches live structure), stale (flow was edited
    outside MCP — edges may be outdated), absent (no MCP ledger on this flow yet;
    reconcile with set_tyflow_wiring_ledger, e.g. after reading an editor capture).
    """
    payload = graph_payload(
        name,
        include_properties=include_properties,
        max_operators_per_event=max_operators_per_event,
        max_properties_per_operator=max_properties_per_operator,
    )
    if "error" in payload:
        payload.setdefault("hint", {"suggested_tools": ["query_scene", "create_tyflow"]})
    return json.dumps(payload)


def _run_connect_maxscript(
    name: str,
    from_event: str,
    from_operator: str,
    to_event: str | None,
    create_if_missing: bool = False,
) -> dict[str, Any]:
    """Shared connect/disconnect MAXScript. `to_event=None` means disconnect.

    `create_if_missing` adds a Send Out named `from_operator` when the source
    event has no operator of that name (connect only). Nothing is created when
    the target event is missing.
    """
    from .tyflow import HELPERS  # lazy: avoids import cycle during registration
    create_block = ""
    if create_if_missing and to_event is not None:
        create_block = f"""
        if op == undefined and (findEventSubAnim flow "{safe_string(to_event)}") != undefined do (
            local srcReal = undefined
            try (srcReal = src.object) catch ()
            if srcReal == undefined do srcReal = src
            try (
                op = srcReal.Event.addOperator "Send Out" -1
                op.Operator.setName "{safe_string(from_operator)}"
                created = true
            ) catch (err = getCurrentException())
        )"""
    if to_event is None:
        action = 'if err == "" do (try (realOp.disconnect(); ok = true) catch (err = getCurrentException()))'
        target_block = ""
    else:
        target_block = f"""
        local dstSub = findEventSubAnim flow "{safe_string(to_event)}"
        if dstSub == undefined then err = "Event not found: {safe_string(to_event)}"
        local realTarget = undefined
        if err == "" do (
            try (realTarget = dstSub.object) catch ()
            if realTarget == undefined do err = "Could not resolve event handle: {safe_string(to_event)}"
        )"""
        action = "if err == \"\" do (try (realOp.connect realTarget; ok = true) catch (err = getCurrentException()))"
    maxscript = f"""(
{HELPERS}
local flow = getNodeByName "{safe_string(name)}"
if flow == undefined then (
    "{{\\"error\\":\\"Object not found: {safe_string(name)}\\"}}"
) else (
    local err = ""
    local ok = false
    local created = false
    local src = findEventSubAnim flow "{safe_string(from_event)}"
    if src == undefined do err = "Event not found: {safe_string(from_event)}"
    local op = undefined
    if err == "" do (
        op = findOperatorSubAnim src "{safe_string(from_operator)}"
        {create_block}
        if err == "" and op == undefined do err = "Operator not found: {safe_string(from_operator)}"
    )
    local realOp = undefined
    if err == "" do (
        try (realOp = op.object) catch ()
        if realOp == undefined do realOp = op
    )
    {target_block}
    {action}
    if ok then (
        "{{\\"ok\\":true,\\"created\\":" + (created as string) + "}}"
    ) else (
        "{{\\"error\\":\\"" + (esc (err as string)) + "\\"}}"
    )
)
)"""
    result = _send_json(maxscript, {"error": "Could not parse connect response."})
    return result if isinstance(result, dict) else {"error": "Unexpected connect response."}


def _rewrite_ledger_edges(
    name: str,
    from_event: str,
    from_operator: str,
    to_event: str | None,
) -> dict[str, Any]:
    """Post-mutation ledger update: replace or remove the edge for (event, op)."""
    return ledger_update(name, replace_edge=(from_event, from_operator, to_event))


@mcp.tool()
def connect_tyflow_operator(
    name: str,
    from_event: str,
    from_operator: str,
    to_event: str,
    create_if_missing: bool = False,
) -> str:
    """Connect a test/Send Out operator's output to a target event (documented API).

    Re-connecting an operator that already has a connection re-targets it; the
    ledger edge for (from_event, from_operator) is replaced accordingly.
    create_if_missing adds a Send Out named from_operator to from_event when no
    operator of that name exists (created_send_out reports it).
    """
    outcome = _run_connect_maxscript(
        name, from_event, from_operator, to_event, create_if_missing=create_if_missing
    )
    if "error" in outcome:
        return json.dumps(outcome)
    created = bool(outcome.get("created"))
    payload: dict[str, Any] = {
        "name": name,
        "from_event": from_event,
        "from_operator": from_operator,
        "to_event": to_event,
        "connected": True,
        "created_send_out": created,
    }
    payload.update(
        ledger_update(
            name,
            replace_edge=(from_event, from_operator, to_event),
            record_ops={f"{from_event}/{from_operator}": "Send Out"} if created else None,
        )
    )
    return json.dumps(payload)


@mcp.tool()
def disconnect_tyflow_operator(name: str, from_event: str, from_operator: str) -> str:
    """Disconnect an operator's output and drop its ledger edge."""
    outcome = _run_connect_maxscript(name, from_event, from_operator, None)
    if "error" in outcome:
        return json.dumps(outcome)
    payload: dict[str, Any] = {
        "name": name,
        "from_event": from_event,
        "from_operator": from_operator,
        "disconnected": True,
    }
    payload.update(_rewrite_ledger_edges(name, from_event, from_operator, None))
    return json.dumps(payload)


@mcp.tool()
def set_tyflow_wiring_ledger(name: str, edges: DictList, mode: str = "replace") -> str:
    """Reconcile the wiring ledger by hand (e.g. after reading an editor capture).

    Each edge: {"from_event", "from_op" (alias "from_operator"), "to_event"}.
    mode=replace overwrites all ledger edges; mode=merge keeps existing edges
    except those with a matching (from_event, from_op). Event names not present
    in the live flow are recorded but flagged in warnings. Marks the ledger
    fresh by definition (hash restamped from the live read).
    """
    mode_key = mode.strip().lower()
    if mode_key not in {"replace", "merge"}:
        return json.dumps({"error": f"Unknown mode: {mode}. Use replace or merge."})
    normalized: list[dict[str, str]] = []
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            return json.dumps({"error": f"Edge {index} is not an object."})
        from_event = canon_name(str(edge.get("from_event", "")).strip())
        from_op = canon_name(str(edge.get("from_op", edge.get("from_operator", ""))).strip())
        to_event = canon_name(str(edge.get("to_event", "")).strip())
        if not from_event or not from_op or not to_event:
            return json.dumps(
                {"error": f"Edge {index} needs from_event, from_op and to_event."}
            )
        normalized.append(
            {"from_event": from_event, "from_op": from_op, "to_event": to_event}
        )

    graph = read_graph(name)
    if "error" in graph:
        return json.dumps(graph)
    live_events = {canon_name(event["name"]) for event in graph.get("events", [])}
    warnings = [
        f"Unknown event in edge {index}: {value}"
        for index, edge in enumerate(normalized)
        for key, value in (("from_event", edge["from_event"]), ("to_event", edge["to_event"]))
        if (value not in live_events)
    ]

    ledger = parse_ledger(graph.get("ledgerRaw", ""))
    if mode_key == "replace":
        ledger["edges"] = normalized
    else:
        replaced_keys = {(edge["from_event"], edge["from_op"]) for edge in normalized}
        ledger["edges"] = [
            edge
            for edge in ledger["edges"]
            if (edge["from_event"], edge["from_op"]) not in replaced_keys
        ] + normalized
    stored = store_ledger(name, ledger, graph=graph)
    if "error" in stored:
        return json.dumps(stored)
    return json.dumps(
        {
            "name": name,
            "mode": mode_key,
            "edgeCount": len(ledger["edges"]),
            "edges": ledger["edges"],
            "ledger_hash": stored["hash"],
            "warnings": warnings,
        }
    )
