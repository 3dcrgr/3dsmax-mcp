# When 3ds Max hangs: a diagnosis instead of timeouts

Upstream pull request: **Hangs and dialogs**, not opened yet ([list](../FORK.md#upstream-pull-requests)).

Fork issues #4 and #6, and the executor shutdown fix. Commits `adcfd93`, `a9d2a0d`, `ed1fb1c`, `2ce5928`, `011e547`, `f7dbb87`, `03f4d64`, `4ba8059` and `b4dd573`. Since the 1.7.5 merge (`a07ef93`), all of this is built to work alongside upstream's dialog handling; see [the last section](#upstream-175s-dialog-handling-fixed-on-the-merge). Since the 1.7.6 merge (`298a295`), it also sits alongside upstream's own reply deadline and shutdown handling; see [Upstream status](#upstream-status).

**Which bridge.** The client side, the Python half of the dialog fixes, `capture_hang_diagnostics` and the orphaned-server watchdog are Python, and work with upstream's bridges too. `health` and the native half of the dialog fixes need the fork's bridge. The shutdown drain and the connected-pipe cancellation (#29) are in upstream's 1.7.6 bridges too.
- In `native/bin/`, only `mcp_bridge_2027.gup` is a fork build: the fork's own build with the Max 2027 SDK (hash in `native/bin/bridge_manifest.json`), not deployed or live-tested yet. The 2023, 2024, 2025 and 2026 files are upstream's builds from `71f72a8` (1.7.6 plus #35).
- A 2027 fork bridge has run on the production machine since 2026-10-11: the 1.7.5-based `04815449…` (from `538a241`) from 2026-10-14, and the 1.7.6 merge's `c5903978…` (from `298a295`) since 2026-10-15.

## What happened

### Bridge packaging and shutdown classification (#32–#34, 2026-10-13)

`native/bin/bridge_manifest.json` records the reviewed 2027 payload and the known superseded build. Wheel/Setup staging verifies native payloads against the checkout and that manifest; a cached stale wheel fails before staging changes. Both the Python installer and Setup refuse to replace an unrecognized installed 2027 bridge, which may be newer, while allowing the matching reviewed payload or known retired build. Review/update the manifest when shipping a different native build. Previously built/upstream installers cannot gain this protection retroactively; replace stale distributed artifacts with a newly reviewed build.

Hang diagnosis reports a symbol-resolved bridge thread join beneath GUP as `mcp_shutdown_join`. Without join symbols, a bridge wait beneath GUP is `mcp_teardown_wait`, explicitly only consistent with teardown. An MCP module alone no longer claims an active request. No additional native capture or Max restart is required to reclassify a saved capture.

The shared `livetests-2027/scratch_max.py` uses UTF-8 stdout/stderr, keeps PID mismatch exit 2, and returns exit 1 for native error markers or structured `ok:false`, `success:false`, `isError:true`, error payloads and failed/error statuses. Offline fake-client checks exercise these routes without touching Max.

### Stale instance registry records (#31)

Instance discovery opportunistically removes unchanged `instances/pid-N.json` registrations only when the PID is certainly exited, its pipe is unavailable, and the filename, recorded PID, instance ID and native pipe agree. It rechecks file identity, pipe availability and process state before unlinking. Records with live/unknown owners, answering/busy/access-denied pipes, custom identities or concurrent refreshes are retained. Cleanup failures do not break routing, and `active_instance.json` is untouched. The native GUP already unregisters its own record on clean exit; this handles leftovers after crashes/interrupted teardown.

### Shutdown and startup diagnostics (2026-10-12, issues #29 and #30)

Connected pipe clients now use cancellable overlapped reads and writes. `PipeServer::Stop` signals shutdown before joining workers; each pending operation is cancelled and drained while its buffer and event remain alive. This covers idle clients, partial requests, and clients that stop reading responses. The executor still closes its submission gate and drains queued work first; started SDK work retains its existing lifetime guarantees. Upstream 1.7.6 fixed the same exit stall its own way: its pipe `Stop` cancels every pipe instance's I/O. The 1.7.6 merge uses upstream's pipe server, adding the `health` client registry and not dispatching a request whose read completed as `Stop` began; the fork's pipe shutdown test (`native/tests/pipe_server_tests.cpp`) is kept.

If auto transport finds neither a native bridge nor a TCP listener, `BRIDGE_DOWN` now includes running Max candidate PIDs, the preferred and attempted transports, and both failures. A candidate may still be starting or may lack the bridge; its presence is not evidence of readiness. Explicit TCP mode keeps its legacy listener advice. These changes do not make `MAXSCRIPT_INTERRUPTED` proof that the Max process has exited: restart workflows must still wait for the exact PID to disappear.

The auto diagnostic covers TCP connection refusal and a timeout before TCP connects. `tcp_failure` distinguishes `connection_refused` from `connect_timeout`; both set `request_sent:false`. A timeout after connection or dispatch retains its existing error behavior and is never reported as an unsent startup failure. The connect phase is tracked in the TCP transport itself rather than inferred from a generic timeout exception.

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
- **Replies have a real deadline.** The caller never blocks in `ReadFile`. The fork first polled with `PeekNamedPipe`. Since the 1.7.5 merge (`a07ef93`), upstream's `_PipeReader` thread does the read, and the fork's deadline and diagnosis run in the caller while it waits on that thread. Past the deadline plus grace it checks the target process: are its top-level windows hung (`IsHungAppWindow`), and how much CPU does it use over a one-second sample? A hung window with under 0.05 CPU-s/s counts as blocked.
  - **Anything short of exited or confirmed blocked** (responsive, hung but still using CPU, or a state it can't read): the client keeps waiting and checks again every 15 s, so a slow result still arrives. There's no upper limit. If the client can't tell Max's PID, it can't check at all. Before the 1.7.6 merge it then just kept waiting; since the merge, upstream's reply deadline ends that wait after the bridge's queue timeout (120 s, or the call's own timeout if longer) plus 10 s, with `MAX_BUSY`: the request was sent, may still be running, and is never replayed (`request_sent: true`, not retryable). Like a `BLOCKED_BY_DIALOG` call, it is tracked (except on the control channel), and `max_dialogs(action="inspect")` reports its result in `blocked_calls` once Max finishes it. This deadline applies only without a PID; with one, the diagnosis decides.
  - **Max has exited, or is blocked on two checks 20 s apart:** the request is abandoned with `MAX_NOT_RESPONDING`. It's never replayed, because it may already have run.
  - **Read-only probes** (`ping`, `health`, `max_dialogs` reads, the client's own dialog checks, the Cosmos import's polls and `vantage_live_link`'s checks): dropped at their deadline.
  - **A reply that arrives during a check is returned**, never abandoned.
  - **Giving up never closes the pipe under a pending read.** On a synchronous pipe, `CloseHandle` waits for the pending `ReadFile`, that is, for Max to answer. So an abandoned request or probe, and a call released with `BLOCKED_BY_DIALOG`, detach the reader instead. The reader closes the handle when its read returns, and the next call opens a new connection.
- **A dialog comes first, but only while Max's main thread pumps.** From 1 s after sending, and every second after that, the client asks the bridge's dialog monitor, over the control channel, whether a dialog on Max's main thread holds the call: one that opened during the call, or any such dialog while the call is still queued. If one does, the call returns `BLOCKED_BY_DIALOG` at once, and Max keeps running it. If the monitor reports that the main thread stopped pumping (`main_thread_pumping: false`), a dialog on screen doesn't count: Max is busy or hung, and the call is diagnosed as above. Upstream's bridges don't report that flag, so with them a dialog always counts. Requests on the control channel itself are never checked.
- **The control channel has a deadline too.** Upstream 1.7.5 waited there, and for replies, with no deadline at all. Control requests now get the same deadline and diagnosis as any other, and their read-only probes are dropped at the deadline.
- **A hung PID is remembered.** Later calls to it fail fast, with nothing sent, until its window pumps messages again.
- **The control channel isn't held up by any of this.** `health`, `max_dialogs`, render cancel and screen capture use their own pipe connection. They never wait for another request's pipe lock, and neither the hung-PID latch nor the Cosmos settling guard refuses them. So `max_dialogs` and `get_bridge_status` still answer during `IMPORT_SETTLING` and after a `MAX_NOT_RESPONDING`. Win32 dialogs are read from the window manager. With the fork's bridge, a Qt dialog can only be read, and any dialog only pressed, while Max's main thread pumps. Otherwise `inspect` marks a Qt dialog `unavailable`, and `respond` fails with `MAIN_THREAD_BUSY` without pressing anything. Upstream's bridges try a Qt read or press anyway, for up to 1.5 s: a read then comes back `unavailable`, a press gives `MAIN_THREAD_BUSY`, and the press may still land after that. They also post a Win32 press at once, so it lands whenever the main thread pumps again.
- **The messages give bounded advice** (`ed1fb1c`): "wait up to about 10 minutes, re-checking with `get_bridge_status`; still blocked after that means a deadlock, and the user must end Max". Errors that say Max is not responding or still settling also get a `hint` with the same 10-minute wait, suggesting `capture_hang_diagnostics` and `get_bridge_status` (`f7dbb87`, `03f4d64`).

Error codes:

| Code | Meaning | Retry |
|---|---|---|
| `MAX_BUSY` (fork, `MaxBusyError`) | Max (or this client's pipe) is occupied. Usually nothing was sent. A read-only probe dropped at its deadline, or a request the bridge cancelled before it ran (`executed: false`), has `request_sent: true` but changed nothing. `retryable: true`. | Yes, later. |
| `MAX_BUSY` (upstream 1.7.6, `MaxBusy`) | Since the 1.7.6 merge, also upstream's reply deadline: a sent request got no reply in time and Max's PID couldn't be found to diagnose it. It may still be running. `request_sent: true`, `retryable: false`; `details.waited_s`, and `details.state` when known. | No. Never repeat it: `max_dialogs(action="inspect")` reports its result in `blocked_calls` once Max finishes it. |
| `MAX_NOT_RESPONDING` | Max is hung or has exited. `request_sent` says whether the request reached Max before it was abandoned. `executed: false` means the bridge cancelled it after 120 s in the queue, before it ran. A dialog on screen doesn't change this when Max's main thread has stopped pumping. | No. Wait, then check with `get_bridge_status`. Never replay a request that was sent, unless `executed` is false. |
| `IMPORT_SETTLING` | A Cosmos import from this server is still running, or a Max or Cosmos browser window stayed hung after one ran or was refused. The request was not sent. The guard lifts when the import returns or the hung windows respond again, and after 15 minutes at most. | Yes, after Max settles. |
| `BLOCKED_BY_DIALOG` | From upstream 1.7.5. A modal dialog on Max's main thread holds the call, and the main thread still pumps (it runs the dialog), so Max isn't hung. The call keeps running in Max and finishes once the dialog is answered. | No. Read the dialog with `max_dialogs`. Answer it only if the user allowed unattended work; otherwise ask the user. Never repeat the call: `max_dialogs` reports its result. |
| `MAX_DIALOG_ERROR` | From upstream 1.7.5. The call ran, but Max showed a recognised MAXScript error box during it, and the bridge acknowledged it. This replaces the call's own result or error. With the fork's bridge, a native call that runs in an undo transaction is rolled back; upstream's bridges can commit it before the error is recorded. Anything else, MAXScript included, may have changed the scene. | No. Inspect the scene first. |
| `REQUEST_OUTCOME_UNKNOWN` | The request was sent, then the connection broke or the reply couldn't be read (Max exiting, the bridge dropping the pipe, a mismatched reply). It may have run. A `BLOCKED_BY_DIALOG` call that ends this way is reported by `max_dialogs` as `status: "lost"`, `outcome: "unknown"`. Until the second merge review this came out as `BAD_PARAM`. | No. Inspect the scene before running it again. |
| `BRIDGE_OUTDATED` | `max_dialogs` on a bridge without the dialog monitor (before 1.7.5, e.g. the fork's 1.7.3-based builds). Nothing was read or pressed. | No. Ask the user to answer the dialog in Max. |
| `DIALOG_OUTCOME_UNKNOWN` | Only from `max_dialogs(action='respond')` on a Qt dialog, with the fork's bridge. Max's main thread started the press but didn't finish it within 1.5 s, so the click may still land. (With that bridge, `MAIN_THREAD_BUSY` from `respond` means nothing was pressed, and can be retried. Upstream's bridges give `MAIN_THREAD_BUSY` in both cases, so inspect again before retrying there.) | No. Inspect again before responding. |

These errors carry their evidence in `error.details`: `request_sent`, and where known `inflight` (the stuck request), `process` (state, window hung, CPU-s/s) or `settling`. `BLOCKED_BY_DIALOG` carries the `request_id`, the `command` and the `dialogs` (title, text, buttons). `MAX_DIALOG_ERROR` carries the acknowledged `dialogs`, the error the call stopped with as `cause` (empty if none) and `scene_state: "verification_required"`.

The checks run in this order, and the first that applies gives the code:
1. **Before sending** (nothing sent): the pipe lock (`MAX_BUSY` / `MAX_NOT_RESPONDING`), then the hung-PID latch (`MAX_NOT_RESPONDING`), then the settling guard (`IMPORT_SETTLING`). None of them applies to the control channel.
2. **While waiting:** `BLOCKED_BY_DIALOG`, then the deadline diagnosis (`MAX_BUSY` / `MAX_NOT_RESPONDING`), or, when Max's PID is unknown, upstream's reply deadline (`MAX_BUSY`, sent, not retryable). A modal dialog runs its own message loop, so it isn't a hang; a main thread that stopped pumping is busy or hung, dialog or not.
3. **In the reply:** a request the bridge cancelled in its queue gives `MAX_BUSY` or `MAX_NOT_RESPONDING` with `executed: false`, if Max is still busy, blocked or gone when the reply arrives; otherwise it gives the bridge's `BRIDGE_DOWN` (retryable). `MAX_DIALOG_ERROR` replaces the error it wraps. An explicit code such as `BAD_PARAM` or `MAXSCRIPT_INTERRUPTED` always beats a code guessed from the message.

### Bridge health from a pipe thread (`2ce5928`, reviewed in `011e547`)

The fork's bridge answers a `health` command on the pipe thread that received it. It never posts to the main thread and never touches the scene, so it answers while Max is hung. The reply contains:

| Field | Meaning |
|---|---|
| `mainThread.state` | `responsive`; `busy_mcp` (a bridge request is executing on the main thread); `busy_other` (the main thread stopped pumping messages and no bridge work is running, e.g. a render, a script run from the UI, a plugin, or a deadlock); `unknown` (no heartbeat is available; callers fall back to `ping`); `not_initialized`; `shutting_down` |
| `mainThread.pumping` | `false` once the heartbeat is older than 3 s, `null` without a heartbeat. The dialog monitor's `main_thread_pumping` uses the same 3 s rule. |
| `mainThread.heartbeatAgeMs` | Age of the last heartbeat. Every second, a thread-pool timer posts one beat message to the executor's hidden window (one at a time; a beat still unanswered after 10 s is posted again), and the main thread records the time when it picks it up. Posted messages are taken in turn with the bridge's own work, so an age above about 3 s means the main thread isn't pumping. The first version used `WM_TIMER`, but Windows starves `WM_TIMER` under steady posted traffic, which made a pumping main thread look stalled. The review caught this. |
| `mainThread.windowHung` | `IsHungAppWindow` on the executor window |
| `executor.running` | The running request's command type, client id, `requestId`, and how long it has run |
| `executor.queued`, `oldestQueuedMs`, `oldestQueuedCmd`, `oldestQueuedRequestId` | Work waiting for the main thread: how many items, and the oldest one's age, command type and `requestId` (no client id) |
| `clients.connected`, `clients.inflight[]` | Connected pipe clients (this also covers #6) and each client's request: client id, `requestId`, command type, elapsed time. The count includes the client asking. Before the 1.7.6 merge, requests the bridge dispatched itself (e.g. through `invoke_tool`) were counted as `nested` and flagged `internal`; since the merge they aren't listed separately: `nested` is always 0. |

`get_bridge_status` asks for `health` first, over the control channel, so another request's pipe lock can't delay it. Then the first of these that applies decides the answer:

- **Not even the pipe threads answer within 3 s:** the probe's own busy/hung verdict is returned, instead of waiting out a second timeout.
- **A Cosmos import settle guard is active:** the busy/not-responding verdict from `health` and the dialog step below are skipped, and the `ping` is refused with `IMPORT_SETTLING`. Open dialogs are listed as context (`open_dialogs`). A held pipe lock or a remembered hung PID is still reported before the guard.
- **A modal dialog holds the running request** (since the 1.7.5 merge): the main thread is `busy_mcp` and still pumping, and the dialog monitor lists a dialog on Max's main thread that opened during that request. It returns `pong: false` with `bridge_state: "blocked_by_dialog"` and `bridge_code: "BLOCKED_BY_DIALOG"`, without queueing a `ping`. The result lists the dialogs and the waiting request's command, says who sent it (`main_thread.running.owner`), and lists this server's calls still waiting on a dialog (`blocked_calls`). The Cosmos browser, the Material Editor and floating viewports never count as dialogs here.
- **`busy_mcp` for at least 2 s, or `busy_other`:** it returns `pong: false` without queueing a `ping` behind the main thread. A main thread that stopped pumping lands here even with a dialog on screen; the dialog is then only listed as context (`open_dialogs`).
  - It says who holds the main thread (`main_thread.running.owner`): this server's request, another MCP client's request, a bridge request it can't attribute (`unknown`), or, with nothing running, work outside the bridge. The holder is matched by `requestId`; a `healthVersion` 1 reply, which has no request ids, falls back to command type and age. The client id only marks the bridge's own internal probes, which never count as another client. A request this server already abandoned is still recognised as its own, and so is a call of this server's that returned `BLOCKED_BY_DIALOG` and still runs in Max (marked `blocked_by_dialog`). `health` and `max_dialogs` requests are never counted as another client's work.
  - CPU is sampled for 1 s, only when the main thread isn't pumping (always the case for `busy_other`). If no bridge work is running and no Max window is flagged hung yet, it pings after all. `IsHungAppWindow` takes about 5 s to flag a window, so this happens early in a stall.
  - The result is called `not_responding` when Max has exited, or when the main thread hasn't pumped for 20 s or more and the sample says blocked. If the request holding it is this server's own, it must also be past its timeout plus grace; for a call that returned `BLOCKED_BY_DIALOG`, the client's record of that call gives its timeout and running time. Then it points to `capture_hang_diagnostics`. That rule covers only the verdict built from `health`. When it pings, a ping unanswered after 5 s is called `not_responding` if a Max window is hung with idle CPU by then. In the live test below, that took about 6 s.
- **Otherwise** (main thread `responsive`, `unknown`, `not_initialized` or `shutting_down`, or a bridge request younger than 2 s): the usual `ping` follows, and the result gains a compact `health` block. A ping held by a dialog gives the `blocked_by_dialog` status, with `request_sent: true`.
- **TCP transport, or a bridge without `health`** (any of upstream's, including the 2023–2026 files in `native/bin/`): `health` is skipped, or answered with "Unknown command type" at once. `get_bridge_status` then falls back to `ping` as before. With upstream's 1.7.5 and 1.7.6 bridges, a ping held by a dialog still gives `blocked_by_dialog`.

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

- **Shutdown drain** (Geddart `8e004f7`). `MCPBridgeGUP::Stop()` now closes the executor gate first, then fails every queued and deferred work item, so client threads blocked in `ExecuteSync` wake at once. A background `ExecuteSync` during shutdown fails fast. `Initialize()` reopens the gate. Requests failed this way never ran, and report `BRIDGE_DOWN` (retryable) since `39c7e8e`. Upstream 1.7.6 added the same drain: `MainThreadExecutor::StopAccepting`, which the 1.7.6 merge uses in place of the fork's `BeginShutdown`, in upstream's shell. Work refused or drained while Max exits now fails with upstream's text, "3ds Max is exiting; the request did not run", still as `BRIDGE_DOWN`. The fork's executor adds reopening the gate in `Initialize()`, dropping the drained requests' queued messages on the main thread, and the `health` heartbeat. So every bridge in `native/bin/` has the drain now.
- **Expired work never runs.** A work item that timed out while still queued can't run later against the caller's unwound stack. The fork ported this from Geddart (`599e6f7`); since the 1.7.5 merge it is upstream's mechanism (`5c44e76`): the item is cancelled with "Main thread execution timed out before starting; queued work cancelled". Work that already started is waited for.

### Orphaned servers (#6, `a9d2a0d`)

On Windows, a child process outlives its parent. So a crashed or killed MCP client used to leave its `maxmcp.server` running, and that server could still hold a pipe connection to Max. Four such servers were found running at once.

The server now exits when its parent client exits. If the parent is a pass-through launcher (a venv `python.exe` stub, `py.exe`/`pyw.exe`, `uv`/`uvx`, the `3dsmax-mcp.exe` console script, or `cmd.exe`), the launcher's own ancestors are watched too. The server also exits after stdin closes. To turn the watchdog off, set `MAXMCP_PARENT_WATCHDOG=0`.

The bridge side of #6, reporting how many clients are connected, is the `clients` block of `health` above.

### Upstream 1.7.5's dialog handling, fixed on the merge

Commits `8a8b26c`, `ffccbab`, `f70b0cc` and `34a7ce5`, after the merge itself (`a07ef93`), and the second merge review's `39c7e8e` (native) and `ec903a5` (Python). The native fixes are only in the fork's bridges (see "Which bridge" at the top of this page), which also have #12 (`aa3d91d`, quiet mode keeps save prompts visible). With upstream's bridges, which `native/bin/` has for 2023–2026 since the 1.7.6 merge, only the Python half applies.

That matters for #12 too. With the fork's bridge, as deployed on the production Max 2027, the native quiet policy keeps a script that mentions a reset, open, fetch or quit command out of quiet mode. With a stock bridge (2023–2026, or any bridge from upstream's installer), such a script sent with no explicit `quiet` still runs in quiet mode; the Python side only turns an explicit `quiet=True` without `allow_discard=True` into `quiet=False`. Since 2026-10-16 the Python side also refuses such a script with `UNSAVED_CHANGES` while the scene has unsaved changes, with any bridge, unless `allow_discard=True`. That covers `loadMaxFile`, which never asks to save even with prompts on. The check reads `getSaveRequired()`, the state Max's own prompt uses, so edits made with undo off, as plain `execute_maxscript` edits are by default, don't count, as in Max. Scripts that call `checkForSave()` are refused too, since the name may sit in a comment or the answer may be ignored.

Merging 1.7.5 brought its blocking-dialog handling: `BLOCKED_BY_DIALOG`, `max_dialogs`, `MAX_DIALOG_ERROR` and quiet mode. Where upstream already solved the same problem, the merge kept its mechanism (the reader thread, the queue cancellation) and fitted the fork's diagnosis around it. How the error codes rank is under [Client side](#client-side-a9d2a0d), and `get_bridge_status`'s dialog step under [Bridge health](#bridge-health-from-a-pipe-thread-2ce5928-reviewed-in-011e547). The Cosmos import's dialog handling and the windows the dialog monitor must never treat as dialogs (`6e03f99`) are in cosmos-import.md: [Dialogs during an import](cosmos-import.md#dialogs-during-an-import-upstream-175) and [Windows that are never dialogs](cosmos-import.md#windows-that-are-never-dialogs-6e03f99).

A review of the merged branch found these problems, all fixed:
- **A hung Max with a dialog on screen was called "blocked by a dialog".** The client raised `BLOCKED_BY_DIALOG` for any main-thread dialog, even while Max's main thread had stopped pumping, so the deadline diagnosis and the hung-PID latch never ran. The dialog monitor's status now reports `main_thread_pumping` (the same heartbeat rule as `health`). While it is false, a dialog doesn't hold the call: the call is diagnosed like any other, gives `MAX_NOT_RESPONDING` once Max stays blocked, and sets the hung-PID latch again. `get_bridge_status` already applied that rule.
- **`get_bridge_status` crashed** (`'NoneType' object has no attribute 'get'`, returned as `BAD_PARAM`) when this server's `BLOCKED_BY_DIALOG` call was still running and Max had then been blocked for 20 s. The overdue rule now uses that call's own record (`blocked_call`): `not_responding` once it is past its timeout plus grace, `busy` before.
- **A transacted call could commit before its acknowledged error was recorded.** The monitor posts the OK click, then publishes the error, so the operation could resume and commit first. `ThrowIfDismissed` now waits (at most 3 s) for the publish, so such a call is always rolled back.
- **A Qt press could click after `respond` reported it failed.** When Max's main thread had started the press but not finished it within 1.5 s, `respond` returned a retryable `MAIN_THREAD_BUSY` and the click could still follow. It now returns `DIALOG_OUTCOME_UNKNOWN` (not retryable: inspect again). A press the main thread never started is withdrawn, so `MAIN_THREAD_BUSY` means nothing was pressed.
- **A direct-mode read could leave Max in quiet mode.** `get_wired_params`, `get_state_sets` and `get_camera_sequence` run their scripts on a pipe thread, and their save/restore of Max's single quiet-mode flag could interleave with a main-thread one. Quiet mode now changes only on the main thread. Upstream 1.7.6 moved these reads to the main thread and fails any bridge MAXScript run off it, which removes the race at its source; the 1.7.6 merge uses that.
- **A Qt MAXScript error box seen while the main thread didn't pump was never acknowledged**, so its call returned `BLOCKED_BY_DIALOG` instead of `MAX_DIALOG_ERROR`. It's read again once the main thread pumps.
- **Render cleanup stopped halfway after an acknowledged error**, leaving the agent viewport's leased V-Ray settings modified. The cleanup now finishes; the call still fails with `MAX_DIALOG_ERROR`.
- **Cosmos imports:** `MAX_DIALOG_ERROR` on the preparation call returns `not_imported` with the restore scripts instead of escaping; on the browser action it still waits for the browser; a dialog already holding Max's main thread stops the import before anything is sent, and again right before the preparation call. After the settle, an error box during the confirming snapshot makes it retake the snapshot once, and one during the restore call has its result read back, instead of offering a restore that already ran (`34a7ce5`).
- Tool windows (Cosmos browser, Material Editor, viewports) of any thread now stop the automatic inspect, and replies never list them as open dialogs, also with upstream's bridges. The `BLOCKED_BY_DIALOG` registry drops probes before real calls when full.

A second review of the merge found these, also fixed. In the Python client (`ec903a5`):
- **A lost request was `BAD_PARAM`.** `RequestOutcomeUnknown` had no code, so a pipe that closed after dispatch told the agent to fix its arguments and call again: the replay the client avoids. It is now `REQUEST_OUTCOME_UNKNOWN`. `get_bridge_status` no longer lets such a ping escape as an error; it reports `connection_lost`, or `not_responding` once Max has exited.
- **A blocked call whose connection broke looked like a clean failure** (`completed`, `ok: false`). `blocked_calls` now reports it as `lost` with `outcome: "unknown"` and `request_sent: true`.
- **`max_dialogs` on an old bridge returned `BAD_PARAM`** ("Unknown command type"), which invites a retry with other arguments. It now gives `BRIDGE_OUTDATED`.
- **The `BLOCKED_BY_DIALOG` hint lacked the always-ask rule.** An agent working unattended was told to answer "as the task intends", which for a reset or load can mean Don't Save. The hint, `get_bridge_status` and `max_dialogs` now say to always ask before Save, Don't Save, overwrite, Fetch or Reset.

In the bridge (`39c7e8e`):
- **A Win32 press could click minutes later.** It was posted even while Max's main thread didn't pump, so it landed whenever Max recovered. `respond` now fails with `MAIN_THREAD_BUSY` then, with nothing pressed, and the press is checked again (same dialog, button still enabled) when the main thread takes it. An error box that couldn't be acknowledged for that reason is tried again later, like a Qt one.
- **Work the executor refused or drained came back as `BAD_PARAM`** (Max exiting, the bridge not initialised, a failed post). It never ran, so it's now `BRIDGE_DOWN`, retryable.

Not changed: the native tool-window rule matches by title only, so a real message box captioned exactly like the Material Editor would not be reported. A class-based exception was considered, but the Compact Material Editor itself may be a `#32770` dialog, which would make it a "dialog" again; that can't be checked without Max.

## How it was verified

- **Executor fixes:** loaded in Max 2026 on 2026-10-01. Ping, read and mutating calls all OK. Quitting Max while a call is queued hasn't been exercised live yet, on either version.
- **Client side, tested live on Max 2026** (2026-10-01, the 1.7.3-based fork): a 20 s `sleep` was run from a second process, and `get_bridge_status` was called from the MCP.
  - The status came back in about 6 s with `bridge_state: "not_responding"`, the in-flight probe, and process evidence (window hung, 0.03 CPU-s/s).
  - After the sleep, the next status was `pong: true` in 2.6 ms, so the fail-fast latch clears.
  - Without the `health` command, a legitimate long call from another client looks like a deadlock from outside. The `health` command narrows that gap: it names the other client's request and reports it busy. A call that keeps the main thread from pumping for 20 s or more with almost no CPU, such as a long `sleep`, is still reported `not_responding`.
- **`health`, tested live on Max 2026** on 2026-10-01, with the rebuilt 1.7.3-based bridge. Idle, `get_bridge_status` returned `pong: true` with the `health` block. During an 8 s `sleep` sent from another process, it answered at once with `bridge_state: "busy"`, `MAX_BUSY`, `busy_mcp` and owner `other_client`.
  - **On Max 2027** (2026-10-11, the fork's 2027 bridge `48c186df…`), idle only: `get_bridge_status` returned the health block (heartbeat, queue, clients).
  - Not run live: the busy case on Max 2027, and the 20 s `not_responding` path on either version.
- **Orphaned servers:** the watchdog is unit tested; a server exiting with its client hasn't been tested live. The client count in `health` was read live on both versions.
- **`capture_hang_diagnostics`:** run against child processes started by the tests. Their frames resolve inside `ntdll`, every thread was confirmed resumed afterwards, and the child still answered. Its rules are also unit-tested on synthetic captures built from frames of the two real hang dumps. It hasn't yet been pointed at a real hung Max with the packaged tool; the original script it comes from was.
- **The 1.7.5 merge:** the Max 2026 tests above ran on the fork's 1.7.3-based build, before the merge replaced the client's read loop. Since 2026-10-11, Max 2027 runs the 1.7.5 merge: Python from the 1.7.5-based `fix/fork-fixes` and the fork's 2027 bridge. The only checks of this page's features there were read-only: the health block above, and `max_dialogs` answering from its monitor. No dialog, busy or hang case has been run on it, so those paths of the merged client, the dialog handling and its fixes have unit tests only. The fork's 1.7.5-based 2026 bridge (`d5cd6ba5…`) never ran in Max, and the 1.7.6 merge replaced it with upstream's build.
- **The 1.7.6 merge** (`298a295`): unit tests (1119 passed, 5 skipped) and the SDK-independent native tests (6/6) pass, and the 2027 bridge builds. Nothing on this page has been run live with it yet: not the health block, the busy and hang paths, upstream's reply deadline, nor quitting Max with clients connected.

Tests:
- `tests/test_hang_diagnosis.py`: lock timeout, deadlines, probes, the hung-PID latch, status payloads, a dialog ahead of the deadline diagnosis, a dialog on a main thread that stopped pumping, detached readers.
- `tests/test_parent_watchdog.py`
- `tests/test_bridge_health.py`: who holds the main thread, and the `blocked_by_dialog` step against the settling guard and a stale heartbeat.
- `tests/test_hang_capture.py`: 46 cases, covering rules, main-thread identification, PID resolution, and live captures of spawned processes.
- `tests/test_suspend_guard.py`: every pause is resumed exactly once, `release_all()` and the exit paths, the 250 ms cut-off, state-change pauses and reused thread ids.
- `tests/test_merge_review_safety.py`: the second review's client fixes (`REQUEST_OUTCOME_UNKNOWN`, lost blocked calls, `BRIDGE_OUTDATED`, the always-ask advice).
- Native, SDK-independent: `native/tests/executor_tests.cpp` and `native/tests/health_tests.cpp`. The second covers the heartbeat, queued and running visibility without blocking, expired items, and the client registry. `native/tests/error_code_tests.cpp` checks that refused or drained work maps to `BRIDGE_DOWN`.
- `native/tests/dialog_watch_tests.cpp`: the monitor's window rules, plus the 1.7.5 fixes above (publish before resume, withdrawn and unknown-outcome presses, the pumping flag, the Qt error-box retry, the deferred cleanup scope). The publish race is reproduced by pinning the test to one CPU: without the fix, 4 of 5 runs resumed before the error was published. None of these 1.7.5 fixes is tested live yet.

## Upstream status

Upstream 1.7.5 (2026-10-01) covers two parts: a call waiting on a modal dialog returns `BLOCKED_BY_DIALOG` (answered with `max_dialogs`), and a timed-out queued request is cancelled.

Upstream 1.7.6 (2026-10-14) adds three more:
- **A reply deadline on every request:** after about 130 s for ordinary calls, `MAX_BUSY`, sent and not retryable. The 1.7.6 merge keeps the fork's rule as the main one (wait while Max is busy or responsive, give up only when it exited or is confirmed hung) and uses upstream's deadline only when Max's PID can't be found. See [Client side](#client-side-a9d2a0d).
- **The shutdown drain** (`StopAccepting`) and **pipe I/O cancelled on `Stop`** (covers #29), so Max quits with clients connected. See [Native executor](#native-executor-adcfd93-ported-from-geddarts-fork).
- **MAXScript-backed reads on Max's main thread**, which settles the quiet-mode race fixed in `8a8b26c`.

The rest is not upstream: the lock timeout, the process diagnosis and `MAX_NOT_RESPONDING`, `health`, `capture_hang_diagnostics` and the orphaned-server watchdog. The fixes to 1.7.5's dialog handling above aren't upstream either. Related work:
- Upstream PR 32 (executor cancellation, closed unmerged) was deliberately not ported. Its contributor's fork has been deleted.
- The lock timeout idea comes from stoxsss111's fork. From Geddart's come the executor drain and the process-exit check (`_process_alive` there, rewritten here in `maxmcp/process_health.py`).

Offline scratch-driver tests use SCRATCH_HELPER_PATH to locate a separately maintained helper. Set it to your helper path before running Python unittest discovery; the tests use fake clients and do not contact Max.
