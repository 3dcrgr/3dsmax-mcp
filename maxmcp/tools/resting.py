"""Resting contact: find items floating above, or sunk into, what lies below them, and drop them onto it."""
from __future__ import annotations

import time
from typing import Any

from ..helpers import resting as rs
from ..helpers.precheck import PrecheckBlocked
from ..max_client import DialogBlocked
from ..server import client, mcp

_STATUS_ORDER = {"penetrating": 0, "floating": 1, "no_support": 2, "unchecked": 3, "resting": 4}
_NOTES = [
    "Gaps run from each item's lowest point per XY cell straight down to the nearest support surface; negative "
    "means sunk into it. min_gap_point and move_log z values are world positions in scene units.",
    "Supports start below the item's median underside and lie under its footprint; a cloth draped over an item is "
    "never its support.",
]


def _mm(value: float | None, upm: float) -> float | None:
    return None if value is None else round(value / upm, 3)


def _rows(report: dict[str, Any], plan: dict[int, dict[str, Any]] | None, include_resting: bool,
          limit: int) -> tuple[list[dict[str, Any]], dict[str, int], dict[int, str], bool]:
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
    for x in report["unchecked"]:
        d = report["declared"].get(x["unit"], {})
        statuses[x["unit"]] = "unchecked"
        rows.append({"name": d.get("name"), "handle": d.get("handle"), "status": "unchecked", "reason": x["reason"]})
    summary = {s: 0 for s in rs.STATUSES}
    for s in statuses.values():
        summary[s] += 1
    rows.sort(key=lambda r: (_STATUS_ORDER[r["status"]], -abs(r.get("min_gap_mm") or 0.0)))
    if not include_resting:
        rows = [r for r in rows if r["status"] != "resting"]
    return rows[:limit], summary, statuses, len(rows) > limit


def _short(names: list[str], limit: int = 8) -> str:
    names = list(dict.fromkeys(n for n in names if n))
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _warnings(report: dict[str, Any], statuses: dict[int, str]) -> list[str]:
    out = []
    units = report["units"]
    if report["unchecked"]:
        dense = [report["declared"][x["unit"]]["name"] for x in report["unchecked"] if x["reason"] == "too_dense"]
        late = len(report["unchecked"]) - len(dense)
        if late:
            out.append(f"The time budget ran out: {late} items weren't checked. Check them in a separate call, or "
                       "raise time_budget_s (at most 45 s).")
        if dense:
            out.append(f"Too dense to read (above {rs.DENSE_FACTOR} x max_item_verts vertices): {_short(dense)}.")
    sampled = [u["name"] for u in units.values() if u["stride"] > 1]
    if sampled:
        out.append(f"Dense items were read at every n-th vertex (max_item_verts): {_short(sampled)}.")
    by_reason: dict[str, list[str]] = {}
    for s in report["supports_skipped"]:
        by_reason.setdefault(s["reason"], []).append(s["name"])
    if "coincident" in by_reason:
        out.append("Coincident duplicates of items (same transform and geometry) were ignored as supports: "
                   f"{_short(by_reason['coincident'])}. scene_qa's duplicate_group_heads finds stacked duplicate groups.")
    if "face_limit" in by_reason:
        out.append(f"Supports above max_support_faces were skipped: {_short(by_reason['face_limit'])}.")
    if "not_mesh" in by_reason:
        out.append(f"Supports that aren't mesh-convertible were skipped: {_short(by_reason['not_mesh'])}.")
    if report["scatter_excluded"]:
        out.append(f"{report['scatter_excluded']} scatter objects (Mesher, Forest Pack, RailClone, tyFlow, particles, "
                   "fur) weren't used as supports.")
    blocked: dict[str, set[str]] = {}
    hidden: dict[str, set[str]] = {}
    for b in report["blockers"]:
        if b["unit"] in units:
            (hidden if b["reason"] in rs.WARN_ONLY_BLOCKS else blocked).setdefault(units[b["unit"]]["name"], set()).add(b["name"])
    if hidden:
        out.append("Hidden or non-renderable meshes lie under: "
                   + _short([f"{k} ({', '.join(sorted(v))})" for k, v in hidden.items()])
                   + ". They aren't used as supports; unhide them first if the items rest on them.")
    if blocked:
        out.append("A surface left out of the measurement (scatter, excluded, too dense or not a mesh) lies under: "
                   + _short([f"{k} ({', '.join(sorted(v))})" for k, v in blocked.items()])
                   + ". Their gaps may be to a surface further down, so drop_to_surface won't move them.")
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
        "complete": not report["unchecked"] and report["stop"] == "none", "stop_reason": report["stop"],
        "elapsed_ms": report["elapsed_ms"], "time_budget_ms": report["time_budget_ms"],
        "queue_wait_ms": max(0, report["queue_wait_ms"]),
        "supports_used": [{"name": s["name"], "handle": s["handle"], "faces": s["faces"],
                           "is_item": s["handle"] in report["mesh_unit"]} for s in report["supports_used"]],
    }


