# When 3ds Max hangs: a diagnosis instead of timeouts

Fork issues #4 and #6, and the executor shutdown fix. Commits `adcfd93`, `a9d2a0d`, `ed1fb1c`, `2ce5928`, `011e547`, `f7dbb87`, `03f4d64`, `4ba8059` and `b4dd573`.

## What happened

When Max's main thread hung, every tool returned the MCP client's generic "Request timed out", `get_bridge_status` included. The agent then had to work out from the outside whether Max was busy, hung or gone. Usually it guessed "permanent deadlock", and Max was killed with unsaved work, even though some of those stalls would have cleared on their own after a few minutes.

## Why

- **The pipe lock blocked indefinitely.** `MaxClient._send_via_pipe` held the pipe lock across a blocking `ReadFile`, and checked its deadline only between chunks. A request stuck on Max's main thread therefore never timed out on the Python side, and every later call waited on that same lock.
- **`ping` queued behind the main thread.** It goes through the main-thread executor, so even a status check waited behind a hung main thread.
- **The native executor had two gaps.**
  - Max's exit could stall for up to 120 s per in-flight request: the main thread was joining the very pipe threads that were waiting on it.
  - A request that timed out while still queued could later run anyway, against a caller stack that no longer existed.

## What changed

### Client side (`a9d2a0d`)

