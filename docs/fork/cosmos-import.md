# Cosmos imports that stall or deadlock 3ds Max

Fork issues #1, #2, #7, #9 and #10. Commits `25bb1fc`, `ed1fb1c`, `2853b1c` (#9) and `c1e09a7` (#10).

## What happened

On a V-Ray 7 GPU scene, `cosmos_import` reported success, and then the next call that edited the scene hung 3ds Max. Max stopped responding but used almost no CPU. This happened in about two of five imports. The first sessions were killed after a minute or two, which lost unsaved work. From the agent's side, every later tool call simply timed out.

## Why

Native stack dumps showed two separate problems. Both are inside Chaos code, and the MCP made both worse. (The dumps were taken with the stack walker that this fork now bundles as [`capture_hang_diagnostics`](hung-max.md#capture_hang_diagnostics).)

1. **The Material Editor preview render.** The Chaos importer opens the Compact Material Editor and puts the new material in the active slot. The editor (`mtl.dll`) then asks the renderer for a sample-slot preview. With V-Ray as the Material Editor renderer, that call blocked Max's main thread inside `vray.dll` for 5–8 minutes on the test machine.
   - That machine has no local Chaos license server, and the Chaos networking threads were waiting on sockets. So the likely cause is an online license round trip, but this wasn't confirmed.
2. **A cross-thread window deadlock.** If no Cosmos browser is open, the importer (`galaxyimporter2026.dll`) creates a hidden "Chaos Cosmos Browser" window on its own thread. After an import, that thread waits on a condition variable.
   - Any main-thread call that makes Windows send a synchronous message to that window then blocks forever. Closing or activating a window does this; `MatEditor.Close()` is one example.
   - This one never recovered: Max was killed after 13 minutes.

If the Cosmos browser is opened through its V-Ray or Corona action *first*, its window belongs to Max's main thread, and neither problem occurs. In the one logged test done that way, with V-Ray still the Material Editor renderer, Max was busy for about 10 s (the preview render) and then answered again. Assigning the material, an undo hold and `MatEditor.Close()` all worked afterwards.

The MCP made both problems worse in three ways:
- `_wait_import` returned as soon as a new material handle appeared, while the importer still had work queued.
- It polled with a full class-instance scan on the main thread every 250 ms (#2).
- The client then blocked on its pipe lock, so the agent saw bare timeouts with no diagnosis (see [hung-max.md](hung-max.md)).

## What changed

`cosmos_import` now runs these steps:

1. **Make sure a main-thread Cosmos browser exists.** This check is done from the OS, with no bridge call.
   - If no browser window belongs to Max's main thread, it runs the renderer's "Cosmos browser" action. The action is looked up by name in the action tables, never by index. It then waits up to 15 s for the window.
   - If a browser is already on the main thread but doesn't answer because Max's main thread is busy, it doesn't run the action. It only waits, from the OS, up to 15 s for the window to answer.
   - If a browser on another thread is already hung, it refuses and sends nothing. The result is `state: "browser_hung"` with code `IMPORT_SETTLING`.
2. **Prepare.** One bridge call records what already exists: nodes tagged with the asset's Cosmos ID, materials and maps whose names or file names match, and (since #9) the handles of every existing material and Material Editor slot. That's how the import's new resources are told apart later. The same call records the Material Editor renderer and, when the swap is on, switches it to Default Scanline. Last, it clears the selection, remembering it first. The native importer otherwise auto-assigns the new material to whatever is selected.
   - Just before this call, it waits up to 15 s for Max's main window to answer. If it doesn't, nothing more is sent. The result is `state: "not_imported"` with code `IMPORT_SETTLING`.
3. **Dispatch the import**, then poll lightly for the result (#2). The polls are read-only and back off. They're also probes: they're never sent into a main window that's already hung, and they're dropped at their deadline.
4. **Wait for Max to settle**, judged from the OS. The main window and every Cosmos browser window, hidden ones included, must answer for a streak longer than 6 s. (`IsHungAppWindow` only reports a window as hung after about 5 s.)
   - Max's CPU use must also fall back near its level before the import. If the windows answer but the CPU stays busy, it stops waiting 10 s later and adds a warning that textures may still be loading.
5. **Restore the selection.** It never calls `MatEditor.Close()` or `MatEditor.Open()`.
6. **If Max is still busy after `settle_seconds`**, the result has `state: "settling"` and `safe_to_edit: false`. Once `get_bridge_status` reports Max responding, run `pending_restore.maxscript` once with `execute_maxscript`.
   - It also arms an `IMPORT_SETTLING` guard. Until that Max's windows pump messages again, further calls from this server to it fail fast and aren't sent. The guard expires after 15 minutes at most.

The same guard covers the import itself: while it runs, other calls from this server to that Max fail at once with `IMPORT_SETTLING`. It's also armed when step 1 or 2 refuses the import as described above.

New parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `swap_medit_renderer` | `false` | Use Default Scanline as the Material Editor renderer during the import. This is turned on automatically when step 1 ends without a responsive browser on Max's main thread. |
| `restore_medit_renderer` | `true` | Put the original Material Editor renderer back once Max is idle and the editor is closed. If the editor is open, it stays Scanline and a warning gives the restore script. |
| `settle_seconds` | `90` (0–300, at least 8 used) | How long to wait for Max to settle before returning `safe_to_edit: false`. |

New result fields:
- `state` can also be `settling` (dispatched, but Max is still busy; `detected` says whether the new resource was seen), `browser_hung` or `not_imported` (nothing imported, `dispatched: false`).
- `cosmos_browser`: `ensured`, `opened`, `main_thread`, `windows`, `warning`, plus `action` when it looked up the browser action.
- `medit_renderer`: `class`, `locked`, `editor_open`, `swapped`, and `swap` (`off`, `requested` or `fallback`).
- `import_timing`: `detected`, `after_s`, `polls`, `skipped_hung`, `pre_dispatch` and `settle`.
- `safe_to_edit`, `next`, `warnings`, and `pending_restore` (`selection`, `restore_medit_renderer`, `maxscript`).

`cosmos_search` (#7) no longer needs Max's main thread:
- The target PID comes from the selected instance, with no bridge call.
- When exactly one Cosmos importer is registered for that PID, the renderer follows from the importer. So a search works even while Max is busy.
- When more than one importer is registered for that PID (V-Ray and Corona, say) and `renderer` is `"current"` (the default), it reads `renderers.current` once. Pass `renderer="vray"` or `"corona"` to skip that. Max is also asked when the selected instance can't be reached or no importer matches.
- The result's `renderer_source` says where the renderer came from: `explicit`, `only_importer` (the scene renderer wasn't checked) or `scene`.

### Finding what the import created (#9, `2853b1c`)

**What happened.** Importing the Cosmos material "Steel Blurry" created a VRayMtl named "Steel_Polished #0" in Material Editor slot 13. The tool polled for 31.5 s, then returned `state: "imported_unverified"` with no materials. The package's `.vrmat` declares the other name: Chaos ships Steel Blurry and Steel Polished as near-identical files. Detection looked for the asset's name, so it never found the material.

**What changed.**
- **Detection by handle, not by name.** The prepare step now also records the handles that already exist: every material instance and the 24 Material Editor slots, plus bitmaps and HDRI maps for HDRI imports. The polls and the confirming snapshot report anything whose handle wasn't there before, whatever it's called.
- The existing matching (nodes tagged with the Cosmos ID, names) still runs. A snapshot without the handle list falls back to names.
- **Other plugins' new items are kept apart** (`c88975e`). During one live import, Forest Pack regenerated its own materials, and about 200 existing maps were listed as created.
  - Maps now count as created only if their handle is newer than a marker taken just before the scan.
  - Materials the import didn't produce go to a separate `other_new_materials` list and never become the primary item. "Didn't produce" means: not on the asset's nodes, not matching its name, not in a Material Editor slot, and not a sub-material of one of those.
  - When nothing can be tied to the import, for a material or a model asset, the old behaviour is kept, with a warning.
- **New result fields.**
  - Each material carries `medit_slot` and `sub_material`.
  - `primary_material` or `primary_map` names the main item, and `primary_reason` says how it was chosen: `name`, `detected_during_import`, `top_level`, `medit_slot`, `only_new` or `first`.
  - `asset_name` comes with `material_name` or `map_name`, plus a `note` when they differ.
- Nothing is renamed. The behaviour from the steps above is unchanged: no new bridge calls, the OS-only settle, the main-thread browser, the `IMPORT_SETTLING` guard, and never opening or closing the Material Editor.

**Status.** Unit tested in `tests/test_cosmos_import.py`, and verified live on 2026-10-01. Re-importing "Steel Blurry" returned `imported` with `primary_material` "Steel_Polished #0" (`medit_slot` 13, `primary_reason` `only_new`), `asset_name` "Steel Blurry" and the note about the different name. The scene's existing "Steel_Blurry" was correctly not reported as new.

### Keeping the user's Material Editor slot (#10, `c1e09a7`)

**What happened.** Chaos's importer puts the new material into the *active* Compact Material Editor slot, replacing whatever material was there. During the #9 test it displaced the user's "Steel_Blurry" from slot 13, silently.

**What changed.**
- **Before the import:** the prepare call records the active slot and its material. If that material is worth keeping, the lowest truly free slot becomes active, so the importer writes there instead.
  - A slot counts as free only if its material is pristine: used by no object, no sub-materials or maps, and every property equal to a fresh one.
  - Pristine slots with a default-style name come first: "NN - Default", or V-Ray's "Material #N" (`c88975e`; the first live test found only V-Ray slots, so nothing counted as free). If there are none, any pristine slot is used. A pristine material with its own name that gets replaced is kept alive too, and reported (`switched_over`, `moved_over`).
  - The slot check runs before the baseline snapshot, so the materials it creates for comparison never show up as new.
  - The kept material is held by a MAXScript global meanwhile, so Max can't auto-delete it.
- **After a quiet settle:** if a material was still displaced, the finalize call puts it back, moves the import to a free slot, and makes the original slot active again.
  - This only happens when no undo operation is open and the scene file hasn't changed.
  - If it can't, a warning names the displaced material and gives the exact MAXScript to restore it.
- Nothing is done while Max is settling. The Material Editor is never opened or closed, and the Slate editor isn't touched beyond `activeMeditSlot`.
- **New result fields:**
  - `medit_active_slot`: `slot`, `material`, `kept`, `switched_to`, `state`;
  - `displaced_material`;
  - `medit_slot_restored`;
  - `medit_slot` now reports where the import ended up.

**Status.** Unit tested and deployed (Python) on 2026-10-01.
- **The first live test (`c1e09a7`) was a partial pass.** The displaced material was kept alive, and the restore hint was right. But slots 16–24 held untouched V-Ray materials named "Material #16" to "Material #24", so no slot counted as free, and the import still landed in the user's active slot.
- `c88975e` fixes that and was deployed the same day; it hasn't been retested live yet.

## How it was verified

Live on 3ds Max 2026 with V-Ray 7 update 4 hotfix 2 (GPU production renderer), on 2026-10-01:

- **Action lookup:** found both `V-Ray | Chaos Cosmos browser` and `Chaos Corona | Corona Open Cosmos Browser`.
- **Import with default settings:**
  - The browser was ensured (it already existed on the main thread), and the Material Editor renderer wasn't swapped.
  - It detected the new material 19.8 s after dispatch, with 8 polls skipped while Max was hung. The settle check then passed with a quiet streak of 7, and it returned `imported` and `safe_to_edit: true`.
  - The user's selection came back, and the material wasn't auto-assigned to it.
- **`cosmos_search`:** answered with `renderer_source: "scene"`, because V-Ray and Corona both registered an importer for that Max.
- **Import on a freshly started Max with no browser open:** the "Steel Blurry" re-import at 13:08 ran right after a Max restart. `cosmos_import` opened the browser on Max's main thread through the V-Ray "Chaos Cosmos browser" action, the Material Editor renderer wasn't swapped, and the import returned `imported` and `safe_to_edit: true` after about 17 s.

Unit tests are in `tests/test_cosmos_import.py`. They use fake windows to cover a hung browser, a missing browser, settle streaks, the guard expiring, and the Scanline fallback.

## If an import still hangs

- **Wait, and check with `get_bridge_status`.** It now answers while Max is hung (see [hung-max.md](hung-max.md)).
  - Preview-render stalls cleared on their own in 5–8 minutes.
  - The cross-thread deadlock never cleared. If Max is still blocked after about 10 minutes, treat it as a deadlock.
- **Run `capture_hang_diagnostics`** and keep the file it saves. It tells the two cases apart:
  - Main thread inside `vray.dll`, called from `mtl.dll`: that's the preview render.
  - Main thread in a USER32 call while a "Chaos Cosmos Browser" thread waits in `galaxyimporter2026.dll`: that's the window deadlock.
- **Don't open or close the Material Editor** while Max is in this state.

## Upstream status

Not fixed upstream as of 2026-10-01. None of the 45 reachable forks touches `cosmos.py`. The other two of the 47, `rocksek` and `mseep-ai`, are deleted.
