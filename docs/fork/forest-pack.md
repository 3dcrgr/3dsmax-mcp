# Forest Pack: items, custom edits, library presets and list checks

Added on 2026-10-06, from a live study of Forest Pack Pro 9 (`Forest_Pro.version()` = 900) in a scratch 3ds Max 2026 with centimetre units.

## Why

The existing `scatter_forest_pack` returned success for scatters with no items. Its `density` parameter wrote `maxdensity`, which does nothing, and its "up" facing mode aligned items to the surface normal. Agents also had no way to read item positions, edit items, place library presets or find out why a forest was empty. Presets merged by hand gave 0 items.

## What changed

**`scatter_forest_pack` (fixed):**
- Density now sets the tile size, `units_x`/`units_y`. `density` is a percentage of `density_units_x_cm`/`density_units_y_cm`: 100 keeps them, 200 gives twice the items. The new `items_per_m2` sets both to 100 × √(1437 / items_per_m2) cm.
- `facing_mode` 0 or `"normal"` writes `direction = 0` (surface normal); 1 or `"up"` writes `direction = 100` (world up). It used to write 1 for "up".
- It uses the minimal verified recipe: `surflist`, the one Surface Area set active, `namelist`, `conamelist`, `cobjlist`, `geomlist = 2`, `problist`, and width/height from each source's bounding box (height is now the real height).
- It builds the forest and returns the real `count`, an `estimated_count` and the surface area. A scatter with 0 items fails with `ZERO_ITEMS`; the object stays in the scene for inspection.
- Inputs are validated in Python first; the object is created in one undo step and gets a unique name. It returns a dict, not a JSON string.

**New tools** (module `maxmcp/tools/forest.py`, helpers in `maxmcp/helpers/forest.py` and `forest_library.py`):
- **`forest_items`** (read only) pages through items: pivot `position`, an instance-ready `transform`, `geom_id`, `size`, and optionally `item_name`, `source`, `rotation_z`, `scale`, `seed`. It builds the forest first. `refresh_surfaces` pulses `surfanim`.
- **`forest_edit_items`** moves, deletes, creates and resizes items, and sets rotation, seed or item type, in one undo step. A procedural forest is refused unless `allow_custom_edit=true`, which builds it and switches it to custom edit first.
- **`forest_set_mode`** switches between `procedural` and `custom_edit`. Going back to procedural needs `confirm_discard=true`.
- **`forest_library`** (Python only) searches every library `index.xml`: presets, 3D items and 2D billboards.
- **`forest_from_library`** places a preset or 3D item on surfaces, with optional include/exclude splines and a material library swap. It fails with `ZERO_ITEMS` when nothing is scattered.
- **`forest_validate`** checks the item and area lists, builds the forest and reports the count. `fix=true` makes conservative repairs.

## Verified Forest Pack facts

These were measured live; the tools rely on them.

**Building and reading**
- The item list is built lazily. `classOf` doesn't build it; `trees.update()` does (12–21 ms for 10–23k items). Every tool calls it before reading.
- `trees.*` indices are 0-based. `getGeomID` is 1-based into the item lists. Out-of-range reads return undefined or -1; out-of-range writes do nothing, without an error.
- `getPosition` is the 2-D distribution point (z is always 0). `getRotation` and `getSize` return stored per-item values; the random rotation and scale exist only in `getFullTransform`.
- Item data is in the forest's object space, relative to `node.objectTransform` (which includes the forest's own pivot offset).
- `getFullTransform i` maps the source object's *object-space* vertices. Its translation is not the item's pivot: for a source whose pivot is offset from its geometry it was 170 cm away.
- **srcOffsetTM** is the source object's pivot offset: `scaleMatrix objectOffsetScale * (objectOffsetRot as matrix3) * transMatrix objectOffsetPos`. An instance of the source placed at `inverse(srcOffsetTM) * getFullTransform i * forest.objectTransform` matched a mesh snapshot of the forest exactly (with the source's pivot moved, the source rotated 20° and the forest rotated 30° and moved). `forest_items` returns that matrix as `transform` and its translation as `position`.
- Forest Pack caches surfaces. After a surface moves, item Z changes only after a `surfanim` pulse (`surfanim = true`, `trees.update()`, back to false). Reassigning the same `surflist` doesn't refresh it.

