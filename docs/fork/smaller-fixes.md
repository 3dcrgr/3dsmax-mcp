# Smaller fixes

Four problems that came up during the same production work. None was dangerous, but each misled the agent or the user.

## `execute_maxscript` reported interrupted scripts as parse errors

Kept in the 1.7.5 merge: upstream has no such check.

Commit `011e547`.

**What happened.** `quitMax #noPrompt` returned "MAXScript execution failed (parse error)", although it has no syntax error. `try (quitMax #noPrompt quiet:true) catch ()` worked.

**Why.** The bridge wraps every script in a MAXScript try/catch, so ordinary runtime errors already came back as `MAXScriptError` with the exception text. It treated any `ExecuteMAXScriptScript` call that returned false as a parse error. But that call also returns false when something the try/catch can't catch stops a script while it runs: `quitMax` (the case here), an Escape/abort, or a system exception.

**What changed.**
- **The bridge checks before choosing an error.** When that call returns false, the bridge compiles the same text again with `Parser::compile`. This only compiles; it never runs the script.
- **A real syntax error** keeps the prefix "MAXScript execution failed (parse error):", now followed by the compiler's own message with the user's line numbers. Its code is `BAD_PARAM`.
- **Text that compiles** is reported as "MAXScript did not complete (not a parse error): the script was interrupted ...". It gets the new code `MAXSCRIPT_INTERRUPTED`, which is not retryable and is never `BRIDGE_DOWN`.
- **No re-check during shutdown.** While Max or the bridge is shutting down, the check is skipped and the reply is "MAXScript did not complete: 3ds Max is shutting down ...", also with `MAXSCRIPT_INTERRUPTED`.
- **If the check itself can't run,** the reply says the failure is either a parse error or an interruption, with code `BAD_PARAM`.
- **Hybrid native handlers** return the same explicit code, so keyword matching on the message can't mislabel them.
- **Max's own error text** (since the 1.7.5 merge). Upstream 1.7.5 reads the error text Max returns with the failure. The fork's messages keep their wording and add that text as "Max reported: ...". A parse error uses it as its detail only when the compiler gives none.
- **The skill guide** says how to quit Max: `try (quitMax #noPrompt quiet:true) catch ()`, and to expect the bridge connection to drop.

Tests: `tests/test_execute_failures.py`. They cover how the Python server classifies the bridge's messages, not the re-compile itself.

