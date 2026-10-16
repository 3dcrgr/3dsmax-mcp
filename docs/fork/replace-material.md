# `replace_material` that "replaced" nothing

Fork issue #3. Commits `a9d2a0d`, `16528de`, `cd85cd2`, `a670484` (review fixes) and `86ea1b8` (bridge tool registry).

**Since the 1.7.6 merge** (`298a295`), the tool is upstream 1.7.6's rewrite (sub-material slots, Material Editor sources, a reference-loop guard, in-order batches, and its result fields and statuses) plus what only the fork does: `include_sub_materials`, `source_from` (with the scene materials and the current material library as sources), a preview with a missing source, and the direction and `blocked` hints. The native handler and the MAXScript fallback match. What follows describes the fork's fix and says where the merge changed it.

**Upstream since #35.** The fork's additions went upstream in pull request [#35](https://github.com/cl0nazepamm/3dsmax-mcp/pull/35), merged on 2026-10-15, and upstream followed up with `8ecefad`. Since `a86ade0` (2026-10-16) the fork carries upstream's `material_replace.py` and `material_handlers.cpp` as they are. See [Upstream status](#upstream-status).

## What happened

On a Revit scene with Hebrew names, `replace_material` was asked to swap a plaster material for a freshly imported Cosmos material. It answered `{"status": "replaced", "replaced_count": 0}`, and nothing changed. At first this looked like a Unicode bug with the Hebrew name.

## Why

It wasn't an encoding problem. In the native handler, `source_material` is the material to **apply**, and `target_material` is the material being **replaced**. The handler looks for objects whose material is named `target_material` and gives them `source_material`. The agent had passed the two names the other way round. The tool's docstring didn't say which was which; upstream had trimmed an older docstring that did. The Hebrew name matched fine: an unmatched source name throws "not found", and this call didn't.

Three other limitations made the tool fail on real scenes. The first two failed quietly; the third failed with a misleading "not found" error:

- **A success status with zero replacements**, which hid the mistake.
- **Only top-level object materials were matched.** A target inside a Multi/Sub-Object material was never replaced, and Revit and ATF imports produce Multi/Sub materials all the time.
- **The source had to be assigned to an object already.** A material sitting in a Material Editor slot, which is where a Cosmos import puts it, or one that only existed as a sub-material, couldn't be applied.

## What changed

**The direction is documented** (`a9d2a0d`). Both docstrings say: source = the material to apply, target = the material to replace. They also give the order the source is searched in and the batch order. Since the 1.7.5 merge (`a07ef93`), the skill guide is upstream's shorter one plus a one-line fork rule for this tool: the direction, what to do on `no_match` and `blocked`, and (since `ec903a5`) `source_from` for a re-imported Cosmos copy.

**Nothing matched → `no_match`; everything skipped → `blocked`** (`a9d2a0d`, refined in `16528de` and `a670484`).
- **`no_match`:** no object and no sub-material slot uses the target. The warning explains the direction and where the source can come from. Since the 1.7.6 merge, `no_match` also means the target isn't in the Material Editor either; a target that only sits there gives `no_objects`, which carries its own direction hint.
- **`blocked`:** the target *is* used, but every match was skipped (see `skipped` below). This holds in preview too. The warning lists the skips by reason and deliberately gives no "swap the arguments" hint: swapping would apply the target over the source's users.
- Since the 1.7.6 merge, upstream's other statuses apply too: `no_objects` (the target exists, but only in the Material Editor, so nothing uses it; the reversed call from the incident above, with the Cosmos material only in the Material Editor, now lands here) and `unchanged` (every match already uses the source). A preview that would change something has `status: "preview"`.
- Batch entries get the same statuses, but their warnings go on the batch result, not on the entry. One combined `no_match` warning names every missed target; the per-entry details (skips, an ambiguous source, `blocked`) start with `'source' -> 'target':`.

