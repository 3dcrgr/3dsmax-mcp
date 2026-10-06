# `replace_material` that "replaced" nothing

Fork issue #3. Commits `a9d2a0d`, `16528de`, `cd85cd2` and `a670484` (review fixes).

## What happened

On a Revit scene with Hebrew names, `replace_material` was asked to swap a plaster material for a freshly imported Cosmos material. It answered `{"status": "replaced", "replaced_count": 0}`, and nothing changed. At first this looked like a Unicode bug with the Hebrew name.

## Why

It wasn't an encoding problem. In the native handler, `source_material` is the material to **apply**, and `target_material` is the material being **replaced**. The handler looks for objects whose material is named `target_material` and gives them `source_material`. The agent had passed the two names the other way round. The tool's docstring didn't say which was which; upstream had trimmed an older docstring that did. The Hebrew name matched fine: an unmatched source name throws "not found", and this call didn't.

Three other limitations made the tool fail on real scenes. The first two failed quietly; the third failed with a misleading "not found" error:

- **A success status with zero replacements**, which hid the mistake.
- **Only top-level object materials were matched.** A target inside a Multi/Sub-Object material was never replaced, and Revit and ATF imports produce Multi/Sub materials all the time.
- **The source had to be assigned to an object already.** A material sitting in a Material Editor slot, which is where a Cosmos import puts it, or one that only existed as a sub-material, couldn't be applied.

## What changed

**The direction is documented** (`a9d2a0d`). Both docstrings say: source = the material to apply, target = the material to replace. They also give the order the source is searched in and the batch order. Since the 1.7.5 merge (`a07ef93`), the skill guide is upstream's shorter one plus a one-line fork rule for this tool: the direction, and what to do on `no_match` and `blocked`.

**Nothing matched → `no_match`; everything skipped → `blocked`** (`a9d2a0d`, refined in `16528de` and `a670484`).
- **`no_match`:** no object and no sub-material slot uses the target. The warning explains the direction and where the source can come from.
- **`blocked`:** the target *is* used, but every match was skipped (see `skipped` below). This holds in preview too. The warning lists the skips by reason and deliberately gives no "swap the arguments" hint: swapping would apply the target over the source's users.
- Batch entries get the same statuses, but their warnings go on the batch result, not on the entry. One combined `no_match` warning names every missed target; the per-entry details (skips, an ambiguous source, `blocked`) start with `'source' -> 'target':`.

**Sub-material slots are matched** (`16528de`). The new parameter `include_sub_materials` defaults to `true`.
- Every Multi/Sub-Object slot, or any other material's sub-material slot, that holds a material named `target_material` gets the source, as long as it sits inside an object's material. A Multi/Sub that is only in the Material Editor or a library isn't changed.
- A material shared by many objects is changed once.
- A material named like the target is replaced as a whole, and isn't searched inside.
- Slots left unchanged are listed in `skipped`, each with a `reason`. The first three come from the loop guard and show up in preview too:
  - `parent_is_source`;
  - `source_contains_parent`;
  - `reference_loop`, from the SDK's `TestForLoop` (native handler only);
  - `set_failed`, real runs only, when a plugin material ignored the change.

**The source is found outside the scene** (`16528de`). The search runs in this order:
1. object materials;
2. their sub-materials;
3. the 24 Material Editor slots;
4. the scene materials;
5. the current material library.

The result reports `source_found_in`: `node`, `sub_material`, `material_editor`, `scene_materials` or `material_library`. If several different materials share the name, the first one found wins, and the result adds `source_ambiguous: true`, `source_candidates` and a warning.

**`source_from` picks where the source comes from** (`a670484`). It takes one of those five places and limits the search to it.
- Typical use: you re-imported a Cosmos material, and the copy in the Material Editor has the same name as the one already on objects. `source_from="material_editor"` applies the new copy.
- An invalid value fails before anything is sent to Max.
- A bridge without this fix ignores `source_from`; in this fork only the 2026 bridge has it. The result then carries a warning that the bridge may have searched everywhere.
- If the source and target names are equal and `source_from` isn't set, the call warns. Without `source_from`, the first match is the target itself.

