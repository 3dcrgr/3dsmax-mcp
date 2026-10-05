"""Read-only contact, gap and interpenetration check between mesh nodes."""
from __future__ import annotations

from typing import Any

from ..helpers.contact_check import (
    DEFAULT_TIME_BUDGET_S,
    DEFAULT_WORK_BUDGET,
    build_script,
    parse_report,
    validate_args,
    validate_budgets,
)
from ..server import client, mcp


@mcp.tool()
def contact_check(
    names: list[str] | None = None,
    against: list[str] | None = None,
    tolerance: float = 0.0,
    near_gap: float = 0.0,
    include_separate: bool = False,
    limit: int = 50,
    max_pairs: int = 200,
    max_faces: int = 200000,
    work_budget: int = DEFAULT_WORK_BUDGET,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
) -> dict[str, Any]:
    """Find meshes that pass through each other, touch, or float just short of contact.

    Scope: `names` checks those nodes pairwise. `names` + `against` checks each
    name only against the `against` nodes (e.g. all legs against the floor and
    seat). With neither, the current selection is used, or all visible geometry
    when nothing is selected. Only pairs whose world bounding boxes come within
    near_gap are measured. Non-mesh nodes in names/against are skipped with a warning.

    Status per pair, most severe first:
      penetrating  - a vertex lies inside the other closed mesh deeper than tolerance
      intersecting - surfaces cross with no vertex inside (thin parts, open meshes)
      touching     - closest vertex within tolerance of the other surface
      near_gap     - clearance above tolerance but within near_gap (floating parts)
      separate     - hidden unless include_separate=true

    tolerance and near_gap are always MILLIMETRES (0 = default 0.1 mm / 10 mm),
    converted to scene units; depth, gap, points and the echoed tolerance/near_gap
    are in scene units (tolerance_mm/near_gap_mm echo the input). depth is a lower
    bound, gap an upper bound; evaluated meshes, world space. An open mesh has no
    inside, so a part piercing it reports intersecting. Read only.

    Cost: dense meshes (foliage, garlands, 100k+ verts) are slow, ~seconds per
    pair. Pairs run cheapest first until work_budget (sum of both vertex counts
    per pair) or time_budget_s (max 45 s, counted from the send, so queue wait
    counts) runs out; then complete=false, unchecked_pairs and heaviest_nodes
    say what was skipped.
    """
    names_l, against_l, tol_mm, near_mm, max_pairs, max_faces = validate_args(
        names, against, tolerance, near_gap, max_pairs, max_faces)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    work_budget, time_budget_s = validate_budgets(work_budget, time_budget_s, getattr(client, "timeout", None))
    script = build_script(names_l, against_l, tol_mm, near_mm, max_pairs, max_faces,
                          work_budget=work_budget, time_budget_s=time_budget_s)
    response = client.send_command(script)
    report = parse_report(str(response.get("result", "")), include_separate=include_separate, limit=limit)
    report["notes"] = [
        "depth is sampled at vertices: a coarse mesh can hide a deeper overlap between its vertices.",
        "gap is vertex to surface; edge-to-edge clearance between two coarse meshes can be smaller.",
        "Parent-child and grouped pairs are checked like any other; a leg seated into a seat by design reports penetrating.",
    ]
    return report