**Sub-material slots are matched** (`16528de`). The new parameter `include_sub_materials` defaults to `true`.
- Every Multi/Sub-Object slot, or any other material's sub-material slot, that holds a material named `target_material` gets the source, as long as it sits inside an object's material. A Multi/Sub that is only in the Material Editor or a library isn't changed.
- A material shared by many objects is changed once.
- A material named like the target is replaced as a whole, and isn't searched inside.
- Matches left unchanged are listed in `skipped`, each with a `reason`. Since the 1.7.6 merge these are upstream's reasons, and objects (`kind: "object"`) are listed as well as slots (`kind: "sub_material"`):
  - `already_source`: the match already uses the source (counts toward `unchanged`, not `blocked`);
  - `parent_is_source`: the slot's parent material is the source;
  - `would_create_loop`: applying the source would create a reference loop (the SDK's `TestForLoop` in the native handler, `refs.dependencyLoopTest` in the fallback, which gives `loop_test_failed` when the test itself fails);
  - `set_rejected`, real runs only: a plugin material ignored the change;
  - `readback_mismatch`, real runs only: something else ended up in the slot or on the object (`actual_material`).
  - The fork's own reasons before the merge were `parent_is_source`, `source_contains_parent`, `reference_loop` and `set_failed`; upstream's replace them.

**The source is found outside the scene** (`16528de`). The search runs in this order:
1. object materials;
2. their sub-materials;
3. the 24 Material Editor slots;
4. the scene materials;
5. the current material library.

Upstream 1.7.6 searches the first three; the last two are the fork's. The result reports `source_found_in`: `node`, `sub_material`, `material_editor`, `scene_materials` or `material_library`. If several different materials share the name, the first one found wins, and the result adds `source_ambiguous: true`, `source_candidates` and a warning.

**`source_from` picks where the source comes from** (`a670484`). It takes one of those five places and limits the search to it.
- Typical use: you re-imported a Cosmos material, and the copy in the Material Editor has the same name as the one already on objects. `source_from="material_editor"` applies the new copy.
- An invalid value fails before anything is sent to Max.
- A stock bridge ignores `source_from` (see below). The result then carries a warning that the bridge didn't echo it back and may have ignored it.
- If the source and target names are equal and `source_from` isn't set, the call warns. Without `source_from`, the first match is the target itself.

**A preview with a missing source no longer fails.** It returns `source_exists: false` together with what the target would affect. That lets the agent see that it probably swapped the arguments.

Result fields, since the 1.7.6 merge (upstream's, plus the fork's marked "fork"):

| Field | Meaning |
|---|---|
| `status` | `replaced`, `preview`, `no_match`, `no_objects`, `blocked` or `unchanged` (an entry that failed: `error`; a failed single call: `failed`) |
| `match_count` | Objects and slots that use the target |
| `replaced_count` | Objects reassigned plus slots replaced. Before the merge the fork counted objects only. |
| `reassigned_count`, `replaced_objects` | Objects whose own material was reassigned |
| `replaced_slots`, `replaced_slot_count` | `{parent_material, parent_class, slot_index, slot_name, previous_material, object_count}` per slot. `slot_index` is 1-based, like MAXScript; for a Multi/Sub it's the position in `materialList`, not the material ID. |
| `skipped`, `skipped_count` | Matches left alone, each with a `kind` and a `reason` (see above) |
| `affected_objects`, `affected_count` | Every object whose material changed or would change, slots included |
| `source_exists`, `source_found_in` | Fork. Whether and where the source was found (the fallback reports `source.found_in`) |
| `source_ambiguous`, `source_candidates` | Fork. Only when the name isn't unique. `source_candidates` is the number of materials with that name, not a list |
| `include_sub_materials`, `source_from` | Fork. Echo of the parameters |
| `depends_on_entries` | Fork. MAXScript fallback batch previews only: 1-based numbers of earlier entries this one depends on (see below) |

A preview uses the same keys, with `preview: true`. The batch result adds `total_replaced`, `total_replaced_slots` and `total_skipped`, and a `status` of `replaced`, `preview` or `no_changes`.

**Batch entries now run in order.** In `batch_replace_materials`, each entry sees the scene as the previous entries left it.
- Before, the handler took one snapshot up front and kept raw material pointers between entries. A material whose last user was reassigned by an earlier entry could be auto-deleted by Max while the batch still held it.
- Side effect: the pair `{source: "A", target: "B"}`, `{source: "B", target: "A"}` no longer swaps two materials: both groups end up with the same one (B if the second entry still finds B, otherwise that entry fails and A stays everywhere). To swap, go through a temporary material, in this order: `{source: "Tmp", target: "A"}`, `{source: "A", target: "B"}`, `{source: "B", target: "Tmp"}`.
- Each entry looks its source up again when it runs, and at that moment each of the three sources is on no object. Put Tmp, A and B in Material Editor slots first. Otherwise an entry can fail with "source material not found", and the rest of the batch still runs and leaves the swap half done.
- **Preview and ordering.** Since the 1.7.6 merge, the native handler's preview chains the entries the way the real run does (upstream's planning overlay), without touching the scene. The MAXScript fallback's preview still plans every entry against the current scene, so there an entry that reuses a name an earlier entry applies or removes gets `depends_on_entries` and a warning, because its real run can differ from its preview (`a670484`). Before the merge this applied to the native handler too.
- `source_from` works as a batch-wide default, or per entry with a `source_from` key.

