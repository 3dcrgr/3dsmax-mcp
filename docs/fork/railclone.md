# RailClone: inspect, parameters, library, and two fixes

Two live test sessions on 2026-10-06 (3ds Max 2026.3, RailClone Pro 7.1.1, V-Ray 7, centimetres) found two bugs in the RailClone tools and worked out how RailClone behaves from MAXScript. This page covers the fixes, the four new tools and the facts they rely on. The changes are all Python and need no bridge change.

## Fixes

**`get_railclone_style` and `set_railclone_style` failed on many styles.**
- RailClone 7.x writes some attributes twice on graph nodes, for example `x="-298.322" y="-206.678" y="-206.678"`. That isn't well-formed XML. In a sample of 43 stock items, 7 styles (versions 210 and 300) had it, among them Metal Fence 1 and Plank Cladding 1.
- `get_railclone_style` raised "Invalid XML: duplicate attribute". `set_railclone_style` refused such XML in Python, and its in-Max check (`XmlDocument.LoadXml`) would have refused it too.
- **Now** both tools parse a copy with repeated attributes removed (`(\s([\w:.-]+)="([^"]*)")(?:\s\2="[^"]*")+` → first one kept), in Python and in Max (`Regex.Replace` before `LoadXml`).
  - `get_railclone_style` still returns the XML verbatim and reports `duplicate_attributes_removed`.
  - `set_railclone_style` sends the de-duplicated XML to `setXMLStyle` and reports `sent_duplicates_removed`; RailClone accepted that live. Tokens and the stale check are unchanged: they hash the XML as RailClone wrote it.
  - The duplicates seen live always had the same value. If they differ, the first is kept and a `DUPLICATE_ATTRIBUTE_CONFLICT` warning says so.

**`get_railclone_output` returned stale positions.**
- Moving a RailClone node, or copying it with MAXScript `copy`/`instance`, doesn't re-evaluate it. The cached output moves with the node (double offset) until something forces an evaluation. `notifyDependents`, `snapshotAsMesh` and redraws don't.
- **Now** it rebuilds first by default (`rebuild=true`): `railclone.segmentsUpdate 1 <segment count>`, or `seed = seed` when there are no segments, with undo off. `rebuild=false` returns the cached export with a `NOT_REBUILT` warning.
- Items get `placeholder: true` for empty slots: no `source_segment` and an inverted sentinel box near ±2.147e9. In the Glass Railing 03 sample, 23 of 109 items were placeholders. They are kept in the page, and left out of `placed`, `by_segment` and the bounds.
- `tm` and `box` are in the RailClone object's local space. The result adds `space: "object"`, `node_tm` (the object transform, rows 1 to 4), `bounds_object`, and `bounds_world` (each item box's 8 corners through `node_tm`).
- A base shared by instances anchors its output to one node only; the result warns `SHARED_BASE`.
- All earlier fields are unchanged.

## New tools

- **`railclone_inspect(name, handle, include_graph=true)`**, read only (it rebuilds with undo off before reading the output; the style description is read before that rebuild, because `getStyleDesc()` empties the output export), one call:
  - **node:** style route, style description, saved version, plugin version, material (name, class, ids), raw settings (`disabled`, `seed`, `gscale`, `vmesh` display mode, `rmesh` render mode, ...), `maxseg`/`maxfaces`/`maxtime`, units, instancing.
  - **bases:** id, name, spline or surface, assigned node, start and length.
  - **params:** each exposed parameter with the value from the array its type uses, its limits, selector options, and with `include_graph` the graph inputs that use it.
  - **segments:** id, name, source node (none = embedded mesh), source class, `sobjmtl`, instance/slice/bend/renderable, and the item count.
  - **graph:** node counts by type and the exposed connectors (`_exp_*`) mapped to parameter ids.
  - **output:** counts by source segment and placeholders, after a rebuild. Use `get_railclone_output` for positions.
- **`railclone_set_param(params, name, handle)`**:
  - Keys are an exact parameter name or its id. An ambiguous or unknown name is refused, and so are separators.
  - The value goes to the array its type uses (table below). A selector accepts an option label or value, and writes the value to `pafloatval` and the 0-based option index to `paintval`. A distance accepts scene units or a string such as `"60cm"`.
  - Values outside the limits are refused when the parameter's limits are on (`palimit`), because RailClone doesn't clamp them.
  - It reads the parameters first (one call), then checks that the base and each parameter's id and type are unchanged (`STALE_PARAMS`), writes them in one undo step ("MCP RailClone params"), rebuilds, and returns before/after values plus item counts before/after and the change per segment.
  - If a dialog in Max holds that first read, nothing was written: the call fails with a retryable `BLOCKED_BY_DIALOG` that says no parameter was set (`PrecheckBlocked` from `cb4d0ff`, as in `clone_objects`). Answer the dialog, then repeat the call. A dialog during the write gives the usual `BLOCKED_BY_DIALOG`, which must not be repeated.
