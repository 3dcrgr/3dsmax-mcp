# About this fork

This is a fork of [cl0nazepamm/3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp), based on upstream 1.7.5. It carries fixes for problems found while using the MCP on production architectural visualization work:
- 3ds Max 2026;
- V-Ray 7 (GPU production renderer);
- Chaos Cosmos;
- scenes linked from Revit, with Hebrew material and object names.

The worst of these problems hung Max and lost unsaved work. Everything here is meant to go back upstream as pull requests. Until then, this fork is where the fixes live.

## What upstream 1.7.5 covers

Upstream 1.7.5 added handling for dialogs that block Max: a call stuck behind one returns `BLOCKED_BY_DIALOG`, and `max_dialogs` reads and answers it. It also runs agent MAXScript in Max's quiet mode, so prompts take their default answer.

Quiet mode may also answer Max's "save changes?" prompt on its own, which would lose unsaved work (fork issue #12). That isn't tested yet. Until it is, run file and session commands such as `resetMaxFile`, `loadMaxFile`, `fetchMaxFile` and `quitMax` with `execute_maxscript(quiet=False)`, so the prompt reaches the user, or pass `#noPrompt` only when losing the changes is intended.

Two of the fork's fixes are now in upstream too, so the merge kept upstream's code for them and dropped the fork's:
- Failed agent scripts no longer print errors in the user's Listener (fork issue #8, see [smaller-fixes.md](fork/smaller-fixes.md#failed-agent-scripts-printed-errors-in-the-users-listener)).
- A request that timed out in the queue no longer runs later. That was half of the fork's executor fix; the other half, the shutdown drain, is still the fork's.

Everything in the table below is fork-only, including the fixes to 1.7.5's own dialog handling.

## What's fixed