**A preview with a missing source no longer fails.** It returns `source_exists: false` together with what the target would affect. That lets the agent see that it probably swapped the arguments.

New result fields:

| Field | Meaning |
|---|---|
| `replaced_slots` / `affected_slots` | `{parent_material, parent_class, slot_index, slot_name}` per slot. `slot_index` is 1-based, like MAXScript; for a Multi/Sub it's the position in `materialList`, not the material ID. |
| `replaced_slot_count` / `affected_slot_count` | Number of slots |
| `source_exists`, `source_found_in` | Where the source was found |
| `source_ambiguous`, `source_candidates` | Only when the name isn't unique. `source_candidates` is the number of materials with that name, not a list |
| `skipped` | Slots left alone, each with a `reason` |
| `include_sub_materials`, `source_from` | Echo of the parameters |
| `depends_on_entries` | Batch only: 1-based numbers of earlier entries this one depends on (see below) |

`replaced_count` and `replaced_objects` still count objects only, as before. A `replace_material` preview has `preview: true` and no `status` unless it is `no_match` or `blocked`. Native `batch_replace_materials` entries that ran use the `replaced_*` keys even in a preview, where their status is `"preview"` unless it is `no_match` or `blocked`. They list objects as `{name, handle, class, layer}`. The batch result adds `total_replaced_slots`.

**Batch entries now run in order.** In `batch_replace_materials`, each entry sees the scene as the previous entries left it.
- Before, the handler took one snapshot up front and kept raw material pointers between entries. A material whose last user was reassigned by an earlier entry could be auto-deleted by Max while the batch still held it.
- Side effect: the pair `{source: "A", target: "B"}`, `{source: "B", target: "A"}` no longer swaps two materials: both groups end up with the same one (B if the second entry still finds B, otherwise that entry fails and A stays everywhere). To swap, go through a temporary material, in this order: `{source: "Tmp", target: "A"}`, `{source: "A", target: "B"}`, `{source: "B", target: "Tmp"}`.
- Each entry looks its source up again when it runs, and at that moment each of the three sources is on no object. Put Tmp, A and B in Material Editor slots first. Otherwise an entry can fail with "source material not found", and the rest of the batch still runs and leaves the swap half done.
- **Preview can't see this ordering.** It plans every entry against the current scene. So an entry that reuses a name an earlier entry applies or removes gets `depends_on_entries` and a warning, because its real run can differ from its preview (`a670484`).
- `source_from` works as a batch-wide default, or per entry with a `source_from` key.

**The MAXScript fallback matches the native handler.** This is the fallback used when the native bridge isn't available. It has the same lookup order and slot matching, and a single call returns the same keys and runs inside one undo step. Its `status` on success is still `"success"`, where native returns `"replaced"`. Its loop guard has no `TestForLoop` check, so it never reports `reference_loop`. In `batch_replace_materials` it runs each entry as its own call and undo step, so its entries keep the single-call shape: `affected_*` keys in a preview, and objects listed by name.

