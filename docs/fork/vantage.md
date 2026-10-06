# Chaos Vantage: status, image and Live Link

Added on the `merge-1.7.5` branch (2026-10-06). Chaos is working on an official MCP server for Vantage but hasn't shipped one: Vantage 3.4.0 has none. These tools cover what can be done safely without it.

## What they use

- **Vantage's local command server.** Vantage 3.4 runs an undocumented HTTP server on localhost, on the IPv6 loopback only. The port is 20702 unless Vantage's preferences (`%APPDATA%\Chaos\Vantage\vantage.ini`, `commandPort`) say otherwise. `GET /ping` answers 200 when Vantage is ready and 503 while it initialises. `POST /command` takes `{"command": ...}`. Chaos's own 3ds Max scripts (`dcc_scripts\3dsMax\Vantage-UtilityFunctions.ms` in the Vantage install) use it the same way. It isn't a public interface, so a Vantage update can change it.
- **V-Ray's MAXScript Live Link functions,** on V-Ray builds with distributed-rendering protocol 2 (DR2), V-Ray 7.3 and later. This machine runs V-Ray GPU 7.40.04 DR2.
  - The calls are `vrayGPUSetLiveLinkServers #("localhost:20703")`, `vrayGPUStartIPR liveLinkMode:true` and `vrayGPUStopIPR()`.
  - That's the minimal path in Chaos's `Vantage-LiveLink.ms`, without its renderer switch, message boxes, sleeps and timers.
  - The link port is Vantage's `liveLink2Port`, 20703 by default.

## `vantage_status`

Reads Vantage's state without sending anything to 3ds Max:
- `running` and `pids`, from the process list. With no `vantage.exe` running, nothing is sent over HTTP: a refused localhost connection takes about 2 s per address on Windows.
- `phase`:
  - `not_running`.
  - `starting`: the process runs but nothing accepts a connection on the port yet (about 5 s after launch); see `ping_error`.
  - `initializing`: ping 503.
  - `unresponsive`: it accepts the connection but doesn't answer within 2 s.
  - `unexpected`: another ping status, in `ping_status`.
  - `ready`. With `ready`, a failed getStatus is reported in `status_error`, `status_code` and `status_retryable`.
- **From `getStatus`:**
  - `state`, for example `RenderingInteractive`, `LoadingScene` or `RenderingOffline`.
  - `live_link`: `Connected` while linked, as seen live. `Online` (waiting for a DCC) and `Offline` (Live Link server off) are inferred from Chaos's wait loop and haven't been observed.
  - `offline_task` with the offline frames, `last_task_id` and `version`.
  - Fields Vantage adds later go in `other_fields`.
  - `linked` is true while a link is connected. It doesn't say which 3ds Max or DCC is linked.
- A warning when more than one `vantage.exe` runs. A second instance can't open the Live Link port, but may still answer on the command port.

## `vantage_get_image`