| Problem | Fix | Details | Status |
|---|---|---|---|
| A Cosmos import stalls Max for minutes, or a later window close such as `MatEditor.Close()` deadlocks it | Open the Cosmos browser on Max's main thread first; settle from the OS; refuse calls with `IMPORT_SETTLING` while Max stays busy; never open or close the Material Editor | [cosmos-import.md](fork/cosmos-import.md) | Verified live, both with the browser already open and with the import opening it on a freshly started Max |
| The import polls Max with a full class scan every 250 ms | Light, read-only probes with backoff | [cosmos-import.md](fork/cosmos-import.md) | Verified live |
| `cosmos_search` needs Max's main thread | Gets the PID without a bridge call, and the renderer too when only one Cosmos importer matches (with both V-Ray and Corona installed, pass `renderer="vray"` or `"corona"`) | [cosmos-import.md](fork/cosmos-import.md) | Verified live with both importers installed, so it read `renderers.current` once; the path without a bridge call is unit tested |
| A hung Max gives every tool a bare timeout | Lock timeout, real reply deadline, process diagnosis, `MAX_BUSY` / `MAX_NOT_RESPONDING` | [hung-max.md](fork/hung-max.md) | Verified live: `MAX_NOT_RESPONDING` with a 20 s `sleep` from another process standing in for a hang, and `MAX_BUSY` during an 8 s one; the lock timeout is unit tested |
| `get_bridge_status` can't tell what holds the main thread | Native `health` command served from a pipe thread | [hung-max.md](fork/hung-max.md#bridge-health-from-a-pipe-thread-2ce5928-reviewed-in-011e547) | Verified live, idle and during an 8 s `sleep` from another process; the 20 s `not_responding` path isn't tested live yet |
| No way to see *where* Max is stuck | `capture_hang_diagnostics`: native stacks via dbghelp, read from the OS | [hung-max.md](fork/hung-max.md#capture_hang_diagnostics) | Reviewed and deployed; tested on spawned processes, not yet on a hung Max |
| Max's exit stalls while MCP requests are queued | Executor shutdown drain (from Geddart's fork): queued work fails at once when Max exits | [hung-max.md](fork/hung-max.md#native-executor-adcfd93-ported-from-geddarts-fork) | Deployed; the bridge loaded and served calls live, but quitting Max with a call queued isn't tested live yet |
| Crashed clients leave `maxmcp.server` processes running, possibly still connected to Max | The server exits with its client; the bridge reports connected clients | [hung-max.md](fork/hung-max.md#orphaned-servers-6-a9d2a0d) | Client count verified live in `health`; the server exiting with its client isn't tested live yet |
| `replace_material` "replaces" nothing and reports success | Direction documented, `no_match` warning, sub-material slots matched, sources found outside the scene; batch entries apply in order (side effect: two entries meant to swap two materials no longer do) | [replace-material.md](fork/replace-material.md) | Direction, `no_match` and a Multi/Sub slot replaced from a Material Editor source verified live; nested Multi/Sub, the loop guard and batch aren't tested live yet |
| Revit (Autodesk Bitmap) texture paths read as empty | Read bitmap-asset parameters; flag unreadable paths | [autodesk-bitmap-paths.md](fork/autodesk-bitmap-paths.md) | Verified live, including the duplicate VRayBitmap row fix |
| `execute_maxscript` calls an interrupted script (e.g. `quitMax`) a "parse error" | Re-compile to tell syntax errors from interruptions; new code `MAXSCRIPT_INTERRUPTED` | [smaller-fixes.md](fork/smaller-fixes.md) | Syntax errors verified live; `MAXSCRIPT_INTERRUPTED` (e.g. `quitMax`) isn't tested live yet |
| Curve tools reject Line objects | Line counts as an editable spline | [smaller-fixes.md](fork/smaller-fixes.md#curve-tools-rejected-line-objects) | Verified live |
| A Cosmos material whose package name differs from the asset name isn't found | Detect what the import created by handle, whatever its name; report both names | [cosmos-import.md](fork/cosmos-import.md#finding-what-the-import-created-9-2853b1c) | Verified live; the follow-up that keeps other plugins' new items apart is deployed, retest pending |
| A Cosmos import silently replaces the material in the user's active Material Editor slot | Point the importer at a free slot, then put any displaced material back once Max is quiet | [cosmos-import.md](fork/cosmos-import.md#keeping-the-users-material-editor-slot-10-c1e09a7) | Partly verified live (the material was kept, but V-Ray's default slots weren't seen as free); the follow-up is deployed, retest pending |
| After a restart and Hold/Fetch, the agent viewport can't be reclaimed | Tag the agent's floating viewport in the scene and reclaim exactly that restored window | [smaller-fixes.md](fork/smaller-fixes.md#the-agent-viewport-couldnt-be-reclaimed-after-a-restart) | Deployed (`1cf0f9d8…`); the first live test was inconclusive, because the fetched Hold predated the tag. Also in the 2026 bridge built from the 1.7.5 merge (`e275e129…`) |
| Upstream 1.7.5's dialog monitor can report the Cosmos browser, the Material Editor or the agent viewport as a blocking dialog, and reads or presses dialogs of any thread | These windows are never dialogs; dialogs of other threads (e.g. the Cosmos importer's) are listed but never read or pressed; Qt dialogs are only read while Max's main thread pumps | [cosmos-import.md](fork/cosmos-import.md#windows-that-are-never-dialogs-6e03f99) | Unit tested; in the 2026 bridge (sha256 `e275e129…`); not tested live yet |
| Upstream 1.7.5's dialog handling calls a hung Max "blocked by a dialog", can commit a call before its acknowledged error is recorded, can click after reporting a failed press, and can leave Max in quiet mode | A main thread that stopped pumping is a hang; errors publish before the call resumes; timed-out presses are withdrawn or reported `DIALOG_OUTCOME_UNKNOWN`; quiet mode only changes on the main thread; `cosmos_import` handles `MAX_DIALOG_ERROR` and dialogs before dispatch | [hung-max.md](fork/hung-max.md#upstream-175s-dialog-handling-fixed-on-the-merge) | Unit tested; in the 2026 bridge (sha256 `e275e129…`); not tested live yet |

What the status words mean:
- **Verified live:** the fix was exercised in a real Max 2026 session on the production machine, on 2026-10-01, with the 1.7.3-based fork. Some checks used a stand-in for the original failure, such as a 20 s `sleep` instead of a real hang.
- **Reviewed:** an adversarial code review was run before deployment.
- **Deployed:** the fix runs on the production machine, which runs the 1.7.3-based fork. The 2026 bridge there is sha256 `1cf0f9d8…`.

Nothing from the 1.7.5 merge has run in Max yet, Python or native. That includes the 2026 bridge now in `native/bin/` (sha256 `e275e129…`).

The details pages say exactly what was and wasn't tested.

## Installing

Install from source, the same way as upstream, but from this fork. Its default branch is `fix/fork-fixes`, so a plain clone already has the fixes:

```powershell
git clone https://github.com/3dcrgr/3dsmax-mcp.git
cd 3dsmax-mcp
uv sync
uv run python install.py
```

Close 3ds Max and your AI clients first, as upstream's README says.

After updating, start a new client session. MCP clients cache the tool list, so new tools such as `capture_hang_diagnostics` don't appear in sessions that were already open, even after the server restarts.

**The native bridge:** `install.py` copies the prebuilt bridges from `native/bin/`.
- In this fork only `mcp_bridge_2026.gup` is rebuilt with the fixes, because only the Max 2026 SDK was available. It's built from `f70b0cc` on this branch, upstream 1.7.5 plus the fork (sha256 `e275e12905ed9745…`). The production machine runs a 1.7.3-based fork build (sha256 `1cf0f9d80486d913…`).
- The 2023, 2024, 2025 and 2027 bridges are still upstream's builds (1.7.5). With those, the Python-side fixes work, but the native ones are missing: `health`, the executor shutdown drain, Autodesk Bitmap paths and the duplicate VRayBitmap row fix, `MAXSCRIPT_INTERRUPTED`, `replace_material`'s sub-material matching and source search, the agent viewport reclaim (#11), the dialog monitor's rules for Cosmos, Material Editor and other-thread windows, and the native half of the 1.7.5 dialog fixes (publishing errors before the call resumes, withdrawn presses, quiet mode on the main thread only, `main_thread_pumping`, the Qt error-box retry, the agent viewport's render cleanup after an error box).
- To get them for another Max version, build the bridge yourself (below).

## Building the native bridge

You need:
- Visual Studio 2022 with the v143 toolset version **14.38** (the toolset Autodesk uses for Max 2026);
- CMake 3.20+;
- Python 3, which the build runs to generate a tool registry;
- the 3ds Max SDK for your version at its default path, or pass `-DMAXSDK_PATH=<path to maxsdk>`.

```powershell
cmake -S native -B build-2026 -G "Visual Studio 17 2022" -A x64 -T "v143,version=14.38" -DMAX_VERSION=2026
cmake --build build-2026 --config Release --target mcp_bridge
```

For another Max version, change `-DMAX_VERSION`, the build folder and the toolset to match that SDK, and use that year in the file names below.

The output is `build-2026\Release\mcp_bridge.gup`. To install it, close Max and copy it over `C:\ProgramData\Autodesk\ApplicationPlugins\3dsmax-mcp\Contents\bin\mcp_bridge_2026.gup`; that may need an elevated prompt. Keep a copy of the original. Re-running `install.py` replaces that whole folder from `native/bin/`, so also copy your build to `native\bin\mcp_bridge_2026.gup`, or the next install puts the committed bridge back.

The build generates a tool registry from `maxmcp/tools/*.py`. To keep uncommitted Python edits out of a binary, build from a clean snapshot of HEAD. Write the archive to a file rather than piping it into `tar`: Windows PowerShell 5.1 corrupts binary data piped between programs.

```powershell
New-Item -ItemType Directory -Force C:\temp\mcp-build | Out-Null
git archive HEAD --prefix=src/ -o C:\temp\mcp-build\src.tar
tar -xf C:\temp\mcp-build\src.tar -C C:\temp\mcp-build
cmake -S C:\temp\mcp-build\src\native -B C:\temp\mcp-build\build-2026 -G "Visual Studio 17 2022" -A x64 -T "v143,version=14.38" -DMAX_VERSION=2026
cmake --build C:\temp\mcp-build\build-2026 --config Release --target mcp_bridge
```

The output is then `C:\temp\mcp-build\build-2026\Release\mcp_bridge.gup`.

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

Native tests. They don't need the Max SDK; they cover the executor, the health bookkeeping and the dialog monitor's rules:

```powershell
cmake -S native/tests -B native/build-tests -G "Visual Studio 17 2022" -A x64
cmake --build native/build-tests --config Release
ctest --test-dir native/build-tests -C Release --output-on-failure
```

## Branches

| Branch | Contents |
|---|---|
| `master` | Upstream's `master`, unchanged |
| `fix/fork-fixes` | Upstream 1.7.5 plus the reviewed fixes. The merge and the fixes to 1.7.5's dialog handling aren't deployed or tested live yet; the Status column above says what is. |
| `fork/integration` | `fix/fork-fixes` plus the newest fixes and these docs while they're under review. It's fast-forwarded into `fix/fork-fixes` after review. |

The production machine still runs the 1.7.3-based fork: commit `928c940`, the last `fix/fork-fixes` before the merge, with a 2026 bridge built from `8330c2c` (sha256 `1cf0f9d8…`).

Pull requests to upstream will be cut from upstream's `master`, one per fix, so each can be reviewed on its own.

## Credits

- **Upstream project:** [cl0nazepamm](https://github.com/cl0nazepamm).
- **Executor shutdown drain:** ported from [Geddart](https://github.com/Geddart/3dsmax-mcp)'s fork (commit `8e004f7`). Its late-execution guard (`599e6f7`) was ported too, and replaced by upstream 1.7.5's equivalent in the merge. The process-exit check in the hung-Max diagnosis comes from the same fork (`_process_alive` there, rewritten here).
- **Pipe-lock timeout:** the idea comes from [stoxsss111](https://github.com/stoxsss111/3dsmax-mcp)'s fork.
- **Exiting orphaned servers:** the idea comes from [kanzaka110](https://github.com/kanzaka110/3dsmax-mcp)'s fork (an auto-shutdown watchdog). It was re-targeted here to follow the client process rather than Max.
- **`material_roles`:** came from [EdgecraftStudio](https://github.com/EdgecraftStudio/3dsmax-mcp) (upstream PR 29). The Autodesk Bitmap fix builds on it.
