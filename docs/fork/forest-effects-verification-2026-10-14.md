# Forest Effects fixes #43 and #45: deployed verification

Verified on 2026-10-14 in disposable 3ds Max 2027 instances with Forest Pack Pro 9.4.3 and safe mode enabled. Calls were pinned to each scratch PID. Fixtures were preserved.

## Fixes

- **#43:** `forest_effect_parameters(action="repair_animation", expected_effects=...)` converts legacy scalar bindings to a source-linked Float_Expression on Forest's animatable real parameter track. New scalar bindings use the same route. The original source controller and keys are retained. Replacing that source controller requires rebinding.
- **#45:** definition-only curve imports allocate native slots on the detached Forest copy before adoption. Curve indices follow the full global parameter row, including numeric rows. Removing earlier rows preserves existing static curve shapes by parameter ID. New curves are neutral `(0,1)` to `(1,1)` with pinned endpoints. Curve-bearing item reads reject missing slots before rebuilding.

Both require the updated native bridge. Only the Max 2027 binary was rebuilt and deployed in this run. Reload the Python MCP server after deployment; replacing files does not update an already-running server's imports.

## Live results

| Check | Result | Scratch PID |
| --- | --- | --- |
| Public animation repair | Four Forest items follow Z=31.5, 39.5, 47.5 at frames 0, 5, 10 | 40724 |
| Save/reload animation | Same three frame outputs after reload | 40724 |
| Source preservation | Exactly two Bezier keys: 0f=31.5, 10f=47.5; proxy targets the current source controller | 40724 |
| Public curve import | Aligned rows and native slots; four items X=Z=100 before and after save/reload | 45456 |
| Numeric before two curves; remove earlier effect | Distinct static midpoint values 0.22/0.33 move with their parameter IDs; four items X=33/Z=22 before and after save/reload | 49992 |
| Malformed three rows/two slots | Public `forest_items` refuses `CURVE_STATE_INVALID`; no rebuild, process alive, state unchanged | 15596 |

The distinct static points in the reindexing test were seeded through a private diagnostic helper. Public custom point editing/export was not implemented. Arbitrary raw Forest Pack writes remain unsafe; this fix covers the supported MCP operations and the item-read guard.

## Build and deployment evidence

Implementation commits: `78ee014`, `d07f509`, `7f296c4`, `374d0f8`; final native artifact commit `e4e0e20`.

| Installed artifact | SHA256 |
| --- | --- |
| Max 2027 GUP, both installed copies | `B95802E2BDE84332070D5356CDB5CEB0F6B22C1A122D59EBEF41A41D31C9AB77` |
| `maxmcp/helpers/forest.py` | `C57887F4E7EB04EDA92B594D76CAA80FEB6B75E3EA22D567E83142CBD6064A2D` |
| `maxmcp/tools/forest.py` | `ABB30F72313531C8F75C1EDE7A78B63BA761416E954132DBBC7F733101A6A022` |
| `maxmcp/tools/itoo_api.py` | `0B2E0B4B7781308CAB2C7F8377E76074E68BFB70570BD23611A9C284E8C5C0E2` |
| Installed bridge manifest | `5C238F61C6AFD51AF150FF880695C383BAA62BEFBF4B9DADDB225BBD049BC058` |

The Max 2027 SDK Release build succeeded. Python `unittest discover -s tests` ran 1,109 tests in 24.588 seconds, OK with 5 skips (1,104 executed passes). Focused iToo tests: 83; Forest tests: 90. SDK-independent native ctest: 6 passed. Unit/mock checks are separate from the live results above.

Durable local evidence is under `%LOCALAPPDATA%/3dsmax-mcp/diagnostics/issue43-45-resolution-20261014/`:

| Receipt | SHA256 |
| --- | --- |
| `issue43-public-candidate-v4-live-20261014.json` | `A44EA5F34DB93992305C64C6884041C3F095BE32B74AFCE6FE35892EEBB7E34C` |
| `issue43-postreload-key-check.json` | `468566F2C65355C851807CA7A26C02183AED0AE431C2AE38777B6E1C66D980F2` |
| `issue45-public-load-v4-live.json` | `C8A2E3E18C39160FBE897810236798ABD90167D043EC0038D7FD5B3A8A1805FA` |
| `issue45-numeric-curves-reorder-v4-run2-live.json` | `01980A6F7DF8F54DB372EB6AA9C2B5112531725D1336C06F965A2ADF7BE2EA24` |
| `issue45-malformed-guard-v4-live.json` | `14502CFA93E56B237A3C14B89BC2F7E42C111DBDDAB7184A13A5404019273B70` |
| `public-candidate-v4-deployment-verification.json` | `03D31A215DD7D97E72599ED5172C5C4F5F7CDB79134D409DE50B6FEABD16AFBC` |

Official deployment backups and `rollback.ps1` scripts are under `%LOCALAPPDATA%/3dsmax-mcp/backups/public-candidate-v4-20261014-native/` and `...-python/`. Rollback requires Max closed and a fresh Python server afterward. Earlier backups retain the pre-fix installation.

## Remaining scope

Custom curve point editing/export (#44), animated curve reindexing, other Max native versions, and a rebuilt Setup/release distribution remain pending. Additional null/rebind/export/undo-specific live coverage is not claimed by these regressions. There is no public effect compile-success signal.
