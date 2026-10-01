# When 3ds Max hangs: a diagnosis instead of timeouts

Fork issues #4 and #6, and the executor shutdown fix. Commits `25ac1ed`, `6e9188b`, `f8d4e6f`, `d971a56`, `91c8662`, `1dab496` and `5466a32`.

## What happened

When Max's main thread hung, every tool returned the MCP client's generic "Request timed out", `get_bridge_status` included. The agent then had to work out from the outside whether Max was busy, hung or gone. Usually it guessed "permanent deadlock", and Max was killed with unsaved work, even though some of those stalls would have cleared on their own after a few minutes.

## Why

- **The pipe lock blocked indefinitely.** `MaxClient._send_via_pipe` held the pipe lock across a blocking `ReadFile`, and checked its deadline only between chunks. A request stuck on Max's main thread therefore never timed out on the Python side, and every later call waited on that same lock.
- **`ping` queued behind the main thread.** It goes through the main-thread executor, so even a status check waited behind a hung main thread.
- **The native executor had two gaps.**
  - Max's exit could stall for up to 120 s per in-flight request: the main thread was joining the very pipe threads that were waiting on it.
  - A request that timed out while still queued could later run anyway, against a caller stack that no longer existed.

## What changed

### Client side (`6e9188b`)

- **The pipe lock has a timeout.** A request that can't get the lock fails with `MAX_BUSY` and isn't sent. The idea comes from the stoxsss111 fork.
- **Replies are polled with a real deadline.** The client uses `PeekNamedPipe` instead of a blocking `ReadFile`. Past the deadline it checks the target process: are its top-level windows hung (`IsHungAppWindow`), and how much CPU does it use over a one-second sample?
  - **Healthy but long work:** the client keeps waiting.
  - **Max has exited, or is confirmed blocked:** the request is abandoned with `MAX_NOT_RESPONDING`. It's never replayed, because it may already have run.
  - **Read-only probes** (`ping`, `health`): dropped at their deadline.
- **A hung PID is remembered.** Later calls to it fail fast, with nothing sent, until its window pumps messages again.
- **The messages give bounded advice:** "wait up to about 10 minutes, re-checking with `get_bridge_status`; still blocked after that means a deadlock, and the user must end Max".

Error codes:

