# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## Fork changes (3dcrgr/3dsmax-mcp, on top of 1.7.5)

- `cosmos_import` no longer stalls or deadlocks V-Ray scenes. It opens the Cosmos browser on Max's main thread before importing (the importer's own hidden browser thread caused the stalls), waits for Max to settle, restores the selection, and never opens or closes the Material Editor. If Max stays busy, the result says `safe_to_edit: false` and other calls fail with `IMPORT_SETTLING` without being sent, for 15 minutes at most. See [docs/fork/cosmos-import.md](fork/cosmos-import.md).
- The new `cosmos_import` option `swap_medit_renderer` (off by default) uses Scanline as the Material Editor renderer during the import. It's turned on automatically when the browser can't be opened on the main thread. It avoids the preview stall, not the deadlock. The original renderer is put back once Max settles. If a Material Editor is open then, or `restore_medit_renderer` is false, it stays at Scanline and the result gives the script to restore it.
- `cosmos_import` polls with light read-only probes instead of a full class scan every 250 ms. `cosmos_search` no longer needs a Max round trip when exactly one Cosmos importer is registered for the target Max, or when `renderer` is `vray` or `corona` and that Max has an importer for it. With both the V-Ray and Corona importers and `renderer="current"`, it still reads the scene renderer from Max once (`renderer_source: "scene"`).
- Over the named pipe, a busy or hung Max now gives `MAX_BUSY` or `MAX_NOT_RESPONDING`, with window and CPU evidence, instead of a bare timeout. The pipe lock has a timeout and replies have a real deadline; long work that is still running is waited for. A request that may have run is never replayed (`request_sent` says whether it reached Max), and a hung Max fails fast until it recovers. See [docs/fork/hung-max.md](fork/hung-max.md).
- `get_bridge_status` answers while Max is hung and says what holds the main thread: this server's request, another MCP client's, or work outside the bridge. It uses a new native `health` command that the bridge answers from its pipe thread, which also reports connected clients and queued work. It was tested live, idle and during an 8 s `sleep` sent from another process. With a bridge that lacks `health`, `get_bridge_status` falls back to `ping`: it still answers in about 6 s while Max is hung, but can't tell another client's request from work outside the bridge.
- Added `capture_hang_diagnostics`, which captures where a hung Max is stuck from native thread stacks read from the OS, without sending anything to Max. It pauses each Max thread for a few milliseconds while it reads the stack, and by default saves the stacks under `%LOCALAPPDATA%\3dsmax-mcp\diagnostics\`.
- The native bridge no longer stalls Max's exit while requests are queued, and a request that timed out in the queue can no longer run later. Ported from Geddart's fork.
- The stdio server exits when its client process exits, and always after its stdin closes, so a crashed client no longer leaves a server connected to Max. `MAXMCP_PARENT_WATCHDOG=0` turns off the client-process check; the exit on stdin close stays.
- `replace_material` documents its direction (source = the material to apply, target = the one to replace) and returns `no_match` with a warning when nothing matched. It also replaces the target inside Multi/Sub and other sub-material slots (`include_sub_materials`, on by default), skips slots that would create a reference loop (`blocked` if all were skipped), and finds a source that isn't on any object: a sub-material, a Material Editor slot or a material library (`source_found_in`; `source_from` limits the search). See [docs/fork/replace-material.md](fork/replace-material.md).
- `batch_replace_materials` runs its entries in order, each on the scene the previous ones left, so `A→B, B→A` no longer swaps two materials. Go through a temporary material instead. Entries that reuse a name an earlier entry applies or removes get `depends_on_entries`, because their preview can differ from the real run.
- `execute_maxscript` no longer reports an interrupted script, for example one that calls `quitMax` or `resetMaxFile`, as a parse error. Real syntax errors include the compiler's message, and interruptions get the new code `MAXSCRIPT_INTERRUPTED`. See [docs/fork/smaller-fixes.md](fork/smaller-fixes.md).
- The curve tools accept Line objects as editable splines.
- `cosmos_import` finds what an import created by comparing handles before and after, so a package material whose name differs from the asset ("Steel Blurry" creates "Steel_Polished #0") is reported. Results add `primary_material`/`primary_map` with `primary_reason`, `asset_name`, `material_name`, `medit_slot` and a `note` when the names differ. See [docs/fork/cosmos-import.md](fork/cosmos-import.md#finding-what-the-import-created-9-2853b1c).
- A failed agent script no longer prints raw compile errors in the user's MAXScript Listener; they go to Max's log, and the caller still gets the detail. Script output such as `print` still appears. Needs the rebuilt bridge.
- `cosmos_import` no longer lets the Chaos importer overwrite the material in the user's active Material Editor slot. It points the importer at a free slot, and puts any displaced material back once Max is quiet (`medit_active_slot`, `displaced_material`, `medit_slot_restored`). See [docs/fork/cosmos-import.md](fork/cosmos-import.md#keeping-the-users-material-editor-slot-10-c1e09a7).
- `agent_viewport` reclaims its own floating viewport after a restart and Hold/Fetch, instead of failing with "All floating viewports are in use". Needs the rebuilt bridge.
- Material graphs read Autodesk Bitmap paths from Revit/ATF materials. They no longer report a hidden, unnamed parameter of Cosmos VRayBitmaps as a second texture file, which raised a false `FILE_MISSING`. `material_roles` reports file-bearing maps whose path can't be read (`FILE_PATH_UNREADABLE`, `complete: false`) and empty slots (`FILE_NOT_ASSIGNED`). See [docs/fork/autodesk-bitmap-paths.md](fork/autodesk-bitmap-paths.md).
- With upstream 1.7.5's dialog handling: a call held by a modal dialog returns `BLOCKED_BY_DIALOG` before the hang diagnosis can call Max busy or hung, and every wait, the dialog checks on the control channel included, stays bounded. `get_bridge_status` reports `bridge_state: "blocked_by_dialog"` when a dialog holds the running request, after a settling Cosmos import and before busy or hung.
- `cosmos_import` treats a call held by a dialog as lost: it stops before dispatch, or returns `state: "dialog_open"` with `pending_restore` instead of confirming the import inside the dialog's loop. Its browser action runs with quiet mode off, as verified live; max_dialogs also reports the import's interrupted calls.
- A failed `execute_maxscript` keeps `BAD_PARAM` with the compiler's message or `MAXSCRIPT_INTERRUPTED`, and now appends Max's own error text when that adds anything.
- The native changes above ship only in the rebuilt `native/bin/mcp_bridge_2026.gup`. The 2023, 2024, 2025 and 2027 bridges are still upstream's builds; for those versions, build the bridge from source ([docs/FORK.md](FORK.md#building-the-native-bridge)). [docs/FORK.md](FORK.md) lists which fixes were verified live.

## [1.7.5] — 2026-10-01

- Agents can read and answer any dialog blocking 3ds Max, Win32 or Qt. A call that opens or waits behind a dialog now returns `BLOCKED_BY_DIALOG` with the title, text and buttons instead of hanging; `max_dialogs` presses a chosen button and returns the interrupted call's result. Other tool replies warn while a dialog is open. Agents ask the user before save, overwrite, discard, Fetch, Reset or licensing choices unless told to proceed.
- MAXScript run by MCP calls now uses Max's quiet mode, so prompts take their default answer instead of stalling the call. `execute_maxscript(quiet=False)` shows them to the agent instead. MAXScript errors are returned as text rather than shown in a dialog.
- Recognized MAXScript error boxes during an MCP call are acknowledged and fail that call with `MAX_DIALOG_ERROR` and the error text. Script Controller Exception boxes are closed automatically and recorded.
- Added `script_controller`: inspect, validate and apply script controllers with typed inputs, frame sampling, token-guarded assignment and rollback.
- A queued bridge request that times out before starting no longer runs later.
- The agent skill is shorter and leaves per-tool detail to tool descriptions.
- `inspect_material_network` now reads 6 levels deep by default (up to 16), so typical wrapper chains no longer report `replicateReady: false` from depth alone. Compact output keeps `complete`. Rebuilt bridges for Max 2023-2027.
- `install.py` now updates an existing Claude Code registration instead of skipping it, keeping its environment variables such as `MCP_TOOL_PROFILE`. Registration failures show the agent CLI's error.
- `install.py` no longer replaces Claude Desktop, Cursor or Gemini settings it cannot parse (comments, invalid JSON), which previously deleted the other MCP servers in that file. It reads files saved with a BOM and writes settings atomically.
- `install.py` no longer breaks the plugin package when 3ds Max is running. It used to delete the package, then fail to copy past the loaded bridge, leaving out `mcp_server.ms` (a missing-component warning at Max startup) while reporting success. It now stops with the plugin package untouched until Max is closed, and verifies every installed file.

## [1.7.3] — 2026-09-23

- Added OpenCode client registration with preserved JSONC settings.
- Added `importFile #noPrompt` guidance to prevent import dialogs from blocking MCP calls.
- Fixed UTF-8 paths in material texture remapping.
- Added `material_roles` for texture sources across shared maps, composites and submaterials, with missing-file checks, advisory filename mismatches and paged scene scans.
- Material graph inspection now preserves every connection and reports depth/node/edge truncation.
- Added `contact_check`, a read-only pairwise check for parts that penetrate, cross, touch or float just short of contact, with depth, gap and world-space sample points.

