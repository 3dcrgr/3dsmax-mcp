# About this fork

This is a fork of [cl0nazepamm/3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp). It carries fixes for problems found while using the MCP on production architectural visualization work:
- 3ds Max 2026;
- V-Ray 7 (GPU production renderer);
- Chaos Cosmos;
- scenes linked from Revit, with Hebrew material and object names.

The worst of these problems hung Max and lost unsaved work. Everything here is meant to go back upstream as pull requests. Until then, this fork is where the fixes live.

## What's fixed

| Problem | Fix | Details | Status |
|---|---|---|---|
| A Cosmos import stalls Max for minutes, or a later window close such as `MatEditor.Close()` deadlocks it | Open the Cosmos browser on Max's main thread first; settle from the OS; refuse calls with `IMPORT_SETTLING` while Max stays busy; never open or close the Material Editor | [cosmos-import.md](fork/cosmos-import.md) | Verified live with the browser already on the main thread; the step that opens it isn't tested live yet |
| The import polls Max with a full class scan every 250 ms | Light, read-only probes with backoff | [cosmos-import.md](fork/cosmos-import.md) | Verified live |
| `cosmos_search` needs Max's main thread | Gets the PID without a bridge call, and the renderer too when only one Cosmos importer matches (with both V-Ray and Corona installed, pass `renderer="vray"` or `"corona"`) | [cosmos-import.md](fork/cosmos-import.md) | Verified live with both importers installed, so it read `renderers.current` once; the path without a bridge call is unit tested |
| A hung Max gives every tool a bare timeout | Lock timeout, real reply deadline, process diagnosis, `MAX_BUSY` / `MAX_NOT_RESPONDING` | [hung-max.md](fork/hung-max.md) | Verified live: `MAX_NOT_RESPONDING` with a 20 s `sleep` from another process standing in for a hang, and `MAX_BUSY` during an 8 s one; the lock timeout is unit tested |
| `get_bridge_status` can't tell what holds the main thread | Native `health` command served from a pipe thread | [hung-max.md](fork/hung-max.md#bridge-health-from-a-pipe-thread-2ce5928-reviewed-in-011e547) | Verified live, idle and during an 8 s `sleep` from another process; the 20 s `not_responding` path isn't tested live yet |
| No way to see *where* Max is stuck | `capture_hang_diagnostics`: native stacks via dbghelp, read from the OS | [hung-max.md](fork/hung-max.md#capture_hang_diagnostics) | Reviewed and deployed; tested on spawned processes, not yet on a hung Max |
| Max's exit stalls while MCP requests are queued; an expired request can run later | Executor shutdown drain and late-execution guard (from Geddart's fork) | [hung-max.md](fork/hung-max.md#native-executor-adcfd93-ported-from-geddarts-fork) | Verified live (loading and calls; exit-with-queued-call not yet exercised) |
| Crashed clients leave `maxmcp.server` processes running, possibly still connected to Max | The server exits with its client; the bridge reports connected clients | [hung-max.md](fork/hung-max.md#orphaned-servers-6-a9d2a0d) | Client count verified live in `health`; the server exiting with its client isn't tested live yet |
| `replace_material` "replaces" nothing and reports success | Direction documented, `no_match` warning, sub-material slots matched, sources found outside the scene; batch entries apply in order (side effect: two entries meant to swap two materials no longer do) | [replace-material.md](fork/replace-material.md) | Direction, `no_match` and a Multi/Sub slot replaced from a Material Editor source verified live; nested Multi/Sub, the loop guard and batch aren't tested live yet |
| Revit (Autodesk Bitmap) texture paths read as empty | Read bitmap-asset parameters; flag unreadable paths | [autodesk-bitmap-paths.md](fork/autodesk-bitmap-paths.md) | Verified live, including the duplicate VRayBitmap row fix |
| `execute_maxscript` calls an interrupted script (e.g. `quitMax`) a "parse error" | Re-compile to tell syntax errors from interruptions; new code `MAXSCRIPT_INTERRUPTED` | [smaller-fixes.md](fork/smaller-fixes.md) | Syntax errors verified live; `MAXSCRIPT_INTERRUPTED` (e.g. `quitMax`) isn't tested live yet |
| Curve tools reject Line objects | Line counts as an editable spline | [smaller-fixes.md](fork/smaller-fixes.md#curve-tools-rejected-line-objects) | Verified live |

What the status words mean:
- **Verified live:** the fix was exercised in a real Max 2026 session on the production machine, on 2026-10-01. Some checks used a stand-in for the original failure, such as a 20 s `sleep` instead of a real hang.
- **Reviewed:** an adversarial code review was run before deployment.
- **Deployed:** the fix runs on the production machine. Native fixes are in the rebuilt 2026 bridge (sha256 `5f49226e…`).

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
- In this fork only `mcp_bridge_2026.gup` is rebuilt with the fixes, because only the Max 2026 SDK was available. It's the same build that's deployed on the production machine (sha256 `5f49226e2341e5ad…`).
- The 2023, 2024, 2025 and 2027 bridges are still upstream's builds. With those, the Python-side fixes work, but the native ones are missing: `health`, the executor fixes, Autodesk Bitmap paths and the duplicate VRayBitmap row fix, `MAXSCRIPT_INTERRUPTED`, and `replace_material`'s sub-material matching and source search.
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

Native tests. They don't need the Max SDK; they cover the executor and the health bookkeeping:

```powershell
cmake -S native/tests -B native/build-tests -G "Visual Studio 17 2022" -A x64
cmake --build native/build-tests --config Release
ctest --test-dir native/build-tests -C Release --output-on-failure
```

## Branches

| Branch | Contents |
|---|---|
| `master` | Upstream's `master`, unchanged |
| `fix/fork-fixes` | The reviewed fixes, all deployed on the production machine. The Status column above says which are verified live. |
| `fork/integration` | `fix/fork-fixes` plus the newest fixes and these docs while they're under review. It's fast-forwarded into `fix/fork-fixes` after review. |

Pull requests to upstream will be cut from upstream's `master`, one per fix, so each can be reviewed on its own.

## Credits

- **Upstream project:** [cl0nazepamm](https://github.com/cl0nazepamm).
- **Executor shutdown drain and late-execution guard:** ported from [Geddart](https://github.com/Geddart/3dsmax-mcp)'s fork (commits `8e004f7` and `599e6f7`). The process-exit check in the hung-Max diagnosis comes from the same fork (`_process_alive` there, rewritten here).
- **Pipe-lock timeout:** the idea comes from [stoxsss111](https://github.com/stoxsss111/3dsmax-mcp)'s fork.
- **Exiting orphaned servers:** the idea comes from [kanzaka110](https://github.com/kanzaka110/3dsmax-mcp)'s fork (an auto-shutdown watchdog). It was re-targeted here to follow the client process rather than Max.
- **`material_roles`:** came from [EdgecraftStudio](https://github.com/EdgecraftStudio/3dsmax-mcp) (upstream PR 29). The Autodesk Bitmap fix builds on it.
