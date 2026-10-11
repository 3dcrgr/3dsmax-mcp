# About this fork

This is a fork of [cl0nazepamm/3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp), based on upstream 1.7.5 and upstream's `master` after it (`29aed53`, a README version bump). It carries fixes for problems found while using the MCP on production architectural visualization work:
- 3ds Max 2027 (2027.2), and 3ds Max 2026 until 2026-10-08;
- V-Ray 7 (GPU production renderer);
- Chaos Cosmos and Chaos Vantage;
- RailClone and Forest Pack;
- scenes linked from Revit, with Hebrew material and object names.

The worst of these problems hung Max and lost unsaved work. Everything here is meant to go back upstream as pull requests. Until then, this fork is where the fixes live.

## What upstream 1.7.5 covers

Upstream 1.7.5 added handling for dialogs that block Max: a call stuck behind one returns `BLOCKED_BY_DIALOG`, and `max_dialogs` reads and answers it. It also runs agent MAXScript in Max's quiet mode, so prompts take their default answer.

Quiet mode could also answer Max's "save changes?" prompt on its own and lose unsaved work (fork issue #12). The fork's bridge keeps quiet mode off for any script that mentions a reset, open, fetch or quit command, so that prompt reaches the agent as `BLOCKED_BY_DIALOG` instead. That check is native. With a stock bridge (a fresh install on Max 2027 today, see [Installing](#installing), or Max 2023 to 2025), such a script sent with no explicit `quiet` still runs in quiet mode; the Python side only turns an explicit `quiet=True` without `allow_discard=True` into `quiet=False`. See [smaller-fixes.md](fork/smaller-fixes.md#quiet-mode-could-discard-unsaved-work).

Two of the fork's fixes are now in upstream too, so the merge kept upstream's code for them and dropped the fork's:
- Failed agent scripts no longer print errors in the user's Listener (fork issue #8, see [smaller-fixes.md](fork/smaller-fixes.md#failed-agent-scripts-printed-errors-in-the-users-listener)).
- A request that timed out in the queue no longer runs later. That was half of the fork's executor fix; the other half, the shutdown drain, is still the fork's.

Everything in the table below is fork-only, including the fixes to 1.7.5's own dialog handling.

## What's fixed

| Problem | Fix | Details | Status |
|---|---|---|---|
| A Cosmos import stalls Max for minutes, or a later window close such as `MatEditor.Close()` deadlocks it | Open the Cosmos browser on Max's main thread first; settle from the OS; refuse calls with `IMPORT_SETTLING` while Max stays busy; never open or close the Material Editor | [cosmos-import.md](fork/cosmos-import.md) | Verified live on Max 2026, both with the browser already open and with the import opening it on a freshly started Max |
| The import polls Max with a full class scan every 250 ms | Light, read-only probes with backoff | [cosmos-import.md](fork/cosmos-import.md) | Verified live on Max 2026 |
| `cosmos_search` needs Max's main thread | Gets the PID without a bridge call, and the renderer too when only one Cosmos importer matches (with both V-Ray and Corona installed, pass `renderer="vray"` or `"corona"`) | [cosmos-import.md](fork/cosmos-import.md) | Verified live on Max 2026 with both importers installed, so it read `renderers.current` once; the path without a bridge call is unit tested |
| A hung Max gives every tool a bare timeout | Lock timeout, real reply deadline, process diagnosis, `MAX_BUSY` / `MAX_NOT_RESPONDING` | [hung-max.md](fork/hung-max.md) | Verified live on Max 2026: `MAX_NOT_RESPONDING` with a 20 s `sleep` from another process standing in for a hang, and `MAX_BUSY` during an 8 s one; the lock timeout is unit tested |
| `get_bridge_status` can't tell what holds the main thread | Native `health` command served from a pipe thread | [hung-max.md](fork/hung-max.md#bridge-health-from-a-pipe-thread-2ce5928-reviewed-in-011e547) | Verified live on Max 2026, idle and during an 8 s `sleep` from another process, and on Max 2027, idle; the 20 s `not_responding` path isn't tested live yet |
| No way to see *where* Max is stuck | `capture_hang_diagnostics`: native stacks via dbghelp, read from the OS | [hung-max.md](fork/hung-max.md#capture_hang_diagnostics) | Reviewed and deployed; tested on spawned processes, not yet on a hung Max |
| Max's exit stalls while MCP requests are queued | Executor shutdown drain (from Geddart's fork): queued work fails at once when Max exits | [hung-max.md](fork/hung-max.md#native-executor-adcfd93-ported-from-geddarts-fork) | In every fork bridge, and deployed on Max 2026 and 2027; the bridge loaded and served calls live, but quitting Max with a call queued isn't tested live yet |
| Crashed clients leave `maxmcp.server` processes running, possibly still connected to Max | The server exits with its client; the bridge reports connected clients | [hung-max.md](fork/hung-max.md#orphaned-servers-6-a9d2a0d) | Client count verified live in `health` on Max 2026 and 2027; the server exiting with its client isn't tested live yet |
| `replace_material` "replaces" nothing and reports success | Direction documented, `no_match` warning, sub-material slots matched, sources found outside the scene; batch entries apply in order (side effect: two entries meant to swap two materials no longer do) | [replace-material.md](fork/replace-material.md) | Direction, `no_match` and a Multi/Sub slot replaced from a Material Editor source verified live on Max 2026; nested Multi/Sub, the loop guard and batch aren't tested live yet |
| Revit (Autodesk Bitmap) texture paths read as empty | Read bitmap-asset parameters; flag unreadable paths | [autodesk-bitmap-paths.md](fork/autodesk-bitmap-paths.md) | Verified live on Max 2026, including the duplicate VRayBitmap row fix; on Max 2027, one row per VRayBitmap slot |
| `execute_maxscript` calls an interrupted script (e.g. `quitMax`) a "parse error" | Re-compile to tell syntax errors from interruptions; new code `MAXSCRIPT_INTERRUPTED` | [smaller-fixes.md](fork/smaller-fixes.md) | Syntax errors verified live on Max 2026 and 2027; `MAXSCRIPT_INTERRUPTED` (e.g. `quitMax`) isn't tested live yet |
| Upstream 1.7.5's quiet mode could answer a "save changes?" prompt and discard unsaved work | The fork's bridge never runs a script that mentions a reset, open, fetch or quit command in quiet mode by default; with any bridge, `quiet=True` on such a script also needs `allow_discard=True` | [smaller-fixes.md](fork/smaller-fixes.md#quiet-mode-could-discard-unsaved-work) | Unit tested; in the fork's 2026 bridge and the deployed 2027 one; not tested live yet |
| Curve tools reject Line objects | Line counts as an editable spline | [smaller-fixes.md](fork/smaller-fixes.md#curve-tools-rejected-line-objects) | Verified live on Max 2026 |
| A Cosmos material whose package name differs from the asset name isn't found | Detect what the import created by handle, whatever its name; report both names | [cosmos-import.md](fork/cosmos-import.md#finding-what-the-import-created-9-2853b1c) | Verified live on Max 2026; the follow-up that keeps other plugins' new items apart is deployed, retest pending |
| A Cosmos import silently replaces the material in the user's active Material Editor slot | Point the importer at a free slot, then put any displaced material back once Max is quiet | [cosmos-import.md](fork/cosmos-import.md#keeping-the-users-material-editor-slot-10-c1e09a7) | Partly verified live on Max 2026 (the material was kept, but V-Ray's default slots weren't seen as free); the follow-up is deployed, retest pending |
| After a restart and Hold/Fetch, the agent viewport can't be reclaimed | Tag the agent's floating viewport in the scene and reclaim exactly that restored window | [smaller-fixes.md](fork/smaller-fixes.md#the-agent-viewport-couldnt-be-reclaimed-after-a-restart) | Deployed on Max 2026 (`1cf0f9d8…`); the first live test was inconclusive, because the fetched Hold predated the tag. In every fork bridge since, the 2026 one in `native/bin/` (`d5cd6ba5…`) and the deployed 2027 one included; not retested |
| Upstream 1.7.5's dialog monitor can report the Cosmos browser, the Material Editor or the agent viewport as a blocking dialog, and reads or presses dialogs of any thread | These windows are never dialogs; dialogs of other threads (e.g. the Cosmos importer's) are listed but never read or pressed; Qt dialogs are only read while Max's main thread pumps | [cosmos-import.md](fork/cosmos-import.md#windows-that-are-never-dialogs-6e03f99) | Unit tested; in the fork's 2026 bridge and the deployed 2027 one; not tested live yet |
| Upstream 1.7.5's dialog handling calls a hung Max "blocked by a dialog", can commit a call before its acknowledged error is recorded, can click after reporting a failed press, and can leave Max in quiet mode | A main thread that stopped pumping is a hang; errors publish before the call resumes; timed-out presses are withdrawn or reported `DIALOG_OUTCOME_UNKNOWN`; quiet mode only changes on the main thread; `cosmos_import` handles `MAX_DIALOG_ERROR` and dialogs before dispatch | [hung-max.md](fork/hung-max.md#upstream-175s-dialog-handling-fixed-on-the-merge) | Unit tested; in the fork's 2026 bridge and the deployed 2027 one; not tested live yet |
| A Cosmos model import reports no nodes, lists untouched "Material #N" slot materials as new, and leaves the Cosmos browser on top of other windows | New nodes found by handle, whatever their class or name (VRayProxy, container, group); untouched default slot materials ignored; the browser the import showed is minimized after a quiet, confirmed import | [cosmos-import.md](fork/cosmos-import.md#model-nodes-slot-noise-and-the-browser-13-3b3210d) | Unit tested; deployed on Max 2026 (1.7.3-based) on 2026-10-05; not tested live yet |
| `contact_check` rejects an explicit `tolerance` or `near_gap`, and one non-mesh node aborts the call | Thresholds are millimetres, converted to scene units; non-mesh nodes are skipped with a warning | [smaller-fixes.md](fork/smaller-fixes.md#contact_check-thresholds-non-mesh-nodes-and-dense-meshes-14-15) | Unit tested; deployed on Max 2026 (1.7.3-based) on 2026-10-05; not tested live yet |
| `contact_check` on dense meshes keeps Max's main thread busy for minutes after the client gave up | Work estimate, cheapest pairs first, a `time_budget_s` deadline (at most 45 s, queue time included); partial results with `complete: false` | [smaller-fixes.md](fork/smaller-fixes.md#contact_check-thresholds-non-mesh-nodes-and-dense-meshes-14-15) | Unit tested; deployed on Max 2026 (1.7.3-based) on 2026-10-05; not tested live yet |
| Cloning a member of a closed group clones the whole group, stacked invisibly on the original | `clone_objects` clones the member alone and detaches the copy, in one undo step with rollback; `scene_qa` reports stacked duplicate groups (`duplicate_group_heads`) | [smaller-fixes.md](fork/smaller-fixes.md#cloning-a-closed-group-member-cloned-the-whole-group-16) | Unit tested; deployed on Max 2026 (1.7.3-based) on 2026-10-05; not tested live yet |
| On the 1.7.5 merge, a dialog holding the read-only check that `clone_objects` or `scene_qa` fix mode runs first says "do not repeat", though nothing was changed | A retryable `BLOCKED_BY_DIALOG` that says nothing was cloned or changed; `scene_qa` never sends its fix behind that check | [smaller-fixes.md](fork/smaller-fixes.md#cloning-a-closed-group-member-cloned-the-whole-group-16) | Unit tested; this branch only; not tested live yet |

What the status words mean:
- **Verified live:** the fix was exercised in a real Max session. On Max 2026 that was the 1.7.3-based fork, from 2026-10-01 to 2026-10-06; on Max 2027, this branch with the fork's 2027 bridge, on 2026-10-11. Some checks used a stand-in for the original failure, such as a 20 s `sleep` instead of a real hang.
- **Reviewed:** an adversarial code review was run before deployment.
- **Deployed:** the fix runs on the production machine. Until 2026-10-08 that was Max 2026 with the 1.7.3-based fork and the 2026 bridge `1cf0f9d8…`; since 2026-10-11 it's Max 2027 with this branch.

The checks run on Max 2027 so far were read-only: the bridge's `health` block, `max_dialogs` running its monitor, a failing script returning its parse error while staying out of the Listener (#8), and `material_roles` giving one row per VRayBitmap slot (#5). The 2026 bridge in `native/bin/` (`d5cd6ba5…`) was never deployed, and Max 2026 is no longer available here to test it.

The details pages say exactly what was and wasn't tested.

## What's added

| Feature | What it does | Details | Status |
|---|---|---|---|
| Chaos Vantage status, image and Live Link | `vantage_status` reads Vantage's state over its local command port, without touching Max. `vantage_get_image` saves the frame Vantage shows as a PNG. `vantage_live_link` starts or stops V-Ray GPU Live Link from Max, refusing rather than switching renderers or launching Vantage, and stopping only the link this MCP server started unless `force=true` | [vantage.md](fork/vantage.md) | Unit tested and reviewed. `vantage_status` read live (read-only) against Vantage 3.4.0 on 2026-10-06, with Max 2026; `vantage_get_image`, starting and stopping the link aren't tested live yet. |
| Floating and sunk props | `resting_check` finds props floating above or sunk into the surface below them; `drop_to_surface` drops floating ones onto it (dry run by default, one undo step re-measured before it's kept). Fork issue #17 | [resting-check.md](fork/resting-check.md) | Unit tested and reviewed; tested live in a scratch Max 2026 over three rounds; deployed on Max 2026 (1.7.3-based) on 2026-10-06; not tested live on Max 2027 yet |
| RailClone inspection, parameters and library | `railclone_inspect` reads bases, exposed parameters (limits, selector options), segments and output counts. `railclone_set_param` sets exposed parameters by name or id, refusing values outside their limits, in one undo step. `railclone_library` searches the library browser roots without calling Max; `railclone_from_library` creates an item by route or name and assigns splines (a `.max` item clears Max's undo history, so it needs `allow_undo_flush=true` and returns `remove_with` instead of an undo step). Also fixes `get_railclone_style`/`set_railclone_style` on styles with repeated attributes (7 of 43 stock styles) and stale `get_railclone_output` positions after a move or copy. Fork issue #20 | [railclone.md](fork/railclone.md) | Unit tested; tested live on Max 2026 (RailClone 7.1.1) on 2026-10-06 and on Max 2027 (RailClone 7.3.5, 25 checks) on 2026-10-11, in scratch scenes, including the undo-off `.max` load. Undo itself is safe by construction and not run live. |
| Forest Pack items, edits, library and checks | `forest_items` reads items with instance-ready transforms. `forest_edit_items` moves, deletes, creates and resizes items in custom edit mode (`forest_set_mode` switches modes); Forest Pack doesn't record item edits in undo, so it returns `revert`, an edit list that puts them back. `forest_library` searches the library without calling Max; `forest_from_library` places a preset or 3D item on surfaces (the merge clears Max's undo history, so it needs `allow_undo_flush=true` and returns `remove_with`). `forest_validate` checks the item and area lists and can pad them (`fix=true`). Also fixes `scatter_forest_pack`: density through the tile size, `direction` 100 for "up", and the real item count, with `ZERO_ITEMS` for an empty scatter. Fork issue #21 | [forest-pack.md](fork/forest-pack.md) | Unit tested; tested live on Max 2026 (Forest Pack 9.0) on 2026-10-06 and on Max 2027 (Forest Pack 9.4.3, 27 checks) on 2026-10-11, in scratch scenes, including the undo-off placement. Undo itself is safe by construction and not run live. |
| iToo → Chaos Vantage readiness check | `itoo_vantage_check` reads every Forest Pack, RailClone and ForestIvy object (or the names given) and reports, with a severity and a basis (doc, live, inferred, user's Vantage logs): what Forest/RailClone Color slot 1 holds (what Vantage renders), opacity maps, CPU-only features, ID-driven maps under `forest_automat_*`, RailClone render settings, modifiers, proxy cache and `sobjmtl` wrapping, and item, segment and face caps. A fallback hint is given for objects with errors. Strictly read-only: it never rebuilds (caps come from RailClone's existing export and Forest's `trees.count()` as they are), writes, selects, renders or exports; what can't be read without that is listed as unknown. Fork issue #25 | [itoo-vantage-check.md](fork/itoo-vantage-check.md) | Unit tested (lint for writes in the generated MAXScript); tested live, read-only, on Max 2027 (RailClone 7.3.5, Forest Pack 9.4.3) on 2026-10-11 in a scratch scene, with a node-event logger and export hashes showing no changes. Vantage itself wasn't run. Not deployed. |

## Installing

Install from source, the same way as upstream, but from this fork. Its default branch, `fix/fork-fixes`, is this line (upstream 1.7.5 plus the fixes, which is what runs on Max 2027), so a plain clone gets it:

```powershell
git clone https://github.com/3dcrgr/3dsmax-mcp.git
cd 3dsmax-mcp
uv sync
uv run python install.py
```

Close 3ds Max and your AI clients first, as upstream's README says.

After updating, start a new client session. MCP clients cache the tool list, so new tools such as `resting_check` don't appear in sessions that were already open, even after the server restarts.

**The native bridge:** `install.py` copies the prebuilt bridges from `native/bin/`.
- `mcp_bridge_2026.gup` is the fork's build from `ec903a5` (sha256 `d5cd6ba5f7f35b83…`): upstream 1.7.5 plus every native fix on this branch. No native source has changed since, and later commits generate the same tool registry.
- `mcp_bridge_2027.gup` is the fork's build from `6397f1f` with the 3ds Max 2027 SDK (sha256 `ca5e16dd43fc8183…`), deployed to the production machine on 2026-10-11 13:39. It has every native fix here, including the V-Ray GPU preview (#18). It replaces the first fork build (`48c186df…`, from `18611d7`, committed in `d90d6db`), which itself replaced upstream's stock 1.7.5 build (`2d5304d4…`).
- The 2023, 2024 and 2025 bridges are upstream's 1.7.5 builds. To get the native fixes on those, build the bridge yourself (below).
- With a stock bridge, the Python-side fixes work, but the native ones are missing: `health`, the executor shutdown drain, Autodesk Bitmap paths and the duplicate VRayBitmap row fix, `MAXSCRIPT_INTERRUPTED`, `replace_material`'s sub-material matching and source search, the agent viewport reclaim (#11), the #12 quiet-mode check, the dialog monitor's rules for Cosmos, Material Editor and other-thread windows, and the native half of the 1.7.5 dialog fixes (publishing errors before the call resumes, withdrawn presses, quiet mode on the main thread only, `main_thread_pumping`, the Qt error-box retry, the agent viewport's render cleanup after an error box). Without the #12 check, a script that mentions a reset, open, fetch or quit command runs in quiet mode, where a save prompt takes its default answer; pass `quiet=False` for such scripts.

## Building the native bridge

You need:
- Visual Studio 2022 with the v143 toolset version **14.38** (the toolset Autodesk uses for Max 2026, and the version the Max 2027 SDK pins);
- CMake 3.20+;
- Python 3, which the build runs to generate a tool registry;
- the 3ds Max SDK for your version, at its default path (`C:\Program Files\Autodesk\3ds Max <year> SDK\maxsdk`) or passed with `-DMAXSDK_PATH=<path to maxsdk>`. The fork's 2027 bridge was built against the SDK extracted from Autodesk's `SDK_3dsMax2027.msi` with `msiexec /a`, without installing it.

`native/CMakeLists.txt` builds with C++20 for Max 2027 and C++17 for earlier versions.

```powershell
cmake -S native -B build-2027 -G "Visual Studio 17 2022" -A x64 -T "v143,version=14.38" -DMAX_VERSION=2027 -DMAXSDK_PATH=<path to maxsdk>
cmake --build build-2027 --config Release --target mcp_bridge
```

For another Max version, change `-DMAX_VERSION`, the build folder and the toolset to match that SDK, and use that year in the file names below.

The output is `build-2027\Release\mcp_bridge.gup`. To install it, close Max and copy it over `C:\ProgramData\Autodesk\ApplicationPlugins\3dsmax-mcp\Contents\bin\mcp_bridge_2027.gup`; that may need an elevated prompt. Keep a copy of the original. Re-running `install.py` replaces that whole folder from `native/bin/`, so also copy your build to `native\bin\mcp_bridge_2027.gup`, or the next install puts the committed bridge back.

The build generates a tool registry from `maxmcp/tools/*.py`. To keep uncommitted Python edits out of a binary, build from a clean snapshot of HEAD. Write the archive to a file rather than piping it into `tar`: Windows PowerShell 5.1 corrupts binary data piped between programs.

```powershell
New-Item -ItemType Directory -Force C:\temp\mcp-build | Out-Null
git archive HEAD --prefix=src/ -o C:\temp\mcp-build\src.tar
tar -xf C:\temp\mcp-build\src.tar -C C:\temp\mcp-build
cmake -S C:\temp\mcp-build\src\native -B C:\temp\mcp-build\build-2027 -G "Visual Studio 17 2022" -A x64 -T "v143,version=14.38" -DMAX_VERSION=2027 -DMAXSDK_PATH=<path to maxsdk>
cmake --build C:\temp\mcp-build\build-2027 --config Release --target mcp_bridge
```

The output is then `C:\temp\mcp-build\build-2027\Release\mcp_bridge.gup`.

## Running the tests

Upstream's `.gitignore` excludes `/tests/` and `/native/tests/`. This fork force-adds the tests that cover its fixes.

Python tests, run from the checkout root in the environment `uv sync` created:

```powershell
uv run python -m unittest discover -s tests
```

With the installer's runtime instead (it ignores `PYTHONPATH`; the tests put the checkout first on `sys.path` themselves):

```powershell
& "C:\Program Files\3dsmax-mcp\runtime\python.exe" -m unittest discover -s tests
```

Native tests. They don't need the Max SDK; they cover the executor, the health bookkeeping, the dialog monitor's rules, the #12 quiet-mode check and the native error codes:

```powershell
cmake -S native/tests -B native/build-tests -G "Visual Studio 17 2022" -A x64
cmake --build native/build-tests --config Release
ctest --test-dir native/build-tests -C Release --output-on-failure
```

## Branches

| Branch | Contents |
|---|---|
| `master` | Upstream's `master`, unchanged |
| `fix/fork-fixes` | This line, and the fork's default branch: upstream 1.7.5 plus `29aed53`, every fork fix, the fixes to 1.7.5's own dialog handling, and the Vantage, RailClone and Forest Pack tools. It runs in production on Max 2027; the Status columns above say what is tested live. Until 2026-10-11 it was the 1.7.3-based line (up to `1d80d35`), which ran on Max 2026 until 2026-10-08; that history is still in it. |
| `merge-1.7.5` | Where the 1.7.5 merge was reviewed. Now the same commit as `fix/fork-fixes`. |
| `feat/railclone-tools`, `feat/forest-tools` | Where the RailClone and Forest Pack tools started, on the 1.7.3-based line. Both are merged, with every later fix. |
| `fork/integration` | Staging for fixes under review, fast-forwarded into `fix/fork-fixes` after review. Last updated on 2026-10-01. |

Since 2026-10-11 the production machine (3ds Max 2027.2) runs this branch's Python straight from a checkout, with the fork's 2027 bridge: built from `18611d7` (sha256 `48c186df…`) at first, and since 13:39 the same day from `6397f1f` (sha256 `ca5e16dd…`, the one in `native/bin/`). Until 2026-10-08 it ran Max 2026 with the 1.7.3-based fork: Python from `fix/fork-fixes` (up to `555c19a`) and a 2026 bridge built from `8330c2c` (sha256 `1cf0f9d8…`). In between, it ran upstream's stock 1.7.5.

Pull requests to upstream will be cut from upstream's `master`, one per fix, so each can be reviewed on its own.

## Credits

- **Upstream project:** [cl0nazepamm](https://github.com/cl0nazepamm).
- **Executor shutdown drain:** ported from [Geddart](https://github.com/Geddart/3dsmax-mcp)'s fork (commit `8e004f7`). Its late-execution guard (`599e6f7`) was ported too, and replaced by upstream 1.7.5's equivalent in the merge. The process-exit check in the hung-Max diagnosis comes from the same fork (`_process_alive` there, rewritten here).
- **Pipe-lock timeout:** the idea comes from [stoxsss111](https://github.com/stoxsss111/3dsmax-mcp)'s fork.
- **Exiting orphaned servers:** the idea comes from [kanzaka110](https://github.com/kanzaka110/3dsmax-mcp)'s fork (an auto-shutdown watchdog). It was re-targeted here to follow the client process rather than Max.
- **`material_roles`:** came from [EdgecraftStudio](https://github.com/EdgecraftStudio/3dsmax-mcp) (upstream PR 29). The Autodesk Bitmap fix builds on it.