## [1.7.2] — 2026-09-15

- Replaced `get_railclone_style_graph` with complete XML style read/write and generated-output tools for RailClone 7.3.5+.
- Added guarded, undoable RailClone style replacement and output inspection for source segments, transforms, bounds, and tags.
- Added RailClone modeling guidance for single-footprint buildings, clipping, and embedded geometry.
- Added a guarded ProBoolean fallback for Boolean operations in Max 2023, with explicit backend selection.

## [1.7.1] — 2026-09-14

- Added FStorm and FStormPBR material workflows, area lights, solar sun controls and cropped IPR capture.
- Added Windows window capture for agent viewports and FStorm, V-Ray and Corona frame buffers without desktop occlusion.
- Fixed plugin class-name aliases across discovery, lookup and material inspection.
- Fixed Phoenix detection, including false positives from Octane compatibility maps.
- Fixed FStorm palette warnings and stale light guards.
- Fixed spline tangent overshoot with bounded handles and backtracking checks.

## [1.7.0] — 2026-09-13

- Added Corona lights and environment support, plus Corona PBR materials for palette laydown, smart import and texture-folder workflows.
- Added Chaos Cosmos search, download and import tools for models, materials and HDRIs through Corona and V-Ray.
- Added `execute_python` with captured output, JSON results, tracebacks and undo rollback.
- Added Corona VFB interactive previews from the agent viewport and cropped captures across monitors.

