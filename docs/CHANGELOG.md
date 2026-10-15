# Changelog

All notable changes to this project are documented here.

## [Unreleased] (fork)

- Merged upstream 1.7.6 (`5a92193`, 2026-10-14) in `298a295`: the fork is now upstream 1.7.6 plus the changes below, no longer 1.7.5 (`29aed53`). Upstream now has its own versions of several fork fixes; the merge started from upstream's code and re-applied what only the fork does. Now upstream's: Line objects in the curve tools (the fork's code dropped); a `contact_check` time budget, skip reasons and strict names (the fork's millimetre thresholds, work budget, 45 s cap and `unchecked_pairs` kept); pipe I/O cancelled on every pipe instance when Max exits (covers #29) and queued work failed at once (`StopAccepting`, formerly the fork's `BeginShutdown`); a reply deadline on every request (the fork's rule stays the main one: wait while Max is busy, give up when it exited or is confirmed hung; upstream's release after about 130 s applies only when Max's PID can't be found); MAXScript-backed reads on Max's main thread; and `replace_material`'s sub-material slots, Material Editor sources and in-order batches (the fork adds `include_sub_materials`, `source_from` including the scene materials and the material library, a preview with a missing source, and the direction and `blocked` hints). Behaviour changes: strict single-node names everywhere (an ambiguous name fails and lists the handles; an exact-case match first, then any case); `contact_check` adds `skipped` and `pairs_measured` (`skipped_nodes` kept); `replace_material` results follow upstream (`replaced_count` = objects + slots, `reassigned_count` = objects; skip reasons `already_source`, `parent_is_source`, `would_create_loop`, `set_rejected` and `readback_mismatch` replace `source_contains_parent`, `reference_loop` and `set_failed`); `MAX_BUSY` has two meanings (the fork's: nothing sent, retryable; upstream's deadline: sent, `request_sent: true`, not retryable); `health` no longer lists nested requests (`nested` is always 0); work refused while Max exits fails with upstream's "3ds Max is exiting; the request did not run" (`BRIDGE_DOWN`). The native bridge is upstream's permanent shell plus hot-reloadable core; the fork's `health` and Forest runtime primitives live in the shell. Upstream's new `pulze_inspect`, `pulze_preflight`, `pulze_set`, `pulze_restore` and `reload_native_core` are in, and its Tool Playground is gone. Bridges: 2023-2026 in `native/bin/` are upstream's 1.7.6 builds (2026: `f62c6b85…`); the fork's 1.7.5-based 2026 bridge (`d5cd6ba5…`) was dropped because it can't be rebuilt here and 1.7.6's Python expects 1.7.6's native commands, so Max 2026 now gets the fork's Python fixes only, like 2023-2025. 2027 is the merge's own build (hash in `native/bin/bridge_manifest.json`). Unit tests (1119 passed, 5 skipped), the SDK-independent native tests (6/6) and a clean 2027 build pass; live checks on Max 2027 are pending. See [docs/FORK.md](FORK.md#what-upstream-176-now-covers).

### Verified deployments, 2026-10-12–13

- Diagnostic helpers now match zero-padded PIDs in Max.log and the lifecycle observer confirms the exact PID has disappeared before logging exit. It drains completed exits on stop and closes retained process handles. Mock observer checks and the saved padded-PID capture passed offline; no Max process was touched.
- New `forest_instances`: turns a Forest Pack scatter into real scene nodes (instance, copy or reference) at the exact item transforms, in one undo step, like Forest Tools' Instantiate button, which has no MAXScript. Reads and creates in the same call; skips billboards and missing sources; refuses Reference-mode forests and more than `max_items`, building with the Max Items Viewport cap temporarily raised so a thinned build can't yield a partial set; `count` and `delete` find exactly its own nodes. Unit-tested; tested live on throwaway Max 2027 (Forest Pack 9.4.3): instances sit exactly at the `forest_items` transforms, are unparented, ungrouped, visible and on their layer; a second run gets a new prefix; `ITEM_LIMIT` creates nothing; delete removes exactly the tagged nodes; `disable_source` works; a Checker / Multi/Sub source works and Forest Color is reported through it; with `vmaxitems` below the item count every item is created and the cap is restored, also when `ITEM_LIMIT` refuses. Confirmed live by the MCP issue fixes session on the 1.7.5-based fork (36/36 checks, 2026-10-14); a rerun on the 1.7.6 merge is pending.
- Forest Effects curve points (#44): `forest_effect_curves(action="set_points")` edits one curve parameter's points on a staged copy in one undo step, through the new native `mcpForestSetCurvePoints` (needs the updated 2027 bridge). `.eff` load accepts curve points and export writes them, as an MCP-specific `<curves><curve><point …/>` child (the XML Forest writes into `efpacurve` in scenes; Forest's own `.eff` loader is untested with it). Animated curves are refused. Verified live on Max 2027 (2026-10-14, bridge `04815449…`): a changed curve evaluates at once, bezier tangents are relative to their point, bezier corners persist, save/reload and export→load keep the points, every refusal leaves the curve and build unchanged. Not tested: undo (max undo isn't run through the MCP), the animated-curve refusal, and Forest's own `.eff` loader.
- Forest Effects numeric parameters: `forest_effect_parameters` provides guarded inspection and edits; `.eff` import initializes numeric parameters and export stores their current values. Live checks cover integer, real, percentage and unit types. The MCP reports `compile_verified: false`; reference bindings are supported by the later update below, while reference serialization and controller, texture and curve parameters remain unsupported.
- Forest Effects reference parameters now inspect, bind, rebind and clear existing node/spline references by guarded identity. Dependency cycles, ambiguous/stale nodes, wrong classes and shared/custom bases are refused. Live checks changed spline distances and object Z values, restoring the saved identity afterward. Binding serialization and controller/texture/curve edits remain unsupported.
- Forest Effects `.eff` files now round-trip mixed numeric, node and spline definitions when callers provide explicit bindings for import. Export requires opt-in and lists bindings required by a later import. UUID keys normalize consistently; repeated imports get distinct IDs. Scratch-Max round trips compare complete transform sets because procedural item indices reorder after rebuild.

- Forest Effects `move`, `load` and `export` (`416ef90`) are deployed. Fresh installed stdio enumeration reports 207 tools and the new actions; normalized source matches the installed module. Twelve scratch-Max checks passed, including effect ordering and `.eff` roundtrip with exact-PID guards. Post-deployment Python suite: 1,064 passed. The later numeric extension below supports typed numeric parameters; other parameter types remain unsupported and stored effect code is not compile-checked.

- #29–#30: cancellable connected-pipe shutdown and native-aware startup failures, including unsent TCP connection timeouts. The reviewed 2027 bridge is SHA256 `3bd55e31d1088cbccac2ea90e6ca1a42b29d75f7c0e797b709240c94b5a8b6cf`, built from `205d467`. Native checks passed 6/6; scratch Max exited with connected clients retained (4.759 s and 4.092 s).
- #31: conservative exited-PID registry cleanup. Live validation removed 14 confirmed dead registrations while retaining two live registrations; exact-PID pings and 8 cleanup tests passed.
- #32–#34: reviewed bridge manifest, installer replacement guards, reliable scratch-driver error reporting and conservative teardown classification. Installed-package checks and 59 focused tests passed; saved-capture replay is offline evidence. The rebuilt Setup compiled successfully but has not been installed or published; older installers remain capable of replacing a bridge.
- Deployed iToo API tools add guarded inventory, slots, metadata, proxies, instances, settings and Forest Effects inspection/editing. See [coverage and live verification](fork/itoo-api-coverage.md).
- Publication uses `fix/fork-fixes` and one combined upstream pull request. Superseded remote branches are retired after preserving their history. Verification does not imply every tool has live coverage.

## Fork changes (3dcrgr/3dsmax-mcp, on top of 1.7.6; on top of 1.7.5 until 2026-10-15)

- `agent_viewport`: `vray_ipr` and `vray_vfb` work with V-Ray GPU (#18). They no longer require `ipr_progressiveMode`, which only V-Ray CPU has. On GPU, whose IPR is always progressive, only the denoiser is leased. Needs the fork's 2027 bridge (`6397f1f` or later); verified live on Max 2027 with V-Ray GPU 7. Upstream 1.7.6 starts these previews on V-Ray GPU too; the 1.7.6 merge ported the fork's code onto upstream's.
- Every tool except the four routing tools takes an optional `expected_pid` (#19). Each bridge call the tool makes is refused with `TARGET_MISMATCH`, nothing sent, unless it reaches that 3ds Max. `selection_generation` in every transport changes on each select, release or re-resolve; the selection belongs to the MCP server process, which sessions may share.
- A target the server picked by itself (`default` or `claim`) re-resolves once its 3ds Max has exited, and the reply warns (#22). It does so only when the new default is unambiguous; otherwise it raises `AmbiguousMaxInstanceError` with nothing sent. Explicit and environment targets still fail until you select or release.
- Bridges, before the 1.7.6 merge (for the current ones see the merge entry above): of the prebuilt bridges in `native/bin/`, `mcp_bridge_2026.gup` (sha256 `d5cd6ba5…`) and `mcp_bridge_2027.gup` (sha256 `04815449…`, built from `538a241` with the 3ds Max 2027 SDK) were fork builds. The 2027 one has the V-Ray GPU preview fix below, which the 2026 one lacked. A 2027 fork bridge has run in production since 2026-10-11; with it, `health`, `max_dialogs`, the compiler message on a syntax error and `material_roles` VRayBitmap paths were checked live on Max 2027, read-only. The 2023, 2024 and 2025 ones were upstream's 1.7.5 builds: with those, the Python-side changes work but the ones marked "needs the fork's bridge" don't. To build a fork bridge, see [docs/FORK.md](FORK.md#building-the-native-bridge) (MSVC 14.38; CMake sets C++20 for 2027). [docs/FORK.md](FORK.md) says what else was verified live, and on which Max.
- Added `vantage_status`, which reads Chaos Vantage's state (ready, Live Link state, offline render progress, version) over its local command port without touching Max, and `vantage_live_link`, which starts or stops V-Ray GPU Live Link from Max. It refuses rather than switching renderers or launching Vantage, and stops only the link this MCP server started unless `force=true`. Vantage's command port isn't a public interface. `vantage_get_image` saves the frame Vantage is showing as a PNG, also without touching Max; each call leaks process handles inside Vantage 3.4 (often dozens), so it isn't for polling. Only `vantage_status` is tested live (Vantage 3.4.0, on 2026-10-06, with Max 2026). See [docs/fork/vantage.md](fork/vantage.md).
- Added `resting_check`, which finds props floating above or sunk into the surface below them (gaps in millimetres, the support each rests on, within a time budget), and `drop_to_surface`, which drops floating ones straight down onto it. `drop_to_surface` is a dry run by default; applied, it moves the named items in one undo step and cancels the step if a re-measure finds any off its planned gap. It never moves frozen, Z-locked, Revit-linked, xref, constrained or animated nodes, and lifts sunk items only with `lift_penetrating=true`. Tested live on Max 2026 in a scratch scene. See [docs/fork/resting-check.md](fork/resting-check.md).
- RailClone: `get_railclone_style` and `set_railclone_style` accept the repeated attributes RailClone 7.x writes (`y="..."` twice), which made 7 of 43 stock styles fail. `get_railclone_output` rebuilds first (moved or copied RailClones returned stale positions), flags placeholder items and adds `node_tm` and world bounds. New: `railclone_inspect`, `railclone_set_param`, `railclone_library` and `railclone_from_library` (a `.rail` item loads in one undo step; a `.max` item clears Max's undo history, so it needs `allow_undo_flush=true` and then loads with undo off, with no copy and nothing deleted; `remove_with` lists the nodes to delete to remove it). `railclone_from_library` checks routes against the library roots 3ds Max's own registry lists, since the server's registry view can be a stale copy, and its merges never replace a scene material of the same name. Tested live on Max 2026 (RailClone 7.1.1) and on Max 2027 (RailClone 7.3.5, 2026-10-11), including the undo-off `.max` load; undo itself is safe by construction and not run live. See [docs/fork/railclone.md](fork/railclone.md).
- Forest Pack: `scatter_forest_pack` now sets density through the tile size (`density` scales it; new `items_per_m2`), writes `direction` 100 for "up" (it wrote 1), and returns the real item count, failing with `ZERO_ITEMS` for an empty scatter. New tools: `forest_items` (instance-ready item transforms), `forest_edit_items` and `forest_set_mode` (custom edit mode), `forest_library` (every kind by default, with `placeable` per item) and `forest_from_library` (presets that scatter after retargeting), and `forest_validate` (list checks, conservative `fix`). Merging a library file clears 3ds Max's undo history, so `forest_from_library` needs `allow_undo_flush=true` and isn't undoable: it returns `remove_with`, the nodes to delete instead. Forest Pack doesn't record item edits in undo, so `forest_edit_items` returns `revert`, an edit list that puts the items back; its `rotation_z` is the final angle. Tested live on Max 2026 (Forest Pack 9.0) and on Max 2027 (Forest Pack 9.4.3, 2026-10-11), including the undo-off placement; undo itself is safe by construction and not run live. See [docs/fork/forest-pack.md](fork/forest-pack.md).
- RailClone and Forest Pack fixes from the iToo docs review, each probed live first (Max 2027, RailClone 7.3.5, Forest Pack 9.4.3; issues #23 and #24). RailClone: the read tools, `railclone_set_param` and `railclone_from_library` rebuild with `seed = seed` instead of `segmentsUpdate`, which renamed every linked segment to its source node; `reload_segments=true` on `railclone_inspect` and `get_railclone_output` still runs it. With `autoUpdate` off a rebuild switches it on for the read and back off (no scripted call rebuilt before, so reads were stale). `railclone_from_library` gives `splines` to path bases and the new `clipping_splines` to clipping-area bases, by each base's role in the style graph (a second spline used to become a window's clipping area), and refuses a base whose role can't be told (`ROLE_UNKNOWN`); `railclone_inspect` reports base roles. The role walk reads an operator's `<groupConnectors>` inputs too, which 12 stock `.rail` items (Glass Railing 03, the Industrial pipes and ducts) use for their only path. `patype` 1 is reported as float, and 6 (string) is read but not written. Forest Pack: `scatter_forest_pack` and `forest_validate` report when the viewport Max Items limit thins `count` (`VIEWPORT_LIMIT`, `estimated_viewport_count`; the "saturation" at 35800 of 143700) and when `maxdensity`, a cap on candidate cells, gives 0 items (`MAXDENSITY_CAP`); the estimate honours `lock_ratio` and gap items. `scatter_forest_pack` takes `direction` from -100 (down) to 100 (up) and reads its distribution settings back (`DIRECTION_NOT_APPLIED`, `NON_DEFAULT_DISTRIBUTION`). `forest_validate` reports `geomlist` 0 as a gap note, not an error, and warns about an exclude listed before an include it should cut. Both libraries resolve `index.xml` paths with a leading backslash against the index folder and find thumbnails in `thumbnails\`. Review fixes on top: the read tools leave a RailClone with `autoUpdate` off alone and return its last build unless `force_rebuild=true` (`railclone_inspect` also takes `rebuild`), and `railclone_inspect(reload_segments=true)` reads the segments after the rebuild, so a renamed linked segment keeps its item count. `forest_validate(fix=true)` reports a `geomlist` 0 it padded itself as `SOURCE_UNRESOLVED` (an error); `MAXDENSITY_CAP` needs a count of 0, and `viewport_thinned` needs a count that agrees with the predicted thinning (else `VIEWPORT_LIMIT_UNCERTAIN`); `scatter_forest_pack` turns `lock_ratio` off for unequal units, so `density_units_y_cm` applies. The plugin guide names `clipping_splines` and no longer says `maxdensity` does nothing. See [docs/fork/railclone.md](fork/railclone.md#fixes-from-the-itoo-docs-review-issue-23-2026-10-11) and [docs/fork/forest-pack.md](fork/forest-pack.md#fixes-from-the-itoo-docs-review-issue-24-2026-10-11).
- Added `itoo_vantage_check` (#25), a read-only check of how ready Forest Pack, RailClone and ForestIvy objects are for Chaos Vantage. It reports what Forest/RailClone Color slot 1 holds (Vantage bypasses those maps and renders slot 1), opacity maps, CPU-only features (Forest Material, billboards, Edge mode on proxies, ForestIvy instancing, XShadow), ID-driven maps under `forest_automat_*`, RailClone render settings, modifiers, proxy cache and `sobjmtl` wrapping, and item, segment and face caps. Each finding gives a severity and its basis (docs, live test, inference or the user's Vantage logs), and objects with a feature Vantage may not translate get a fallback hint (Instantiate / Forest Tools). It never rebuilds, writes, selects, renders or exports: RailClone counts come from its existing export and Forest counts from `trees.count()` as it is, which the viewport Max Items limit can thin; what can't be read without a write is listed as unknown. Tested live, read-only, on Max 2027 (2026-10-11). See [docs/fork/itoo-vantage-check.md](fork/itoo-vantage-check.md).
- RailClone versions and RailClone-linked Forests, from iToo's forum release notes, each probed live first (Max 2027, RailClone 7.3.5, Forest Pack 9.4.3; issues #26 and #27). `railclone_inspect` and `railclone_from_library` report the running RailClone (`rc_version` "7.3.5", `rc_version_int` 735, read from a new instance's `createdversion`), the object's `createdversion` and `savedversion`, and `proxymode`; `RailClone_Pro.version()` (400 on 7.1.1 and 7.3.5 alike) is now `api_version`, an API number, not `plugin_version`. New warnings: `RC_SELECTOR_BUG_VERSION` (RailClone older than 7.3.1 and selectors in the style), `RC_LIBRARY_MAPPING_BUG_VERSION` (older than 7.1.3, on a library load), `SELECTOR_INDEX_MISMATCH` (a selector's option index and value disagree; the engine builds with the value) and `STYLE_MAY_PREDATE_MAPPING_FIX` (a library style on an object created or saved before 7.1.3). Under Proxy Cache the output names no segment, so the RailClone tools no longer count every item as a placeholder or report `EMPTY_OUTPUT`: they warn `PROXY_CACHE` and leave the counts by segment unknown. `forest_validate` no longer reports the area checks as errors on a Reference-mode forest (`NO_ACTIVE_AREA` failed every one); it reads each linked RailClone without a rebuild and names why such a forest is empty (`REF_NO_NODES`, `RC_LINK_NO_EXPORT`, `RC_LINK_PROXY_CACHE`, `RC_LINK_DISABLED`, `RC_LINK_EMPTY`, `RC_LINK_UNSUPPORTED`, listed in `NO_ITEMS`), and warns `RC_LINK_STALE`, `RC_LINK_OFF`, `FPGEOM_IGNORED` and `FPGEOM_UNMATCHED`. `forest_items` returns a read-only `reference` field for such forests. The `reload_segments` docs now say it renames linked segments after their sources, has no geometry effect on RailClone 7.3.5, and that `force_rebuild` picks up source edits. See [docs/fork/railclone.md](fork/railclone.md#versions-library-load-bugs-and-proxy-cache-issue-26-2026-10-11) and [docs/fork/forest-pack.md](fork/forest-pack.md#reference-mode-and-railclone-links-issue-27-2026-10-11).
- Scripts that mention `resetMaxFile`, `loadMaxFile`, `fetchMaxFile`, `quitMax`, `checkForSave`, `max reset file`, `max file new`, `max file open`, `max fetch` or the Interface's `FileReset`, `FileFetch` or `LoadFromFile` no longer run in Max's quiet mode by default, so a "save changes?" prompt reaches the agent as `BLOCKED_BY_DIALOG` instead of discarding unsaved work. That default needs the fork's bridge: with a stock bridge, such a script sent without `quiet` still runs quiet. With any bridge, `execute_maxscript(quiet=True)` on such a script forces quiet mode only together with `allow_discard=True`; `quiet=True` alone is sent as `quiet=False`. Replies say when quiet mode was off for that reason (`transport.quiet_override` and a warning). See [docs/fork/smaller-fixes.md](fork/smaller-fixes.md#quiet-mode-could-discard-unsaved-work).
- `cosmos_import` no longer stalls or deadlocks V-Ray scenes. It opens the Cosmos browser on Max's main thread before importing (the importer's own hidden browser thread caused the stalls), waits for Max to settle, restores the selection, and never opens or closes the Material Editor. If Max stays busy, the result says `safe_to_edit: false` and other calls fail with `IMPORT_SETTLING` without being sent, for 15 minutes at most. See [docs/fork/cosmos-import.md](fork/cosmos-import.md).
- The new `cosmos_import` option `swap_medit_renderer` (off by default) uses Scanline as the Material Editor renderer during the import. It's turned on automatically when the browser can't be opened on the main thread. It avoids the preview stall, not the deadlock. The original renderer is put back once Max settles. If a Material Editor is open then, or `restore_medit_renderer` is false, it stays at Scanline and the result gives the script to restore it.
- `cosmos_import` polls with light read-only probes instead of a full class scan every 250 ms. `cosmos_search` no longer needs a Max round trip when exactly one Cosmos importer is registered for the target Max, or when `renderer` is `vray` or `corona` and that Max has an importer for it. With both the V-Ray and Corona importers and `renderer="current"`, it still reads the scene renderer from Max once (`renderer_source: "scene"`).
- Over the named pipe, a busy or hung Max now gives `MAX_BUSY` or `MAX_NOT_RESPONDING`, with window and CPU evidence, instead of a bare timeout. The pipe lock has a timeout and replies have a real deadline; long work that is still running is waited for. A request that may have run is never replayed (`request_sent` says whether it reached Max), and a hung Max fails fast until it recovers. See [docs/fork/hung-max.md](fork/hung-max.md).
- `get_bridge_status` answers while Max is hung and says what holds the main thread: this server's request, another MCP client's, or work outside the bridge. It uses a new native `health` command that the bridge answers from its pipe thread, which also reports connected clients and queued work. With a stock bridge, which lacks `health`, `get_bridge_status` falls back to `ping`: it still answers in about 6 s while Max is hung, but can't tell another client's request from work outside the bridge.
- Added `capture_hang_diagnostics`, which captures where a hung Max is stuck from native thread stacks read from the OS, without sending anything to Max. It pauses each Max thread for a few milliseconds while it reads the stack, and by default saves the stacks under `%LOCALAPPDATA%\3dsmax-mcp\diagnostics\`.
- The fork's bridge no longer stalls Max's exit while requests are queued: queued work fails at once when Max exits. Ported from Geddart's fork.
- The stdio server exits when its client process exits, and always after its stdin closes, so a crashed client no longer leaves a server connected to Max. `MAXMCP_PARENT_WATCHDOG=0` turns off the client-process check; the exit on stdin close stays.
- `replace_material` documents its direction (source = the material to apply, target = the one to replace) and returns `no_match` with a warning when nothing matched. It also replaces the target inside Multi/Sub and other sub-material slots (`include_sub_materials`, on by default), skips slots that would create a reference loop (`blocked` if all were skipped), and finds a source that isn't on any object: a sub-material, a Material Editor slot or a material library (`source_found_in`; `source_from` limits the search). The sub-material slots and the source search need the fork's bridge. See [docs/fork/replace-material.md](fork/replace-material.md).
- `batch_replace_materials` runs its entries in order, each on the scene the previous ones left, so `A→B, B→A` no longer swaps two materials. The in-order run needs the fork's bridge; a stock bridge plans every entry from one snapshot, so there `A→B, B→A` still swaps. Go through a temporary material instead. Entries that reuse a name an earlier entry applies or removes get `depends_on_entries`, because their preview can differ from the real run.
- `execute_maxscript` no longer reports an interrupted script, for example one that calls `quitMax` or `resetMaxFile`, as a parse error. Real syntax errors include the compiler's message. Interruptions get the new code `MAXSCRIPT_INTERRUPTED`, with Max's own error text when it gives any. Needs the fork's bridge. See [docs/fork/smaller-fixes.md](fork/smaller-fixes.md).
- The curve tools accept Line objects as editable splines.
- `cosmos_import` finds what an import created by comparing handles before and after, so a package material whose name differs from the asset ("Steel Blurry" creates "Steel_Polished #0") is reported. Results add `primary_material`/`primary_map` with `primary_reason`, `asset_name`, `material_name`, `medit_slot` and a `note` when the names differ. New items from other plugins, such as Forest Pack regenerating its materials during the import, are kept apart (`other_new_materials`). See [docs/fork/cosmos-import.md](fork/cosmos-import.md#finding-what-the-import-created-9-2853b1c).
- `cosmos_import` no longer lets the Chaos importer overwrite the material in the user's active Material Editor slot. It points the importer at a free slot (an untouched default material, including V-Ray's "Material #N" slots), and puts any displaced material back once Max is quiet (`medit_active_slot`, `displaced_material`, `medit_slot_restored`). See [docs/fork/cosmos-import.md](fork/cosmos-import.md#keeping-the-users-material-editor-slot-10-c1e09a7).
- `cosmos_import` of a model reports the nodes it created, found by handle whatever their class or name (a VRayProxy, a container, a group); new nodes it can't tie to the import go to `other_new_nodes`. Untouched default "Material #N" materials are no longer listed as new. A Cosmos browser the import showed is minimized, never closed, after a quiet, confirmed import (`minimize_browser=false` turns that off).
- `contact_check` takes an explicit `tolerance` or `near_gap` in millimetres, converted like the defaults (any explicit value used to fail with "invalid thresholds"), and skips non-mesh nodes with a warning instead of failing. Pairs run cheapest first within a work budget and `time_budget_s` (at most 45 s, queue time included); when either runs out it returns partial results with `complete: false` instead of keeping Max's main thread busy for minutes. See [docs/fork/smaller-fixes.md](fork/smaller-fixes.md).
- `clone_objects` on a member of a closed group clones that member alone and detaches the copy, in one undo step; Max cloned the whole group, stacked invisibly on the original. `clone_whole_group=true` keeps Max's behaviour. `scene_qa` reports such stacked duplicates (`duplicate_group_heads`) and never deletes them.
- When a dialog holds the read-only check that `clone_objects`, `scene_qa` fix mode, `drop_to_surface`, `railclone_set_param` or `forest_validate(fix=true)` runs first, the call gives a retryable `BLOCKED_BY_DIALOG` saying nothing was changed, and the change is never sent.
- `agent_viewport` reclaims its own floating viewport after a restart and Hold/Fetch, instead of failing with "All floating viewports are in use". Needs the fork's bridge.
- Material graphs read Autodesk Bitmap paths from Revit/ATF materials. They no longer report a hidden, unnamed parameter of Cosmos VRayBitmaps as a second texture file, which raised a false `FILE_MISSING`. Both need the fork's bridge. With any bridge, `material_roles` reports file-bearing maps whose path can't be read (`FILE_PATH_UNREADABLE`, `complete: false`). Empty slots give `FILE_NOT_ASSIGNED`, which needs the fork's bridge; a stock bridge lacks the native `fileUnset` flag, so there they give `FILE_PATH_UNREADABLE` too. See [docs/fork/autodesk-bitmap-paths.md](fork/autodesk-bitmap-paths.md).
- A call held by a modal dialog returns 1.7.5's `BLOCKED_BY_DIALOG` only while Max's main thread still pumps. Once it stops, the call is diagnosed like any other, so a hung Max with a dialog on screen still gives `MAX_NOT_RESPONDING` (needs the fork's bridge). Each check is bounded (dialog polls and probes, the control channel's included, are dropped at their deadline), but a request on a Max that stays responsive or busy still waits until it answers. `get_bridge_status` reports `bridge_state: "blocked_by_dialog"` when a dialog holds the running request.
- `cosmos_import` won't start while a dialog holds Max's main thread. A call held by a dialog stops the import before dispatch, or later gives `state: "dialog_open"` with `pending_restore` instead of confirming the import inside the dialog's loop. `MAX_DIALOG_ERROR` on the preparation call gives `not_imported` with the restore scripts. After the settle, a confirming snapshot that gets one is taken once more, and a restore call that gets one is read back instead of being sent again. The browser action runs with quiet mode off, and `max_dialogs` also reports the import's interrupted calls.
- The dialog monitor never treats the Cosmos browser, the Material Editor or a floating viewport as a blocking dialog, and never reads or presses a dialog on a thread other than Max's main thread (such as the Cosmos importer's, which may be deadlocked). Qt dialogs are read only while Max's main thread pumps. The monitor's rules need the fork's bridge; with a stock bridge the client still leaves these windows out of its own dialog checks.
- Fixes to 1.7.5's dialog handling: a transacted native call with an acknowledged MAXScript error box is always rolled back, never committed first (MAXScript calls are never rolled back); a Qt button press reported as failed (`MAIN_THREAD_BUSY`) never clicks afterwards, and one Max had already started reports `DIALOG_OUTCOME_UNKNOWN`; direct-mode reads no longer risk leaving Max in quiet mode; and the agent viewport's render cleanup finishes after an error box. Needs the fork's bridge. See [docs/fork/hung-max.md](fork/hung-max.md#upstream-175s-dialog-handling-fixed-on-the-merge).
- A request whose reply was lost after it was sent (the pipe closed, a mismatched reply) now gives `REQUEST_OUTCOME_UNKNOWN` (not retryable, `request_sent: true`) instead of `BAD_PARAM`; `get_bridge_status` reports such a ping as `bridge_state: "connection_lost"` (or `not_responding` once Max has exited). A `BLOCKED_BY_DIALOG` call whose connection broke is reported by `max_dialogs` as `status: "lost"`, `outcome: "unknown"`, not as a clean failure. `max_dialogs` on a bridge without the dialog monitor (any bridge before 1.7.5) gives `BRIDGE_OUTDATED`. The `BLOCKED_BY_DIALOG` hint and `max_dialogs` say to always ask the user before Save, Don't Save, overwrite, Fetch or Reset, even when working unattended.

## [1.7.6] — 2026-10-14

- Faster mesh tools: `inspect_mesh` on a 288k-face mesh takes about 11 ms instead of 4.7 s (72k faces: 7 ms instead of 1 s), whatever the `limit`. `inspect_mesh`, `mesh_edit`, `create_mesh`, `pick_component` and `loft_mesh` get `mesh_token` from the native bridge, which hashes the Editable Poly base cage in the same main-thread step as the tool's script, instead of formatting every vertex, edge and face in MAXScript for a SHA-256. Tokens from earlier versions no longer match; inspect again. `mesh_edit` and `create_mesh` started inside another 3ds Max undo operation fail with `USER_BUSY` (retryable). These tools require the native bridge.
- Faster repeated `mesh_edit`: it updates the Modify panel only where it differs, so repeated edits on one object take a few milliseconds instead of about 400 ms.
- Faster `geometry_qa`: 8k triangles take 32 ms (was 265 ms) and 72k take 0.3 s (was 2.4 s). The native bridge exports the evaluated mesh and the analysis builds issue rows only for reported samples. It also works with safe mode on (the default); its MAXScript export used `createFile`, which safe mode blocks.
- Tools that take object names no longer act on an arbitrary object when several share a name. Single-object tools fail with `AMBIGUOUS` and list the candidates with their handles; batch tools (selection, visibility, layers, groups, selection sets, `set_parent`, `batch_rename_objects`, `set_modifier_property`, keyframes, `replicate_material`, `add_tyflow_collision` colliders, `assign_to` of texture and Shell materials) skip shared names and report them in `ambiguous` with `status: "partial"`. A name with no exact match matches by letter case alone, as MAXScript does.
- tyFlow, MCG, Data Channel and Boolean tools report what applied. Edits, collisions, PhysX settings, patches, census and manifest runs that applied in part return `status: "partial"` with warnings; edits that applied nothing are errors. `add_tyflow_collision` with only unknown names no longer clears the operator's colliders, an interrupted `harvest_tyflow_manifest` no longer replaces the cached manifest, and a failed `tyflow_apply_patch` returns `PATCH_APPLY_FAILED`. `load_dc_preset` rejects unknown presets before touching the stack. These tools keep `BLOCKED_BY_DIALOG`, `MAX_BUSY` and `TARGET_UNAVAILABLE` errors instead of flattening them.
- `transform_object` and `set_object_property(position)` fail with `TRANSFORM_IGNORED` when a constraint, script or expression controller ignores the edit, instead of reporting success for an object that did not move. A `transform_object` call that moved only some parts returns `status: "partial"` with `ignored`. `set_object_property` names `transform_object` for rotation and scale instead of reporting the property missing.
- `scene_patch` warns when no operation changed the scene, and `scene_qa` without the native bridge returns an error. Transforms that a constraint, script or expression controller ignored are reported in `ignored` with the controller (`status: "partial"`); a patch in which nothing took effect fails with `PATCH_APPLY_FAILED` and leaves no undo entry.
- A call that 3ds Max does not answer no longer freezes the MCP server. After its deadline (about 130 s for ordinary calls, the exact timeout for probes, longer timeouts honored) it returns `MAX_BUSY`, not retryable; the call keeps running in Max and `max_dialogs(action="inspect")` reports its result once it finishes.
- Mutating tools report what actually happened. A MAXScript error inside a native edit fails the call and rolls back its undo step instead of returning success. Requests that matched nothing are errors; requests that applied only in part return `status: "partial"` with the skipped names and warnings. This covers objects, selection, visibility, modifiers, layers, groups, selection sets, `set_parent`, `batch_rename_objects`, `manage_scene` save, materials, texture maps, controllers, wires, keyframes, effects and render.
- `replace_material` and `batch_replace_materials` also replace materials inside sub-material slots (Multi/Sub and other material trees), find the source in sub-material slots and the Material Editor, refuse slots that would create a reference loop, and report `replaced_slots`, `skipped` and a status of `replaced`, `no_match`, `no_objects`, `blocked` or `unchanged`. Batch entries apply in order.
- `contact_check` takes `time_budget_s` (default 20 s): it measures pairs with the fewest faces first and returns `complete: false` with what it measured when time runs out. Named group heads, dummies, shapes, lights, empty meshes and meshes over `max_faces` are returned in `skipped` instead of failing the call.
- The agent viewport restores its preview settings (temporary denoiser, render elements, VFB denoiser layer, render view lock) when a V-Ray or Corona preview is stopped from the renderer's own UI.
- MAXScript reached from a read handler that runs off Max's main thread now runs on the main thread; `inspect_properties` on modifiers without parameter blocks (Normal, Surface) no longer evaluated MAXScript off it.
- `set_modifier_property` matches `modifier_class` by the MAXScript class name (`Bend`) as well as the SDK name (`BendMod`).
- `add_tyflow_event` returns the new event instead of a parse error.
- Removed `inspect_modifier_properties`, a compatibility alias. Call `inspect_properties(name, target="modifier", modifier_index=N)`.
- Removed `refresh_plugin_manifest`. `get_plugin_manifest` already rebuilds the manifest from the live runtime on every call; the removed tool only added `"refreshed": true`.
- `create_tyflow` takes `preset` (rain, snow, fountain, burst, debris) with `amount` and `speed`, replacing `create_tyflow_preset`. Presets build the same operators, flow name and event name as before; `event_name` now overrides the preset's event name.
- `connect_tyflow_operator` takes `create_if_missing` to add a missing Send Out, replacing `connect_tyflow_events`. Call `connect_tyflow_operator(name, from_event, from_operator="Send Out", to_event, create_if_missing=true)`; the reply reports `created_send_out`.
- Removed `list_tyflow_operator_types`. It probed a fixed list of 34 operator names; `harvest_tyflow_manifest` probes the documented catalog the same way and caches it per tyFlow version, and `list_tyflow_operators` reports each type's `available` flag. Pass `operator_names` to `harvest_tyflow_manifest` to probe specific types live.
- tyFlow particle reads (`get_tyflow_info`, `get_tyflow_particle_count`, `get_tyflow_particles`) refuse objects that are not tyFlow and report a failed read instead of 0 particles; a frame update or attribute read that failed is a warning.
- A `tyflow_apply_patch` whose applied operations were kept records them in the wiring ledger, and a tyFlow edit whose ledger update failed says so in `warnings`.
- `add_data_channel` checks `order` and `expected_operator_count` before it adds a modifier or operator, so a rejected request leaves the stack unchanged. `list_dc_presets` escapes preset names.
- `mcg_search_operators` warns when it falls back to the offline catalog, and `mcg_create_graph` no longer treats a class-name probe that could not run as proof the name is free.
- `scene_qa` fix runs that had nothing to repair return `status: "noop"` with a warning.
- `create_lights` and `edit_lights` warn when a committed light could not be inspected afterwards.
- 3ds Max now quits while an MCP client is connected. The bridge waited for every connected client to disconnect, so Max kept running in the background after its window closed. Requests still queued when Max exits now fail at once instead of waiting out their timeout.
- After the selected 3ds Max exits or restarts, calls fail with `TARGET_UNAVAILABLE`, not retryable, and point to `list_max_instances`, `select_max_instance` and `release_max_instance`, instead of a retryable bridge error with a scene-search hint.
- `get_state_sets`, `get_camera_sequence`, `get_wired_params` and `introspect_instance` run on Max's main thread. They ran MAXScript on a pipe thread.
- `inspect_curve` and `edit_curve` accept Line objects, and `draw_spline` edits a Line in place instead of converting it, or collapsing its modifiers with `convert=true`.
- `contact_check` no longer misreads a whole-number `tolerance` or `near_gap` as almost zero.
- `clone_objects` refuses a member of a closed group and names the group, instead of cloning the whole group on top of the original. Clone the group, or open it to clone the member alone.
- `replace_material` returns `no_objects` with a warning when no object uses the target material, instead of reporting success.
- Agent viewport V-Ray previews (`vray_ipr`, `vray_vfb`) start on V-Ray GPU; they were refused.
- `execute_maxscript` and the agent skill warn that quiet mode skips the save prompt: `checkForSave()` returns true and `resetMaxFile()` discards unsaved work.
- Added Pulze Scene Manager tools: `pulze_inspect`, `pulze_preflight`, and guarded, undoable `pulze_set` edits with `pulze_restore` from a backup (#26).
- A Boolean forced to the modifier backend on 3ds Max 2023, which has no Boolean modifier, now fails with a script error naming the working backends instead of a bridge error.
- The native bridge is split into a permanent shell and a core. Installed bridges still ship as one plugin; development builds rebuild the core and reload it into a running Max with `reload_native_core`, and `scripts/mcpcall.py` runs a tool from current source without restarting the MCP server.
- Removed the Tool Playground GUI (`Tool Playground.bat`, `scripts/tool_playground.py`) and its generated catalog.
- Rebuilt bridges for Max 2023-2027.

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