- **The pipe lock has a timeout.** A request waits for the lock for up to its own timeout (120 s by default). If it still can't get it, nothing is sent. It fails with `MAX_BUSY`, or with `MAX_NOT_RESPONDING` if Max has exited, or is blocked while the request holding the lock is past its own timeout plus grace (the larger of 10 s and 10 % of that timeout). The idea comes from the stoxsss111 fork.
- **Replies have a real deadline.** The caller never blocks in `ReadFile`: the fork polled with `PeekNamedPipe`; since the 1.7.5 merge, upstream's reader thread does the read and the caller waits on it. Past the deadline plus grace it checks the target process: are its top-level windows hung (`IsHungAppWindow`), and how much CPU does it use over a one-second sample? A hung window with under 0.05 CPU-s/s counts as blocked.
  - **Anything short of exited or confirmed blocked** (responsive, hung but still using CPU, or a state it can't read): the client keeps waiting and checks again every 15 s, so a slow result still arrives. There's no upper limit. If the client can't tell Max's PID, it can't check at all and just keeps waiting.
  - **Max has exited, or is blocked on two checks 20 s apart:** the request is abandoned with `MAX_NOT_RESPONDING`. It's never replayed, because it may already have run.
  - **Read-only probes** (`ping`, `health`): dropped at their deadline.
- **A hung PID is remembered.** Later calls to it fail fast, with nothing sent, until its window pumps messages again.
- **The messages give bounded advice** (`ed1fb1c`): "wait up to about 10 minutes, re-checking with `get_bridge_status`; still blocked after that means a deadlock, and the user must end Max". Errors that say Max is not responding or still settling also get a `hint` with the same 10-minute wait, suggesting `capture_hang_diagnostics` and `get_bridge_status` (`f7dbb87`, `03f4d64`).

Error codes:

| Code | Meaning | Retry |
|---|---|---|
| `MAX_BUSY` | Max (or this client's pipe) is occupied. Usually nothing was sent. A `ping`/`health` probe dropped at its deadline, or a request the bridge cancelled before it ran (`executed: false`), has `request_sent: true` but changed nothing. | Yes, later. |
| `MAX_NOT_RESPONDING` | Max is hung or has exited. `request_sent` says whether the request reached Max before it was abandoned. `executed: false` means the bridge cancelled it after 120 s in the queue, before it ran. | No. Wait, then check with `get_bridge_status`. Never replay a request that was sent, unless `executed` is false. |
| `IMPORT_SETTLING` | A Cosmos import from this server is still running, or a Max or Cosmos browser window stayed hung after one ran or was refused. The request was not sent. The guard lifts when the import returns or the hung windows respond again, and after 15 minutes at most. | Yes, after Max settles. |

These errors carry their evidence in `error.details`: `request_sent`, and where known `inflight` (the stuck request), `process` (state, window hung, CPU-s/s) or `settling`.

### Bridge health from a pipe thread (`2ce5928`, reviewed in `011e547`)

The native bridge now answers a `health` command on the pipe thread that received it. It never posts to the main thread and never touches the scene, so it answers while Max is hung. The reply contains:

| Field | Meaning |
|---|---|
| `mainThread.state` | `responsive`; `busy_mcp` (a bridge request is executing on the main thread); `busy_other` (the main thread stopped pumping messages and no bridge work is running, e.g. a render, a script run from the UI, a plugin, or a deadlock); `unknown` (no heartbeat is available; callers fall back to `ping`); `not_initialized`; `shutting_down` |
| `mainThread.heartbeatAgeMs` | Age of the last heartbeat. Every second, a thread-pool timer posts one beat message to the executor's hidden window (at most one is outstanding), and the main thread records the time when it picks it up. Posted messages are taken in turn with the bridge's own work, so an age above about 3 s means the main thread isn't pumping. The first version used `WM_TIMER`, but Windows starves `WM_TIMER` under steady posted traffic, which made a pumping main thread look stalled. The review caught this. |
| `mainThread.windowHung` | `IsHungAppWindow` on the executor window |
| `executor.running` | The running request's command type, client id, `requestId`, and how long it has run |
| `executor.queued`, `oldestQueuedMs`, `oldestQueuedCmd`, `oldestQueuedRequestId` | Work waiting for the main thread: how many items, and the oldest one's age, command type and `requestId` (no client id) |
| `clients.connected`, `clients.inflight[]` | Connected pipe clients (this also covers #6) and each client's request: client id, `requestId`, command type, elapsed time. Nested probes that the bridge dispatches itself are flagged `internal`. The count includes the client asking. |

`get_bridge_status` asks for `health` first, over the control channel, so another request's pipe lock can't delay it:

- **Main thread `responsive`, `unknown`, `not_initialized` or `shutting_down`, or a bridge request younger than 2 s:** the usual `ping` follows. The result gains a compact `health` block.
- **`busy_mcp` for at least 2 s, or `busy_other`:** it returns `pong: false` without queueing a `ping` behind the main thread.
  - It says who holds the main thread (`main_thread.running.owner`): this server's request, another MCP client's request, a bridge request it can't attribute (`unknown`), or, with nothing running, work outside the bridge. The holder is matched by `requestId`; a `healthVersion` 1 reply, which has no request ids, falls back to command type and age. The client id only marks the bridge's own internal probes, which never count as another client. A request this server already abandoned is still recognised as its own.
  - CPU is sampled for 1 s, only when the main thread isn't pumping (always the case for `busy_other`). If no bridge work is running and no Max window is flagged hung yet, it pings after all. `IsHungAppWindow` takes about 5 s to flag a window, so this happens early in a stall.
  - The result is called `not_responding` when Max has exited, or when the main thread hasn't pumped for 20 s or more and the sample says blocked. If the request holding it is this server's own, it must also be past its timeout plus grace. Then it points to `capture_hang_diagnostics`. That rule covers only the verdict built from `health`. When it pings, a ping unanswered after 5 s is called `not_responding` if a Max window is hung with idle CPU by then. In the live test below, that took about 6 s.
- **A Cosmos import settle guard is active:** the busy/not-responding verdict from `health` is skipped, and the `ping` is refused with `IMPORT_SETTLING`. A silent health probe, a held pipe lock or a remembered hung PID is still reported first.
- **Not even the pipe threads answer within 3 s:** the probe's own busy/hung verdict is returned, instead of waiting out a second timeout.
- **TCP transport, or a bridge older than this fork:** `health` is skipped, or answered with "Unknown command type" at once. `get_bridge_status` then falls back to `ping` as before.

### `capture_hang_diagnostics`

Commits `f7dbb87`, `03f4d64`, `4ba8059` and `b4dd573` (review fixes).

Timeouts tell you *that* Max is stuck, not *where*. During the Cosmos investigation ([cosmos-import.md](cosmos-import.md)), a small script that walked native thread stacks with `dbghelp` attributed both hangs within minutes, with no debugger installed. That script is now part of the package.

```text
capture_hang_diagnostics(pid=None, all_threads=False, depth=48, save=True)
```

- **It sends nothing to Max: OS-only.** It pauses each Max thread for a few milliseconds while it reads the thread's context and walks its stack, so it's safe while Max is hung.
  - `dbghelp` loads all module symbols *before* any thread is paused, so no pause waits on a module load.
  - The walk stops at 250 ms, and the stack is then marked truncated.
  - The capture runs on its own thread, so Ctrl+C can't interrupt it halfway through a pause.
- **A paused thread never outlives the server.** A thread left suspended by a process that dies would stay suspended for good, freezing Max.
  - **Windows 11 and Server 2022+:** each thread is paused through a thread state-change object (`NtCreateThreadStateChange` / `NtChangeThreadState`). The kernel resumes the thread itself when that handle closes, so even a hard kill of the server can't leave Max frozen.
  - **Older Windows:** it falls back to `SuspendThread` under `maxmcp/suspend_guard.py`. Every pause is registered in the same step as the suspend, and resumed exactly once.
    - Every exit path calls `release_all()` before `os._exit`, which itself runs in a `finally`. The exit paths are the parent watchdog and its fallback timer, the exit after stdin closes, and `atexit`.
    - `release_all()` blocks new pauses, waits up to 0.5 s for active walks, then resumes anything still paused itself.
    - Here only a hard kill of the server (`TerminateProcess`) can skip the guard.
  - `pause_methods` in the saved `.json` (and the command line's `--json` output) says which method was used.
  - Before pausing, a thread id is checked with `GetProcessIdOfThread`, so a reused id that now belongs to another process is never paused.
- **Finding the target.** It uses `pid` if given, which must be a running `3dsmax.exe`. Otherwise it uses the Max this server is talking to (read from memory, without the pipe lock), and only if none is selected, the only running `3dsmax.exe`. It never falls back past a choice that fails, and lists the running PIDs instead of guessing:
  - `BAD_PARAM`: `pid` isn't a positive number;
  - `NOT_FOUND`: the given or selected PID isn't a running Max (even if another one is, e.g. after a restart), or no Max is running;
  - `AMBIGUOUS`: several are running and none is selected.
- **The main thread** is the thread that owns the visible window whose title contains "Autodesk 3ds Max". Without one, it's the owner of the largest visible top-level window, and with no visible window, the oldest thread. `main_thread.source` (`title`, `largest_window` or `first_thread`) says which rule picked it.
- **What it returns** (or `DIAGNOSTICS_FAILED` if no stack could be read):
  - `main_thread`: its top modules, a `blocked_in` sentence, and the first frames as `module+0xoffset`;
  - `threads`: how many there are, were kept and were left out, and the longest pause (`max_suspended_ms`);
  - `findings` from a small rule table;
  - `hung_windows`;
  - process health;
  - the paths of the files it saved.
- **Saved files.** With `save=true`, the full stacks go to `%LOCALAPPDATA%\3dsmax-mcp\diagnostics\hang-<pid>-<timestamp>.txt` and `.json`, for bug reports to Autodesk or Chaos. Only the newest 20 captures are kept.
- **Not marked read-only.** It pauses threads and writes files, so it doesn't claim the MCP `readOnlyHint`.
- **Which threads are kept.** By default it keeps the main thread, threads that own a window, threads whose top frame is application code (not waiting in a Windows or C runtime DLL), and any thread with a watched module such as `galaxyimporter` on its stack. A worker blocked in a kernel wait is left out unless it matches one of these. Use `all_threads=true` to keep every thread.
- **Command line:** `python -m maxmcp.diagnostics.stackdump <pid> [tid ...] [--all] [--depth N] [--json]`. Thread ids after the PID dump only those threads.
- **A new tool needs a new client session.** MCP clients cache the tool list, and a restarted server doesn't refresh it (there's no `tools/list_changed`). So after an update, `capture_hang_diagnostics` appears only in a new session; in the Claude desktop app that's a new Code-tab session. Until then, use the command line above with the installed runtime. It only prints; it saves no files.

The rules (`RULES` in `maxmcp/diagnostics/stackdump.py`; add a dict to add a rule):

| Finding | Matches |
|---|---|
| `medit_vray_render` | The main thread's first non-system module is `vray`/`vrender`, with `mtl` deeper on the stack: a Material Editor sample-slot render |
| `cross_thread_window_deadlock` | The main thread is in a USER32 call (not an idle message loop) while another thread, in a kernel wait, owns a hung window. The finding names that window, thread and module. |
| `cosmos_importer` | `galaxyimporter` is on any thread's stack |
| `mcp_bridge_call` | The main thread is running a bridge request |
| `maxscript` | Fallback, checked only when neither `medit_vray_render` nor `cross_thread_window_deadlock` matched: `MAXScrpt` among the main thread's first eight non-system frames, meaning a long-running script |
| `main_thread_modules` | Fallback: the top three non-system modules on the main thread |

If the main thread can't be found or read, the findings say so (`main_thread_unknown`, `main_thread_unreadable`).

The tests build synthetic captures from frames of the two real hang dumps from this investigation; the dump files themselves aren't read. The first gives `medit_vray_render`. The second gives `cross_thread_window_deadlock` with the "Chaos Cosmos Browser" window, plus `cosmos_importer` and `mcp_bridge_call`, because the stuck `MatEditor.Close()` came through the bridge.

Limitations:
- **No symbols:** frames are `module+offset`. Function names are shown only in the core Windows DLLs (`ntdll`, `kernelbase`, `kernel32`, `win32u`, `user32`, near an export) and in modules with a PDB next to them. That's enough to tell a wait from a `SendMessage`. Other system DLLs, such as the C runtime, stay `module+offset`. No symbol server is ever contacted while a thread is paused.
- **Same user and integrity level:** the server must run as the same user, at the same integrity level, as Max. An elevated Max needs an elevated server.
- **x64 targets only.**
- **Hung-window lag:** `IsHungAppWindow` only reports a window as hung after about 5 s.

The original script set `SizeOfStruct` wrong for `SYMBOL_INFOW` (90 instead of 88), so `SymFromAddrW` always failed. That's why the original dumps show offsets only. This version fixes it.

### Native executor (`adcfd93`, ported from Geddart's fork)

- **Shutdown drain** (Geddart `8e004f7`). `MCPBridgeGUP::Stop()` now closes the executor gate first, then fails every queued and deferred work item, so client threads blocked in `ExecuteSync` wake at once. A background `ExecuteSync` during shutdown fails fast. `Initialize()` reopens the gate.
- **Expired work never runs.** A work item that timed out while still queued can't run later against the caller's unwound stack. The fork ported this from Geddart (`599e6f7`); since the 1.7.5 merge it is upstream's mechanism (`5c44e76`): the item is cancelled with "Main thread execution timed out before starting; queued work cancelled". Work that already started is waited for.

### Orphaned servers (#6, `a9d2a0d`)

On Windows, a child process outlives its parent. So a crashed or killed MCP client used to leave its `maxmcp.server` running, and that server could still hold a pipe connection to Max. Four such servers were found running at once.

The server now exits when its parent client exits. If the parent is a pass-through launcher (a venv `python.exe` stub, `py.exe`/`pyw.exe`, `uv`/`uvx`, the `3dsmax-mcp.exe` console script, or `cmd.exe`), the launcher's own ancestors are watched too. The server also exits after stdin closes. To turn the watchdog off, set `MAXMCP_PARENT_WATCHDOG=0`.

The bridge side of #6, reporting how many clients are connected, is the `clients` block of `health` above.

### Upstream 1.7.5's dialog handling, fixed on the merge

Merging 1.7.5 brought its blocking-dialog handling (`BLOCKED_BY_DIALOG`, `max_dialogs`, `MAX_DIALOG_ERROR`, quiet mode). A review of the merged branch found these problems, all fixed:
- **A hung Max with a dialog on screen was called "blocked by a dialog".** The client raised `BLOCKED_BY_DIALOG` for any main-thread dialog, even while Max's main thread had stopped pumping, so the deadline diagnosis and the hung-PID latch never ran. The dialog monitor's status now reports `main_thread_pumping` (the heartbeat rule `health` uses); while it is false, a call is diagnosed like any other and gives `MAX_NOT_RESPONDING`. `get_bridge_status` already applied that rule.
- **`get_bridge_status` crashed** (`'NoneType' object has no attribute 'get'`, returned as `BAD_PARAM`) when this server's `BLOCKED_BY_DIALOG` call was still running and Max had then been blocked for 20 s. The overdue rule now uses that call's own record (`blocked_call`): `not_responding` once it is past its timeout plus grace, `busy` before.
- **A transacted call could commit before its acknowledged error was recorded.** The monitor posts the OK click, then publishes the error, so the operation could resume and commit first. `ThrowIfDismissed` now waits (at most 3 s) for the publish, so such a call is always rolled back.
- **A Qt press could click after `respond` reported it failed.** When Max's main thread had started the press but not finished it within 1.5 s, `respond` returned a retryable `MAIN_THREAD_BUSY` and the click followed. It now returns `DIALOG_OUTCOME_UNKNOWN` (not retryable: inspect again). A press the main thread never started is withdrawn, so `MAIN_THREAD_BUSY` means nothing was pressed.
- **A direct-mode read could leave Max in quiet mode.** `get_wired_params`, `get_state_sets` and `get_camera_sequence` run their scripts on a pipe thread, and their save/restore of Max's single quiet-mode flag could interleave with a main-thread one. Quiet mode now changes only on the main thread.
- **A Qt MAXScript error box seen while the main thread didn't pump was never acknowledged**, so its call returned `BLOCKED_BY_DIALOG` instead of `MAX_DIALOG_ERROR`. It's read again once the main thread pumps.
- **Render cleanup stopped halfway after an acknowledged error**, leaving the agent viewport's leased V-Ray settings modified. The cleanup now finishes; the call still fails with `MAX_DIALOG_ERROR`.
- **Cosmos imports:** `MAX_DIALOG_ERROR` on the preparation call returns `not_imported` with the restore scripts instead of escaping; on the browser action it still waits for the browser; a dialog already holding Max's main thread stops the import before anything is sent.
- Tool windows (Cosmos browser, Material Editor, viewports) of any thread now stop the automatic inspect, and replies never list them as open dialogs, also with upstream's bridges. The `BLOCKED_BY_DIALOG` registry drops probes before real calls when full.

Not changed: the native tool-window rule matches by title only, so a real message box captioned exactly like the Material Editor would not be reported. A class-based exception was considered, but the Compact Material Editor itself may be a `#32770` dialog, which would make it a "dialog" again; that can't be checked without Max.

## How it was verified

- **Executor fixes:** loaded in Max 2026 on 2026-10-01. Ping, read and mutating calls all OK. Quitting Max while a call is queued hasn't been exercised live yet.
- **Client side, tested live:** a 20 s `sleep` was run from a second process, and `get_bridge_status` was called from the MCP.
  - The status came back in about 6 s with `bridge_state: "not_responding"`, the in-flight probe, and process evidence (window hung, 0.03 CPU-s/s).
  - After the sleep, the next status was `pong: true` in 2.6 ms, so the fail-fast latch clears.
  - Without the `health` command, a legitimate long call from another client looks like a deadlock from outside. The `health` command narrows that gap: it names the other client's request and reports it busy. A call that keeps the main thread from pumping for 20 s or more with almost no CPU, such as a long `sleep`, is still reported `not_responding`.
- **`health`, tested live:** with the rebuilt bridge loaded in Max 2026 on 2026-10-01. Idle, `get_bridge_status` returned `pong: true` with the `health` block. During an 8 s `sleep` sent from another process, it answered at once with `bridge_state: "busy"`, `MAX_BUSY`, `busy_mcp` and owner `other_client`. The 20 s `not_responding` path hasn't been exercised live.
- **`capture_hang_diagnostics`:** run against child processes started by the tests. Their frames resolve inside `ntdll`, every thread was confirmed resumed afterwards, and the child still answered. Its rules are also unit-tested on synthetic captures built from frames of the two real hang dumps. It hasn't yet been pointed at a real hung Max with the packaged tool; the original script it comes from was.

Tests:
- `tests/test_hang_diagnosis.py`: lock timeout, deadlines, probes, the hung-PID latch, status payloads.
- `tests/test_parent_watchdog.py`
- `tests/test_bridge_health.py`
- `tests/test_hang_capture.py`: 46 cases, covering rules, main-thread identification, PID resolution, and live captures of spawned processes.
- `tests/test_suspend_guard.py`: every pause is resumed exactly once, `release_all()` and the exit paths, the 250 ms cut-off, state-change pauses and reused thread ids.
- Native, SDK-independent: `native/tests/executor_tests.cpp` and `native/tests/health_tests.cpp`. The second covers the heartbeat, queued and running visibility without blocking, expired items, and the client registry.
- `native/tests/dialog_watch_tests.cpp`: the monitor's window rules, plus the 1.7.5 fixes above (publish before resume, withdrawn and unknown-outcome presses, the pumping flag, the Qt error-box retry, the deferred cleanup scope). The publish race is reproduced by pinning the test to one CPU: without the fix, 4 of 5 runs resumed before the error was published. None of these 1.7.5 fixes is tested live yet.

## Upstream status

Upstream 1.7.5 (2026-10-01) covers two parts: a call waiting on a modal dialog returns `BLOCKED_BY_DIALOG` (answered with `max_dialogs`), and a timed-out queued request is cancelled. The rest is not upstream: the reply deadline and process diagnosis (`MAX_BUSY` / `MAX_NOT_RESPONDING`), `health`, `capture_hang_diagnostics`, the shutdown drain and the orphaned-server watchdog. Related work:
- Upstream PR 32 (executor cancellation, closed unmerged) was deliberately not ported. Its contributor's fork has been deleted.
- The lock timeout idea comes from stoxsss111's fork. From Geddart's come the executor drain and the process-exit check (`_process_alive` there, rewritten here in `maxmcp/process_health.py`).