- **`railclone_library(query, category, root, limit=50)`**, Python only:
  - Reads the library browser roots from `HKCU\Software\Itoo Software\RailClone Pro\LibraryDir` (value name = root name, data = folder), walks each root's `index.xml` files and returns the matching items.
  - Each item: `route` (`\<root>\<category path>\<item name>`), category, name, `.max`/`.rail` file, object, material, matlib, uuid, thumbnail, and `load_method`.
  - Matlib macros are expanded where possible: `$(LIBROOTPATH)` is the root folder, `$(LIBFULLPATH)` the category folder, and a relative path is relative to the category folder. `$(RENDERER)` and `$(SELECTEDRENDERER)` stay, listed in `matlib_macros`.
  - A root that doesn't answer within 3 s (an offline share) is skipped with `ROOT_UNREACHABLE`. The scan stops after 20 s in all, or 5000 `index.xml` files in one root (`complete: false`).
  - The roots come from the registry as this server sees it. Under Claude Desktop's MSIX package that is a private copy of HKCU, which doesn't follow the real hive 3ds Max reads (on 2026-10-11 the package's copy of `RailClone Pro\LibraryDir` was last written on 10-08, the real key on 10-11). A root added, renamed, re-pointed or removed in RailClone's browser can therefore be missing or stale here. `railclone_from_library` doesn't rely on this list (below).
- **`railclone_from_library(item, splines, name, method="auto", fix_maps=true, allow_undo_flush=false)`**:
  - **Undo:**
    - **`.rail` items** load by route in one undo step ("MCP RailClone from library"): `RailClone_Pro()`, the route load, the bases, the position and the map fixes. A route load of a `.rail` item and `RailClone_Pro()` are recorded normally and keep the undo history (verified live on Max 2026). The result says `undoable: true`, `undo_history_cleared: false`.
    - **`.max` items** clear 3ds Max's whole undo history when they load, by merge or by route (RailClone's own browser does the same; verified live on Max 2026). `loadTempMaterialLibrary` clears it too, and `mergeMAXFile` is never recorded in undo. Such items are refused before anything reaches Max unless `allow_undo_flush=true`. With consent, the whole load runs with undo off: the merge or the route load, the material, the bases, the position and the map fixes. Nothing is copied and nothing is deleted. The result says `undoable: false`, `undo: null`, `undo_history_cleared: true`.
    - Any other item file type is handled like a `.max` item, since only `.rail` loads are known to keep the history.
    - `remove_with` lists the nodes the call created: the RailClone, then any other nodes a merge brought along. Deleting them removes the load. Agents never run undo through MCP, so this is how an agent removes a load of either kind.
    - Why no copy: `5ae0338` merged first, made a `copy` inside the undo step and deleted the merged original with undo off afterwards. That passed one live undo on Max 2026, but the same pattern crashed Max in Forest Pack's `forest_from_library`: the undo record held a node that was then deleted with undo off, and a later undo crashed. Material and `banode` assignments on such a copy can store old values that point at the deleted original. Now no undo record can refer to a node deleted with undo off.
    - **On errors** the created nodes are removed and the selection is restored. With an undo step, the step is cancelled, which removes the new node; anything the cancel leaves is deleted in an undo step of its own ("MCP RailClone cleanup"), never with undo off. Without an undo step, the new nodes (found by comparing node handles with those before the call) are deleted with undo off, since nothing about them was recorded. If the load had already run, the undo history is then cleared once more (`clearUndoBuffer()`), so no record the load itself may have made can refer to a deleted node; the caller already agreed to losing the history.
  - **Library roots from 3ds Max.** The call first reads `HKCU\Software\Itoo Software\RailClone Pro\LibraryDir` inside 3ds Max (one read-only call through .NET's `Microsoft.Win32.Registry`), because the server's own registry view can be stale (see `railclone_library`), and a route that RailClone in Max doesn't know opens the modal "Library Browser" dialog. Those roots are used for everything that follows. Roots that differ from the server's view give a `ROOTS_DIFFER` warning. If 3ds Max lists no roots, the call fails with `NO_LIBRARY_ROOTS`. If the read itself fails, the server's view is used and the result warns `ROOTS_NOT_CHECKED`. If a dialog holds the read, the call fails with a retryable `BLOCKED_BY_DIALOG` that says nothing was loaded (`PrecheckBlocked`).
  - `item` is a route or an exact item name (or uuid). It's validated in Python before anything is created: the root name must match one of 3ds Max's roots exactly (case included), every category folder must exist with that exact spelling, and the category's `index.xml` must have exactly one item with that name. A name must match exactly one library item (`AMBIGUOUS_ITEM` otherwise; pass the route). The item file must exist.
  - **merge** (auto for every `.max` item that has an object and no `.pmat` matlib, whether its matlib is a `.mat` or missing): `mergeMAXFile <file> #(<object>) #autoRenameDups #useSceneMtlDups #neverReparent quiet:true`, the new RailClone found by comparing node handles, then the material: `$(RENDERER)`/`$(SELECTEDRENDERER)` become `RC_getRenderer()`, the `.mat` file must exist, `loadTempMaterialLibrary`, and the material is picked by name and assigned to the new RailClone only. An item without a `.mat` matlib (all 195 such items in the studio's CRGR roots) keeps the materials from its file, with a `NO_MATERIAL` warning. `.style` stays empty after a merge. The merged RailClone itself is the result.
  - `#useSceneMtlDups`, as in Forest Pack's `forest_from_library` and `smart_import`: when a merged material has the name of a scene material, the new node gets the scene's material, and the scene material is never replaced (the live recipe used `#useMergedMtlDups`, which would reset, say, a material converted for Vantage on every object that uses it). The library `.max` files on this machine carry no node materials (iToo supplies them through the `.mat`), so this hasn't changed a load yet; it guards against a library item that does.
  - **route** (auto for `.rail` items, `.pmat` matlibs, or items without an object): `RailClone_Pro()`, the name set afterwards, the selection saved, only the new object selected, `loadLibraryItemByPath <route>` (0 = loaded), the selection restored. The browser assigns the renderer's material. If the new object isn't the only selected node after `select` (for example on a hidden or frozen current layer), it stops with `SELECT_FAILED` before the load, since a failed load opens the modal dialog.
  - The splines go to the spline bases (`batype` 0) in order; more splines than spline bases is refused. Like the Forest Pack tools, `splines` takes a list, one name, or comma-separated names (`StrList`). The node is moved to the first spline's pivot; that is cosmetic, because the output follows the spline after a rebuild.
  - With `fix_maps`, bitmaps (`BitmapTexture`, `VRayBitmap`) whose file doesn't exist and isn't found through the map paths are pointed at `<category>\maps\<file>` when that file exists. Route-loaded `.rail` items need this: 29 of 29 bitmaps of Baseboard 002 were missing before.
  - It then reads the style description, rebuilds (`getStyleDesc()` empties the output export until the next rebuild, so it comes first) and returns the node, method, route, style, material, bases, `item_count` and output counts, `verified` (placed items > 0), fixed and missing maps, `extra_nodes`, `undoable`, `undo`, `undo_history_cleared`, `remove_with`, and warnings (`NO_SPLINES`, `EMPTY_OUTPUT`, `NO_MATERIAL`, `MISSING_MAPS`, `REBUILD_FAILED`, ...).

## Verified API facts

| Fact | Evidence |
|---|---|
| `getXMLStyle`, `setXMLStyle`, `getXMLOutput`, `segmentsUpdate`, `loadLibraryItemByPath` exist on 7.1.1 (`version()` = 400); the same interface is on the class, the node and the base object | `showInterfaces` |
| Exposed parameter values are only in the `pa*` arrays; the XML `Parameter/Numeric` node id equals `paid` and holds no value; consumers point at it through `_exp_<param>_<n>` connectors | both sessions |
| `patype` 0 int → `paintval`; 1 angle/float → `pafloatval`; 2 percent → `pafloatval`; 3 distance (scene units) → `paunitval`; 4 bool → `paboolval`; 5 selector → `pafloatval` = option value, `paintval` = option index (UI only); 7 separator → none | an output change for each type |
| After a write the arrays are independent; the engine reads only the typed array. `paselector` can hold stale text on other types | writes to the other arrays changed nothing |
| Writes take effect at once; limits aren't enforced (`paintval` 100004 with max 99999 read back as written); `pamodified` stays false | `x7_limits`, `t3_*` |
| Writes inside an undo record are undone by `max undo` | `t3_t2undo` |
| Rebuild: `segmentsUpdate 1 n`, `seed = seed`, `gscale = gscale`, a same-value parameter write and `banode` reassignment all work; `notifyDependents` doesn't | `x7_t3_update`, `q3_touch` |
| `loadLibraryItemByPath` takes the browser route, returns 0 when loaded and 1 on failure, and loads into the first *selected* RailClone, whichever object it's called on | `t2_selreq`, `t2_seltarget*` |
| A failed route load shows a modal "Library Browser" dialog; a disk path or other non-route string fails in about 1 ms without one | user had to dismiss it |
| A successful route load sets `.style` to the route and assigns the renderer-specific Multimaterial; library `<maxscript>` hooks aren't run | `t2_load_route2` |
| `loadTempMaterialLibrary` on a `.mat` returns the library without changing the current one; on a `.pmat` it blocked for over 120 s | `q7_matlib`, `x7_pmat` |
| `loadTempMaterialLibrary` clears the whole undo history; inside a `theHold` step it also wipes that step | `rc4_matlib_undo*` (third session) |
| Merging a RailClone library `.max` item clears the undo history, and so does a route load of a `.max` item; a route load of a `.rail` item, a plain box merge and `RailClone_Pro()` don't | `rc4_flush4`, `rc4_route_flush*` |
| `mergeMAXFile` isn't recorded in undo, even inside `theHold` | `rc4_mergeundo` |
| An undo record that refers to a node deleted with undo off crashes 3ds Max on a later `max undo` | Forest Pack `forest_from_library` (copy in an undo step, original deleted with undo off) |
| `getStyleDesc()` empties the output export (`getXMLOutput` returns `<RailClone version="1"/>` until the next rebuild) | `rc4_ox5` |
| A `for ... collect` written straight into a function call's arguments that reads the caller's locals fails ("Bad free thunk local member index") or reads the wrong variable (a loop index became the stringStream) | `rc4_repro*` |
| Library segments are embedded meshes (`sobjnode` undefined); their face IDs index the node's Multimaterial; writing `sobjmtl` has no effect | `x7_t7_*`, `q7_mtl` |

## Limits

- `railclone_from_library` doesn't guard against the modal dialog beyond validating the route against 3ds Max's roots and checking that only the new node is selected; a route that passes validation but still fails (for example a library whose folder names differ from its browser names) would show it. The DialogMonitor idea from the reports is untested and not implemented.
- `railclone_library` lists the roots in the server's registry view, which under Claude Desktop's package can lag behind 3ds Max's (see above). `railclone_from_library` reports such a difference as `ROOTS_DIFFER`.
- Routes are built from folder names and the item's english `<name>` (or its first one). That matched the stock library; a library whose category names differ from its folders isn't handled.
- `.pmat` material libraries are only loaded through a route load.
- Merge only finds the new RailClone; other nodes the merge brings along are kept and listed in `extra_nodes` and `remove_with`.
- A `.max` item can't be undone (see Undo above); `allow_undo_flush=true` is the caller's consent to that and to losing the earlier undo history.
- Parameter type 6 (`pastrval`, never seen) and unknown types are refused by `railclone_set_param`.
- Output counts in `railclone_set_param` and `railclone_from_library` count items with a `source_segment` as placed; `get_railclone_output` also treats an item with an inverted box as a placeholder.
- An instanced base anchors its output to one node; rebuilding re-anchors it to that node only.

## Status

- **Tested live on Max 2027** (2027.2, RailClone Pro 7.3.5, V-Ray 7.4) on 2026-10-11, in a scratch Max separate from production, at `4a4023e`. All 25 RailClone checks passed:
  - Metal Fence 1 by merge, with consent: 106 items, `_RC_fencing`, `undoable: false`, `remove_with` = the RailClone only, selection kept, nothing left over. Without consent it is refused before anything is sent.
  - Style read (12 nodes, 12 duplicate attributes removed: 7.3.5 still writes them), a round trip `Ok`, and `STALE_STYLE` for the old token.
  - Output: stale cache with `NOT_REBUILT` when `rebuild=false`; after a 300-unit move, the rebuilt `bounds_world` is back on the spline. `railclone_inspect` reports 106 items, rebuilt.
  - `railclone_set_param`: "20cm" halves the pickets (48 → 24). Out-of-limit and unknown names are refused. Glass Railing 03's `Spandrel Type` selector set by label (`"Glass"`, `"Solid"` for RailClone's `"Solid "`) swaps the wood and glass segments.
  - `.rail` route loads: Baseboard 002 (5 items, 29 maps fixed, `undoable: true`, selection restored) and Glass Railing 03 (109 items, 23 placeholders, 86 placed). A wrong-case root is refused in Python.
  - Review fixes: `TOO_MANY_SPLINES` leaves no node and keeps the selection. A single spline name works through FastMCP. 3ds Max's own root list matched the server's (no `ROOTS_*` warning). A scene material named `_RC_fencing` kept its own settings after the merge (`#useSceneMtlDups`).
  - Not tested: undo itself. Agents don't run `max undo`/`max redo` (a redo crashed Max 2027), so only a person can check it with Ctrl+Z.
- Unit tests: `tests/test_railclone_tools.py` (duplicate-attribute XML in get/set, placeholders and world bounds, the inspect report, parameter planning and refusals, the library index, route validation against 3ds Max's roots, the generated MAXScript for the undo step and the undo-off load, registration). A lint runs over every generated script for the live MAXScript facts: no `for ... collect` in a call's arguments, no keyword as a variable name, every `getHandleByAnim` through `as integer64`, a rebuild after every `getStyleDesc()`, and no `delete` outside a recorded undo context in a script that opens an undo step.
- **Tested live** on 2026-10-06 in a scratch 3ds Max 2026 (RailClone Pro 7.1.1, V-Ray 7), third session, at `5ae0338` (item 6 below is that version's copy-based merge). It found and fixed: the two MAXScript closure failures, `handle` returned as `IntegerPtr` text, inspect reporting 0 items (`getStyleDesc` emptied the export), merges that one undo didn't remove, the undo history clearing (now refused without consent), float noise in converted distances (`"20cm"` gave 20.0000002) and search ranking (exact names first). Results after the fixes:
  1. `get_railclone_style` on Metal Fence 1: 12 nodes, 12 duplicate attributes removed; `set_railclone_style` round trip `Ok`, `base_replaced`; the old token then gives `STALE_STYLE`.
  2. After moving the node 300 units: `rebuild=false` returns the shifted cache (with a `NOT_REBUILT` warning), the default rebuild puts `bounds_world` back on the spline.
  3. `railclone_inspect` on Glass Railing 03: 9 params with selector options and `used_by`, 20 segments, 109 items (23 placeholders, 86 placed).
  4. `railclone_set_param({"Pickets Distance": "20cm"})`: pickets 48 → 24; one undo restores 10 cm and 106 items. `-5` and an unknown name are refused before anything is written. The selector `Spandrel Type` set by label (`"Glass"`, `"Solid"` for RailClone's `"Solid "`) swaps the wood and glass segments and writes both arrays.
  5. `railclone_library`: the CRGR roots on `\\crgr-server` were reachable; `"Metal Fence 1"` ranks the exact item first.
  6. `railclone_from_library`: Metal Fence 1 (merge, with consent): 106 items, `_RC_fencing`, selection unchanged, one undo removes it, nothing left over. Baseboard 002 (`.rail`, route): 5 items, the browser's material, 29 maps fixed, selection restored, undo history kept, one undo removes it. Glass Railing 03 (`.rail`): 86 placed, 9 maps fixed. A wrong-case root is refused in Python, with no dialog.
- Commits: `5dd82d6` (fixes and tools), `5ae0338` (fixes from the live test), `fe36c3a` (undo rework), merged into `merge-1.7.5` at `42963c1`; review fixes after the merge in `9435851`.
- The undo behaviour is safe by construction and was not run live: agents never run `max undo` or `max redo` through MCP (a redo crashed Max 2027). Only a person can check it with Ctrl+Z.
- Next live run (Max 2027, scratch scene; the user does any Ctrl+Z):
  1. `railclone_from_library("Metal Fence 1", ["<spline>"], allow_undo_flush=true)`: about 106 items, `undoable: false`, `remove_with` = the RailClone only, no `*_mcp_merged` node; deleting `remove_with` leaves nothing behind.
  2. The same item with `method="route"`: the undo-off route load of a `.max` item; output > 0, `remove_with` lists the RailClone.
  3. The same item with one spline more than it has spline bases: `TOO_MANY_SPLINES`, no new node, selection unchanged (this runs the error cleanup and `clearUndoBuffer()`).
  4. Baseboard 002 (`.rail`): `undoable: true`, maps fixed; one Ctrl+Z by the user removes it.
  5. `railclone_inspect`, then `get_railclone_output(rebuild=false)`: the output isn't empty.
