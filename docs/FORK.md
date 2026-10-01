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
| A Cosmos import stalls Max for minutes, or deadlocks it, on the next edit | Open the Cosmos browser on Max's main thread first; settle from the OS; never touch the Material Editor | [cosmos-import.md](fork/cosmos-import.md) | Verified live |
| The import polls Max with a full class scan every 250 ms | Light, read-only probes with backoff | [cosmos-import.md](fork/cosmos-import.md) | Verified live |
| `cosmos_search` needs Max's main thread | Gets the PID and renderer without a bridge call | [cosmos-import.md](fork/cosmos-import.md) | Verified live |
| A hung Max gives every tool a bare timeout | Lock timeout, real reply deadline, process diagnosis, `MAX_BUSY` / `MAX_NOT_RESPONDING` | [hung-max.md](fork/hung-max.md) | Verified live |
| `get_bridge_status` can't tell what holds the main thread | Native `health` command served from a pipe thread | [hung-max.md](fork/hung-max.md#bridge-health-from-a-pipe-thread-f8d4e6f) | Unit tested; live test pending |
| No way to see *where* Max is stuck | `capture_hang_diagnostics`: native stacks via dbghelp, read from the OS | [hung-max.md](fork/hung-max.md#capture_hang_diagnostics) | Unit tested |
| Max's exit stalls while MCP requests are queued; an expired request can run later | Executor shutdown drain and late-execution guard (from Geddart's fork) | [hung-max.md](fork/hung-max.md#native-executor-25ac1ed-ported-from-geddarts-fork) | Verified live (loading and calls; exit-with-queued-call not yet exercised) |
| Crashed clients leave `maxmcp.server` processes connected to Max | The server exits with its client; the bridge reports connected clients | [hung-max.md](fork/hung-max.md#orphaned-servers-6-6e9188b) | Verified live (partly) |
| `replace_material` "replaces" nothing and reports success | Direction documented, `no_match` warning, sub-material slots matched, sources found outside the scene | [replace-material.md](fork/replace-material.md) | Partly verified live |
| Revit (Autodesk Bitmap) texture paths read as empty | Read bitmap-asset parameters; flag unreadable paths | [autodesk-bitmap-paths.md](fork/autodesk-bitmap-paths.md) | Verified live |
| `execute_maxscript` calls an interrupted script (e.g. `quitMax`) a "parse error" | Re-compile to tell syntax errors from interruptions; new code `MAXSCRIPT_INTERRUPTED` | [smaller-fixes.md](fork/smaller-fixes.md) | Unit tested |
| Curve tools reject Line objects | Line counts as an editable spline | [smaller-fixes.md](fork/smaller-fixes.md#curve-tools-rejected-line-objects) | Unit tested |

"Verified live" means the fix was exercised in a real Max 2026 session, against the scene that showed the problem, on 2026-10-01. The details pages say exactly what was and wasn't tested.

## Installing

Install from source, the same way as upstream, but from this fork:

```powershell
git clone https://github.com/3dcrgr/3dsmax-mcp.git
cd 3dsmax-mcp
git checkout fix/fork-fixes
uv sync
uv run python install.py
```

Close 3ds Max and your AI clients first, as upstream's README says.

**The native bridge:** `install.py` copies the prebuilt bridges from `native/bin/`.
- In this fork only `mcp_bridge_2026.gup` is rebuilt with the fixes, because only the Max 2026 SDK was available.
- The 2023, 2024, 2025 and 2027 bridges are still upstream's builds. With those, the Python-side fixes work, but the native ones (`health`, sub-material matching, Autodesk Bitmap paths, the executor fixes) are missing.
- To get them for another Max version, build the bridge yourself (below).

## Building the native bridge

You need:
- Visual Studio 2022 with the v143 toolset version **14.38** (the toolset Autodesk uses for Max 2026);
- CMake 3.20+;
- the 3ds Max SDK for your version at its default path.

```powershell
cmake -S native -B build-2026 -G "Visual Studio 17 2022" -A x64 -T "v143,version=14.38" -DMAX_VERSION=2026
cmake --build build-2026 --config Release --target mcp_bridge
```

The output is `build-2026\Release\mcp_bridge.gup`. To install it, close Max and copy it over `C:\ProgramData\Autodesk\ApplicationPlugins\3dsmax-mcp\Contents\bin\mcp_bridge_2026.gup`. Keep a copy of the original.

The build generates a tool registry from `maxmcp/tools/*.py`. To keep uncommitted Python edits out of a binary, build from a clean snapshot:

```powershell
git archive HEAD --prefix=src/ | tar -x -C C:\temp\mcp-build
```

## Running the tests

Upstream's `.gitignore` excludes `/tests/` and `/native/tests/`. This fork force-adds the tests that cover its fixes.

Python tests, run with the installed runtime from the checkout root:

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
| `fix/fork-fixes` | All the fixes, integrated and deployed together |
| `fork/*` | One fix each, based on `fix/fork-fixes`, kept separate so each can become its own upstream pull request |

## Credits

- **Upstream project:** [cl0nazepamm](https://github.com/cl0nazepamm).
- **Executor shutdown drain and late-execution guard:** ported from [Geddart](https://github.com/Geddart/3dsmax-mcp)'s fork (commits `8e004f7` and `599e6f7`).
- **Pipe-lock timeout:** the idea comes from [stoxsss111](https://github.com/stoxsss111/3dsmax-mcp)'s fork.
- **Exiting orphaned servers:** the idea comes from [kanzaka110](https://github.com/kanzaka110/3dsmax-mcp)'s fork (an auto-shutdown watchdog). It was re-targeted here to follow the client process rather than Max.
- **`material_roles`:** came from [EdgecraftStudio](https://github.com/EdgecraftStudio/3dsmax-mcp) (upstream PR 29). The Autodesk Bitmap fix builds on it.