| Code | Meaning | Retry |
|---|---|---|
| `MAX_BUSY` | Max (or this client's pipe) is occupied. The request was not sent. | Yes, later. |
| `MAX_NOT_RESPONDING` | Max is hung or has exited. `request_sent` says whether the request reached Max before it was abandoned. | No. Wait, then check with `get_bridge_status`. Never replay a request that was sent. |
| `IMPORT_SETTLING` | A native import (Cosmos) left a Max window hung. The request was not sent. | Yes, after Max settles. |

### Bridge health from a pipe thread (`f8d4e6f`, reviewed in `d971a56`)

The native bridge now answers a `health` command on the pipe thread that received it. It never posts to the main thread and never touches the scene, so it answers while Max is hung. The reply contains:

| Field | Meaning |
|---|---|
| `mainThread.state` | `responsive`; `busy_mcp` (a bridge request is executing on the main thread); `busy_other` (the main thread stopped pumping messages and no bridge work is running, e.g. a render, a script run from the UI, a plugin, or a deadlock); `unknown` (no heartbeat is available; callers fall back to `ping`); `not_initialized`; `shutting_down` |
| `mainThread.heartbeatAgeMs` | Age of the last heartbeat. Every second, a thread-pool timer posts one beat message to the executor's hidden window (at most one is outstanding), and the main thread records the time when it picks it up. Posted messages are taken in turn with the bridge's own work, so an age above about 3 s means the main thread isn't pumping. The first version used `WM_TIMER`, but Windows starves `WM_TIMER` under steady posted traffic, which made a pumping main thread look stalled. The review caught this. |
| `mainThread.windowHung` | `IsHungAppWindow` on the executor window |
| `executor.running` | The running request's command type, client id, `requestId`, and how long it has run |
| `executor.queued`, `oldestQueuedMs`, `oldestQueuedCmd` | Work waiting for the main thread, with the oldest item's ids |
| `clients.connected`, `clients.inflight[]` | Connected pipe clients (this also covers #6) and each client's request: client id, `requestId`, command type, elapsed time. Nested probes that the bridge dispatches itself are flagged `internal`. The count includes the client asking. |

`get_bridge_status` asks for `health` first, over the control channel, so another request's pipe lock can't delay it:

- **Main thread responsive, or `unknown`:** the usual `ping` follows. The result gains a compact `health` block.
- **`busy_mcp` for at least 2 s, or `busy_other`:** it returns `pong: false` at once, without queueing a `ping` behind the main thread.
  - It says who holds the main thread: this server's request, another MCP client's request, or work outside the bridge. The holder is matched by `requestId` and client id, so it's never guessed from the command type. A request this server already abandoned is still recognised as its own.
  - CPU is sampled only when the main thread isn't pumping.
  - The result is called `not_responding` only after the main thread has been idle in a wait for 20 s or more. Then it points to `capture_hang_diagnostics`.
- **A Cosmos import settle guard is active:** `IMPORT_SETTLING` wins over everything else.
- **Not even the pipe threads answer:** the probe's own busy/hung verdict is returned, instead of waiting out a second timeout.
- **TCP transport, or a bridge older than this fork:** `health` is skipped, or answered with "Unknown command type" at once. `get_bridge_status` then falls back to `ping` as before.

### `capture_hang_diagnostics`

Commits `91c8662`, `1dab496` and `5466a32`.

Timeouts tell you *that* Max is stuck, not *where*. During the Cosmos investigation ([cosmos-import.md](cosmos-import.md)), a small script that walked native thread stacks with `dbghelp` attributed both hangs within minutes, with no debugger installed. That script is now part of the package.

```text
capture_hang_diagnostics(pid=None, all_threads=False, depth=48, save=True)
```

- **It sends nothing to Max.** Everything is read from the OS, so it's safe while Max is hung. Each thread is paused only while its context is read and its stack walked: about 0.2–2 ms, or 10–20 ms for the first thread that touches a module. The walk stops at 250 ms, and the stack is then marked truncated.
- **A paused thread never outlives the server.** A thread suspended by a process that dies stays suspended for good, and that would freeze Max. So every suspension is registered in `maxmcp/suspend_guard.py` in the same step as `SuspendThread`, and it's resumed exactly once.
  - Every exit path calls `release_all()` first: the parent watchdog's `os._exit` and its fallback timer, the exit after stdin closes, and `atexit`.
  - `release_all()` blocks new suspensions, waits up to 0.5 s for active walks, then resumes anything still suspended itself.
  - Only a hard kill of the server (`TerminateProcess`) can skip this.
- **Finding the target.** It uses `pid` if given, otherwise the Max this server is talking to (read from memory, without the pipe lock), otherwise the only running `3dsmax.exe`. If none of those works, it lists the candidates instead of guessing.
- **The main thread** is the thread that owns Max's main window.
- **What it returns:**
  - `main_thread`: its top modules, a `blocked_in` sentence, and the first frames as `module+0xoffset`;
  - `findings` from a small rule table;
  - `hung_windows`;
  - process health;
  - the paths of the files it saved.
- **Saved files.** With `save=true`, the full stacks go to `%LOCALAPPDATA%\3dsmax-mcp\diagnostics\hang-<pid>-<timestamp>.txt` and `.json`, for bug reports to Autodesk or Chaos.
- **Which threads are kept.** By default it keeps the main thread, threads that own a window, threads in application code, and any thread inside a watched module such as `galaxyimporter`. Use `all_threads=true` to keep every thread.
- **Command line:** `python -m maxmcp.diagnostics.stackdump <pid> [--all] [--depth N] [--json]`.

The rules (`RULES` in `maxmcp/diagnostics/stackdump.py`; add a dict to add a rule):

| Finding | Matches |
|---|---|
| `medit_vray_render` | The main thread's first non-system module is `vray`/`vrender`, with `mtl` deeper on the stack: a Material Editor sample-slot render |
| `cross_thread_window_deadlock` | The main thread is in a USER32 call (not an idle message loop) while another thread, in a kernel wait, owns a hung window. The finding names that window, thread and module. |
| `cosmos_importer` | `galaxyimporter` is on any thread's stack |
| `mcp_bridge_call` | The main thread is running a bridge request |
| `maxscript` | `MAXScrpt` near the top of the main thread: a long-running script |
| `main_thread_modules` | Fallback: the top three non-system modules on the main thread |

When a test replays the two real hang dumps from this investigation, the first gives `medit_vray_render`. The second gives `cross_thread_window_deadlock` with the "Chaos Cosmos Browser" window, plus `cosmos_importer`.

Limitations:
- **No symbols:** frames are `module+offset`. Names are resolved only inside the Windows system DLLs, which is enough to tell a wait from a `SendMessage`. No symbol server is ever contacted while a thread is paused.
- **Same user and integrity level:** the server must run as the same user, at the same integrity level, as Max. An elevated Max needs an elevated server.
- **x64 targets only.**
- **Hung-window lag:** `IsHungAppWindow` only reports a window as hung after about 5 s.

The original script set `SizeOfStruct` wrong for `SYMBOL_INFOW` (90 instead of 88), so `SymFromAddrW` always failed. That's why the original dumps show offsets only. This version fixes it.

### Native executor (`25ac1ed`, ported from Geddart's fork)

- **Shutdown drain** (Geddart `8e004f7`). `MCPBridgeGUP::Stop()` now closes the executor gate first, then fails every queued and deferred work item, so client threads blocked in `ExecuteSync` wake at once. A background `ExecuteSync` during shutdown fails fast. `Initialize()` reopens the gate.
- **Expired work never runs** (Geddart `599e6f7`). A work item that timed out while still queued is marked done and its callback dropped. It can't run later against the caller's unwound stack.

### Orphaned servers (#6, `6e9188b`)

On Windows, a child process outlives its parent. So a crashed or killed MCP client used to leave its `maxmcp.server` running, and that server could still hold a pipe connection to Max. Four such servers were found running at once.

The server now exits when its parent client exits. If the parent is a pass-through launcher (a venv `python.exe` stub, `py.exe`, `uv`, the `3dsmax-mcp.exe` console script, or `cmd.exe`), the launcher's own ancestors are watched too. The server also exits after stdin closes. To turn the watchdog off, set `MAXMCP_PARENT_WATCHDOG=0`.

The bridge side of #6, reporting how many clients are connected, is the `clients` block of `health` above.

## How it was verified

- **Executor fixes:** loaded in Max 2026 on 2026-10-01. Ping, read and mutating calls all OK. Quitting Max while a call is queued hasn't been exercised live yet.
- **Client side, tested live:** a 20 s `sleep` was run from a second process, and `get_bridge_status` was called from the MCP.
  - The status came back in about 6 s with `bridge_state: "not_responding"`, the in-flight probe, and process evidence (window hung, 0.03 CPU-s/s).
  - After the sleep, the next status was `pong: true` in 2.6 ms, so the fail-fast latch clears.
  - Without the `health` command, a legitimate long call from another client looks like a deadlock from outside. That's the gap the `health` command closes.
- **`health`:** unit-tested only so far. The live test is pending deployment of the rebuilt bridge.
- **`capture_hang_diagnostics`:** run against child processes started by the tests. Their frames resolve inside `ntdll`, every thread was confirmed resumed afterwards, and the child still answered. It has also replayed the two real hang dumps. It hasn't yet been pointed at a real hung Max with the packaged tool; the original script it comes from was.

Tests:
- `tests/test_hang_diagnosis.py`: lock timeout, deadlines, probes, the hung-PID latch, status payloads.
- `tests/test_parent_watchdog.py`
- `tests/test_bridge_health.py`
- `tests/test_hang_capture.py`: 42 cases, covering rules, main-thread identification, PID resolution, and live captures of spawned processes.
- Native, SDK-independent: `native/tests/executor_tests.cpp` and `native/tests/health_tests.cpp`. The second covers the heartbeat, queued and running visibility without blocking, expired items, and the client registry.

## Upstream status

None of this is fixed upstream as of 2026-10-01. Related work:
- Upstream PR 32 (executor cancellation, closed unmerged) was deliberately not ported. Its contributor's fork has been deleted.
- The lock timeout idea comes from stoxsss111's fork, and `_process_alive` and the executor drain from Geddart's.