**Density and lists**
- Density is set by `units_x`/`units_y`, the tile size: about 1437 items per tile with the default density map (`dense.bmp`, threshold 50). On a 10 m plane, 1000 cm tiles gave 1437 items and 300 cm tiles 16023. `maxdensity` had no effect from 1 to 1000. Very small tiles saturate: 100 cm on 100 m² gave 35800, not 143700.
- `namelist` is the master list. On a rebuild, Forest Pack pads every other item list to its length with 0, 0.0 or undefined; padded items silently produce nothing or empty items at the forest's origin. Nothing raises an error.
- `geomlist`: 0 = nothing, 1 = billboard, 2 = custom object from `cobjlist`. **3 crashed Max** (access violation) on a rebuild with no proxy, and that object never produced items again. No tool writes 3, and no tool rebuilds a forest whose `geomlist` holds anything but 0, 1 or 2 (`CRASH_RISK`).
- `direction` blends from the surface normal (0) to world up (100).
- The minimal working forest: `surflist`, `namelist`, `geomlist` (1 or 2), `pf_aractivelist = #(true)` and a tile size suited to the surface. `cobjlist` is needed for visible geometry. `arnodelist` isn't needed for the Surface Area.
- `pf_aractivelist` is the area "active" flag. Forest Pack always keeps one type-3 Surface Area entry and appends an inactive one when it is missing. Areas add up: the Surface Area plus an include spline gives double density inside the spline; an exclude spline (`arincexclist` 1) cuts a hole.

**Custom edit**
- Per-item writes work only in custom edit mode, `mode = 1`. In `mode = 0` every write is ignored.
- Switching to `mode = 1` freezes the items as last built; on a forest never built it froze an empty set. So the forest is built in mode 0 first.
- Setting `mode = 0` discards the edits for good; setting it back to 1 doesn't bring them back. Edits survive seed and tile changes, node moves, surface refreshes and save/merge.
- `setRotation` takes radians and only Z takes effect. `setSize` takes absolute sizes `[w, w, h]`. `create p w h geomid` appends at index count−1 and projects onto the surface; a point off every surface is kept unprojected.
- Deletes shift the later indices down at once, so the tools delete highest first.
- With random translation on, a moved item's pivot lands off the requested point.
- World to object space: `p * inverse(forest.objectTransform)`.

**Library and presets**
- Library roots are listed in `HKCU\Software\Itoo Software\Forest Pack Pro\LibraryDir` (this machine has a local and a network root); the default is `%LOCALAPPDATA%\Itoo Software\Forest Pack Pro\lib`. Each folder's `index.xml` lists `<item class="maxobject">` entries (`maxfile`, `object`, `matlib`, `material`) and `tree_billboard` entries (2D, with `<library>`). `$(LIBROOTPATH)` is the root; other relative paths are relative to the index folder.
- Merged alone, a preset's `cobjlist` and spline `arnodelist` entries come back undefined. `conamelist` names the source objects (`namelist` is only a label and can be in another order).
- Merging the forest and its sources in **one** `mergeMAXFile` call relinks `cobjlist`, even when Max renames a duplicate. Merging them in two calls and wiring by name broke when Max renamed `_fp_wildgrass_09` to `_fp_wildgrass_001`.
- **Stock presets ship their Surface Area inactive.** That is why a retargeted preset gave 0 items. Activating it gave 23473 items on a 10 m plane.
- `loadTempMaterialLibrary` loads a `.mat` (about 0.4 s for 24 materials). Replacing `matList[i]` with the library material of the same name makes Forest Pack rebuild its `forest_automat_*` Multi/Sub.

