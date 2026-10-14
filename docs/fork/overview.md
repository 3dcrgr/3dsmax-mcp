# The fork in detail

<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../images/logo-white.png">
    <img src="../../images/logo.png" alt="3dsmax-mcp logo" width="600">
  </picture>
</p>

This page was the fork's README until 2026-10-14. The repository README is now upstream's, with a short fork note, so the fork stays a set of changes to upstream rather than a separate project. [FORK.md](../FORK.md) lists every change and how it was verified.

This is a fork of [cl0nazepamm/3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp), based on upstream **1.7.5** (`29aed53`).

It carries fixes for problems found while using the MCP on production architectural visualization. The worst of them hung 3ds Max and lost unsaved work. It also adds tools for Chaos Vantage, RailClone, Forest Pack and props that float or sink.

Verified and deployed improvements are published on the fork's default branch, `fix/fork-fixes`. We are consolidating the improvements into one pull request to upstream.

## At a glance

Forest Effects supports guarded ordering, numeric parameters, object, spline, existing texture-map, and scalar `FloatController` bindings. Scalar bindings use a source-linked expression driver on Forest's animated real parameter track; `forest_effect_parameters(action="repair_animation")` migrates earlier bindings that sampled frame zero. `.eff` import requires explicit scene bindings; export writes portable definitions with required bindings listed for the next import. Curve definitions allocate native slots on the staged copy before commit, with neutral flat y=1 points and parameter-ID preservation during removal. `forest_effect_curves` remains read-only; animated curve reordering, curve point editing and custom `.eff` curve data remain unsupported. Stored effect code has no public compile-success signal. See [iToo coverage](itoo-api-coverage.md).

- **209 tools:** all 179 of upstream's, plus 30 new ones (marked ★ in the [catalog](#tools)).
- **Tested in production on:**
  - 3ds Max 2027.2, and 3ds Max 2026 before that;
  - V-Ray 7 (GPU as the production renderer);
  - Chaos Cosmos and Chaos Vantage 3.4;
  - RailClone 7.3.5 and Forest Pack 9.4.3;
  - scenes linked from Revit.
- **Native bridges:** fork builds for Max 2026 and 2027 in `native/bin/`. The 2023–2025 bridges are still upstream's; see [Native bridge](#native-bridge).
- **Same requirements as upstream:** Windows, 3ds Max 2023–2027, and an MCP client such as Claude Desktop, Claude Code, Codex or Cursor.

[docs/FORK.md](../FORK.md) lists every change with how it was verified (live in Max, or unit-tested only). The pages under [docs/fork/](./) give the details.

Startup and lifecycle diagnostic helpers also handle zero-padded Max.log PIDs and confirm process disappearance before recording exit. Their offline checks are documented in [iToo coverage](itoo-api-coverage.md); these checks did not restart or modify Max.

## What it fixes

### When 3ds Max hangs or is busy
- A hung Max no longer turns every tool into a bare timeout.
  - Calls fail fast with `MAX_BUSY` or `MAX_NOT_RESPONDING` and a process diagnosis: whether the main window responds, CPU use, whether it exited.
  - The client never replays a request that may already have run.
- `get_bridge_status` answers from a pipe thread while Max's main thread is blocked. It reports what holds the main thread and which clients are connected.
- Max no longer stalls on exit with queued requests or connected idle pipe clients: queued work fails and pending pipe reads/writes are cancelled and drained. The 2027 bridge passed scratch-Max quit checks with clients retained.
- Startup failures include native target and transport context. Exited instance registrations are cleaned conservatively, preserving live, uncertain and changed records.
- An MCP server whose client crashed exits with it, instead of staying connected to Max.

### Dialogs and unsaved work
- **Save prompts:** quiet mode never answers a "Save changes?" prompt by itself. A script that mentions a reset, open, fetch or quit command runs with quiet mode off, so the prompt reaches the agent as `BLOCKED_BY_DIALOG`. Forcing `quiet=True` on such a script also needs `allow_discard=True`.
- **Fixes to upstream 1.7.5's dialog handling:**
  - A hung main thread is reported as a hang, not as "blocked by a dialog".
  - Errors are recorded before the blocked call resumes.
  - A press that timed out is withdrawn or reported as `DIALOG_OUTCOME_UNKNOWN`.
  - Quiet mode only changes on Max's main thread.
  - The Cosmos browser, the Material Editor and the agent viewport are never treated as dialogs. Dialogs that belong to other threads are listed, never pressed.
