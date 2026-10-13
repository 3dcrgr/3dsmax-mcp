# 3dsmax-mcp 1.7.5-fork.1

Release notes for the [3dcrgr fork](https://github.com/3dcrgr/3dsmax-mcp) of [cl0nazepamm/3dsmax-mcp](https://github.com/cl0nazepamm/3dsmax-mcp). It's upstream **1.7.5** plus fixes for problems found in production architectural visualization, and 28 new tools (207 total).

The default branch contains verified deployed source updates through #34. A replacement Setup with reviewed bridge packaging has compiled and passed offline checks; its installation and release publication are pending. An older downloaded installer does not gain these guards from a source update. See [deployment details](docs/fork/hung-max.md) and [changelog](docs/CHANGELOG.md).

**Tested in production on:**
- 3ds Max 2027.2, and 3ds Max 2026 before that;
- V-Ray 7 (GPU);
- Chaos Cosmos and Chaos Vantage 3.4;
- RailClone 7.3.5 and Forest Pack 9.4.3.

## Install

1. Close 3ds Max and your AI clients.
2. Run `3dsmax-mcp-1.7.5-fork.1-Setup.exe` and pick your clients. It replaces an upstream install: same folder, same bridge bundle, same client setup.
3. Start 3ds Max, then start a **new** client session. Clients cache the tool list.

The installer isn't code-signed, so Windows SmartScreen may warn ("More info" → "Run anyway"). Check the download against the `.sha256` file next to it. Upstream's installer can replace this one again at any time.

From source: `git clone https://github.com/3dcrgr/3dsmax-mcp.git`, `uv sync`, `uv run python install.py`. The wheel and sdist are attached too.

## What it fixes

**When 3ds Max hangs or is busy**
1. A hung Max gives `MAX_BUSY` / `MAX_NOT_RESPONDING` with a process diagnosis instead of a bare timeout. A request that may already have run is never replayed.
2. `get_bridge_status` answers from a pipe thread while Max's main thread is blocked, and says what holds it.
3. Max no longer stalls on exit when MCP requests are queued.
4. An MCP server whose client crashed exits with it, instead of staying connected to Max.

**Dialogs and unsaved work**

5. Quiet mode never answers a "Save changes?" prompt by itself. Scripts that reset, open, fetch or quit run with quiet mode off, and the prompt reaches the agent as `BLOCKED_BY_DIALOG`.
6. Fixes to 1.7.5's dialog handling:
   - A hung main thread is reported as a hang, not as a dialog.
   - The Cosmos browser, the Material Editor and the agent viewport are never treated as dialogs.
   - Dialogs of other threads are never pressed.
   - Quiet mode only changes on the main thread.
7. A script interrupted by `quitMax` is `MAXSCRIPT_INTERRUPTED`, not a parse error.

**Several sessions or several 3ds Max instances**

8. Every tool takes an optional `expected_pid`. The call is refused, with nothing sent, if it would reach another Max. Replies carry `selection_generation`.
9. A default target whose Max exited re-resolves when the choice is unambiguous. With several Max instances running it refuses instead of guessing.

**Chaos Cosmos**

10. `cosmos_import`:
    - no longer stalls or deadlocks Max, and polls lightly;
    - finds what it created by handle;
    - keeps the active Material Editor slot;
    - minimizes the browser it opened.

**Materials**

11. `replace_material` matches Multi/Sub slots, finds sources outside the scene and warns on `no_match`. Batch entries apply in order.
12. `material_roles` reads Revit (Autodesk Bitmap) texture paths and flags unreadable ones.

**Modeling, scene and viewport**

13. The curve tools accept Line objects.
14. `contact_check` takes thresholds in millimetres, skips non-mesh nodes and has a time budget for dense meshes.
15. Cloning a closed-group member clones only that member. `scene_qa` flags stacked duplicate groups.
16. `agent_viewport` reclaims its own viewport after a restart, and its V-Ray previews work with **V-Ray GPU**.

**RailClone and Forest Pack (the existing tools)**

17. RailClone styles with repeated attributes load. Output is rebuilt before it's read. Reads never re-capture segment geometry.
18. `scatter_forest_pack`:
    - density works through the tile size;
    - "up" facing is right;
    - it returns the real item count;
    - it never writes the crashing `geomlist` 3.

## What it adds (17 tools)

- `capture_hang_diagnostics`: native stacks of a hung Max.
- `resting_check`, `drop_to_surface`: find props floating above or sunk into the surface below them, and drop them onto it.
- `vantage_status`, `vantage_get_image`, `vantage_live_link`: Chaos Vantage's state, its current frame as a PNG, and V-Ray GPU Live Link on request.
- `railclone_inspect`, `railclone_set_param`, `railclone_library`, `railclone_from_library`.
- `forest_items`, `forest_edit_items`, `forest_set_mode`, `forest_library`, `forest_from_library`, `forest_validate`.
- `itoo_vantage_check`: a read-only readiness check of Forest Pack, RailClone and ForestIvy objects for Chaos Vantage.

## What it removes or changes

Nothing of upstream's is removed: all 179 upstream tools remain. These behaviours change on purpose:
- Quiet mode is off for scripts that mention reset, open, fetch or quit.
- `batch_replace_materials` applies entries in order, so two entries meant to swap two materials no longer swap.
- RailClone reads no longer call `segmentsUpdate`; `reload_segments=true` does.
- `scatter_forest_pack`: `density` means tile size, and "up" is `direction` 100.

## Native bridges

| Max | Bridge |
|---|---|
| 2027 | Fork build with every native fix (sha256 `ca5e16dd…`) |
| 2026 | Fork build with every native fix except the V-Ray GPU preview (sha256 `d5cd6ba5…`) |
| 2023–2025 | Upstream's 1.7.5 builds: the Python-side fixes work, the native ones need a bridge built from source |

## More

- [README](https://github.com/3dcrgr/3dsmax-mcp#readme): the full tool catalog.
- [docs/FORK.md](https://github.com/3dcrgr/3dsmax-mcp/blob/fix/fork-fixes/docs/FORK.md): every change and how it was verified.
- These changes are being offered upstream as separate pull requests.
