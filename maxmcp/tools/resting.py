"""Resting contact: find items floating above, or sunk into, what lies below them, and drop them onto it."""
from __future__ import annotations

import time
from typing import Any

from ..helpers import resting as rs
from ..server import client, mcp

_STATUS_ORDER = {"penetrating": 0, "floating": 1, "no_support": 2, "unchecked": 3, "resting": 4}
_NOTES = [
    "Gaps are measured from each item's lowest vertex per XY cell straight down to the nearest support surface; "
    "negative means sunk into it.",
    "Supports start strictly lower than the item, so a cloth draped over an item is never its support.",
]


def _mm(value: float | None, upm: float) -> float | None:
    return None if value is None else round(value / upm, 3)


def _rows(report: dict[str, Any], plan: dict[int, dict[str, Any]] | None, include_resting: bool,
          limit: int) -> tuple[list[dict[str, Any]], dict[str, int], dict[int, str]]:
    upm, tol = report["upm"], report["tolerance"]
    gaps: dict[int, list[dict[str, Any]]] = {}
    for g in report["gaps"]:
        gaps.setdefault(g["unit"], []).append(g)
    statuses: dict[int, str] = {}
    rows = []
    for idx, u in report["units"].items():
        status = rs.classify(u, tol)
        statuses[idx] = status
        hits = sorted(gaps.get(idx, []), key=lambda g: g["gap"])
        flags = []
        if u["stride"] > 1:
            flags.append("sampled")
        if any("proxy" in c.lower() for c in u["classes"]):
            flags.append("proxy_preview")
        row: dict[str, Any] = {
            "name": u["name"], "handle": u["handle"], "kind": u["kind"],
            "group": u["group"]["name"] if u["group"] else None, "status": status,
            "min_gap_mm": _mm(u["min_gap"], upm), "gap_p10_mm": _mm(u["p10"], upm), "gap_p50_mm": _mm(u["p50"], upm),
            "samples": u["samples"], "hits": u["hits"],
            "contact_fraction": round(u["contact"] / u["hits"], 3) if u["hits"] else 0.0,
            "support": ({"name": hits[0]["support_name"], "handle": hits[0]["support"],
                         "is_item": hits[0]["support"] in report["mesh_unit"]} if hits else None),
            "supports_hit": [{"name": g["support_name"], "gap_mm": _mm(g["gap"], upm), "hits": g["hits"]}
                             for g in hits[:3]],
            "min_gap_point": u["min_gap_point"],
            "movable": not u["not_movable"],
        }
        if u["not_movable"]:
            row["not_movable_reason"] = u["not_movable"]
        if flags:
            row["flags"] = flags
        if plan is not None and idx in plan:
            p = plan[idx]
            row["plan"] = {"dz_mm": _mm(p["own"], upm), "moves_with_parent_mm": _mm(p["inherited"], upm),
                           "reason": p["reason"] or ("drop" if p["own"] < 0 else "lift" if p["own"] > 0 else "")}
        rows.append(row)
    declared = report.get("declared", {})
    for x in report["unchecked"]:
        d = declared.get(x["unit"], {})
        statuses[x["unit"]] = "unchecked"
        rows.append({"name": d.get("name"), "handle": d.get("handle"), "status": "unchecked", "reason": x["reason"]})
    summary = {s: 0 for s in rs.STATUSES}
    for s in statuses.values():
        summary[s] += 1
    rows.sort(key=lambda r: (_STATUS_ORDER[r["status"]], -abs(r.get("min_gap_mm") or 0.0)))
    if not include_resting:
        rows = [r for r in rows if r["status"] != "resting"]
    return rows[:limit], summary, statuses