def _measure(settings: dict[str, Any]) -> dict[str, Any]:
    response = client.send_command(rs.build_measure_script(settings))
    return rs.parse_measure(str(response.get("result", "")))


def _limit(limit: Any) -> int:
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
    Supports, unless `supports` lists them (the items always count too): visible,
    renderable meshes below the item's footprint, scatter objects excluded;
    `exclude_supports` removes some. Each item's underside comes from its lowest
    points on an XY grid (`samples` cells), so single-sided cloth and sparse meshes
    measure correctly; one double-sided ray per cell goes down to each support.

    Status per item: penetrating (sunk deeper than tolerance_mm), floating (lowest
    point above the surface by more than tolerance_mm), no_support (nothing within
    max_drop_mm below), resting (hidden unless include_resting), unchecked (budget or
    too dense). Lengths are MILLIMETRES. Read only; drop_to_surface fixes floating
    items. Stops at time_budget_s (max 45 s, counted from the send) with complete=false.
    """
    limit = _limit(limit)
    if not isinstance(include_resting, bool):
        raise ValueError("include_resting must be true or false")
    settings = rs.validate(names, supports, exclude_supports, group_mode, tolerance_mm, max_drop_mm, max_sink_mm,
                           samples, max_item_verts, max_support_faces, time_budget_s, getattr(client, "timeout", None))
    report = _measure(settings)
    rows, summary, statuses, truncated = _rows(report, None, include_resting, limit)
    result = _base(report)
    result.update(summary=summary, nodes=rows, nodes_truncated=truncated, notes=list(_NOTES))
    warnings = _warnings(report, statuses)
    if warnings:
        result["warnings"] = warnings
    return result


@mcp.tool()
def drop_to_surface(
    names: list[str] | None = None,
    dry_run: bool = True,
    group_mode: str | None = None,
    supports: list[str] | None = None,
    exclude_supports: list[str] | None = None,
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

    Measures like resting_check, then plans in support order: each floating item moves
    along world Z only until its lowest point is clearance_mm above its support, and
    items resting on a moved item follow it. dry_run=True (default) only reports the
    plan. dry_run=False needs `names`; it moves the items in one undo step, re-measures
    them inside it and cancels the whole step if any ends off its planned gap.
    group_mode is required when names include a group: "members" drops each prop of a
    table-setting group, "rigid" moves furniture or assemblies as one piece.
    Never moves frozen, Z-locked, linked (Revit), xref, constrained or animated nodes,
    items over a surface it couldn't measure (scatter, excluded), or supports whose
    dependents can't follow. Refuses while Auto Key is on, an undo operation is open,
    or the measurement was incomplete. Reference twins (same base object and
    transform, e.g. *_ENMESH) move with their item. Lengths are MILLIMETRES.
    """
    limit = _limit(limit)
    started = time.monotonic()
    if group_mode is not None and group_mode not in rs.GROUP_MODES:
        raise ValueError("group_mode must be members or rigid")
    if not isinstance(dry_run, bool) or not isinstance(lift_penetrating, bool):
        raise ValueError("dry_run and lift_penetrating must be true or false")
    settings = rs.validate(names, supports, exclude_supports, group_mode or "members", tolerance_mm, max_drop_mm,
                           max_sink_mm, samples, max_item_verts, max_support_faces, time_budget_s,
                           getattr(client, "timeout", None), tool="drop_to_surface")
    clearance_mm = rs._mm(clearance_mm, "clearance_mm", 0.0, 10.0)
    if clearance_mm > settings["tolerance_mm"]:
        raise ValueError("clearance_mm must not exceed tolerance_mm, or a dropped item would still read as floating")
    if not dry_run and not settings["names"]:
        raise ValueError("drop_to_surface with dry_run=false needs names: it never acts on the live selection")
    if not dry_run and settings["time_budget_s"] < 5:
        raise ValueError("drop_to_surface with dry_run=false needs time_budget_s of at least 5 s to measure, "
                         "apply and verify")
    try:
        report = _measure(settings)
    except DialogBlocked as exc:  # only the read-only measurement is queued; nothing was moved
        raise PrecheckBlocked(exc, "drop_to_surface", "Nothing was moved") from exc
    upm = report["upm"]
    plan = rs.plan_drops(report, clearance=clearance_mm * upm, lift_penetrating=lift_penetrating)
    rows, summary, statuses, truncated = _rows(report, plan, True, limit)
    units = report["units"]
    result = _base(report)
    result.update(dry_run=dry_run, applied=False, clearance_mm=clearance_mm, group_mode=group_mode, summary=summary,
                  nodes=rows, nodes_truncated=truncated, notes=list(_NOTES))
    warnings = _warnings(report, statuses)

    def finish(refused: tuple[str, str] | None = None) -> dict[str, Any]:
        if refused:
            result["refused"] = {"code": refused[0], "message": refused[1]}
        if warnings:
            result["warnings"] = warnings
        return result

    try:
        moves, verify, companions = rs.moves_and_checks(report, plan, clearance=clearance_mm * upm)
    except ValueError as exc:
        return finish(("PLAN_CONFLICT", f"{exc}; nothing was moved."))
    moving = sorted((i for i, p in plan.items() if p["own"]), key=lambda i: (units[i]["min_z"], i))
    result.update(planned_moves=len(moving), stack_order=[units[i]["name"] for i in moving])
    if companions:
        result["companions"] = companions
        warnings.append(f"Reference twins move with their items: {_short([c['name'] for c in companions])}.")
    moved_units = {i for i, p in plan.items() if p["total"]}
    moving_twins = {m["handle"] for m in moves}
    outside = [d["name"] for d in report["dependents"] if d["unit"] in moved_units and d["handle"] not in moving_twins]
    if outside:
        warnings.append(f"These nodes sit on moved items but aren't in names, so they'd be left behind: {_short(outside)}.")
    by_reason: dict[str, list[str]] = {}
    for i, p in plan.items():
        by_reason.setdefault(p["reason"], []).append(units[i]["name"])
    held_twins = {c["unit"]: c["name"] for c in report["companions"] if c["not_movable"]}
    held = [n + (f" (twin {held_twins[i]})" if i in held_twins else "")
            for i, p in plan.items() for n in [units[i]["name"]]
            if p["reason"] in ("frozen", "locked_z", "linked", "xref", "constrained", "animated", "check_failed",
                               "companion_not_movable") and statuses.get(i) in ("floating", "penetrating")]
    if held:
        warnings.append(f"Not moved (frozen, locked, linked, constrained or animated): {_short(held)}.")
    if by_reason.get("dependent_not_movable"):
        warnings.append("Not moved because something resting on them can't move with them: "
                        f"{_short(by_reason['dependent_not_movable'])}.")
    if by_reason.get("cyclic_support"):
        warnings.append(f"These items support each other in a loop, so they aren't moved: {_short(by_reason['cyclic_support'])}.")
    if by_reason.get("penetrating_not_lifted"):
        warnings.append(f"Sunk items weren't lifted (lift_penetrating=false): {_short(by_reason['penetrating_not_lifted'])}.")
    if by_reason.get("too_deep_to_lift"):
        warnings.append("Sunk deeper than max_sink_mm, so not lifted (check them by hand): "
                        f"{_short(by_reason['too_deep_to_lift'])}.")
    if report["group_heads"] and group_mode is None:
        msg = ("names include groups (" + _short([g["name"] for g in report["group_heads"]]) + "): pass group_mode "
               "\"members\" to drop each prop, or \"rigid\" to move furniture or assemblies as one piece.")
        if dry_run:
            warnings.append("Planned with group_mode=\"members\". " + msg)
        else:
            return finish(("GROUP_MODE_REQUIRED", "Nothing was moved: " + msg))

    if dry_run or not moves:
        return finish()
    if report["unchecked"] or report["stop"] != "none":
        return finish(("INCOMPLETE", "The measurement didn't finish, so nothing was moved. Narrow names or raise "
                                     "time_budget_s."))
    if report["auto_key"]:
        return finish(("AUTO_KEY_ON", "Auto Key is on in 3ds Max, so nothing was moved."))
    remaining = settings["time_budget_s"] - (time.monotonic() - started)
    if remaining * 1000 < max(rs.MIN_APPLY_MS, report["elapsed_ms"]):
        return finish(("DEADLINE_BEFORE_APPLY", "Too little of time_budget_s was left to apply and verify; nothing was "
                                                "moved. Raise time_budget_s."))
    zs = [abs(m["z_before"]) for m in moves] + [abs(m["z_after"]) for m in moves]
    eps = max(upm * 0.01, max(zs) * 2e-7)
    label = "MCP drop to surface"
    script = rs.build_apply_script(settings, moves, verify, label=label, eps=eps, time_budget_s=remaining)
    outcome = rs.parse_apply(str(client.send_command(script).get("result", "")))
    if outcome["status"] == "refused":
        return finish((outcome["code"], outcome["detail"] + " Nothing was moved."))
    names_by_unit = {i: units[i]["name"] for i in units}
    result["verify"] = [{"name": names_by_unit.get(v["unit"]), "gap_mm": _mm(v["gap"], upm), "ok": v["ok"]}
                        for v in outcome.get("verify", [])]
    if outcome["status"] != "applied":
        result["rolled_back"] = True
        bad = [v["name"] for v in result["verify"] if not v["ok"]]
        if outcome["status"] == "rollback_failed":
            return finish(("ROLLBACK_FAILED", "The undo step was cancelled, but some nodes didn't return to where "
                                              "they were; check move_log against the scene."))
        why = ("ran out of time" if outcome["status"] == "rolled_back_deadline"
               else f"found items off their planned gap ({_short(bad)})" if bad
               else outcome.get("detail", outcome["status"]))
        result["move_log"] = _move_log(moves, units, companions, upm)
        return finish((outcome.get("code", "ROLLED_BACK"), f"The re-measure after moving {why}, so the undo step was "
                                                            "cancelled and nothing was moved."))
    result.update(applied=True, undo_label=label, move_log=_move_log(moves, units, companions, upm))
    return finish()


def _move_log(moves, units, companions, upm):
    names = {u["handle"]: u["name"] for u in units.values()}
    names.update({c["handle"]: c["name"] for c in companions})
    return [{"name": names.get(m["handle"]), "handle": m["handle"], "dz_mm": _mm(m["dz"], upm),
             "z_before": m["z_before"], "z_after": m["z_after"]} for m in moves]
