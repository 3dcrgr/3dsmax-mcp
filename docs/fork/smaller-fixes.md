# Smaller fixes

Two problems that came up during the same production work. Neither was dangerous, but both misled the agent.

## `execute_maxscript` reported every failure as a parse error

Commit `d971a56`.

**What happened.** `quitMax #noPrompt` returned "MAXScript execution failed (parse error)", and so did `(quitMax #noPrompt; "x")`. Neither has a syntax error. `try (quitMax #noPrompt quiet:true) catch ()` worked.

**Why.** The bridge treated any `ExecuteMAXScriptScript` call that returned false as a parse error. But that call also returns false when a script is interrupted while it runs: by `quitMax`, `resetMaxFile`, `exit`, an Escape/abort, or a system exception.

**What changed.**
- **The bridge checks before choosing an error.** When a script fails, it compiles the same text again with `Parser::compile`. This only compiles; it never runs the script.
- **A real syntax error** keeps the prefix "MAXScript execution failed (parse error):", now followed by the compiler's own message with the user's line numbers. Its code is `BAD_PARAM`.
- **Text that compiles** is reported as "MAXScript did not complete (not a parse error): the script was interrupted ...". It gets the new code `MAXSCRIPT_INTERRUPTED`, which is not retryable and is never `BRIDGE_DOWN`.
- **No re-check during shutdown.** The check is skipped while Max or the bridge is shutting down.
- **Hybrid native handlers** return the same explicit code, so keyword matching on the message can't mislabel them.
- **The skill guide** says how to quit Max: `try (quitMax #noPrompt quiet:true) catch ()`.

Tests: `tests/test_execute_failures.py`.

## Curve tools rejected Line objects

Commit `e1dbabe`.

**What happened.** `inspect_curve` refused objects of class `line` with "Editable spline base required". A Line is an editable spline: it supports all the spline functions.

**What changed.** One shared MAXScript check (SplineShape *or* line) now decides "editable spline base" everywhere spline data is read or edited. Parametric shapes such as Rectangle, Circle and Text still need an explicit conversion, and NURBS curves are still rejected.

**Verified live** on 2026-10-01: `inspect_curve` reads Line objects.
