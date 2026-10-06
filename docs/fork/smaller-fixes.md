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
- **Not tested live yet:** the 2026 bridge built from the 1.7.5 merge (sha256 `d5cd6ba5…`), and a bare `quitMax #noPrompt`, which should give `MAXSCRIPT_INTERRUPTED`. Run that check with the merged bridge, on a throwaway Max.
- Since the merge, `execute_maxscript` runs in Max's quiet mode by default, except for scripts that mention a scene file command (see [#12](#quiet-mode-could-discard-unsaved-work) below).

## Failed agent scripts printed errors in the user's Listener

Superseded by upstream 1.7.5 (`5c44e76`), which made the same quiet-errors change and also runs agent scripts in Max's quiet mode. The fork dropped its own lines in the merge (`a07ef93`).

Fork issue #8. Commit `6552786`.

**What happened.** When an agent's script didn't compile, Max printed the raw exception into the MAXScript Listener the user had open, for example `-- MAXScript ExecuteMAXScriptScript Exception: -- Syntax error: at off, expected name`. Nothing had run and the scene hadn't changed, but the user saw red errors, from code they hadn't written, with nothing saying they came from the MCP.

**What changed.** `execute_maxscript`, and the native tools that run MAXScript internally, now run scripts with quiet errors. Compile errors and aborts go to Max's log, not the Listener, and the compile-only check behind the parse-error detail prints nothing either. The caller gets the same results as before: `BAD_PARAM` "MAXScript execution failed (parse error): <detail>", `MAXSCRIPT_INTERRUPTED`, and runtime errors with their message. A script's own output, such as `print`, still reaches the Listener.

**Status.** This is in the native bridge. All of upstream's 1.7.5 bridges have the same change, and so does the 2026 bridge built from the merge. Verified live with the fork's 1.7.3-based 2026 bridge (sha256 `c621db10…`) on 2026-10-01: `(1 +` returned `BAD_PARAM` with the compiler's message and left the Listener unchanged, and `print "hello"; 42` returned `42` with only `"hello"` added to the Listener. Not checked live yet with the merged bridge (sha256 `d5cd6ba5…`).

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

**Status.** This is in the native bridge, which now also imports `SetWindowSubclass` from COMCTL32. It's in the 2026 bridge in `native/bin/`, built from the 1.7.5 merge (sha256 `d5cd6ba5…`). An earlier build with it (sha256 `1cf0f9d8…`) was deployed on 2026-10-01. Unit tested in `tests/test_agent_viewport_reclaim.py`.
- **The first live test was inconclusive.** After the restart, `open` worked, but it couldn't reclaim the restored panel, because that Hold was written by the old bridge, which never tagged it. Closing the stale panel let agent captures work again.
- A Hold taken with the new bridge is waiting for the next restart.

## Quiet mode could discard unsaved work

Fork issue #12. Commit `aa3d91d`.

**What happened.** Upstream 1.7.5 runs MAXScript from the bridge in Max's quiet mode, where every prompt takes its default answer. For `resetMaxFile`, `loadMaxFile`, `fetchMaxFile` or `quitMax` without `#noPrompt`, that prompt is "save changes?", and its default can throw away unsaved work without anyone seeing it. This was found in review, before it happened to anyone.

**What changed.**
- A script that mentions `resetMaxFile`, `loadMaxFile`, `fetchMaxFile`, `quitMax`, `checkForSave`, `max reset file`, `max file new`, `max file open` or `max fetch`, or the Interface's `FileReset`, `FileFetch` or `LoadFromFile` (as reached through .NET's `COREInterface`), is never run in quiet mode by default. Its prompt reaches the agent as `BLOCKED_BY_DIALOG`, to answer with `max_dialogs` or hand to the user.
- The check is deliberately blunt: anywhere in the text, case-insensitive, strings and comments included. A false alarm only leaves a prompt visible, and `#noPrompt` or `quiet:true` arguments still suppress it. A miss could lose a scene.
- Merging isn't listed, because it never asks to save.
- `quiet=False` always shows prompts. `execute_maxscript(quiet=True)` forces quiet mode on such a script only together with `allow_discard=True`, because quiet mode answers the save prompt by discarding unsaved changes. Until the 1.7.5 merge, upstream's default was an explicit `quiet=True`, so agents and saved prompts that still pass it would have bypassed the rule; `quiet=True` alone is now sent as `quiet=False` for such a script (the Python check uses the bridge's list). The bridge's own `quiet: true` request field still forces quiet mode, for example through `invoke_tool`.
- When quiet mode was off for this reason, the bridge marks the response `meta.quietOverride: "file_command"` (or the client `"file_command_quiet_ignored"` when it set `quiet=True` aside). The agent sees it as `transport.quiet_override` (full tripback mode) and as a reply warning.
- Native tools that run MAXScript internally follow the same rule. The `manage_scene` fallbacks (`resetMaxFile #noPrompt`, `fetchMaxFile quiet:true`) still run without a prompt.
- **Limits:** the check reads only the submitted text. It doesn't see a command name built from pieces at runtime (`execute ("reset" + "MaxFile()")`), a file action run through `actionMan`, or commands inside scripts run with `fileIn`, `include`, `python.ExecuteFile` or `macros.run` (or startup and pipeline scripts called by name). Those run inside the quiet call, so their save prompts take the default answer. Agents should pass `quiet=False` when a script runs other scripts that may reset, open or quit. `#noPrompt` and `quiet:true` arguments in the script suppress the prompt whatever the bridge does, so the skill tells agents to add them only with the user's OK.