- **Interrupted scripts:** a script interrupted by `quitMax` is reported as `MAXSCRIPT_INTERRUPTED`, not as a parse error.

### Several sessions or several 3ds Max instances
- **Shared selection:** the selected Max belongs to the MCP server process, so sessions that share a server share it.
  - Every tool except the four routing tools takes an optional `expected_pid`. The call is refused, with nothing sent, if it would reach another Max.
  - Every reply's `transport` carries `selection_generation`, so a session can see that the selection moved.
- **A default target whose Max exited** re-resolves to the new Max when that choice is unambiguous, and the reply warns. With several Max instances running it refuses instead of guessing. An explicit selection is never replaced.

### Chaos Cosmos
- **No more stalls:** `cosmos_import` no longer stalls Max for minutes or deadlocks it on a later window close. It opens the Cosmos browser on Max's main thread first, waits for Max to settle, and refuses other calls with `IMPORT_SETTLING` while Max is still busy.
- **Lighter waiting:** it polls with light read-only probes instead of a full class scan every 250 ms. `cosmos_search` works without Max's main thread.
- **Finds what it created** by handle, whatever the names: materials, maps, proxies, containers and groups. Untouched default slot materials aren't listed as new.
- **Leaves the user's things alone:** it keeps the active Material Editor slot, and minimizes the browser it opened once the import is confirmed.

### Materials
- **`replace_material`:**
  - It no longer "replaces" nothing while reporting success.
  - Its direction is documented, and `no_match` warns.
  - It matches Multi/Sub sub-material slots, and finds source materials outside the scene.
  - Batch entries apply in order.
- **`material_roles`:**
  - It reads texture paths of Revit materials (Autodesk Bitmap), which read as empty before.
  - It flags unreadable paths.
  - It gives one row per VRayBitmap slot.

### Modeling and scene
- The curve tools accept Line objects as editable splines.
- `contact_check` takes its thresholds in millimetres, skips non-mesh nodes with a warning, and works within a time budget (`time_budget_s`) on dense meshes.
- Cloning a member of a closed group clones only that member, instead of the whole group stacked invisibly on the original. `scene_qa` reports such stacked duplicates.

### Agent viewport
- `agent_viewport` reclaims its own floating viewport after a restart, save or Hold/Fetch. It never takes an untagged panel.
- The V-Ray previews (`vray_ipr`, `vray_vfb`) work with **V-Ray GPU**. They used to require a V-Ray CPU property and refused GPU.

### RailClone (the existing tools)
- `get_railclone_style` and `set_railclone_style` accept the repeated attributes RailClone 7 writes. They rejected 7 of 43 stock styles.
- `get_railclone_output` rebuilds before reading, so it no longer returns stale positions after a move or copy. It flags placeholder items and adds world bounds.
- Reading never re-captures segment geometry. A reload only renames linked segments, so it is opt-in (`reload_segments`), and `force_rebuild` picks up source edits made while auto-update is off.

### Forest Pack (the existing tool)
- `scatter_forest_pack`:
  - **Density** is set through the tile size (`items_per_m2`, or `density` as a % of `density_units_*_cm`). The old `maxdensity` write did nothing.
  - **"Up" facing** is `direction` 100, on the real −100…100 range.
  - **The real item count** is returned, with `ZERO_ITEMS` for an empty scatter.
  - It **never writes `geomlist` 3**, which crashes Max.
  - It **warns** when a user-saved default preset changes a new Forest.

## What it adds