**The MAXScript fallback matches the native handler.** This is the fallback used when the native bridge isn't available. It has the same lookup order and slot matching, returns the same keys and statuses (`replaced` on success since the 1.7.6 merge; before it, `"success"`), and derives status and warnings with the same rules. Its loop guard uses `refs.dependencyLoopTest`. Since the merge, a batch runs every entry in order in one script, inside one undo record when applied.

**With a stock bridge, all of this works now** if the bridge was built after #35, like the 2023-2026 ones in `native/bin/` since `a86ade0`. Upstream 1.7.6's native handler has sub-material matching, Material Editor sources, the loop guard and the in-order batch. What it lacks is the part that went upstream in #35:
- `source_from`, and the scene materials and current material library as sources. A stock bridge ignores `source_from`; the result then carries a warning that the bridge didn't echo it back.
- `include_sub_materials=false`. A stock bridge always searches slots; the result then warns that slots may have been replaced.
- A preview with a missing source, which fails on a stock bridge.
- In `native/bin/`, only the 2027 bridge is a fork build. An install on 2023–2026 gets upstream's rebuild from `71f72a8`, which has #35. Upstream's 1.7.5 and older bridges match object materials only and need the source on an object.

**An error box fails the call** (upstream `5c44e76`, merged in `a07ef93`). If Max shows a MAXScript error box during a real run, from a callback script or a scripted material for example, the bridge acknowledges it and the call fails with `MAX_DIALOG_ERROR`. The same happens if the agent presses OK on it with `max_dialogs`. As the error says, check the scene before retrying.
- **Native handler:** the dispatcher rolls back the call's undo transaction. A `batch_replace_materials` call is one transaction, so every entry is undone. A stock 1.7.5 bridge could commit the call before the error was recorded; the fork's bridges wait for it (`ffccbab`, see [hung-max.md](hung-max.md#upstream-175s-dialog-handling-fixed-on-the-merge)).
- **MAXScript fallback:** it runs over the TCP listener, which has no dialog monitor, so it never returns `MAX_DIALOG_ERROR`. Before the 1.7.6 merge each batch entry was its own call and undo step, and a call that failed outright stopped the batch with the earlier entries applied. Since the merge the batch is one script in one undo record; what a MAXScript error halfway through leaves applied hasn't been tested.

**Quiet mode** (upstream `5c44e76`). MAXScript run through the bridge now uses Max's quiet mode by default, so prompts take their default answer; `execute_maxscript(quiet=False)` shows them. The fork's bridge leaves it off by default for scripts that mention a reset, open, fetch or quit command, and a stock bridge doesn't (#12, see [smaller-fixes.md](smaller-fixes.md#quiet-mode-could-discard-unsaved-work)). None of this changes `replace_material`: the native handler runs no MAXScript, and the TCP listener the fallback uses has no quiet mode.

**`invoke_tool` can run `replace_material` again** (`86ea1b8`). The bridge build generates its own tool registry from the Python tools, and the generator only finds a tool's native command in the tool's own body. `a9d2a0d` had moved that call into a helper, so every fork bridge built from then until this fix, the ones deployed on Max 2026 included, answered `invoke_tool("replace_material")` with "Unknown tool". Agents calling `replace_material` directly were never affected. The call is inline again, and a unit test checks that the generator finds both tools. The fork's bridges built since have the fix: the 1.7.5-based 2026 one (sha256 `d5cd6ba5…`, dropped in the 1.7.6 merge) and every 2027 one, the merge's included. Upstream's bridges never had the problem.

## Examples

```text
replace_material(source_material="Cosmos_Plaster", target_material="Plaster", preview=True)
→ status: "preview", source_found_in: "material_editor"
  replaced_count: 1, reassigned_count: 0, replaced_slot_count: 1
  replaced_slots: [{parent_material: "Walls", parent_class: "Multimaterial", slot_index: 1, ...}]
```

```text
replace_material(source_material="Plaster", target_material="Cosmos_Plaster")   # reversed
→ status: "no_objects", warnings: ["'Cosmos_Plaster' exists only in the Material Editor; no object
  or sub-material slot uses it, so nothing was replaced.", "If you meant to put 'Cosmos_Plaster' onto the
  objects that use 'Plaster', the arguments are reversed: source = material to apply, target = material to
  replace."]
```

```text
replace_material(source_material="Plaster", target_material="Plaster_typo")
→ status: "no_match", warnings: [..., "No material named 'Plaster_typo' was found to replace.
  source = material to apply (on ...), target = material to replace; if reversed, swap them. ..."]
```

Before the 1.7.6 merge, the first example reported `affected_slot_count` and `affected_slots`; the merged handler uses the `replaced_*` keys in a preview too. The reversed call returned `no_match` with the direction hint; since the merge, a target that sits only in the Material Editor gives upstream's `no_objects`, and the fork adds the direction hint to it.

## How it was verified

- **The direction docs and `no_match`** (`a9d2a0d`) were verified live on Max 2026 on 2026-10-01. The bridge then still had upstream's handler, so this is also the Python side working with a stock handler.
  - `replace_material(source="Glass", target="NoSuchMaterial_xyz", preview=True)` returned `no_match` with the direction warning.
  - The correct direction still listed the affected objects.
- **Sub-material matching, the source search, `source_from`, `blocked` and the loop guard** (`16528de`, `a670484`) have Python unit tests in `tests/test_material_replace.py`, but they mock the bridge. They check the payload, the text of the generated MAXScript and the Python post-processing (`no_match`, `blocked`, the `source_from` checks, warnings, `depends_on_entries`). The matching, the source search and the loop guard only run inside Max, so the unit tests don't cover them. An adversarial review covered native undo, reference lifetimes, the `TestForLoop` direction, and search side effects. The Python side was deployed on Max 2026 on 2026-10-01. The native handler is in the rebuilt 2026 bridge (sha256 `5f49226e…`, shipped in `53cd33a`), deployed the same day.
  - The native handler was checked live on Max 2026 at 12:30 on a Multi/Sub whose source was only in the Material Editor. The preview gave `source_found_in` "material_editor", one affected slot and an empty `skipped`. The real run replaced the slot, and `undo_last` put it back.
  - That empty `skipped` was the check that mattered most: a normal Multi/Sub slot isn't reported as `reference_loop`, so the SDK's `TestForLoop` result is read the right way round.
  - Not run in Max yet: nested Multi/Sub, the loop guard's skips, `source_from`, `blocked`, Hebrew names, batch, plugin parents such as VRayBlendMtl and Shell_Material, and the MAXScript fallback.
- **The 1.7.5 merge** didn't change how a replacement runs, native or Python. The 2026 bridge built from it (sha256 `d5cd6ba5…`, from `ec903a5`) was never deployed: production moved to Max 2027 on 2026-10-08.
  - **Max 2027:** the Python side runs from this branch's source, and the fork's 2027 bridge, built from `18611d7` with the same native code, was deployed on 2026-10-11. Its live checks that day were read-only and didn't include `replace_material`, so nothing on this page has been tested live on Max 2027 yet.
  - The rollback after an error box hasn't been tried in Max. `native/tests/dialog_watch_tests.cpp` checks, without Max, that an acknowledged error box fails the operation where it resumes (`ffccbab`).
  - The fallback batch stopping at a failed call comes from reading the code; no test covers it.
- **The 1.7.6 merge** (`298a295`) replaced the handler with upstream's rewrite plus the fork's additions, in both the native handler and the fallback. `tests/test_material_replace.py` was updated for upstream's result fields and skip reasons and passes. Nothing on this page has been run live with the merged handler yet.
- **The merge of upstream's `master`** (`a86ade0`) took upstream's `material_replace.py` and `material_handlers.cpp` from #35 and `8ecefad` as they are. `tests/test_material_replace.py` passes against them. Nothing on this page has been run live with upstream's code yet.

## Upstream status

Merged upstream. Not fixed in upstream v1.7.5. Upstream 1.7.6 (`5a92193`) fixed most of it its own way: sub-material slots, Material Editor sources, the loop guard, the in-order batch, `no_match` with a warning and `no_objects`. The rest went upstream in pull request [#35](https://github.com/cl0nazepamm/3dsmax-mcp/pull/35), merged on 2026-10-15 (`e156c00`): `include_sub_materials`, `source_from`, the scene materials and material library as sources, a preview with a missing source, and the direction and `blocked` hints.

Upstream then added `8ecefad`. It refreshes the scene material list before `replace_material` takes a source from it, so a batch entry whose source an earlier entry removed no longer picks up a deleted object's material. A native batch preview skips materials an earlier entry takes off every object. The MAXScript fallback applies `source_from` before it removes duplicates. The fork carries upstream's code since `a86ade0`. The older, explicit docstring survives in kanzaka110's fork.