Saves the frame Vantage is showing as a PNG under `%TEMP%\3dsmax-mcp\` and returns its `file`, `width`, `height`, `source_width`, `source_height` and `factor`. It never sends anything to 3ds Max.

- **What it shows:** Vantage's current interactive frame, with its exposure and colour corrections, from the scene Vantage has open. With Live Link that's Max's linked view; without a link a warning says so.
- **Size:** Vantage returns the frame at its own resolution. The tool keeps every n-th pixel and row, with n a whole number, so the result is at most `max_width` pixels wide (default 1600; 0 = full size). Pixels are never resampled.
- **Waiting:** `minimum_samples` (1-16384) waits until Vantage has rendered that many samples, at most `timeout_seconds` (1-15 then; 1-60 with 0, default 5). With 0 it takes the next frame. While it waits, Vantage spins one CPU core and holds its other commands, and a target it never reaches costs the whole timeout. While Vantage is paused, `minimum_samples` is refused (`VANTAGE_PAUSED`).
- **Refusals:**
  - Vantage not ready, as for `vantage_live_link`.
  - `VANTAGE_BUSY` while it loads a scene or renders offline.
  - `VANTAGE_NO_FRAME` when Vantage had no frame.
  - `VANTAGE_TIMEOUT`: the samples weren't reached in time (not retryable), or, without `minimum_samples`, Vantage's command queue didn't advance (it may be paused, or another getImage is waiting; retry at most once).
  - `VANTAGE_FRAME_TOO_LARGE` for a reply over 512 MB (frames above roughly 13K × 7.5K).
  - `VANTAGE_NO_HOST` when Vantage found no supported DCC process. It also happens when 3ds Max runs as administrator and Vantage doesn't.
- **Don't poll.** Each `getImage` call leaks handles inside Vantage 3.4: it opens running processes one by one to look for a DCC and never closes them. That's one handle for every process it scans before finding 3ds Max, often dozens, and all of them when it finds none. Call it when an image is needed, never in a loop.

**The protocol**, worked out from `vantage.exe` 3.4.0 because Chaos doesn't document it:
- **Request:** `POST /command {"command": "getImage", "timeoutSeconds": n, "minimumSamples": m}`. The values must be JSON numbers; strings are ignored. The timeout defaults to 5 and is clamped to 1-120.
- **Reply:** `{"bitmap": <base64>, "headerSize": 56}`.
  - The base64 text ends with one NUL character.
  - It decodes to a complete 32-bit BI_RGB BMP, top-down (negative height), with pixels B,G,R,X. The fourth byte isn't a usable alpha. The tool accepts only uncompressed BI_RGB, and refuses when `headerSize` disagrees with the BMP's own pixel offset, so a format change fails instead of giving wrong colours.
  - `{"bitmap": null, "headerSize": 0}` means no frame.
- **Errors:** 403, 408 and 503 have empty bodies. 400 carries `{"errorText": ...}`.
- **Converting:** the embedded Python has no Pillow or NumPy, so the tool decodes the BMP and writes the PNG with the standard library. Measured end to end against a local server:
  - 1080p takes about 0.1 s.
  - 4K takes about 0.25 s and roughly 150 MB of memory at the default width.
  - 8K takes about 1 s and roughly 600 MB.

## `vantage_live_link`

Only on request. `action` is required.

**`action="start"`.** Each check below refuses without changing anything:
- **Vantage must be ready.** Otherwise it refuses with:
  - `VANTAGE_NOT_RUNNING`: the tool doesn't launch Vantage.
  - `VANTAGE_STARTING`, also while Vantage reports `Startup`.
  - `VANTAGE_UNRESPONSIVE`.
  - `VANTAGE_UNAVAILABLE`, also while Vantage reports `Shutdown`.
  - `VANTAGE_BAD_REPLY`.
- **Already linked:** if Vantage reports a connected link, the result is `already_linked` and nothing is sent to Max.
- **Live Link server off:** `VANTAGE_LINK_OFFLINE`.
- **Vantage busy:** loading a scene or rendering offline gives `VANTAGE_BUSY`.
- **A dialog open in Max** (read from the bridge's dialog monitor, not Max's main thread) gives `BLOCKED_BY_DIALOG`, and nothing is sent. Otherwise the start would run inside the dialog's loop.
- **Agent viewport:** if it holds a render preview, or its window is hidden or offscreen (it may still hold one), the result is `RENDER_BUSY`. A Live Link would otherwise render the agent's locked view with its preview settings. When a V-Ray IPR is already running, this check is skipped, because reading the agent viewport's state briefly activates its view.
- **In one MAXScript call,** so nothing can start an IPR in between:
  - The V-Ray functions must exist, or the result is `UNSUPPORTED`.
  - No undo operation may be open (`USER_BUSY`).
  - The production renderer must be V-Ray GPU, checked by class ID (`UNSUPPORTED`). The tool never switches renderers.
  - V-Ray must report protocol 2 (`UNSUPPORTED` otherwise). On protocol 1, Chaos's script links over Live Link v1 (port 20701) and turns off Cryptomatte and Enhancer VFB output with message boxes.
  - No V-Ray IPR may be running, and no V-Ray viewport IPR checked in the active viewport (`RENDER_BUSY`). The exception is the link this MCP server started, which is still connecting: that gives `changed: false`, and the tool waits for it.
  - Then the call sets the link server, starts the Live Link IPR and stores a new token in the Max global `mcp_vantageLiveLink`.
- **Not detected:** a production render in progress.
- **Then it polls Vantage, never Max,** every 0.5 s for up to `wait_seconds` (default 20, at most 60). The result is one of:
  - `linked`, on `Connected`.
  - `link_failed`, if Vantage reports `Offline`.
  - `starting`, on timeout. The IPR keeps running, because large scenes take longer to translate.
  - `start_failed`, if no V-Ray IPR runs in Max afterwards.
  - `requested`, with `wait_seconds=0`.
- **Afterwards,** one read-only probe reports `vrayIsRenderingIPR()` as `vray_ipr`, and any dialog open in Max is listed under `dialogs`.
- **If the start request's reply is lost** (`REQUEST_OUTCOME_UNKNOWN`), the link may have started. Check `vantage_status` before doing anything else.

**Ownership.** No V-Ray query identifies an IPR session, so "the link this tool started" means all of these:
- this MCP server process started it and still holds its token;
- the Max global still holds that token;
- Chaos's toolbar log (`VRayToVantageLog.log` in Max's user scripts folder, recreated on every toolbar Live Link action) is unchanged since.

Any call that sees no IPR running clears the global. After an MCP server restart, or once the toolbar has been used, the tool no longer counts the link as its own.

**`action="stop"`:**
- **With a connected link:**
  - The tool's own link is stopped.
  - Any other link gives `LIVE_LINK_NOT_OWNED`.
  - With `force=true`, whatever V-Ray GPU IPR runs in this Max is stopped, since Vantage doesn't say which DCC is linked. Use it only when the user asked.
- **Without a connected link:** only the tool's own link that never reached `Connected` is stopped, for example after `starting` or `link_failed`. A link that connected and then dropped may have been replaced by the user's own IPR, so it's left alone, and `force` changes nothing. Otherwise the result is `not_linked`, and nothing is sent to Max.
- **In one MAXScript call:**
  - No IPR running gives `not_linked`.
  - A production renderer other than V-Ray GPU gives `UNSUPPORTED`.
  - Otherwise it calls `vrayGPUStopIPR()`, never `vrayStopIPR()`, clears the global, and reports `vray_ipr_after`.
- **Then,** if a link was connected, it polls Vantage for up to 15 s until the link is no longer `Connected`.

**Nothing else is changed.**
- `rt_system_vrayLog_show` isn't touched. Chaos's scripts set it to 0 on start, then to 2 once Vantage answers the ping, on timeout and on stop, without restoring the user's value.
- The link server list is left set, as Chaos's stop leaves it.
- Chaos's Vantage toolbar button in Max doesn't show a link started by this tool, and clicking it stops that link.

## Not included

- **Launching Vantage.** A process started by the MCP server might be ended with it. Chaos's own launch check can also start a second instance while the first is still initialising.
- **Moving Vantage's view.** No command moves or rebinds it. Under Live Link it follows Max's viewport, so set it there.
- **Loading scene files, and command-line or offline renders of a scene file.** These need a `.vrscene` export, which must not be done on production scenes: it crashed Max.
- **Untested commands:**
  - Live Link sequence renders (`startLiveLinkSequence` and the like). Chaos's own sequence render hands control to a Vantage dialog.
  - `startSnapshot`.
  - `loadConfig`. It changes scene and camera settings, and has an open bug that can detach Vantage's view from the Max camera.

## While linked

- **What reaches Vantage:** scene changes (hiding, isolating, edits), and view changes in the viewport the Live Link IPR renders. So prefer agent viewport captures, and avoid `capture_multi_view` without it and `isolate_and_capture_selected`.
- **Agent viewport render modes** are expected to refuse while linked, with the message `RENDER_BUSY: V-Ray IPR is running outside AGENT VIEWPORT`, currently under code `BAD_PARAM`. Shaded captures don't start an IPR. This isn't tested live.
- **`render_scene`:** whether it stops, fails or runs alongside a Live Link IPR is untested.

## How it was verified

- **Unit tests** in `tests/test_vantage.py`:
  - The HTTP client runs against a real local server on `::1`, and against a socket that accepts but never answers.
  - The tool's checks, refusals, ownership rules, polling and MAXScript text are tested against a mocked bridge.
  - Every script is checked against the side-effecting calls Chaos's toolbar makes: renderer assignment, `VRayVantage_*`, `ShellLaunch`, `queryBox`, `messageBox`, `sleep`, `vrayExportVRScene`, `openFile`, `vrayStopIPR`, .NET, `rt_system_vrayLog_show`, `distributed_rendering` and callbacks. None mentions a file command (#12). Besides the three Live Link calls, they only read state.
- **An adversarial review** (four lenses, each finding checked by a skeptic) found two majors. Both are fixed:
  - Ownership could go stale.
  - A link that never connected couldn't be stopped.
- **Live, read-only, on 2026-10-06:** `read_status()`, all that `vantage_status` does, run from this checkout against Vantage 3.4.0 with Max linked. It returned `ready`, `RenderingInteractive`, `Connected` and version 3.4.0, in 20 ms.
- **Not tested live yet:** starting and stopping the link. Test it on a small throwaway scene first, then check:
  - which `vrayIsRenderingIPR()` value a Live Link IPR reports;
  - how long Vantage takes to report `Connected`;
  - what `liveLinkState` reports when idle, after a stop, and with the Live Link server off;
  - whether V-Ray shows a dialog, the VFB or the V-Ray Messages window;
  - how the agent viewport behaves while linked.
- **`vantage_get_image`:** the decoder and every error path are tested against a local stub server that serves the protocol above. It hasn't been run against a real Vantage yet; Vantage wasn't running when it was built. Check the colours and orientation against Vantage's window on the first live use.
- The tools are on `merge-1.7.5` only and aren't deployed. They need the 1.7.5 bridge, for quiet mode and the dialog monitor, so don't backport them to the 1.7.3-based line as they are.