**An error box fails the call** (upstream `5c44e76`, merged in `a07ef93`). If Max shows a MAXScript error box during a real run, from a callback script or a scripted material for example, the bridge acknowledges it and the call fails with `MAX_DIALOG_ERROR`. The same happens if the agent presses OK on it with `max_dialogs`. As the error says, check the scene before retrying.
- **Native handler:** the dispatcher rolls back the call's undo transaction. A `batch_replace_materials` call is one transaction, so every entry is undone. With upstream's 1.7.5 bridges, which this fork ships for 2023–2025 and 2027, the call could commit before the error was recorded; the 2026 bridge waits for it (`ffccbab`, see [hung-max.md](hung-max.md#upstream-175s-dialog-handling-fixed-on-the-merge)).
- **MAXScript fallback:** it runs over the TCP listener, which has no dialog monitor, so it never returns `MAX_DIALOG_ERROR`. Its batch has no rollback either. Each entry is its own call and undo step, and a call that fails outright (a MAXScript error or a timeout, not a missing source) stops the batch with that error. The entries before it stay applied, the ones after it don't run, and the error doesn't say which ran.

**Quiet mode** (upstream `5c44e76`). MAXScript run through the bridge now uses Max's quiet mode by default, so prompts take their default answer; `execute_maxscript(quiet=False)` shows them. This doesn't change `replace_material`: the native handler runs no MAXScript, and the TCP listener doesn't use quiet mode.

**`invoke_tool` can run `replace_material` again** (`86ea1b8`). The bridge build generates its own tool registry from the Python tools, and the generator only finds a tool's native command in the tool's own body. `a9d2a0d` had moved that call into a helper, so every fork bridge built since then, the deployed one included, answered `invoke_tool("replace_material")` with "Unknown tool". Agents calling `replace_material` directly were never affected. The call is inline again, and a unit test checks that the generator finds both tools. The 2026 bridge in `native/bin/` (sha256 `d5cd6ba5…`) has the fix. Upstream's bridges never had the problem.

## Examples

```text
replace_material(source_material="Cosmos_Plaster", target_material="Plaster", preview=True)
→ source_found_in: "material_editor"
  affected_count: 0, affected_slot_count: 1
  affected_slots: [{parent_material: "Walls", parent_class: "Multimaterial", slot_index: 1, ...}]
```

```text
replace_material(source_material="Plaster", target_material="Cosmos_Plaster")   # reversed
→ status: "no_match", warnings: ["No object has a top-level or sub-material material named 'Cosmos_Plaster'.
  source = material to apply (on ...), target = material to replace; if reversed, swap them. ..."]
```

## How it was verified

- **The direction docs and `no_match`** (`a9d2a0d`) were verified live on 2026-10-01.
  - `replace_material(source="Glass", target="NoSuchMaterial_xyz", preview=True)` returned `no_match` with the direction warning.
  - The correct direction still listed the affected objects.
- **Sub-material matching, the source search, `source_from`, `blocked` and the loop guard** (`16528de`, `a670484`) have Python unit tests in `tests/test_material_replace.py`, but they mock the bridge. They check the payload, the text of the generated MAXScript and the Python post-processing (`no_match`, `blocked`, the `source_from` checks, warnings, `depends_on_entries`). The matching, the source search and the loop guard only run inside Max, so the unit tests don't cover them. An adversarial review covered native undo, reference lifetimes, the `TestForLoop` direction, and search side effects. The Python side was deployed on 2026-10-01. The native handler is in the rebuilt 2026 bridge (sha256 `5f49226e…`, shipped in `53cd33a`), deployed the same day.
  - The native handler was checked live at 12:30 on a Multi/Sub whose source was only in the Material Editor. The preview gave `source_found_in` "material_editor", one affected slot and an empty `skipped`. The real run replaced the slot, and `undo_last` put it back.
  - That empty `skipped` was the check that mattered most: a normal Multi/Sub slot isn't reported as `reference_loop`, so the SDK's `TestForLoop` result is read the right way round.
  - Not run in Max yet: nested Multi/Sub, the loop guard's skips, `source_from`, `blocked`, Hebrew names, batch, plugin parents such as VRayBlendMtl and Shell_Material, and the MAXScript fallback.
- **The 1.7.5 merge** didn't change how a replacement runs, native or Python. The 2026 bridge built from it (sha256 `d5cd6ba5…`, from `ec903a5`) isn't deployed or tested live yet.
  - The rollback after an error box hasn't been tried in Max. `native/tests/dialog_watch_tests.cpp` checks, without Max, that an acknowledged error box fails the operation where it resumes (`ffccbab`).
  - The fallback batch stopping at a failed call comes from reading the code; no test covers it.

## Upstream status

Not fixed upstream as of 2026-10-01, v1.7.5 included. The older, explicit docstring survives in kanzaka110's fork.