**Status.** The check is in the native bridge, and only the 2026 bridge in `native/bin/` has it. With upstream's 1.7.5 bridges (2023, 2024, 2025 and 2027), an interrupted script gets the same reply as a syntax error: "MAXScript execution failed: <Max's error text>", with a code picked from keywords in that text, usually `BAD_PARAM`.
- **Verified live** on 2026-10-01 with the fork's 1.7.3-based 2026 bridge: `(1 +` returned `BAD_PARAM` with the compiler's message. Quitting with `try (quitMax #noPrompt quiet:true) catch ()` was also run live: the bridge connection dropped and Max exited cleanly.
- **Not tested live yet:** the 2026 bridge built from the 1.7.5 merge (sha256 `e275e129…`), and a bare `quitMax #noPrompt`, which should give `MAXSCRIPT_INTERRUPTED`. Run that check with the merged bridge, on a throwaway Max.
- Since the merge, `execute_maxscript` runs in Max's quiet mode by default. Without `#noPrompt`, quiet mode may answer Max's save prompt on its own and lose unsaved work (fork issue #12, not tested yet).

## Failed agent scripts printed errors in the user's Listener

Superseded by upstream 1.7.5 (`5c44e76`), which made the same quiet-errors change and also runs agent scripts in Max's quiet mode. The fork dropped its own lines in the merge (`a07ef93`).

Fork issue #8. Commit `6552786`.

**What happened.** When an agent's script didn't compile, Max printed the raw exception into the MAXScript Listener the user had open, for example `-- MAXScript ExecuteMAXScriptScript Exception: -- Syntax error: at off, expected name`. Nothing had run and the scene hadn't changed, but the user saw red errors, from code they hadn't written, with nothing saying they came from the MCP.

**What changed.** `execute_maxscript`, and the native tools that run MAXScript internally, now run scripts with quiet errors. Compile errors and aborts go to Max's log, not the Listener, and the compile-only check behind the parse-error detail prints nothing either. The caller gets the same results as before: `BAD_PARAM` "MAXScript execution failed (parse error): <detail>", `MAXSCRIPT_INTERRUPTED`, and runtime errors with their message. A script's own output, such as `print`, still reaches the Listener.

**Status.** This is in the native bridge. All of upstream's 1.7.5 bridges have the same change, and so does the 2026 bridge built from the merge. Verified live with the fork's 1.7.3-based 2026 bridge (sha256 `c621db10…`) on 2026-10-01: `(1 +` returned `BAD_PARAM` with the compiler's message and left the Listener unchanged, and `print "hello"; 42` returned `42` with only `"hello"` added to the Listener. Not checked live yet with the merged bridge (sha256 `e275e129…`).

Upstream's quiet mode makes prompts take their default answer; `execute_maxscript(quiet=False)` doesn't set it. The fork changes quiet mode only on Max's main thread (`8a8b26c`, see [hung-max.md](hung-max.md#upstream-175s-dialog-handling-fixed-on-the-merge)).

## Curve tools rejected Line objects

Commit `1261998`.

**What happened.** `inspect_curve` refused objects of class `line` with "Editable spline base required". A Line is an editable spline: it supports all the spline functions.

**What changed.** One shared MAXScript check (SplineShape *or* line) now decides "editable spline base" everywhere spline data is read or edited. `inspect_curve` and `edit_curve` still refuse parametric shapes such as Rectangle, Circle and Text, so convert those first. NURBS curves are still rejected. `draw_spline` now edits a Line in place instead of converting it to an Editable Spline, and its `get` action reports a Line as `editable: true`. It still converts a bare parametric shape automatically; one with modifiers needs `convert=true`.

Tests: `tests/test_curve_line_support.py`.

**Verified live** on 2026-10-01: `inspect_curve` reads Line objects.

## The agent viewport couldn't be reclaimed after a restart

Fork issue #11. Commit `8330c2c`.

**What happened.** The agent viewport is a floating viewport the bridge opens for its own captures. The steps were: open it, take a Hold, restart Max, then `fetchMaxFile`. The restored layout brought back "Floating Viewport - 3", but the bridge didn't recognise it as its own. `agent_viewport(action="open")` then failed with "All floating viewports are in use", and every `source="agent"` call failed until the window was closed by hand.

**What changed.**
- The agent's floating slot is tagged in the scene's AppData.
- `open` reclaims a tagged floating viewport only if it's the same panel window that showed the tagged slot when the scene loaded, or when the bridge first looked after a restart, and only if that window hasn't been hidden or destroyed since. Max's default floating viewport, and a user's own panel, are never taken over.
- Scene loads (open, reset, new) are watched from the moment the bridge starts, so the first load after a restart counts.
- `release` restores the panel's previous name, and clears the tag only if this process held the window.
- `status` reports `reclaimable`, the window and `next_action: "open"`, and errors name the stale window.

**Status.** This is in the native bridge, which now also imports `SetWindowSubclass` from COMCTL32. It's in the 2026 bridge in `native/bin/`, built from the 1.7.5 merge (sha256 `e275e129…`). An earlier build with it (sha256 `1cf0f9d8…`) was deployed on 2026-10-01. Unit tested in `tests/test_agent_viewport_reclaim.py`.
- **The first live test was inconclusive.** After the restart, `open` worked, but it couldn't reclaim the restored panel, because that Hold was written by the old bridge, which never tagged it. Closing the stale panel let agent captures work again.
- A Hold taken with the new bridge is waiting for the next restart.
