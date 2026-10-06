# Floating and sunk props: `resting_check` and `drop_to_surface`

Fork issue #17 (feature request, 2026-10-06).

## What happened

Props on a dining table (placemats, plates, cutlery, glasses) floated 0.2–2.2 cm above the table and its runner. An agent-written float check hung Max for about 2½ minutes:
- It looped every vertex in MAXScript.
- It cast the global `intersectRay` per cell against every overlapping support.
- It couldn't be stopped.

An earlier attempt cast rays *up* into each item to find its underside. `intersectRay` ignores back faces, so single-sided cloth and sparse garland needles returned their upper surfaces, and 93 objects were sunk up to 7 cm into the table.

## What changed

Two new tools share one helper (`maxmcp/helpers/resting.py`). Each sends one bounded MAXScript, plus a second for an applied drop.

- **`resting_check`** (read only) reports, per item:
  - `status`: `penetrating`, `floating`, `no_support`, `resting` or `unchecked`;
  - `min_gap_mm`, `gap_p10_mm`, `gap_p50_mm` and `contact_fraction`;
  - the support it rests on, the three closest supports, and the world point of the smallest gap.
- **`drop_to_surface`** plans the same measurement into world-Z moves. `dry_run` is true by default.
  - Floating items drop until their lowest point sits `clearance_mm` (default 0.2) above their support.
  - Items already resting follow any support that moves, so a plate on a dropped placemat goes down with it.
  - Sunk items are lifted only with `lift_penetrating=true`.
  - An applied drop needs `names` (never the live selection) and at least 5 s of `time_budget_s`. When `names` include a group it also needs `group_mode`:
    - `members` drops each prop of a table-setting group;
    - `rigid` moves furniture or assemblies as one piece.

**Measuring:**
- **Undersides:** the lowest point per cell of an XY grid over the item's footprint (`samples`, default 64), from its evaluated mesh. Points come from vertices, plus points on every face larger than a cell (a box, a 1×1 plane), for meshes up to 20,000 faces; never from rays into the item, so cloth and needles measure correctly. Dense meshes are read at every n-th vertex above `max_item_verts` (flagged `sampled`). Items above 20 × `max_item_verts` vertices are skipped (`too_dense`).
- **Gaps:** one double-sided segment per cell, cast straight down against each support, from `max_sink_mm` above the point to `max_drop_mm` below it. Each support gets one `RayMeshGridIntersect`, built once and freed on every path.
  - A parity test (distinct crossings straight up) runs first. It tells a sunk point from an overhang such as a plate rim over a fork, and gives the exact depth.
  - Within `max_sink_mm`, a point under an open sheet (a 1-plane floor) counts as sunk. Deeper, only a closed solid counts: odd crossings downwards too, with the surface above facing up and the one below facing down (both away from the point). So an item under a ceiling or a single-plane tabletop isn't read as 70 cm deep.
  - `lift_penetrating` never lifts an item sunk deeper than `max_sink_mm`; it reports `too_deep_to_lift` instead.
- **Supports**, unless `supports` lists them:
  - visible, renderable meshes that start below the item's median underside (or lie wholly at or below it, such as a zero-thickness mat under a cup, but never the cup on that mat), lie under its footprint and reach within `max_drop_mm` of its lowest point;
  - never the item's own descendants. Its parent counts: a cup linked to the tray it stands on rests on the tray.
  - The other items always count too, even with explicit `supports`, so stacks work.
  - Scatter objects (Mesher, Forest Pack, RailClone, tyFlow, particles, fur) are left out, and `exclude_supports` removes others.
  - Coincident duplicates of an item (same transform *and* geometry, such as the stacked copies from #16) are ignored and reported.
  - Hidden and non-renderable meshes under an item are never supports. The result names them in a warning, but they don't hold the item.
- **Twins:** reference twins (same base object and transform, such as `*_ENMESH` copies) move with their item. When both are named, they become one item. A twin that can't move holds its item, and the warning names the twin.
- **Names:** a name `getNodeByName` misses (seen live on linked children) is found by a scene scan; `contact_check` does the same.
- **Budget:** the time budget counts from the send, like `contact_check`, at most 45 s. Items it didn't reach are `unchecked`, with `complete: false`.

**Planning:**
- **Order:** items are planned in support order and ancestry order; items that support each other in a loop are held. An item that only moves because its parent moves has the reason `follows_parent`.
- **Pinning:** an item that can't move but would lose its support pins that support, and through it the supports below.
- **Blocked:** an item over something left out of the measurement (scatter, excluded, not a mesh, above the face limit) is never moved. Its gap may be to a surface further down.

**Applying** (`dry_run=false`):
- **Refuses, moving nothing,** while:
  - Auto Key is on;
  - an undo operation is open or suspended;
  - the measurement was incomplete;
  - too little budget is left.
- **Never moves:**
  - frozen (including by layer), Z-locked, linked (Revit), xref, constrained or animated nodes;
  - in rigid mode, a group with any such member;
  - an item whose twin can't move.
- **One undo step,** "MCP drop to surface":
  - It checks every node is still where it was measured, then moves with animation off.
  - Inside the step it re-measures every moved and carried item against one fresh grid per support, with the hold suspended so the grids aren't recorded.
  - If any item ends more than `tolerance_mm` off its planned gap, it cancels the step.
  - After a cancel it checks every node is back, and puts stragglers back by hand (`rollback_failed` if that fails).
- **Result:** `move_log` lists each node with `dz_mm` and `z_before`/`z_after` (world Z in scene units). The Max global `mcpLastDrop` keeps the last outcome, in case a reply is lost.
- **Left behind:** nodes outside `names` that sit on a moved item stay where they are, and the result names them.

## Limits

- **Moves are along world Z only.** A tilted item isn't rotated to lie flat; that's the user's call.
- **Proxies (VRayProxy and similar)** are measured from their viewport preview mesh.
- **Measurements are at the current frame.**
- **Members mode on furniture groups** drops their attached parts. Use `group_mode="rigid"` for furniture.

## Status

- **Unit tests** in `tests/test_resting_check.py`: validation, report parsing, classification, and the plan: following, inheritance, child-below-parent order, pinning, cycles, blockers, twins as supports, twins of carried items, sunk followers, lifting. Plus the apply paths and the script contents.
- **Reviews:** an adversarial review in four lenses, and a live run in a scratch 3ds Max 2026 (V-Ray 7, cm).
- **Live run results:**
  - The first version found correct statuses and stacked drops: runner −1.8 mm, placemat −4.6, plate −8.4, the glass following. One undo reverted it, and the refusals and deadlines worked.
  - It also exposed five wrong-result cases, which the review fixes cover:
    - a cup linked to its tray;
    - a twin as a support;
    - an origin-pivot slab;
    - both twins named;
    - a twin carried by its parent.
- **Production:** not tested live yet. Test on scratch objects first.