## [1.6.8] — 2026-09-11

- Added a Windows installer with Python, dependencies, native bridges and agent skills included.
- Simplified uninstall and migration cleanup while preserving preferences and backups.
- Removed MCP Smoke from the Max UI and renamed the fallback controls to MCP Start (TCP) and MCP Stop (TCP).
- Simplified the installation instructions and added a warning to close AI clients before setup.

## [1.6.7] — 2026-09-07

### Added

- Sticky per-process Max routing with instance selection, release and startup PID
  pinning in every profile. Existing clients stay on their target when another Max
  is started or claimed; unavailable targets never silently fall back (#19).

- Renderer-agnostic lighting discovery, creation, inspection and guarded editing,
  with provider-specific emitters, output units and environment bindings.
- Plugin inspection schema v2 with exact identities, bounded queries, declared
  limits, sourced enums, linked maps and state tokens; atomic typed `plugin_patch`.

### Fixed

- Physical Material colors use the correct SDK parameter type; failed material
  parameters report errors instead of silently keeping defaults.
- Concurrent fallback viewport and identification captures use unique output paths.

- Guarded light edits distinguish actual sharing from script, undo and renderer
  bookkeeping. Corrected photometric initialization/dimensions and Octane shape IDs.
- Agent viewport framing supports light rigs; light vectors use numeric arrays
  and vertical aiming has a stable default orientation.
- Failed object parameters propagate errors instead of leaving default objects.
- Lost native responses are not replayed as new mutations or fallback constructors.
- SDK introspection distinguishes declared defaults/ranges from uninitialized
  metadata, and the new inspector discovers deferred classes through Max's registry.

## [1.6.6] — 2026-09-05

**Astra Special Release** — an agent modeling workspace with independent vision,
precise component editing and persistent curve construction.

### Added

- **AGENT VIEWPORT:** an owned floating viewport for orbiting, framing, projection,
  visual targeting and captures while the user keeps their working view. Its title
  asks users not to close or minimize it while the agent is working.
- `create_mesh`, `inspect_mesh`, `mesh_edit` and `pick_component` for editable quad
  cages, labeled component inspection, image-to-geometry targeting and guarded,
  undoable vertex/edge/face edits that preserve modifiers.
- `curve_model` for named curves, local construction planes, tangent arcs, rounded
  profiles, sweeps and resampled quad lofts. Numeric controls and source recipes
  persist in the `.max` file and support guarded parameter updates.
- `inspect_curve` and `edit_curve` for world-space knots and Bezier handles,
  labeled captures, visual picking and atomic topology edits with stale-token checks.
- `loft_mesh` for parameterized matched-section quad lofts, plus `geometry_qa` for
  evaluated mesh boundaries, winding conflicts, degeneracy and connected components.
- Basic V-Ray preview controls through the agent viewport, with viewport/VFB capture,
  cropped screen capture and a separate render-cancellation channel. (This might change)

### Fixed

- Spline construction and edits dispatch on the scene node in local coordinates
  and refresh through `updateShape`, preserving modifiers and correct world-space
  targeting under rotation, nonuniform scale and object offsets.

### Changed

- Full remains the default MCP profile. Full, core and progressive discovery expose
  the new modeling tools; native diagnostic schemas exclude Python-only orchestration.
- The portable usage skill includes curve recipes and inspect/edit/verify workflows.

### Removed

- `build_floor_plan` tool and its progressive discovery toolset.
- Standalone chat

### Additional Notes

- Keep the agent viewport visible for Nitrous captures as it cannot update when it's minimized. Exact surface-intersection/thickness checks and an assembly-wide parameter graph
  are not yet added. Curve QA uses sampled geometry. ActiveShade/V-Ray IPR is experimental.

## [1.5.5] — 2026-08-31

Progressive tool discovery, atomic scene operations, and expanded native animation tooling.

### Added

- **Progressive MCP profile.** Advertises only `list_toolsets`, `describe_toolset`, and `call_tool`, then loads exact operational schemas on demand. The installer now offers progressive, full, and core profiles and persists the choice in user configuration.
- Native canonical NodeRefs and `scene_patch` for preflighted rename, relative-transform, flag, and parenting operations committed as one undo step with mutation-only stale-scene guards.
- Deterministic, non-mesh `scene_qa` checks and narrowly scoped naming repairs.
- Native keyframe timeline, delete/move/scale, bake/resample, tangent-normalization, hierarchy-aware match, and loop operations.

### Changed

- Scene journaling separates persistent mutation sequence changes from selection activity, so normal viewport interaction does not invalidate guarded scene patches.
- Native mutation dispatch defers nested work arriving during an active main-thread item, and node flag edits now have explicit undo/redo restore records.
- Generated tool registries, smoke cases, documentation, and the bundled agent skill include the new discovery, scene, and animation surfaces.

## [1.5.1] — 2026-08-04

Packaging fixes and Chinese docs.

### Fixed

- `mcp[cli]` now pinned `<2.0.0`. mcp 2.0 removed `mcp.server.fastmcp` (it is `mcp.server.mcpserver` now), so any resolve that skipped `uv.lock` — `pip install`, a fresh `uv pip install` — pulled 2.x and crashed on import.
- The package directory is renamed `src/` → `maxmcp/`, so the wheel no longer installs a top-level package called `src` — that name collides with any other distribution shipping the same layout, and made the import surface hostage to whatever `src/` happened to be on `sys.path`. Imports inside the package are relative; imports elsewhere (`tests/`, `scripts/`) are now `maxmcp.*`. Console script entry point is `maxmcp.server:main`. Distribution name on PyPI is unchanged (`3dsmax-mcp`); `3dsmax_mcp` is not a legal Python identifier, hence `maxmcp` as the import name.
- Tool-profile counts in ADVANCED.md corrected to 151 full / 87 core (were 114/77), and the specialty module list now includes `mcg`, `render_automations`, and the four `tyflow_*` modules.

### Added

- **Installable from PyPI.** The wheel now carries the whole Max-side payload — the five per-year `.gup` binaries, `mcp_server.ms`, the PackageContents template, `mcp_config.ini`, `.env.example` and the agent skill — plus a `3dsmax-mcp-install` console script, so `pip install 3dsmax-mcp` followed by `3dsmax-mcp-install` is a complete install with no checkout. Assets are shipped at the same relative paths the repo uses, so `install.py` runs unmodified either way (`ROOT` is the repo root from a checkout, the package directory from a wheel); only MCP client registration differs, using the console script's absolute path where there is no repo to point `uv run --directory` at. Matters most for users behind slow GitHub access, who can now install via a domestic PyPI mirror.
- `README.zh-CN.md` — Chinese documentation written for the domestic stack: Cline + DeepSeek as the primary client config (Qwen/GLM via OpenAI-compatible endpoints), archviz and MMD/animation walkthroughs, and TUNA/Aliyun pip mirrors for installs behind slow GitHub access.

## [1.5.0] — 2026-08-01

Modeling tools, faster native inspection/material workflows, and a new install format.

### Added

- `boolean_operation`: Boolean modifier (BooleanMod) workflows — apply union/subtract/intersect/merge/attach/insert/split operands (imprint/cookie options, mesh/OpenVDB method, live references), list/retune/rename/disable operands, remove or extract them. Non-live operands are consumed and keep their node names in the operand list. Extract handles the Modify-panel context requirement internally.
- `boolean_operation action=apply`: inline `cutters` — scratch primitives defined in-call ({name, shape: box|cylinder|sphere, size, pos (bbox center), rot, operation?}), created, named, and consumed atomically; failed appends are deleted on the spot, so no scene litter on any path. `repeat` {count, axis, spacing} arrays every cutter along an axis (`vent_1..N` naming) for vents, ribs, and window grids. `operands` is optional when `cutters` is given.
- `draw_spline`: spline authoring from world-space points (corner/smooth/bezier knots, closed loops, multi-spline holes via add_spline), knot readback with length-uniform samples, knot edits (bezier handles dragged along by default), insert/delete knots, renderable thickness.
- `edit_vertices`: Editable_Poly vertex reads (index/bbox/radius filters), moves with soft (1-d/r)² falloff, explicit sets, and conform — pull verts onto a spline (axis-masked, e.g. fit the xz silhouette while preserving width) or ray-project onto geometry. World-space; edits the poly base beneath live modifiers.

### Changed

- Install format: ApplicationPlugins bundle at `%ProgramData%\Autodesk\ApplicationPlugins\3dsmax-mcp\` (PackageContents.xml + per-year GUPs + `mcp_server.ms` as post-start-up script) replaces copying into the Max install directory. `python install.py` migrates automatically — it removes old-format files from every detected Max install before deploying, and elevates when ProgramData or legacy paths need admin rights. One bundle serves Max 2023–2027; the per-version prompt is gone. (PR #17, hardened: uninstall now removes the bundle, elevation fallbacks, staged bundle outputs gitignored.)
- `native/build.bat` finds the SDK via `ADSK_3DSMAX_SDK_<year>` (falls back to the default SDK path) and stages GUPs into `bundle/Contents/bin/`; `deploy` now defers to `install.py`. New `scripts/stage_bundle.py` + `ADSK_APPLICATION_PLUGINS` for testing a staged bundle without installing.
- `get_plugin_capabilities`, `get_material_library`, `backup_material_library`, `isolate_and_capture_selected`, and existing-material `create_shell_material` use native SDK routes when available, with compatibility fallback for older bridges.
- New `DictValue` coerced type (stringified JSON objects absorbed, same spirit as the list coercions).
- `build_skill.py` bundles `tyflow-graphs.md` into the deployed skill and rewrites its AGENTS.md link.

### Fixed

- `assign_material` access violation (0xC0000005) on a non-material `material_class`. The native handler's class lookup fell back to an unfiltered `FindClassDescByName` across every superclass and blind-cast the result to `Mtl*`; `material_class="Physical"` therefore instantiated the Physical *Camera* and dispatched `SetName`/`SetMtl`/redraw through the wrong vtable. The lookup now stays inside `MATERIAL_CLASS_ID` and raises a structured `BAD_PARAM` with `hint.didYouMean` (`Physical` → `PhysicalMaterial`). Requires a native redeploy; the same unguarded pattern remains in the modifier and object handlers.
- Native compatibility fixes cover Max 2023–2027 SDK differences in scene-node filtering, material-library paths, class enumeration, reference cloning, class labels, persistent object ownership, and GDI+ capture lifetime.

### Removed

- `maxscript/startup/mcp_autostart.ms` — the bundle manifest starts `mcp_server.ms` directly.

## [1.3.1] — 2026-07-20

### Fixed

- Tool replies never inline base64: captures always return the saved file path (`return_image` is deprecated and ignored), and the envelope spills any image or oversized binary payload to `%TEMP%\3dsmax-mcp\payloads` as an `image_file`/`bytes_file` reference.

## [1.3.0] — 2026-07-19

Agentic Max Creation Graph authoring, robust Data Channel control, and portable MCG references.

### Added

- End-to-end MCG tools for context discovery, graph/operator search, temporary graph creation, structured patching, compilation, instance inspection, modifier application, testing, checkpoint restore, and workspace cleanup.
- A bounded MCG iteration transaction with expected-hash concurrency checks, safety checkpoints, compile diagnostics, disposable semantic verification, automatic rollback, and compact proof-of-change.
- Native MCG handlers for deterministic Viper validation/compilation, exact generated-class resolution, safe modifier application, typed parameter assignment, instance readback, and UI error text capture.
- A read-only Autodesk 3ds Max 2017 MCG reference corpus containing 43 `.maxtool` and 282 `.maxcompound` XML graphs, pinned to its MIT-licensed upstream commit and shipped without scenes or packages.
- Data Channel operator discovery, preset discovery/loading, modifier inspection, operator editing, and validated stack management.

### Changed

- Data Channel builds now use the live Max operator catalog, explicit Replace mode for the first Input operator, complete reorder validation, and UI error readback instead of version-fragile hardcoded IDs.
- MCG graphs compile and verify only from process-scoped temporary workspaces; installed graphs and bundled samples remain read-only fork sources.
- MCG executable surfaces and impure operators fail closed under safe mode, while graph UUID/version identity, source ports, named destination ports, and generated plug-in classes are preserved or resolved explicitly.
- Procedural graph guidance moved into a dedicated skill reference so the normal 3ds Max MCP prompt remains compact.

### Fixed

- Native bridge error codes survive Python wrapper failures instead of degrading to generic `BAD_PARAM` responses.
- MCG compilation returns actual Viper diagnostics and generated wrapper details instead of relying on undocumented or nonexistent reload-message APIs.
- Data Channel's first operator no longer remains in the invalid unset blend mode that causes `First operator must be an Input Operator and in Replace`.

## [1.2.0] — 2026-07-09

Structured tool envelopes, centralized error hints, atomic undo, and handle addressing.

### Changed

- MCP tools return a structured `ToolEnvelope` object (`ok`/`result`/`error`/`hint`) with an advertised output schema, replacing JSON-string tripbacks.
- Errors carry a closed `code` enum (`NOT_FOUND`, `AMBIGUOUS`, `PLUGIN_MISSING`, `BRIDGE_DOWN`, `RENDER_BUSY`, `SAFE_MODE`, `BAD_PARAM`) plus `retryable`; native structured errors propagate instead of falling back to MAXScript.
- Mutating native handlers run inside a `theHold` transaction: one MCP call = one undo step, and mid-operation failures roll back atomically.
- Mutating tools return compact post-state proof (new transform/bbox, modifier stack order, resolved material class) so agents don't need a verify round trip.
- MAXScript intent-suggestion rules moved to `src/helpers/error_hints.py` and now apply at the envelope layer, so exceptions from `execute_maxscript` get intent hints too.
- Tool docstrings gained "Use when / Not when" guidance to steer agents toward dedicated tools.

### Added

- `undo_last` — reverts the previous MCP-initiated scene change.
- Anim-handle addressing: tripbacks include `handle`, node tools accept name or handle, and ambiguous names return candidate lists (handle + class + layer) in the hint.
- NodeEvent scene journal in the native bridge; `query_scene(action=delta)` reads seq-numbered changes and answers `unchanged_since` cheaply instead of rescanning.
- MCP tool annotations (`readOnlyHint`/`destructiveHint`/`idempotentHint`), `dry_run` on destructive tools, and explicit scene units in `get_session_context`.
- Auto-resolved error hints when the tool authored none: not-found → `query_scene`, safe-mode → don't retry, bridge/pipe failures → `get_bridge_status`. Tool-authored hints always win.
- Hint normalization: string, plural `hints`, and list hints coerce to a canonical `{message, suggested_tools, next}` shape.
- `scripts/benchmark_agent_ergonomics.py` — measures round trips and approximate tokens per canonical task against a live Max.

## [1.1.0] — 2026-07-06

Render automation (done-signal) and material-library tooling.

### Added

- `render_automations` — arms a render done-signal at 3ds Max's `NOTIFY_POST_RENDER` and reports completion (with the real `frames_rendered` count) through an event-driven file watcher (`scripts/render_signal_wait.ps1`); no polling, never blocks the bridge. Includes `cancel` to abort a render in flight from the pipe thread.
- Native `render_start` / `render_cancel` handlers and an always-on render-completion pinger.
- `get_material_library` — inspects the volatile material scratchpads (`currentMaterialLibrary` and the Compact Material Editor slots) that aren't saved with the scene, and warns when the current library has no backing `.mat` file.
- `backup_material_library` — saves those scratchpads to timestamped `.mat` files without touching the scene.

### Changed

- The bridge is a render *listener*, not a trigger: `render_automations(start)` only arms the done-signal, and the render is fired externally (Render button, or `max quick render` via `execute_maxscript`). Launching the render from inside the bridge caused 3ds Max to auto-start a second render on completion and loop; keeping the trigger outside the bridge avoids it.
- `execute_maxscript` suggests the material-library tools when raw MAXScript touches the material library.
- `SKILL.md` trimmed to Max-usage gotchas only.

## [1.0.6] — 2026-06-24

Keyframing and installer release draft with the package version bumped to `1.0.6`.

### Added

- `keyframe_tracks` for native key inspection, setting, endpoint matching, loop closure, tangent styling, and out-of-range behavior edits.
- Compact, budgeted keyframe summaries for baked animation and mocap-heavy controllers.
- Keyed `value` and `move` writes so animation edits can avoid `transform_object` offset side effects.

### Changed

- Keyframe result counters distinguish logical track edits from raw sub-controller edits.
- Installer discovery covers classic and Microsoft Store Claude Desktop config paths.

### Fixed

- Composite Position/Euler/Scale controllers now create and style explicit-frame keys through their child tracks.
- Keyframe styling reports stable candidate counts on first set-and-style calls.
- Track-path matching is exact so narrow keyframe edits do not leak into similarly named tracks.

## [1.0.5] — 2026-06-01

Material-network release draft with the package version bumped to `1.0.5`.

### Added

- `inspect_material_network` for native semantic material graph reads: wired slots, nested maps, file manifests, health issues, and compact mode.
- `replicate_material` for preview-first graph cloning, texture remapping, verification, and explicit apply.
- Material-network tool catalog, smoke input coverage, in-Max chat registry entries, and user-facing docs/spec.

### Changed

- Viewport capture tools now return saved-file metadata by default, with `return_image=true` preserving inline image behavior.
- Native viewport captures accept max dimensions and report source/final image sizes.
- Tool surface is streamlined around `inspect_properties(target="modifier")`; `inspect_modifier_properties` remains as a compatibility alias but is hidden from the playground and default smoke pass.
- `get_object_properties` and `inspect_object` descriptions now distinguish compact readback from deep exploratory inspection.

### Fixed

- Material replication now blocks missing source textures and non-texture graph dependencies unless explicitly allowed.
- OSL shader source is no longer misclassified as a remappable file path; OSL file paths remain visible in inspection output.
- Wired material slots are deduplicated in graph inspection output.

## [1.0.0] — 2026-05-28

First stable release. Production-ready MCP bridge for 3ds Max 2023–2027 with prebuilt native plugins shipped in the repo.

> **Patched in place (2026-05-28)** — binaries rebuilt, no version bump: fixed a native main-thread deadlock in the **MCP Smoke** macro, a `palette_laydown` crash on flat texture folders, invalid spatial JSON from `create_object` on TCP transport, Forest Pack dropping zero-footprint items, unescaped names in `query_scene` MAXScript fallbacks, and `query_scene(delta)` mis-tracking duplicate-named nodes.

### Highlights

- **114 MCP tools** (77 in `core` profile) — scene reads, objects, modifiers, materials, controllers, viewport capture, plugin introspection, and specialty modules (tyFlow, Forest Pack, RailClone, Data Channel, and more).
- **Native bridge** — named-pipe transport by default; prebuilt `mcp_bridge_20XX.gup` for Max 2023–2027 in `native/bin/`.
- **`query_scene`** — unified scene reads (`overview`, `filter`, `class`, `property`, `selection`, `delta`) replacing scattered snapshot tools.
- **`smart_import`** — folder-aware mesh + PBR import (per-subfolder asset packs, shared atlases, multi-variant bundles).
- **`create_shell_material`** — dual render/export pipeline wrapper for any two materials or texture-built PBR pair (OpenPBR, Physical, Arnold, Octane, etc.).
- **`scatter_forest_pack`** — per-geometry footprint sizing for multi-variant Forest Pack scatters.
- **Spatial placement** — `create_object` / `clone_objects` ground-contact placement with rich tripback (`bbox`, `placement`, `groundContact`).
- **Tool Inspector** — `Tool Playground.bat` GUI for manual one-tool-at-a-time testing with full tripback.
- **Live smoke harness** — `run_tool_smoke`, `run_live_tool_smoke.py`, in-Max **MCP Smoke** macro.
- **Multi-Max** — **MCP Claim This Max** routes clients to the correct instance.
- **Agent skill** — bundled Max usage guide + MAXScript reference files; installer deploys automatically.
- **Docs** — user-facing README + [Advanced configuration](ADVANCED.md).

### Breaking changes (from 0.8.x)

- **`create_shell_material`** — new API: wrap existing materials by name or build from `texture_folder` + `render_material_class` / `export_material_class`. Old UberBitmap-only parameters removed.
- **Scene reads** — prefer `query_scene(action=…)` over removed `get_scene_snapshot` / legacy snapshot modules.
- **Standalone in-Max chat** — still experimental (WIP); external MCP recommended for production.

### Install / upgrade

```powershell
git pull
uv sync
uv run python install.py
```

Restart 3ds Max after install.

---

## [0.8.5] and earlier

Pre-1.0 development releases. See git history for details.
