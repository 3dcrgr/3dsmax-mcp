# Chaos Vantage: status and Live Link

Added on the `merge-1.7.5` branch (2026-10-06). Chaos is working on an official MCP server for Vantage but hasn't shipped one: Vantage 3.4.0 has none. These two tools cover what can be done safely without it.

## What they use

- **Vantage's local command server.** Vantage 3.4 runs an undocumented HTTP server on localhost, on the IPv6 loopback only. The port is 20702 unless Vantage's preferences (`%APPDATA%\Chaos\Vantage\vantage.ini`, `commandPort`) say otherwise. `GET /ping` answers 200 when Vantage is ready and 503 while it initialises. `POST /command` takes `{"command": ...}`. Chaos's own 3ds Max scripts (`dcc_scripts\3dsMax\Vantage-UtilityFunctions.ms` in the Vantage install) use it the same way. It isn't a public interface, so a Vantage update can change it.
- **V-Ray's MAXScript Live Link functions** (V-Ray 6 and later, distributed-rendering protocol 2): `vrayGPUSetLiveLinkServers #("localhost:20703")`, `vrayGPUStartIPR liveLinkMode:true` and `vrayGPUStopIPR()`. That's the minimal path in Chaos's `Vantage-LiveLink.ms`, without its renderer switch, message boxes, sleeps and timers. The link port is Vantage's `liveLink2Port`, 20703 by default.

## `vantage_status`

Reads Vantage's state without sending anything to 3ds Max:
- `running` and `pids`, from the process list. With no `vantage.exe` running, nothing is sent over HTTP: a refused localhost connection takes about 2 s per address on Windows.
- `phase`: `not_running`, `starting` (the process runs but the port isn't open yet, for about 5 s after launch), `initializing` (ping 503) or `ready`.
- From `getStatus`: `state` (for example `RenderingInteractive`, `LoadingScene` or `RenderingOffline`), `live_link`, `offline_task` with the offline frames, `last_task_id` and `version`. Fields Vantage adds later are reported in `other_fields`.
  - `live_link` is `Online` while waiting for a DCC, `Connected` while linked, and `Offline` when the Live Link server is off.
  - `linked` is true while a link is connected. It doesn't say which 3ds Max is linked.
- A warning when more than one `vantage.exe` runs. A second instance can't open the Live Link port, but may still answer on the command port.

## `vantage_live_link`

**`action="start"`.** Each check below refuses without changing anything:
- Vantage must be running and ready: `VANTAGE_NOT_RUNNING`, `VANTAGE_STARTING` or `VANTAGE_UNAVAILABLE`. The tool doesn't launch Vantage.
- If Vantage already reports a connected link, the result is `already_linked` and nothing is sent to Max.
- If Vantage's Live Link server is off, the result is `VANTAGE_LINK_OFFLINE`. During an offline render it's `VANTAGE_BUSY`.
- If the agent viewport holds a render preview, the result is `RENDER_BUSY`. A Live Link would otherwise render the agent's locked view with its preview settings.
- **In one MAXScript call,** so nothing can start an IPR in between:
  - The V-Ray functions must exist, or the result is `UNSUPPORTED`.
  - No undo operation may be open (`USER_BUSY`).
  - The production renderer must be V-Ray GPU, checked by class ID (`UNSUPPORTED`). The tool never switches renderers.
  - V-Ray must report protocol 2 (`UNSUPPORTED` otherwise). Protocol 1 needs Chaos's script, which rewrites `vrayrt_dr.cfg` and render settings.
  - No V-Ray IPR may be running (`vrayIsRenderingIPR()`), and no V-Ray viewport IPR checked in the active viewport (`RENDER_BUSY`).
  - Then the call sets the link server, starts the Live Link IPR and marks it in the Max global `mcp_vantageLiveLink`.
- **Then it polls Vantage, never Max,** every 0.5 s for up to `wait_seconds` (default 20, at most 60).
  - The result is `linked` on `Connected`, or `link_failed` if Vantage reports `Offline`.
  - On timeout it's `starting`, with a warning. The IPR keeps running, because large scenes take longer to translate.
- **Afterwards,** one read-only probe reports `vrayIsRenderingIPR()` as `vray_ipr`, and any dialog open in Max is listed under `dialogs`.

**`action="stop"`:**
- If Vantage doesn't report a connected link, the result is `not_linked`, and nothing is sent to Max.
- In one MAXScript call:
  - If no V-Ray IPR runs in this Max, the result is `not_linked`; the link may belong to another Max.
  - A link without the `mcp_vantageLiveLink` mark gives `LIVE_LINK_NOT_OWNED`, unless `force=true`, which is for when the user asked.
  - Otherwise it calls `vrayGPUStopIPR()`, never `vrayStopIPR()`, and clears the mark.
- Then it polls Vantage for up to 15 s until the link is no longer `Connected`.

Nothing else is changed. In particular, `rt_system_vrayLog_show` isn't touched: Chaos's scripts force it to 0 on start and to 2 on stop. The link server list is left set, as Chaos's stop leaves it.

## Not included

- **Launching Vantage.** A process started by the MCP server might be ended with it, and Chaos's own launch check can start a second instance while the first is still initialising.
- **Setting the camera or scene state on a running Vantage.** No command exists for either. Under Live Link the camera follows Max, so set it there.
- **Loading scenes, and offline or command-line renders.** These need a `.vrscene` export, which must not be done on production scenes: it crashed Max.
- **`startLiveLinkSequence` renders, `startSnapshot`, `getImage` and `loadConfig`.** They're untested, and Chaos's own sequence render hands control to a Vantage dialog. `loadConfig` has an open bug that can detach Vantage's view from the Max camera.

## While linked

Live Link streams every scene, camera and light change from Max to Vantage, so:
- Agent viewport render modes refuse with `RENDER_BUSY`; shaded captures still work.
- `capture_multi_view` without the agent viewport, and `isolate_and_capture_selected`, change the view or hide nodes, and those changes reach Vantage.
- Whether `render_scene` stops, fails or runs alongside a Live Link IPR is untested.

## How it was verified

- **Unit tests:** 33 in `tests/test_vantage.py`. The HTTP client runs against a real local server on `::1`. The tool's checks, refusals, polling and MAXScript text are tested against a mocked bridge. The scripts never mention a file command (#12) or any call Chaos's toolbar makes beyond the minimal ones.
- **Live, read-only, on 2026-10-06:** `vantage_status` against Vantage 3.4.0 with Max linked returned `ready`, `RenderingInteractive`, `Connected` and version 3.4.0, in 20 ms.
- **Not tested live yet:** starting and stopping the link. Test it on a small throwaway scene first, then check:
  - which `vrayIsRenderingIPR()` value a Live Link IPR reports;
  - how long Vantage takes to report `Connected`;
  - whether V-Ray shows a dialog;
  - how the agent viewport behaves while linked.
- The tools are on `merge-1.7.5` only and aren't deployed.
