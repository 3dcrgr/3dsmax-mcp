# `replace_material` that "replaced" nothing

Fork issue #3. Commits `6e9188b` and `9f520ba`.

## What happened

On a Revit scene with Hebrew names, `replace_material` was asked to swap a plaster material for a freshly imported Cosmos material. It answered `{"status": "replaced", "replaced_count": 0}`, and nothing changed. At first this looked like a Unicode bug with the Hebrew name.

## Why

It wasn't an encoding problem. In the native handler, `source_material` is the material to **apply**, and `target_material` is the material being **replaced**. The handler looks for objects whose material is named `target_material` and gives them `source_material`. The agent had passed the two names the other way round. The tool's docstring didn't say which was which; upstream had trimmed an older docstring that did. The Hebrew name matched fine: an unmatched source name throws "not found", and this call didn't.

Three other limitations made the tool fail quietly on real scenes:

- **A success status with zero replacements**, which hid the mistake.
- **Only top-level object materials were matched.** A target inside a Multi/Sub-Object material was never replaced, and Revit and ATF imports produce Multi/Sub materials all the time.
- **The source had to be assigned to an object already.** A material sitting in a Material Editor slot, which is where a Cosmos import puts it, or one that only existed as a sub-material, couldn't be applied.

## What changed

**The direction is documented** (`6e9188b`). Both docstrings say: source = the material to apply, target = the material to replace. The skill guide says the same.

**Nothing matched → `no_match`** (`6e9188b`, refined in `9f520ba`).
- When no object *and* no sub-material slot matched, the result is `status: "no_match"` plus a warning. The warning explains the direction and where the source can come from.
- Batch entries behave the same way.

**Sub-material slots are matched** (`9f520ba`). The new parameter `include_sub_materials` defaults to `true`.
- Every Multi/Sub-Object slot, or any other material's sub-material slot, that holds a material named `target_material` gets the source.
- A material shared by many objects is changed once.
- A material named like the target is replaced as a whole, and isn't searched inside.
- Slots that would create a reference loop are skipped and listed in `skipped` with a reason:
  - `parent_is_source`;
  - `source_contains_parent`;
  - `reference_loop`, from the SDK's `TestForLoop`;
  - `set_failed`, when a plugin material ignored the change.

**The source is found outside the scene** (`9f520ba`). The search runs in this order:
1. object materials;
2. their sub-materials;
3. the 24 Material Editor slots;
4. the scene materials;
5. the current material library.

The result reports `source_found_in`. If several different materials share the name, the first one found wins, and the result adds `source_ambiguous: true`, `source_candidates` and a warning.

**A preview with a missing source no longer fails.** It returns `source_exists: false` together with what the target would affect. That lets the agent see that it probably swapped the arguments.

New result fields:

| Field | Meaning |
|---|---|
| `replaced_slots` / `affected_slots` | `{parent_material, parent_class, slot_index, slot_name}` per slot. `slot_index` is 1-based, like MAXScript; for a Multi/Sub it's the position in `materialList`, not the material ID. |
| `replaced_slot_count` / `affected_slot_count` | Number of slots |
| `source_exists`, `source_found_in` | Where the source was found |
| `source_ambiguous`, `source_candidates` | Only when the name isn't unique |
| `skipped` | Slots left alone, each with a `reason` |
| `include_sub_materials` | Echo of the parameter |

`replaced_count` and `replaced_objects` still count objects only, as before.

**Batch entries now run in order.** In `batch_replace_materials`, each entry sees the scene as the previous entries left it.
- Before, the handler took one snapshot up front and kept raw material pointers between entries. A material whose last user was reassigned by an earlier entry could be auto-deleted by Max while the batch still held it.
- Side effect: a pair like `A→B, B→A` no longer swaps two materials. To swap, go through a temporary material: `A→Tmp`, `B→A`, `Tmp→B`.

**The MAXScript fallback matches the native handler.** This is the fallback used when the native bridge isn't available. It has the same lookup order, slot matching and result keys, and it runs inside one undo step. Its `status` on success is still `"success"`, where native returns `"replaced"`.

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

- **The direction docs and `no_match`** (`6e9188b`) were verified live on 2026-10-01.
  - `replace_material(source="Glass", target="NoSuchMaterial_xyz", preview=True)` returned `no_match` with the direction warning.
  - The correct direction still listed the affected objects.
- **Sub-material matching, the source search and the loop guard** (`9f520ba`) are unit-tested on the Python side only (`tests/test_material_replace.py`, 14 new cases). The native handler compiles cleanly, but it hasn't been run in Max yet.
  - The live test plan includes Multi/Sub, nested Multi/Sub, a source that's only in the Material Editor, the loop guard, Hebrew names, batch, and plugin parents such as VRayBlendMtl and Shell_Material.
  - One check matters most: a normal Multi/Sub replacement must not be reported as `reference_loop`. If it is, the meaning of the SDK's `TestForLoop` result is inverted.

## Upstream status

Not fixed upstream as of 2026-10-01. The older, explicit docstring survives in kanzaka110's fork.
