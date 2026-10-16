# Floating and sunk props: `resting_check` and `drop_to_surface`

Upstream pull request: **Scene checks**, not opened yet ([list](../FORK.md#upstream-pull-requests)).

Fork issue #17 (feature request, 2026-10-06).

## What happened

Props on a dining table (placemats, plates, cutlery, glasses) floated 0.2–2.2 cm above the table and its runner. An agent-written float check hung Max for about 2½ minutes:
- It looped every vertex in MAXScript.
- It cast the global `intersectRay` per cell against every overlapping support.
- It couldn't be stopped.

An earlier attempt cast rays *up* into each item to find its underside. `intersectRay` ignores back faces, so single-sided cloth and sparse garland needles returned their upper surfaces, and 93 objects were sunk up to 7 cm into the table.

## What changed

Two new tools share one helper (`maxmcp/helpers/resting.py`). Each sends one bounded MAXScript, plus a second for an applied drop.

- **`resting_check`** (read only) checks `names`, else the selection, never the whole scene; at most 500 items. A group is split into its mesh members, or is one item with `group_mode="rigid"`. It reports, per item:
  - `status`: `penetrating` or `floating` (by more than `tolerance_mm`, default 0.5), `no_support` (nothing within `max_drop_mm`, default 50, below), `resting` or `unchecked`. Resting items are listed only with `include_resting=true`;
  - `min_gap_mm`, `gap_p10_mm`, `gap_p50_mm` and `contact_fraction`;
  - the support it rests on, the three closest supports, and the world point of the smallest gap.
- **`drop_to_surface`** plans the same measurement into world-Z moves. `dry_run` is true by default.
  - Floating items drop until their lowest point sits `clearance_mm` (default 0.2, never more than `tolerance_mm`) above their support.
  - Items already resting follow any support that moves, so a plate on a dropped placemat goes down with it.
  - Sunk items are lifted only with `lift_penetrating=true`.
  - An applied drop needs `names` (never the live selection) and at least 5 s of `time_budget_s`. When `names` include a group it also needs `group_mode`:
    - `members` drops each prop of a table-setting group;
    - `rigid` moves furniture or assemblies as one piece.

**Measuring:**
- **Undersides:** the lowest point per cell of an XY grid over the item's footprint (`samples`, default 64), from its evaluated mesh. Points come from vertices, plus points on every face larger than a cell (a box, a 1×1 plane), for meshes up to 20,000 faces; never from rays into the item, so cloth and needles measure correctly. Dense meshes are read at every n-th vertex above `max_item_verts` (default 100,000; flagged `sampled`). Items above 20 × `max_item_verts` vertices aren't read: they're `unchecked` (`too_dense`), and the result is incomplete.
- **Gaps:** one double-sided segment per cell, cast straight down against each support, from `max_sink_mm` (default 20) above the point to `max_drop_mm` below it. Each support gets one `RayMeshGridIntersect`, built once and freed on every path.
  - A parity test (distinct crossings straight up) runs first. It tells a sunk point from an overhang such as a plate rim over a fork, and gives the exact depth.
  - Within `max_sink_mm`, a point under an open sheet (a 1-plane floor) counts as sunk. Deeper, only a closed solid counts: odd crossings downwards too, with the surface above facing up and the one below facing down (both away from the point). So an item under a ceiling or a single-plane tabletop isn't read as 70 cm deep.
  - `lift_penetrating` never lifts an item sunk deeper than `max_sink_mm`; it reports `too_deep_to_lift` instead.
- **Supports**, unless `supports` lists them (it can't be combined with `exclude_supports`):
  - visible, renderable meshes that start below the item's median underside (or lie wholly at or below it, such as a zero-thickness mat under a cup, but never the cup on that mat), lie under its footprint and reach within `max_drop_mm` of its lowest point;
  - never the item's own descendants. Its parent counts: a cup linked to the tray it stands on rests on the tray.
  - The other items always count too, even with explicit `supports`, so stacks work.
  - Scatter objects (Mesher, Forest Pack, RailClone, tyFlow, particles, fur) and meshes above `max_support_faces` (default 1,000,000) are left out, and `exclude_supports` removes others.
  - Coincident duplicates of an item (same transform *and* geometry, such as the stacked copies from #16) are ignored and reported.
  - Hidden and non-renderable meshes under an item are never supports. The result names them in a warning, but they don't hold the item.
- **Twins:** reference twins (same base object and transform, such as `*_ENMESH` copies) move with their item. When both are named, they become one item. A twin that can't move holds its item, and the warning names the twin.
- **Names:** a name `getNodeByName` misses (seen live on linked children) is found by a scene scan; `contact_check` does the same.
- **Budget:** `time_budget_s` counts from the send, like `contact_check`: default 20 s (30 s for `drop_to_surface`), at most 45 s. Items it didn't reach are `unchecked`, with `complete: false`.

**Planning:**
- **Order:** items are planned in support order and ancestry order; items that support each other in a loop are held. An item that only moves because its parent moves has the reason `follows_parent`.
- **Pinning:** an item that can't move but would lose its support pins that support, and through it the supports below.
- **Blocked:** an item over something left out of the measurement (scatter, excluded, not a mesh, above the face limit) is never moved. Its gap may be to a surface further down.

**Applying** (`dry_run=false`):
- **Refuses, moving nothing,** with the code in `refused`:
  - `AUTO_KEY_ON`: Auto Key is on;
  - `USER_BUSY`: an undo operation is open or suspended;
  - `INCOMPLETE`: the measurement was incomplete;
  - `DEADLINE_BEFORE_APPLY`: too little budget is left;
  - `GROUP_MODE_REQUIRED`: `names` include a group and `group_mode` isn't given;
  - `SCENE_CHANGED` or `NODE_GONE`: a node it would move changed height, or was deleted, since it was measured;
  - `PLAN_CONFLICT`: the plan would move one node by two amounts.
- **A dialog in Max** during the measurement gives `BLOCKED_BY_DIALOG`. Nothing was moved, and the call can be repeated once the dialog is answered.
- **Never moves:**
  - frozen (including by layer), Z-locked, linked (Revit), xref, constrained or animated nodes;
  - in rigid mode, a group with any such member;
  - an item whose twin can't move.
- **One undo step,** "MCP drop to surface":
  - It moves with animation off, then checks every node is at its planned Z.
  - Inside the step it re-measures every moved and carried item against one fresh grid per support, with the hold suspended so the grids aren't recorded.
  - It cancels the step if any item ends more than `tolerance_mm` off its planned gap (`ROLLED_BACK`), if time runs out (`ROLLED_BACK`), or if a node isn't at its planned Z (`READBACK_FAILED`). The result then has `rolled_back: true`.
  - After a cancel it checks every node is back, and puts stragglers back by hand (`ROLLBACK_FAILED` if that fails).
- **Result:** `applied: true`, `undo_label`, `verify` (each item's re-measured gap) and `move_log`, which lists each node with `dz_mm` and `z_before`/`z_after` (world Z in scene units). The Max global `mcpLastDrop` keeps the last outcome, in case a reply is lost.
- **Left behind:** nodes outside `names` that sit on a moved item stay where they are, and the result names them. They're found only when `supports` isn't given.

## Limits

- **Moves are along world Z only.** A tilted item isn't rotated to lie flat; that's the user's call.
- **Proxies (VRayProxy and similar)** are measured from their viewport preview mesh.
- **Measurements are at the current frame.**
- **Members mode on furniture groups** drops their attached parts. Use `group_mode="rigid"` for furniture.

## Status

- **Unit tests** in `tests/test_resting_check.py`: validation, report parsing, classification, and the plan: following, inheritance, child-below-parent order, pinning, cycles, blockers, twins as supports, twins of carried items, sunk followers, lifting. Plus the apply paths, the refusals, a dialog during the measurement, and the script contents.
- **Review:** an adversarial review in four lenses (fixed in `cd72a1f`).
- **Live, in a scratch 3ds Max 2026,** over three rounds; `cd72a1f` (with the review), `c52260c`, `26d1b0c` and `555c19a` fix what they found. The last round checked:
  - a cup on a zero-thickness plane mat (it had been pushed 4.8 mm through it);
  - a 60 cm unsubdivided board over a 2 cm rail (it had been dropped through the rail);
  - items sunk 30 mm into closed boxes: the depth is exact, nothing deeper than `max_sink_mm` is lifted, and 40 mm lifts were verified;
  - a chair 5 mm above the floor of a one-mesh room (floor and ceiling planes) reads floating, whichever way the ceiling faces;
  - hidden meshes under an item give a warning, and names of linked children resolve when `getNodeByName` misses them;
  - an applied drop on 14 items moved exactly the 7 expected, to 0.2 mm, and one undo restored everything.
- **Deployed** from `fix/fork-fixes` (`555c19a`) on 3ds Max 2026 on 2026-10-06.
- **3ds Max 2027:** since 2026-10-11 the MCP server runs from this branch's source, so both tools are live on 2027, but they haven't been tested there yet. They're Python only, so they don't need the fork's bridge.
- **Production scenes:** not tested yet. Test on scratch objects first.