| Area | New tools | What they do |
|---|---|---|
| Diagnostics | `capture_hang_diagnostics` | Native thread stacks of a hung Max, read from the OS, showing what blocks the main thread |
| Props | `resting_check`, `drop_to_surface` | Find props floating above or sunk into the surface below them, gaps in millimetres; drop floating ones onto it (dry run by default, one undo step) |
| Chaos Vantage | `vantage_status`, `vantage_get_image`, `vantage_live_link` | Read Vantage's state over its local command port, save the frame it shows as a PNG, start or stop V-Ray GPU Live Link. Vantage's command port isn't documented by Chaos |
| RailClone | `railclone_inspect`, `railclone_set_param`, `railclone_library`, `railclone_from_library` | Inspect bases, exposed parameters, segments and output; set parameters by name within their limits; search the library; create items by route or name, with splines assigned by role. They report the running RailClone version, warn about version-specific library bugs, and handle Proxy Cache |
| Forest Pack | `forest_items`, `forest_edit_items`, `forest_set_mode`, `forest_library`, `forest_from_library`, `forest_validate` | Read items with instance-ready transforms; edit items in custom edit mode with a revert list (Forest doesn't undo item edits); search the library and place presets or 3D items; validate item and area lists, including Reference-mode forests fed by a RailClone and warnings when random X/Y translation can cross active spline boundaries; name why a forest is empty |
| iToo API extensions | `itoo_api_inventory`, `itoo_catalog`, `railclone_slots`, `railclone_metadata`, `railclone_proxy`, `railclone_instances`, `railclone_settings`, `forest_inspect`, `forest_effects`, `forest_effect_parameters`, `forest_effect_curves`, `forest_settings`, `forest_effect_library` | [Coverage and remaining gaps](itoo-api-coverage.md): live API signatures, guarded source/segment/settings edits, read-only Forest translation extents and CurveControl inspection, metadata, cache/instance controls and tested Forest Effects editing and library reads |
| iToo → Vantage | `itoo_vantage_check` | Read-only check of how ready Forest Pack, RailClone and ForestIvy objects are for Chaos Vantage: colour-map slot 1, opacity maps, CPU-only features, ID-driven maps, render settings, caps and the `fullMeshProxy` setting. A fallback is suggested for objects with errors |

New options on existing tools:
- `expected_pid` on every tool, and `selection_generation` in every reply's transport;
- `allow_discard` on `execute_maxscript`;
- `time_budget_s` on `contact_check`;
- `reload_segments` and `force_rebuild` on the RailClone read tools;
- `items_per_m2` and `direction` on `scatter_forest_pack`.

## What it removes or changes on purpose

Nothing of upstream's is removed: every upstream tool and feature is still here. These behaviours change on purpose:

- **Quiet mode around file commands:** a script that mentions a scene reset, open, fetch or quit command runs with quiet mode off. Its prompts reach the agent instead of taking their default answer, which could discard unsaved work.
- **`batch_replace_materials`** applies entries in order. Two entries meant to swap two materials no longer swap.
- **RailClone reads** no longer call `segmentsUpdate`. It changed no geometry and renamed linked segments. Pass `reload_segments=true` to run it on purpose.
- **`scatter_forest_pack`:**
  - `density` now means tile size, because `maxdensity` had no effect;
  - "up" facing writes `direction` 100, where the old value 1 was almost the surface normal;
  - `geomlist` 3 is refused.
- **The dialog monitor** no longer reads or presses dialogs of other threads. The Cosmos browser, the Material Editor and the agent viewport are never dialogs.
- **Max targets:**
  - An explicit selection is never replaced silently.
  - A default target is only re-resolved after its Max exited, and only when that choice is unambiguous.
- **The 2026 and 2027 bridges** in `native/bin/` are fork builds, not upstream's.

## Install

Install from source, from this fork. The default branch, `fix/fork-fixes`, is the line described here. Close 3ds Max and your AI clients first.

```powershell
git clone https://github.com/3dcrgr/3dsmax-mcp.git
cd 3dsmax-mcp
uv sync
uv run python install.py
```

Requires [Python 3.12+](https://www.python.org/), [uv](https://docs.astral.sh/uv/) and Git. Upstream's `3dsmax-mcp-*-Setup.exe` installs upstream, without these changes.

**After updating, start a new client session.** MCP clients cache the tool list, so new tools don't appear in sessions that were already open.

You can also run the server straight from a checkout, without the installer, by pointing your MCP client at the installed runtime. This is how the production machine runs it:

```json
"3dsmax-mcp": {
  "command": "C:\\Program Files\\3dsmax-mcp\\runtime\\python.exe",
  "args": ["-c", "import sys; sys.path.insert(0, r'C:\\path\\to\\3dsmax-mcp'); import runpy; runpy.run_module('maxmcp.server', run_name='__main__', alter_sys=True)"]
}
```

The installer's runtime ignores `PYTHONPATH`, hence the `sys.path` line. See [Advanced configuration](../ADVANCED.md) for manual client setup and tool profiles.

### Native bridge

`install.py` copies the bridges from `native/bin/` into `C:\ProgramData\Autodesk\ApplicationPlugins\3dsmax-mcp\Contents\bin\`.

| Max | Bridge in `native/bin/` |
|---|---|
| 2027 | Fork build, with every native fix, including the V-Ray GPU preview (sha256 `b95802e2…`) |
| 2026 | Fork build with every native fix except the V-Ray GPU preview (sha256 `d5cd6ba5…`) |
| 2023–2025 | Upstream's 1.7.5 builds |

With an upstream bridge, the Python-side fixes work but the native ones are missing. For those, build the bridge for your version yourself: see [Building the native bridge](../FORK.md#building-the-native-bridge). It needs Visual Studio 2022 with the v143 14.38 toolset and the 3ds Max SDK. To replace only a bridge, close every 3ds Max first and keep a copy of the old file.

## Tools

The ★ marks tools this fork adds. The others are upstream's, some fixed here, as described above.

<details>
<summary>Browse all 196 tools</summary>

### Bridge & session

| Tool | Description |
|------|-------------|
| `get_bridge_status` | Ping the MCP bridge when diagnosing connection errors |
| `get_session_context` | Bundle bridge status, capabilities, scene summary, and selection in one call |
| `get_plugin_capabilities` | Max version, renderers, installed plugins, and class counts |
| `list_max_instances` | List live Max instances in default order, without selecting one |
| `select_max_instance` | Bind this MCP server process to one Max (shared by every session using that server) |
| `get_selected_max_instance` | Read the selected target, its availability and `selection_generation` |
| `release_max_instance` | Release the target; the next call uses the default |

### Scene state & transactions

| Tool | Description |
|------|-------------|
| `query_scene` | Unified reads: `overview`, `filter`, `class`, `property`, `selection`, `delta` |
| `resolve_node_refs` | Resolve names, handles, or hierarchy paths to canonical, cross-checked NodeRefs |
| `scene_qa` | Scan scene-graph hygiene and apply explicit safe naming repairs; never analyzes meshes |
| `scene_patch` | Preflight and atomically apply up to 256 mechanical node edits in one native undo step |
| `get_hierarchy` | Recursive child tree for an object |
| `get_instances` | All instances sharing the same base object |
| `get_dependencies` | Reference graph via dependents / dependent nodes |

### Objects

| Tool | Description |
|------|-------------|
| `create_object` | Create geometry with spatial placement feedback |
| `delete_objects` | Delete objects by name |
| `get_object_properties` | Compact properties for one object |
| `set_object_property` | Set a single object property |
| `transform_object` | Move, rotate, and/or scale by offset |
| `analyze_node_orientation` | Pivot, bbox, local axes, and world matrix for rigging and placement |
| `clone_objects` | Copy, instance, or reference clones |
| `set_parent` | Parent or unparent objects |
| `select_objects` | Change the current selection |
| `set_visibility` | Show, hide, freeze, or unfreeze |
| `batch_rename_objects` | Rename many objects in one call |

### Modifiers

| Tool | Description |
|------|-------------|
| `add_modifier` | Add a modifier to an object |
| `remove_modifier` | Remove a modifier by name |
| `set_modifier_state` | Enable/disable with viewport/render granularity |
| `set_modifier_property` | Set a modifier parameter on one or many objects |
| `collapse_modifier_stack` | Collapse the stack |
| `make_modifier_unique` | De-instance a shared modifier |
| `inspect_modifier_properties` | Compatibility alias for `inspect_properties(target="modifier")` |

### Modeling

| Tool | Description |
|------|-------------|
| `curve_model` | Named curves, rounded profiles, sweeps and quad lofts with controls saved in the scene |
| `inspect_curve` / `edit_curve` | Inspect, visually target and edit spline knots and handles with stale-token protection |
| `create_mesh` | Build editable polygon cages from explicit vertices and faces |
| `inspect_mesh` / `pick_component` | Read cage geometry, label components and map image positions to edit targets |
| `mesh_edit` | Undoable vertex, edge and face edits that preserve the modifier stack |
| `loft_mesh` | Matched-section quad lofts with persistent numeric parameters |
| `geometry_qa` | Check mesh boundaries, winding, degeneracy and connected components |
| `contact_check` | Find parts that pass through each other, touch, or float just short of contact (thresholds in mm, time budget) |
| ★ `resting_check` | Find props floating above or sunk into the surface below them, gaps in millimetres |
| ★ `drop_to_surface` | Drop floating props onto the surface below them (dry run by default, one undo step) |
| `boolean_operation` | Apply, inspect, retune, rename, or extract Boolean modifier operands; supports inline repeated cutters |
| `draw_spline` | Create, read, and edit spline shapes from explicit world-space points and knots |
| `edit_vertices` | Read, move, set, or conform Editable Poly vertices in world space |

### Materials & textures

| Tool | Description |
|------|-------------|
| `get_materials` | List materials assigned in the scene |
| `get_material_library` | Inspect `currentMaterialLibrary` and Material Editor scratch slots |
| `backup_material_library` | Save temporary/scratch material libraries to `.mat` files |
| `get_material_slots` | Compact slot/property readback for a material |
| `inspect_material_network` | Semantic material graph, wired slots, texture manifest, health checks |
| `material_roles` | Texture sources per material slot, including shared maps, composites, submaterials and Autodesk Bitmap (Revit) paths; flags unreadable and missing files |
| `replicate_material` | Preview/apply structure-preserving material clone and texture remap |
| `assign_material` | Create a material and assign it to objects |
| `set_material_property` | Set one property on an object's material |
| `set_material_properties` | Set multiple material properties at once |
| `set_sub_material` | Create or assign a Multi/Sub-Object slot |
| `create_texture_map` | Create a texture map (stored as a MAXScript global) |
| `set_texture_map_properties` | Edit a texture map global |
| `create_material_from_textures` | Build a fully wired PBR material from texture files |
| `create_shell_material` | Wrap any render + export materials in Shell Material (dual pipeline) |
| `write_osl_shader` | Write OSL to disk and compile an OSLMap |
| `replace_material` | Swap one material for another on all users, including Multi/Sub slots; finds sources outside the scene |
| `batch_replace_materials` | Batch material replacement, applied in order |
| `palette_laydown` | Fill Material Editor palette slots from a texture folder |
| `smart_import` | Batch-import meshes from a folder with auto PBR assignment |
| `cosmos_search` | Find compatible Cosmos models, materials and HDRIs |
| `cosmos_download` | Download a Cosmos asset without importing it |
| `cosmos_import` | Import a Cosmos asset through Corona or V-Ray, without stalling Max; reports what it created |

### Inspection

| Tool | Description |
|------|-------------|
| `inspect_object` | Deep exploratory object summary |
| `inspect_properties` | Deep property dump (`target="object"|"modifier"|"baseobject"|"material"`) |
| `introspect_osl` | API surface for OSLMap and shader classes |
| `walk_references` | Full reference dependency walk |
| `learn_scene_patterns` | Analyze class usage patterns in the live scene |
| `map_class_relationships` | ParamBlock2 reference relationships between classes |
| `watch_scene` | Live event watcher for interactive sessions |
| `isolate_and_capture_selected` | Per-selection isolated viewport captures |
| `main_thread` | Inspect or clean up callbacks and timers running on Max's main/UI thread |
| `max_dialogs` | Read any dialog blocking Max (Win32 or Qt) and press a chosen button, even while a tool call waits on it |

### Plugins & introspection

| Tool | Description |
|------|-------------|
| `discover_plugin_surface` | Find plugin-related classes and entry points |
| `discover_plugin_classes` | Enumerate registered SDK classes |
| `list_plugin_classes` | List classes for a plugin or superclass family |
| `inspect_plugin_class` | SDK schema v2: bounded metadata, exact bindings and enum sources; legacy reflection retained |
| `inspect_plugin_constructor` | Creation notes for a plugin class |
| `inspect_plugin_instance` | SDK schema v2: live values, linked maps and guarded edit tokens |
| `plugin_patch` | Atomic typed PB2 edits with readback and stale/shared/animation guards |
| `get_plugin_manifest` | Structured manifest (classes, workflows, gotchas) |
| `refresh_plugin_manifest` | Rebuild manifest from live runtime |
| `introspect_class` | SDK parameter descriptors and published interface metadata |
| `introspect_instance` | Deep SDK introspection with live values |

MCP resources: `resource://3dsmax-mcp/plugins/{name}/manifest|guide|recipes|gotchas`

### Lighting

| Tool | Description |
|------|-------------|
| `lighting_capabilities` | Installed renderer routes, emitter shapes, units and color policy |
| `create_lights` | Semantic lights and environment graphs in one verified native transaction |
| `inspect_lights` | Decode actual light settings and linked emission maps |
| `edit_lights` | Guarded color, output, size, enable and shadow edits |

Providers cover Corona, V-Ray, Octane and native photometric emitters. Query capabilities
for the current renderer: shapes, units and environment routes differ. HDRI maps
and existing procedural skies use the provider's dome or environment binding.

### Controllers & animation

| Tool | Description |
|------|-------------|
| `assign_controller` | Create and assign a controller to a sub-anim track |
| `script_controller` | Inspect, validate and apply scripts with typed inputs, frame checks and rollback |
| `inspect_controller` | Inspect one controller track |
| `inspect_track_view` | Track View-style controller hierarchy |
| `set_controller_props` | Edit script text or controller properties |
| `add_controller_target` | Add a target to script/expression/constraint controllers |
| `keyframe_tracks` | Timeline control; bounded delete/move/scale, bake/resample, tangent normalization, pose matching, and loops |

### Parameter wiring

| Tool | Description |
|------|-------------|
| `list_wireable_params` | Discover wireable sub-anims on an object |
| `wire_params` | Connect parameters with a wire expression |
| `get_wired_params` | List existing wire connections |
| `unwire_params` | Remove a wire |

### Organization

| Tool | Description |
|------|-------------|
| `manage_layers` | Create, delete, list, and configure layers; move/select objects |
| `manage_groups` | Create, ungroup, open, close, attach, detach groups |
| `manage_selection_sets` | Named selection sets |
| `manage_scene` | Hold, fetch, reset, save, scene info |
| `undo_last` | Undo the last 3ds Max scene operation |

### Viewport & render

| Tool | Description |
|------|-------------|
| `agent_viewport` | Independent floating agent view, visual targeting and V-Ray preview controls (V-Ray CPU and GPU); reclaimed after a restart |
| `set_viewport` | Position and frame the agent or user viewport |
| `capture_viewport` | Capture the agent or user viewport as an image |
| `capture_multi_view` | Capture several views into one image, with agent-view restoration |
| `capture_screen` | Capture visible desktop pixels or crop to the V-Ray frame buffer |
| `render_scene` | Render the current view |
| `render_automations` | Arm/poll completion signals, request cancellation or capture a progressive render before cancelling |

### External `.max` files

| Tool | Description |
|------|-------------|
| `inspect_max_file` | Read metadata and object names without loading |
| `merge_from_file` | Selective merge with duplicate handling |
| `search_max_files` | Scan a folder for objects matching a pattern |
| `batch_file_info` | Parallel metadata from many `.max` files |

### Effects & state sets

| Tool | Description |
|------|-------------|
| `get_effects` | List atmospheric and render effects |
| `toggle_effect` | Enable or disable an effect by index |
| `delete_effect` | Remove an effect by index |
| `get_state_sets` | State Sets with camera assignments |
| `get_camera_sequence` | Camera-assigned State Sets sorted by frame |

### Data Channel

| Tool | Description |
|------|-------------|
| `list_dc_operators` | Search the live version-specific operator catalog |
| `list_dc_presets` | List installed Data Channel presets |
| `add_data_channel` | Append operators to a Data Channel modifier stack |
| `inspect_data_channel` | Read the active stack in visible processing order |
| `set_data_channel_operator` | Edit one visible operator with rollback on failure |
| `manage_data_channel_stack` | Delete, enable, disable, or safely reorder operators |
| `load_dc_preset` | Load a preset into the stack |
| `add_dc_script_operator` | Add executable MAXScript with explicit authorization |

### Max Creation Graph

| Tool | Description |
|------|-------------|
| `mcg_get_context` | Compiler, temporary workspace, templates, samples, and safety state |
| `mcg_list_graphs` | List temporary, installed, or bundled sample graphs |
| `mcg_inspect_graph` | Normalize graph metadata, nodes, connections, and parameters |
| `mcg_search_operators` | Search live typed operators and offline compound references |
| `mcg_create_graph` | Fork a read-only source into the temporary workspace |
| `mcg_apply_patch` | Patch, compile, verify, checkpoint, and roll back transactionally |
| `mcg_compile_graph` | Validate and compile one temporary graph with diagnostics |
| `mcg_test_tool` | Create, inspect, and remove a disposable generated instance |
| `mcg_resolve_class` | Resolve the exact generated modifier class descriptor |
| `mcg_apply_modifier` | Compile and apply a typed MCG modifier safely |
| `mcg_inspect_instance` | Verify graph identity and inspect a live MCG modifier |
| `mcg_set_node_parameter` | Retarget one supported scalar node parameter |
| `mcg_restore_checkpoint` | Restore an opaque checkpoint with hash protection |
| `mcg_cleanup_workspace` | Remove one graph family or the temporary MCG workspace |
| `mcg_reload_operators` | Explicitly refresh Max's global MCG operator depot |

### tyFlow

| Tool | Description |
|------|-------------|
| `list_tyflow_operator_types` | Available operator names for this install |
| `create_tyflow` | Create tyFlow with events and operators |
| `create_tyflow_preset` | Presets: rain, snow, fountain, burst, debris |
| `get_tyflow_info` | Deep flow/event/operator readback |
| `harvest_tyflow_manifest` | Probe and cache the installed tyFlow operator surface by tyFlow version |
| `list_tyflow_operators` | Query the cached tyFlow operator manifest without touching Max |
| `get_tyflow_graph` | Read events, operators, properties, ledger edges, and wiring staleness |
| `tyflow_apply_patch` | Apply a batch of tyFlow graph operations as one verified transaction |
| `connect_tyflow_operator` | Connect or retarget a test/Send Out operator to an event |
| `disconnect_tyflow_operator` | Disconnect an operator output and remove its ledger edge |
| `set_tyflow_wiring_ledger` | Reconcile recorded graph wiring after external or visual edits |
| `tyflow_event_census` | Count particles per event at probe frames with temporary instrumentation |
| `capture_tyflow_editor` | Open and capture the tyFlow editor for visual wire inspection |
| `modify_tyflow_operator` | Edit operator properties |
| `set_tyflow_shape` | Configure Shape operator |
| `set_tyflow_physx` | Object-level PhysX settings |
| `add_tyflow_collision` | Collision operator + collider list |
| `add_tyflow_event` | Add an event |
| `connect_tyflow_events` | Wire Send Out destinations |
| `remove_tyflow_element` | Remove operator or event |
| `get_tyflow_particle_count` | Particle count at a frame |
| `get_tyflow_particles` | Particle data rows |
| `reset_tyflow_simulation` | Reset one or all tyFlow sims |

### RailClone

| Tool | Description |
|------|-------------|
| ★ `railclone_inspect` | Read bases, exposed parameters, segments, graph summary, output counts and the running RailClone version |
| ★ `railclone_set_param` | Set exposed parameters by name with limit checks, in one undo step |
| ★ `railclone_library` | Search the RailClone library browser roots (no Max call) |
| ★ `railclone_from_library` | Create a library item by validated route or name and assign splines by role |
| `get_railclone_style` | Read complete style XML and edit token (RailClone 7.x) |
| `set_railclone_style` | Replace style XML with guarded, undoable adoption |
| `get_railclone_output` | Rebuild, then read generated segments, transforms, world bounds and tags; placeholders flagged |

### Forest Pack

| Tool | Description |
|------|-------------|
| `scatter_forest_pack` | Create a Forest Pack scatter with surfaces and source geometry; returns the real item count |
| ★ `forest_items` | Read items, paged: pivot position, instance-ready transform, item type and size |
| ★ `forest_edit_items` | Move, delete, create or resize items in custom edit mode; returns a revert list (item edits aren't in undo) |
| ★ `forest_set_mode` | Switch between procedural and custom edit mode |
| ★ `forest_library` | Search the Forest Pack library: presets, 3D items, billboards and Forest Ivy items (no Max call) |
| ★ `forest_from_library` | Place a library preset or 3D item on surfaces (clears the undo history; needs `allow_undo_flush`) |
| ★ `forest_validate` | Check the item and area lists and the item count, including Reference-mode forests fed by a RailClone; `fix=true` pads short lists |

### Forest Pack / RailClone → Chaos Vantage

| Tool | Description |
|------|-------------|
| ★ `itoo_vantage_check` | Read-only readiness check for Chaos Vantage: colour-map slot 1, opacity maps, CPU-only features, ID-driven maps, render settings, caps, `fullMeshProxy` |

### Chaos Vantage

| Tool | Description |
|------|-------------|
| ★ `vantage_status` | Read Vantage's state over its local command port, without touching Max |
| ★ `vantage_get_image` | Save the frame Vantage is showing as a PNG |
| ★ `vantage_live_link` | Start or stop V-Ray GPU Live Link from Max to a running Vantage, only on request |

### Scripting & diagnostics

| Tool | Description |
|------|-------------|
| `execute_maxscript` | Run MAXScript when no dedicated tool exists (respects safe mode) |
| `execute_python` | Run Python with captured output, JSON results, tracebacks and undo rollback (requires safe mode off) |
| `invoke_tool` | Call any registered tool from inside Max (testing) |
| `run_tool_smoke` | Run generated live smoke cases against the native bridge inside Max |
| ★ `capture_hang_diagnostics` | Native thread stacks of a hung Max, read from the OS: what blocks the main thread |

</details>

Every tool except the four instance-routing tools also takes an optional `expected_pid`.

---

## Skill & reference

The installer builds an agent skill from `skills/3dsmax-mcp-dev/SKILL.md`: tool-choice rules, material pipeline notes, MAXScript reference files, and this fork's notes for RailClone, Forest Pack, Cosmos and multiple Max instances. Rebuild it manually with `python scripts/build_skill.py`; see [Advanced configuration](../ADVANCED.md#agent-skill).

## Tests

Upstream's `.gitignore` excludes `/tests/` and `/native/tests/`. This fork force-adds the tests that cover its changes.

```powershell
uv run python -m unittest discover -s tests
```

[docs/FORK.md](../FORK.md#running-the-tests) has the native tests and other ways to run the Python ones.

## Further reading

- **[docs/FORK.md](../FORK.md):** every fix and addition, with how it was verified, the bridges, building and branches.
- **[docs/fork/](./):** one page per area: hung Max, Cosmos import, materials, RailClone, Forest Pack, Vantage, props.
- **[CHANGELOG.md](../CHANGELOG.md):** release history, with the fork's changes on top.
- **[Advanced configuration](../ADVANCED.md):** architecture, safe mode, tool profiles, native builds.
- **[Contributing](../../CONTRIBUTING.md):** upstream's contribution rules.

## Credits

- **The project:** [cl0nazepamm](https://github.com/cl0nazepamm/3dsmax-mcp) and upstream's contributors.
- **The executor shutdown drain and the process-exit check:** from [Geddart](https://github.com/Geddart/3dsmax-mcp)'s fork.
- **The pipe-lock timeout idea:** from [stoxsss111](https://github.com/stoxsss111/3dsmax-mcp)'s fork.
- **The orphaned-server watchdog idea:** from [kanzaka110](https://github.com/kanzaka110/3dsmax-mcp)'s fork.
- **`material_roles`:** from [EdgecraftStudio](https://github.com/EdgecraftStudio/3dsmax-mcp) (upstream PR 29); the Autodesk Bitmap fix builds on it.

## License

[MIT](../../LICENSE), as upstream.

### Forest Effects verification, 2026-10-14

Issues #43 and #45 passed deployed scratch-Max 2027 checks: source animation before/after save-reload, numeric-before-curves import, static curve reindexing, and malformed-slot refusal. The new repair action requires reloading the Python MCP server and the updated native bridge. Only the Max 2027 bridge was rebuilt; Setup/release distribution remains pending. See [verification and hashes](forest-effects-verification-2026-10-14.md).
