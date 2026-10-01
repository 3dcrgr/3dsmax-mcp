# Cosmos imports that stall or deadlock 3ds Max

Fork issues #1, #2 and #7. Commits `530bc3e` and `7ff1cec`.

## What happened

On a V-Ray 7 GPU scene, `cosmos_import` reported success, and then the next call that edited the scene hung 3ds Max. Max stopped responding but used almost no CPU. This happened in about two of five imports. The first sessions were killed after a minute or two, which lost unsaved work. From the agent's side, every later tool call simply timed out.

## Why

Native stack dumps showed two separate problems. Both are inside Chaos code, and the MCP made both worse. (The dumps were taken with the stack walker that this fork now bundles as [`capture_hang_diagnostics`](hung-max.md#capture_hang_diagnostics).)

1. **The Material Editor preview render.** The Chaos importer opens the Compact Material Editor and puts the new material in the active slot. The editor (`mtl.dll`) then asks the renderer for a sample-slot preview. With V-Ray as the Material Editor renderer, that call blocked Max's main thread inside `vray.dll` for 5–8 minutes on the test machine.
   - That machine has no local Chaos license server, and the Chaos networking threads were waiting on sockets. So the likely cause is an online license round trip, but this wasn't confirmed.
2. **A cross-thread window deadlock.** If no Cosmos browser is open, the importer (`galaxyimporter2026.dll`) creates a hidden "Chaos Cosmos Browser" window on its own thread. After an import, that thread waits on a condition variable.
   - Any main-thread call that makes Windows send a synchronous message to that window then blocks forever. Closing or activating a window does this; `MatEditor.Close()` is one example.
   - This one never recovered: Max was killed after 13 minutes.

If the Cosmos browser is opened through its V-Ray or Corona action *first*, its window belongs to Max's main thread, and neither problem occurs. In about 40 imports done that way, each import took about 10 s of real work. Assignments, undo holds and `MatEditor.Close()` all worked afterwards.

The MCP made both problems worse in three ways:
- `_wait_import` returned as soon as a new material handle appeared, while the importer still had work queued.
- It polled with a full class-instance scan on the main thread every 250 ms (#2).
- The client then blocked on its pipe lock, so the agent saw bare timeouts with no diagnosis (see [hung-max.md](hung-max.md)).

## What changed

`cosmos_import` now runs these steps:

1. **Make sure a main-thread Cosmos browser exists.** This check is done from the OS, with no bridge call.
   - If there's no responsive browser window owned by Max's main thread, it runs the renderer's "Cosmos browser" action. The action is looked up by name in the action tables, never by index. It then waits up to 15 s for the window.
   - If a browser on another thread is already hung, it refuses and sends nothing. The result is `state: "browser_hung"` with code `IMPORT_SETTLING`.
2. **Prepare.** One bridge call takes a scene snapshot and clears the selection, remembering it first. The native importer otherwise auto-assigns the new material to whatever is selected.
3. **Dispatch the import**, then poll lightly for the result (#2). The polls are read-only and back off. They're also probes: they're never sent into a main window that's already hung, and they're dropped at their deadline.
4. **Wait for Max to settle**, judged from the OS. The main window and every Cosmos browser window, hidden ones included, must answer for a streak longer than 6 s. (`IsHungAppWindow` only reports a window as hung after about 5 s.)
5. **Restore the selection.** It never calls `MatEditor.Close()` or `MatEditor.Open()`.
6. **If Max is still busy after `settle_seconds`**, the result says `safe_to_edit: false` and includes a `pending_restore` script to run later.
   - It also arms an `IMPORT_SETTLING` guard. Until that Max's windows pump messages again, further calls to it fail fast and aren't sent. The guard expires after 15 minutes at most.

New parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `swap_medit_renderer` | `false` | Use Default Scanline as the Material Editor renderer during the import. This is turned on automatically when the browser can't be opened. |
| `restore_medit_renderer` | `true` | Put the original Material Editor renderer back once Max is idle and the editor is closed. |
| `settle_seconds` | `90` (0–300, at least 8 used) | How long to wait for Max to settle before returning `safe_to_edit: false`. |

New result fields:
- `cosmos_browser`: `ensured`, `opened`, `windows`, `warning`.
- `medit_renderer.swap`: `off`, `requested` or `fallback`.
- `safe_to_edit`, `next`, `pending_restore`.

`cosmos_search` (#7) no longer needs Max's main thread:
- The target PID comes from the selected instance, with no bridge call.
- When exactly one Cosmos importer is registered for that PID, the renderer follows from the importer. So a search works even while Max is busy.
- With both V-Ray and Corona installed, it still reads `renderers.current` once.

## How it was verified

Live on 3ds Max 2026 with V-Ray 7 update 4 hotfix 2 (GPU production renderer), on 2026-10-01:

- **Action lookup:** found both `V-Ray | Chaos Cosmos browser` and `Chaos Corona | Corona Open Cosmos Browser`.
- **Import with default settings:**
  - The browser was ensured (it already existed on the main thread), and the Material Editor renderer wasn't swapped.
  - It returned `imported` and `safe_to_edit: true` after about 20 s.
  - The user's selection came back, and the material wasn't auto-assigned to it.
- **`cosmos_search`:** answered with `renderer_source: "scene"` (two importers are installed on that machine).
- **Not yet run:** an import on a freshly started Max with the browser closed, which exercises the "open the browser" path.

Unit tests are in `tests/test_cosmos_import.py`. They use fake windows to cover a hung browser, a missing browser, settle streaks, the guard expiring, and the Scanline fallback.

## If an import still hangs

- **Wait, and check with `get_bridge_status`.** It now answers while Max is hung (see [hung-max.md](hung-max.md)).
  - Preview-render stalls cleared on their own in 5–8 minutes.
  - The cross-thread deadlock never cleared. If Max is still blocked after about 10 minutes, treat it as a deadlock.
- **Run `capture_hang_diagnostics`** and keep the file it saves. It tells the two cases apart:
  - Main thread inside `vray.dll`, called from `mtl.dll`: that's the preview render.
  - Main thread in a USER32 call while a "Chaos Cosmos Browser" thread waits in `galaxyimporter2026.dll`: that's the window deadlock.
- **Don't open or close the Material Editor** while Max is in this state.

## Upstream status

Not fixed upstream as of 2026-10-01. None of the 47 forks touches `cosmos.py`.