**Export**
- `Forest_Pro.ForestPack.exportData file fields` is class level (`node.ForestPack` doesn't exist). It always writes **every** forest in the scene, whatever is selected, hidden or disabled: 82 MB for 12 forests. No tool uses it; `forest_items` reads one forest through `trees.*`.

## How the tools work

**`forest_items`.** One script: build (and pulse `surfanim` with `refresh_surfaces`, outside the undo stack), count items per type, then return the page. Types without a valid source return the raw item TM, flagged `no_source`. `limit` is at most 5000; a full transform costs about 100 bytes per item.

**`forest_edit_items`.** Python validates every edit (ops, keys, indices, sizes, seeds) and refuses an index both edited and deleted. In Max: refuse an open undo, a `geomlist` crash risk, an unknown mode, or a procedural forest without `allow_custom_edit`; check item types (1 or 2, with a source for 2) and indices against the built count. Then, in one undo step: switch to custom edit if needed (and switch back without editing if the frozen count differs), apply edits, then deletes from the highest index, then creates, then rebuild. Edited and created items are read back with the stored point and the actual pivot.

**`forest_from_library`.** Python finds the item by uuid, object or English name (ambiguous names list the uuids). In Max:
1. Outside the undo stack, merge the object alone (the probe). A Forest object gives the source names from `conamelist` (falling back to `namelist`) that exist in the file, and the names of its spline areas, which are dropped. A plain mesh makes it a 3D item. The probe is deleted.
2. In one undo step, merge the object plus its sources in one call. A 3D item becomes the only source of a new forest (`items_per_m2`, default 300 cm tiles).
3. Retarget: `surflist` = the surfaces; the area lists are cut to the Surface Area; it is active unless include splines are given; include splines are appended as active spline areas and exclude splines as active exclusion areas.
4. Optionally swap materials from the item library's `.mat` for `matlib_renderer` (presets: `matList` by name; 3D items: the `<material>` on the source). Hide the merged sources. Build and return the count, the item types and their linked sources, the areas, the merged nodes and the materials swapped or missing.

Merges use `#autoRenameDups #useSceneMtlDups #neverReparent quiet:true`. The live recipe used `#useMergedMtlDups`; `#useSceneMtlDups` is used instead so a merge never replaces a material already in the scene (for example one converted for Vantage).

**`forest_validate`.** One read script collects the list lengths, every item and area entry, the surfaces (with their upward area) and the count; Python analyses them. Errors: item lists shorter than `namelist`, `geomlist` 3 or another invalid value (the forest is then not rebuilt), `geomlist` 0, missing sources, probabilities or sizes ≤ 0, unequal area lists, more than one Surface Area, no active area, an active spline area without a spline, an active Surface Area with no surfaces, missing surfaces, 0 items. Warnings: lists longer than `namelist`, short auxiliary lists, `conamelist` mismatches, a missing Surface Area entry, a tile too large for the surface, custom edit mode, disabled or manual update. The estimate covers the Surface Area with the default density map only.

`fix=true` pads short item lists (sources relinked by a unique matching name, `geomlist` 2 where a source exists and 0 otherwise, probability 100, size from the source's bounding box), relinks missing sources of type-2 items the same way, and pads short area lists with the defaults of a new spline area (inactive), in one undo step. A second script checks the lengths it read haven't changed (`STALE`), applies the plan and checks again. It never truncates a list (a long list may mean `namelist` lost an entry), never changes `geomlist` 3, and never activates an area.

## Limits

- Not tested live yet. The MAXScript recipes underneath were verified one by one, but these tools as a whole haven't run in Max.
- Whether Max's undo restores a merge, and whether Forest Pack records custom item edits in the undo stack, is not verified. Undoing a switch to custom edit sets `mode` back to 0, which discards the edits.
- A forest whose `geomlist` holds 3 (perhaps valid with a proxy) is refused by every tool that rebuilds it.
- Indices in procedural mode change when the forest rebuilds.
- Include splines with the Surface Area inactive were verified with the spline at the surface's height only.
- 2D billboard items can't be placed. `.pmat` material libraries are refused.
- 3D library items get Forest Pack's default viewport mode; the presets use `vmesh = 3`, which shows no meshes in viewports (not an error).
- The density estimate assumes the default density map and counts the Surface Area only.

## Status

- **Unit tests** in `tests/test_forest_tools.py`: input validation, the generated scripts (order of build, switch, edits, deletes and creates; probe before the one-call merge; no file commands, no `geomlist` 3, balanced parentheses, one argument per `format` placeholder), parsing of canned reports, the library scan on a temporary library, the validator's analysis and fix plan, and registration.
- **Not tested live yet.**
