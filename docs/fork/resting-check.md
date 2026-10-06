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
  - It moves only items that float, until their lowest point sits `clearance_mm` (default 0.2) above the support.
  - Items already resting follow any support that moves, so a plate on a dropped placemat goes down with it.
  - Sunk items are lifted only with `lift_penetrating=true`.

**Method:**
- **Undersides:** each item's lowest vertex per cell of an XY grid over its footprint (`samples`, default 64), from its evaluated mesh. They come from vertices, never from rays into the item, so cloth and needles measure correctly. Dense meshes are read at every n-th vertex above `max_item_verts`, and flagged `sampled`.
- **Rays:** one double-sided segment per cell, cast straight down against each support. It runs from `max_sink_mm` above the point (to see sinking) to `max_drop_mm` below it. Each support gets one `RayMeshGridIntersect`, built once and freed on every path.
- **Supports**, unless `supports` lists them:
  - visible, renderable meshes that start strictly lower than the item, lie under its footprint and reach within `max_drop_mm` of it. Starting lower keeps stacks acyclic, and a cloth draped over an item is never its support.
  - Scatter objects (Mesher, Forest Pack, RailClone, tyFlow, particles, fur) are left out.
  - Coincident duplicates of an item, such as the stacked copies from #16, are ignored and reported.
  - `exclude_supports` removes others.
- **Groups:** a group head in `names` expands to its mesh members (`group_mode="members"`). `group_mode="rigid"` treats the whole group as one item and moves its head.
- **Twins:** reference twins (same base object and transform, such as `*_ENMESH` copies) move with their item.
- **Budget:** the time budget counts from the send, like `contact_check`, at most 45 s. Items it didn't reach are `unchecked`, with `complete: false`.

**Applying** (`dry_run=false`):
- **Refuses, moving nothing,** while Auto Key is on, an undo operation is open, the measurement was incomplete, or less than 3 s of budget is left.
- **Never moves** frozen, Z-locked, linked (Revit), xref, constrained or animated nodes, or an item whose twin is one of those. Items on them are planned against them where they are.
- **One undo step,** "MCP drop to surface". Before moving, it checks that every node is still where it was measured. Inside the step it re-measures every moved item against fresh grids of its supports. If any ends up floating or sunk beyond `tolerance_mm`, it cancels the step, so nothing moves.
- **Result:** `move_log` lists each node with `dz_mm`, `z_before` and `z_after`.
- **Left behind:** nodes outside `names` that sit on a moved item stay where they are, and the result names them.

## Limits

- **Gaps run straight down from the lowest vertex per cell.** A tilted item moves only in Z: rotating it to lie flat is the user's call. A knife on a plate rim needs more `samples`.
- **Proxies (VRayProxy and similar)** are measured from their viewport preview mesh, not the render mesh.
- **Supports are evaluated at the current frame,** and so are the moves.

## Status

- Unit tested in `tests/test_resting_check.py`: validation, report parsing, classification, the stack plan (following, inheritance, frozen supports, twins, lifting) and the apply paths.
- **Not tested live yet:** test on scratch objects first, never on production props.