def _short(names: list[str], limit: int = 8) -> str:
    names = list(dict.fromkeys(n for n in names if n))
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _warnings(report: dict[str, Any], statuses: dict[int, str]) -> list[str]:
    out = []
    units = report["units"]
    if report["unchecked"]:
        out.append(f"The time budget ran out: {len(report['unchecked'])} items weren't checked. Check them in a "
                   "separate call, or raise time_budget_s (at most 45 s).")
    sampled = [u["name"] for u in units.values() if u["stride"] > 1]
    if sampled:
        out.append(f"Dense items were read at every n-th vertex (max_item_verts): {_short(sampled)}.")
    by_reason: dict[str, list[str]] = {}
    for s in report["supports_skipped"]:
        by_reason.setdefault(s["reason"], []).append(s["name"])
    if "coincident" in by_reason:
        out.append("Coincident duplicates of items were ignored as supports: "
                   f"{_short(by_reason['coincident'])}. scene_qa's duplicate_group_heads finds stacked duplicate groups.")
    if "face_limit" in by_reason:
        out.append(f"Supports above max_support_faces were skipped: {_short(by_reason['face_limit'])}. "
                   "Pass them in supports to use them.")
    if "not_mesh" in by_reason:
        out.append(f"Supports that aren't mesh-convertible were skipped: {_short(by_reason['not_mesh'])}.")
    if report["scatter_excluded"]:
        out.append(f"{report['scatter_excluded']} scatter objects (Mesher, Forest Pack, RailClone, tyFlow, particles, "
                   "fur) weren't used as supports; pass them in supports to use them.")
    no_support = [units[i]["name"] for i, s in statuses.items() if s == "no_support" and i in units]
    if no_support:
        out.append(f"Nothing within max_drop_mm below: {_short(no_support)}. A support may be excluded, hidden or "
                   "not a mesh, or the item overhangs an edge.")
    proxies = [u["name"] for u in units.values() if any("proxy" in c.lower() for c in u["classes"])]
    proxies += [s["name"] for s in report["supports_used"] if "proxy" in s.get("info", "").lower()]
    if proxies:
        out.append(f"Proxies are measured from their viewport preview mesh, not the render mesh: {_short(proxies)}.")
    if report["skipped"]:
        out.append("Skipped: " + _short([f"{s['name']} ({rs.SKIP_REASONS.get(s['reason'], s['reason'])})"
                                         for s in report["skipped"]]))
    return out


def _base(report: dict[str, Any]) -> dict[str, Any]:
    upm = report["upm"]
    return {
        "units": report["units_name"], "units_per_mm": upm, "frame": report["frame"],
        "tolerance_mm": _mm(report["tolerance"], upm), "max_drop_mm": _mm(report["max_drop"], upm),
        "max_sink_mm": _mm(report["lift"], upm),
        "complete": not report["unchecked"], "stop_reason": report["stop"],
        "elapsed_ms": report["elapsed_ms"], "time_budget_ms": report["time_budget_ms"],
        "queue_wait_ms": max(0, report["queue_wait_ms"]),
        "supports_used": [{"name": s["name"], "handle": s["handle"], "faces": s["faces"],
                           "is_item": s["handle"] in report["mesh_unit"]} for s in report["supports_used"]],
    }


def _measure(settings: dict[str, Any]) -> dict[str, Any]:
    script = rs.build_measure_script(settings)
    response = client.send_command(script)
    return rs.parse_measure(str(response.get("result", "")))