**Status.** Unit tested (`native/tests/quiet_policy_tests.cpp`, `tests/test_execute_failures.py`, `tests/test_merge_review_safety.py`). It's in the 2026 bridge built from the 1.7.5 merge; not tested live yet. The second merge review added `max fetch` and the Interface spellings to the list, and the client-side `allow_discard` rule. The client side works with any bridge; the longer native list is in the 2026 bridge in `native/bin/` (`d5cd6ba5…`). The live check: on a throwaway scene with unsaved changes, `resetMaxFile()` should return `BLOCKED_BY_DIALOG` with the save prompt, not reset.

## `contact_check` thresholds, non-mesh nodes and dense meshes (#14, #15)

Fork issues #14 and #15. Commit `02cb984`.

**What happened.**
- An explicit `near_gap` or `tolerance` failed with "invalid thresholds", and one node in `against` that isn't a mesh aborted the whole call (#14).
- Two 148k-vertex garlands kept Max's main thread busy for more than 5 minutes, long after the client had timed out (#15).

**What changed.**
- Explicit `tolerance` and `near_gap` are always millimetres (0 means the defaults, 0.1 mm and 10 mm), converted to scene units like the defaults. Results stay in scene units. Values are checked before anything is sent.
- Nodes in `names` or `against` that can't become a mesh are skipped with a warning and listed in `skipped_nodes`. If none of the `names` nodes is a mesh, the call fails with a clear error.
- After the bounding-box pass, the script estimates the work (vertices per candidate pair) and checks the cheapest pairs first, within a work budget. It checks a deadline, `time_budget_s`, between pairs and inside long vertex loops. The deadline is at most 45 s, below the client timeout, and counts from when the request was built, so time spent queued counts too.
- On the budget or the deadline it stops and returns partial results: `complete: false`, `pairs_total`, `pairs_checked`, `work_estimate`, the heaviest nodes, the stop reason and `queue_wait_ms`. Check dense meshes (garlands, foliage, Mesher objects) separately.
- The report header changed, so the parser and the script ship together. The skill explains units, skipping and the budget.

**Status.** Unit tested in `tests/test_contact_check.py`. Deployed (Python) on the 1.7.3-based fork on 2026-10-05; not tested live yet. On the 1.7.5 merge, the 45 s cap stays below the client's reply deadline, and a request that waited too long in the queue is cancelled and never runs.

## Cloning a closed-group member cloned the whole group (#16)

Fork issue #16. Commit `a83f57c`.

**What happened.** `maxOps.cloneNodes` (and the SDK's `CloneNodes`, which `clone_objects` used) clones the *whole* group when given a member of a closed group. The copies sit exactly on the original, so nothing looks wrong. Cloning 16 props in a production scene left 1,646 duplicate objects.

**What changed.**
- `clone_objects` first runs one read-only check for requested names that are group members: `isGroupMember`, or a closed group head among the ancestors, nested and open groups included. Names match case-exactly; an ambiguous exact name is refused.
- Members are copied node by node (copy, instance or reference, as `mode` says), with their own children and links rebuilt. The top copy is detached (`.parent = undefined; setGroupMember c false`) and checked. It all happens in one undo step, with the same `offset`, `count` and naming as the normal path. If the scene gains more nodes than the script tracked, everything is rolled back.
- Calls that name no group member take the existing paths. In a mixed call, non-members and group heads are cloned with `maxOps.cloneNodes` in the same undo step, so naming a group head still clones the group. `clone_whole_group=True` restores Max's behaviour. The result adds `group_members` (`name`, `group`, `open_group`), `detached_from_group` and warnings.
- `scene_qa` has a new read-only check, `duplicate_group_heads`. It finds group heads with the same position, rotation and scale whose child lists match by class and name, ignoring case, trailing digits and the `_mcp` suffix. Near matches are reported as `name_match: "partial"`. It never deletes anything, in fix mode either.
- **On the 1.7.5 merge** (`cb4d0ff`): if a dialog holds the read-only check that `clone_objects`, or `scene_qa` in fix mode, runs first, the call returns `BLOCKED_BY_DIALOG` with `retryable: true` and "Nothing was cloned" or "Nothing was changed". Repeat it once the dialog is answered. The queued check runs then too, harmlessly. `scene_qa` never sends its fix behind a check that couldn't run.

**Status.** Unit tested in `tests/test_clone_groups.py` and `tests/test_scene_qa_groups.py`. Deployed (Python) on the 1.7.3-based fork on 2026-10-05; not tested live yet.
