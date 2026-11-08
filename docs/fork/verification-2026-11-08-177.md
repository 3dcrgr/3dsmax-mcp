# Upstream 1.7.7 merge and regression fixes: deployed verification

The installed Max 2027 runtime was verified on 2026-11-08 after reviewing the existing 2026-11-03 receipts. No production scene or existing user server was changed. New live checks used disposable Max instances, fresh installed-runtime Python processes, safe mode and expected-PID guards. No renders or IPR were run.

## Source and fixes

The candidate includes upstream 1.7.7 merge `da1093d`, job-entry retention fix `bde250a`, native artifact `cd6d7c2`, and Python follow-up `ccdaaadd8a42c063c2a5238f80bd7c25a83e3dba`.

- **#54 — job ownership:** a long native capture returns a job handle and leaves the client's ordinary inflight slot. It is neither abandoned nor blocked by a dialog. Health attribution now includes unfinished jobs owned by this server and excludes them from `other_clients`, without consuming their results. Another server still sees them as foreign work.
- **#55 — frame-strip metadata:** `capture_multi_view` now forwards the native `framed_over_frames` result on immediate and job-resumed replies, alongside `frames`, `framed_root` and `restored_frame`.

No routing patch was needed. The earlier restart assertions expected a raw transport field in minimal replies, and one receipt did not capture warnings. The corrected checks assert the user-visible warnings naming both PIDs. A copied harness also used a filesystem existence check for Windows named pipes; its run-specific readiness probe was corrected.

## New installed-runtime live checks

| Area | Result | Evidence |
| --- | --- | --- |
| Own capture job and second-server attribution | 2/2 | `j02-owner-live.json` |
| Progress, ownership, final 12-frame result, one native send, one-time collection | 8/8 | `j01-jobs-12345-results.json` |
| Immediate and resumed frame-root strips retain `framed_over_frames=true` | 4/4 | `m55-live-results.json` |
| Private registry restart/claim sequence | 10/10 | `r10-restart-results.json` |

The routing sequence keeps the original target when a second Max opens, refuses an already-open Max after the original exits without sending scene work, follows a newly started replacement with a PID warning, and honors a private claim with a PID warning. The user's real claim file was untouched. All tester-owned Max and test-server processes were closed. The initial readiness-stalled attempt is retained as a harness failure, not counted as a completed run.

The three Python files were applied through the official deployment script with backups and rollback. Fresh imports asserted installed module paths and exact hashes. A complete post-deployment comparison found **461/461 package files identical to the candidate checkout**, with no missing files or mismatches. Both installed native bridge copies match the source binary. Existing user servers retain their previous imports until restarted.

## Unit and earlier live scope

`runtime/python.exe -m unittest discover -s tests` ran **1,255 tests in 23.938 seconds, OK with 5 skips** (1,250 executed passes). Focused job, bridge-health, viewport and routing suites passed 6, 42, 6 and 26 tests respectively. One new unit test initially patched the wrong image helper; the harness was corrected and passed before the full run. These unit/mock checks are separate from the 24 new live checks above.

The independent audit of `runs-1103-full-177` supports 62/62 targeted #46–#53 checks after identifying a PID-guard placement error in the harness. Its other existing feature/regression receipts were reviewed, not rerun wholesale. The earlier `runs-1103-full-installed/RESULTS.md` concerns the older installed `53a06f5` build: 60/60 feature checks, regression 150/153 with three old S7b harness failures and a rebuilt S7b 4/4. These different runs and counts are not combined into a new total.

The previously failing J01/J02 attribution and M01 metadata checks are covered by the new passes; the incomplete R10 sequence is replaced by the completed corrected run. Historical failures and original receipts remain available.

## Hashes and durable evidence

| Runtime artifact | SHA256 |
| --- | --- |
| Max 2027 GUP, source and both installed copies | `B81ECE26303767312C5EBE0E06C6975E2F70856C277727CA2E21AD3E149FFD65` |
| `maxmcp/max_client.py` | `4282E18EB328F7985FCE7CFD315F7CA7B770C51FAC5E7180EA9B49F37E1047E0` |
| `maxmcp/tools/bridge.py` | `0837F953825D3D444B46FFD2C3862B4254C8B1746B8C7CE4E86664067A94E2F4` |
| `maxmcp/tools/viewport.py` | `03DCAA06A6710C693704A626A20DE62E2C8A7284101CCC526F20D969C7B9499B` |

Python hashes describe installed files and the Windows checkout; Git blobs can differ only in line endings. The native binary requires exact byte equality.

Live receipts are under `docs/livetests-2027/runs-1108-ccdaaadd-live/` on the shared development root:

| Receipt | SHA256 |
| --- | --- |
| `j02-owner-live.json` | `4618D539D809BB4C5CAC26BAA08552219A798045832662691522D4DBFFB7DEE7` |
| `j01-jobs-12345-results.json` | `88FD39AFA65B23014BBA3C9217455107238043EBC8A4670DA18EC8968E6FB933` |
| `m55-live-results.json` | `0B0CD4F939A5C1FB5E1C77FDCD1EA8213D5531416ACCDA92CC04BAF848259AD4` |
| `r10-restart-results.json` | `9B41504AE6860C8F08E7C527C8A3DA538D1542826F173232A62E3460EABF015E` |

Candidate, unit log and full installed-file comparison: `%LOCALAPPDATA%/3dsmax-mcp/diagnostics/merge177-regressions-20261108/`. Its `deploy-backup-ccdaaadd-20261108-125827/` contains the three pre-deploy files, `deploy.log` and `rollback.ps1` (SHA256 `D275149EDD39F6D82DC7DB48C79E078874A435736BACD608DA8BBC2F9E42956C`). Rollback requires restarting the affected Python server; these two fixes do not replace the native bridge. The independent tester found no remaining essential publication blocker after the 24 live passes; the limitations below remain explicit.

## Remaining scope

A real locked-VRIMG failed Live Link start, the Cloth warning after an actual silent no-op, unsafe-mode `execute_python`, and the other explicit gaps in the existing live reports remain unverified. Vendor V-Ray shutdown and NVIDIA/DLSS crashes are not claimed repaired by MCP diagnostics. Other Max native versions and a rebuilt Setup/release distribution are outside this verification.