def _validate_common(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    return limit


@mcp.tool()
def resting_check(
    names: list[str] | None = None,
    supports: list[str] | None = None,
    exclude_supports: list[str] | None = None,
    group_mode: str = "members",
    tolerance_mm: float = rs.DEFAULT_TOLERANCE_MM,
    max_drop_mm: float = rs.DEFAULT_MAX_DROP_MM,
    max_sink_mm: float = rs.DEFAULT_MAX_SINK_MM,
    samples: int = rs.DEFAULT_SAMPLES,
    max_item_verts: int = rs.DEFAULT_MAX_ITEM_VERTS,
    max_support_faces: int = rs.DEFAULT_MAX_SUPPORT_FACES,
    include_resting: bool = False,
    limit: int = 100,
    time_budget_s: float = rs.DEFAULT_TIME_BUDGET_S,
) -> dict[str, Any]:
    """Find items floating above, or sunk into, the surface below them (props on tables, cloth on cloth).

    Items: `names` (a group head expands to its mesh members; group_mode="rigid"
    measures the whole group as one item), else the selection. Never the whole scene.
    Supports, unless `supports` lists them: visible, renderable meshes that start
    lower than the item and lie under its footprint, scatter objects excluded;
    `exclude_supports` removes some. Each item's underside comes from its lowest
    vertices on an XY grid (`samples` cells), so single-sided cloth and sparse
    meshes measure correctly; one double-sided ray per cell goes down to each support.

    Status per item: penetrating (sunk deeper than tolerance_mm), floating (lowest
    point above the surface by more than tolerance_mm), no_support (nothing within
    max_drop_mm below), resting (hidden unless include_resting), unchecked (budget).
    All lengths are MILLIMETRES (results too). Read only: nothing moves; use
    drop_to_surface to fix floating items. Stops at time_budget_s (max 45 s, counted
    from the send) with complete=false.
    """
    limit = _validate_common(limit)
    settings = rs.validate(names, supports, exclude_supports, group_mode, tolerance_mm, max_drop_mm, max_sink_mm,
                           samples, max_item_verts, max_support_faces, time_budget_s, getattr(client, "timeout", None))
    report = _measure(settings)
    rows, summary, statuses = _rows(report, None, include_resting, limit)
    result = _base(report)
    result.update(summary=summary, nodes=rows, nodes_truncated=sum(summary.values()) > len(rows) and
                  (include_resting or sum(v for k, v in summary.items() if k != "resting") > len(rows)),
                  notes=list(_NOTES))
    warnings = _warnings(report, statuses)
    if warnings:
        result["warnings"] = warnings
    return result


@mcp.tool()
def drop_to_surface(
    names: list[str] | None = None,
    dry_run: bool = True,
    supports: list[str] | None = None,
    exclude_supports: list[str] | None = None,
    group_mode: str = "members",
    clearance_mm: float = rs.DEFAULT_CLEARANCE_MM,
    tolerance_mm: float = rs.DEFAULT_TOLERANCE_MM,
    max_drop_mm: float = rs.DEFAULT_MAX_DROP_MM,
    max_sink_mm: float = rs.DEFAULT_MAX_SINK_MM,
    lift_penetrating: bool = False,
    samples: int = rs.DEFAULT_SAMPLES,
    max_item_verts: int = rs.DEFAULT_MAX_ITEM_VERTS,
    max_support_faces: int = rs.DEFAULT_MAX_SUPPORT_FACES,
    limit: int = 100,
    time_budget_s: float = 30.0,
) -> dict[str, Any]:
    """Drop floating items straight down onto the surface below them (props onto a table, plates onto placemats).

    Measures like resting_check, then plans bottom-up: an item on a dropped item
    follows it, and each floating item moves along world Z only until its lowest
    point is clearance_mm above its support. dry_run=True (default) only reports
    the plan. dry_run=False moves the items in one undo step, then re-measures them
    inside it and cancels the whole step if any ends up floating or sunk beyond
    tolerance_mm. Sunk items aren't lifted unless lift_penetrating=True.
    Never moves: frozen, Z-locked, linked (Revit), xref, constrained or animated
    nodes, and refuses while Auto Key is on, an undo operation is open, or the
    measurement was incomplete. Reference twins (same base object, same transform,
    e.g. *_ENMESH) move with their item. Lengths are MILLIMETRES.
    """
    limit = _validate_common(limit)
    started = time.monotonic()
    settings = rs.validate(names, supports, exclude_supports, group_mode, tolerance_mm, max_drop_mm, max_sink_mm,
                           samples, max_item_verts, max_support_faces, time_budget_s, getattr(client, "timeout", None))
    clearance_mm = rs._mm(clearance_mm, "clearance_mm", 0.0, 10.0)
    if clearance_mm > settings["tolerance_mm"]:
        raise ValueError("clearance_mm must not exceed tolerance_mm, or a dropped item would still read as floating")
    if not isinstance(dry_run, bool) or not isinstance(lift_penetrating, bool):
        raise ValueError("dry_run and lift_penetrating must be true or false")
    report = _measure(settings)
    upm = report["upm"]
    plan = rs.plan_drops(report, clearance=clearance_mm * upm, lift_penetrating=lift_penetrating)
    rows, summary, statuses = _rows(report, plan, True, limit)
    moves, verify, companions = rs.moves_and_checks(report, plan, clearance=clearance_mm * upm)
    units = report["units"]
    moving = sorted((i for i, p in plan.items() if p["own"]), key=lambda i: (units[i]["min_z"], i))
    result = _base(report)
    result.update(dry_run=dry_run, applied=False, clearance_mm=clearance_mm, summary=summary,
                  planned_moves=len(moving), stack_order=[units[i]["name"] for i in moving], nodes=rows,
                  notes=list(_NOTES))
    if companions:
        result["companions"] = companions
    warnings = _warnings(report, statuses)
    if companions:
        warnings.append(f"Reference twins move with their items: {_short([c['name'] for c in companions])}.")
    moved_units = {i for i, p in plan.items() if p["total"]}
    outside = [d["name"] for d in report["dependents"] if d["unit"] in moved_units]
    if outside:
        warnings.append(f"These nodes sit on moved items but aren't in names, so they'd be left behind: {_short(outside)}.")
    held = [units[i]["name"] for i, p in plan.items() if p["reason"] in rs.NOT_MOVABLE_REASONS
            and statuses.get(i) in ("floating", "penetrating")]
    if held:
        warnings.append(f"Not moved (frozen, locked, linked, constrained or animated): {_short(held)}.")
    sunk = [units[i]["name"] for i, p in plan.items() if p["reason"] == "penetrating_not_lifted"]
    if sunk:
        warnings.append(f"Sunk items weren't lifted (lift_penetrating=false): {_short(sunk)}.")

    def finish() -> dict[str, Any]:
        if warnings:
            result["warnings"] = warnings
        return result

    if dry_run or not moves:
        return finish()
    if report["unchecked"]:
        result["refused"] = {"code": "INCOMPLETE", "message": "The measurement didn't finish, so nothing was moved. "
                             "Narrow names or raise time_budget_s."}
        return finish()
    if report["auto_key"]:
        result["refused"] = {"code": "AUTO_KEY_ON", "message": "Auto Key is on in 3ds Max, so nothing was moved."}
        return finish()
    remaining = settings["time_budget_s"] - (time.monotonic() - started)
    if remaining * 1000 < rs.MIN_APPLY_MS:
        result["refused"] = {"code": "DEADLINE_BEFORE_APPLY",
                             "message": "Too little of time_budget_s was left to apply and verify; nothing was moved."}
        return finish()
    zs = [abs(m["z_before"]) for m in moves] + [abs(m["z_after"]) for m in moves]
    eps = max(upm * 0.01, max(zs) * 2e-7)
    label = "MCP drop to surface"
    script = rs.build_apply_script(settings, moves, verify, label=label, eps=eps, time_budget_s=remaining)
    outcome = rs.parse_apply(str(client.send_command(script).get("result", "")))
    names_by_unit = {i: units[i]["name"] for i in units}
    if outcome["status"] == "refused":
        result["refused"] = {"code": outcome["code"], "message": outcome["detail"] + " Nothing was moved."}
        return finish()
    verified = [{"name": names_by_unit.get(v["unit"]), "gap_mm": _mm(v["gap"], upm), "ok": v["ok"]}
                for v in outcome.get("verify", [])]
    result["verify"] = verified
    if outcome["status"] != "applied":
        result["rolled_back"] = True
        bad = [v["name"] for v in verified if not v["ok"]]
        warnings.append("Nothing was moved: the re-measure after moving "
                        + ("ran out of time" if outcome["status"] == "rolled_back_deadline"
                           else f"found items still off their support ({_short(bad)})" if bad
                           else f"failed ({outcome.get('code', outcome['status'])}: {outcome.get('detail', '')})")
                        + ", so the undo step was cancelled.")
        return finish()
    result.update(applied=True, undo_label=label, move_log=[
        {"name": next((units[i]["name"] for i in units if units[i]["handle"] == m["handle"]),
                      next((c["name"] for c in companions if c["handle"] == m["handle"]), None)),
         "handle": m["handle"], "dz_mm": _mm(m["dz"], upm), "z_before": m["z_before"], "z_after": m["z_after"]}
        for m in moves])
    return finish()
