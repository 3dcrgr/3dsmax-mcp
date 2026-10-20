# Live confirmation of the deploy 993ed0b (pipe-gap retry plus HDRI dome light)

Date 2026-10-18. Tested by the "MCP issue fixes" session on its own throwaway 3ds Max 2027 instances (PIDs 37312 and 30488, scene copy, V-Ray 7 GPU, no renders, no `max undo`/`redo`), with the **installed** Python (`C:\Program Files\3dsmax-mcp\runtime`). Every script asserts that `maxmcp` is loaded from that folder.

## Verdict

PASS. Both fixes work on the deployed build and the regression is clean. Verified source commits: `608b3d0` (pipe gap) and `993ed0b` (HDRI dome light). A fresh check on 2026-10-20 confirmed all 458 installed maxmcp files match 993ed0b byte-for-byte, with no missing files, and both installed Max 2027 bridges match the source binary (`4b8064f4638e596d26862f2c01c293434862376dc6f66a63253350603e363b10`).

## Installed state (read-only check before the run)

- Worktree `merge-upstream-1016` at 993ed0b, clean. Installed `maxmcp`: 458 files identical to the branch, 0 missing. Aggregate sha256 `94934a62…63415`, equal to the value MCP dev reported.
- 2027 bridge `4b8064f4638e596d` in the ProgramData plugin folder and in the runtime package, equal to the manifest. The 2023 to 2026 bridges equal the branch.
- Remote-tracking `fork/fix/fork-fixes` is 7038e7f, so the fast-forward is 7038e7f to 993ed0b (two commits: 608b3d0 and 993ed0b).

## 1. The pipe gap (608b3d0)

- **Offline timing** (`c98d_gap_timing.py` against the installed tree): a client pointed at a pipe that does not exist still raises `ConnectionError`, after 0.256 s. The installed `max_client.py` has `_PIPE_GAP_RETRY_S = 0.25`.
- **Live, 8 rounds** (`c98c_pipe_gap.py`): a modal box with 0, 1, 2 and 4 further calls blocked behind it (two repetitions each), then `max_dialogs(respond)`, then polling the pipe for 3 s and a normal call at once. 8 of 8 `respond` calls answered without error (0.03 to 0.05 s), 0 samples with the pipe missing, 8 of 8 next calls `ok`. On the unpatched PR1 build 6 of 8 of these rounds failed with "pipe not found".
- **Original demonstrator** (`c98b_pipe_after_dialog.py`): no respond error, no missing pipe sample, Max alive afterwards.

## 2. HDRI dome light (993ed0b) and model imports

- **HDRI** (`c16_hdri_nodes.py`, 2 of 2): `Sunset 027` and `Day 013` each list their new VRayLight (`<asset> lightsource_001`, `created: true`), the list equals the one node that really appeared, `other_new_nodes` is empty, nothing foreign, state `imported`, selection restored.
- **Models unchanged** (`c03_models.py`, 8 of 8): all VRayProxy, reported nodes and materials equal what really appeared, 8.4 to 8.5 s each, same as on PR3 before the patch.
- **Textured materials and HDRIs** (`c09_textured_hdri.py`, 7 of 7): reported set equals the scene, files exist, selection restored.

## 3. Short regression

| Script | What | Result |
|---|---|---|
| `c10_search` | `cosmos_search` round trips, filters, paging, bad arguments | 19 of 19 |
| `c08a_failures_first` | invalid ids, argument validation, `renderer=current`, open undo operation, concurrent import, pending download | 13 of 14, see note |
| `c08c_x6b_slow_download` | the one open item of `c08a`, rerun with a patient wait | 1 of 1 |
| `c11_edges` + `c11b_undo` | re-import, 12 materials scanned, unsaved scene (in-process and through the real stdio server), editor close right after an import, undo state | 9 of 9, see note |
| `c06_settle` | `IMPORT_SETTLING` guard, argument validation, floor, guard expiry | 14 of 14, see note |
| `c13b_dialog_open` | simulated dialog after the dispatch, `pending_restore` | 2 of 2 |
| `c07c_medit_fixed` | active slot kept, swap, restore script, `restore_medit_renderer=False`, default | 9 of 9 |
| `c15_trigger pr3` | cold import on a fresh Max, `MatEditor.Close()` at once, 45 s watch, `quitMax` | import 27.2 s `imported`, close answered in 0.0 s, 0 hung samples, Max exited 5.0 s after the quit |

Notes on the three results that needed a second look, none of them a product problem:

- **`c08a` X6b**: the pending-download asset this time was 22 MB, so the script's 90 s poll plus 30 s import wait ended while Cosmos was still downloading. `cosmos_import` correctly answered `downloading`. `c08c` waits patiently (the asset was `ready` by then) and the import passes: `imported`, one VRayProxy, selection restored.
- **`c11` R6** reads the undo state with a string that prints `SuperLevel()` instead of `0`. `c11b` has the correct check and passes (`holding=false, restoring=false, superlevel=0`).
- **`c06` T5** failed once because the script wanted the CPU-quiet flag `quiet` while this Max had `quiet: false, windows_quiet: true, waited_s: 8.0, state: responsive`. The import code itself accepts either (`helpers/cosmos.py`: `quiet or windows_quiet`). I made the check accept both and reran: 14 of 14 (`quiet: true` that time).

## Durable evidence files

The scripts and redacted JSON results are archived at `\\devbox.kiko-egret.ts.net/src/CGGhost/autodesk/mcp/3dsmax/docs/livetests-2027/runs-1018-deploy-993ed0b/`. This page preserves the independent test report; no repeat tests or deployment were performed on 2026-10-20.

- Harness: `c_common.py`, `c12_common.py`, `p1drv.py`, `stdio_mcp.py`, `launch_scratch.ps1`, `close_max.py`, `c14c_state.py`
- Tests and their `*-results.json` (account, server and user names redacted, a client-like asset name replaced by `<name>`)
- `c15-trigger-pr3.json`: the cold import trigger on the deployed build
- Not copied: scene fixtures (the base scene is `runs-1015-merge-1.7.6/fixture.max`)

Nothing was committed or pushed by me.
