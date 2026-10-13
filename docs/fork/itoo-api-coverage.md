# iToo API coverage â€” Max 2027, 2026-10-12

The API is larger than the dedicated MCP tool list. Inspect the installed
surface using `itoo_api_inventory(plugin="railclone"|"forest")`: it reports
interfaces, signatures, property types, module product versions, and coverage
per method. A workflow equivalent counts as coverage even when a different
native alias implements it. Internal callbacks and browser operations are
listed separately. API numbers 400/900 are not product versions.

Sources: the repository's `ITOO_MAXSCRIPT_MAP.md`, the summaries under
`itoo-docs/digests/`, especially A (API) and D (effects), and live `showInterfaces`
on RailClone 7.3.5.687 / Forest Pack 9.4.3.766. Official references:
[RailClone MAXScript](https://docs.itoosoft.com/railclone/maxscript-reference),
[Forest Tools](https://docs.itoosoft.com/forestpack/instantiating-and-exporting/forest-tools).
Live results override older documentation where they differ.

## Twelve added tools

| Tool | Actions or API coverage |
|---|---|
| `itoo_api_inventory` | Installed methods, signatures, product/API versions, property types, tool/missing/UI/internal classification |
| `itoo_catalog` | `getMacroCount`, stored `getMacro`, selected browser item name/property/custom property; never evaluates a macro or opens a browser |
| `railclone_slots` | Inspect/edit base or segment slots by ID or unique name; guarded node bindings and 45 segment settings, including mapping/material IDs/Export to Forest |
| `railclone_metadata` | `setStyleDesc`, explicit confirmed `resetCreatedVersion`; staged base replacement in one undo hold |
| `railclone_proxy` | Inspect; `setProxyMode` disabled/embedded/external, existing-file refusal unless overwrite requested |
| `railclone_instances` | Capped `Instantiate`, `InstantiateEnable`, explicitly confirmed vendor `InstantiateDelete`; restores selection |
| `forest_inspect` | Settings, geometry/surface/area/effect/parameter arrays, bounded readback, `getSelected`; no build |
| `forest_effects` | List/get/add, change code, toggle, move, load/export numeric effects, remove with parameter-row cleanup; token guard and staged undo |
| `forest_effect_parameters` | Inspect parameters; edit integer, real, percentage and scene-unit values with schema and bounds checks |
| `railclone_settings` | Token-guarded global/display/instancing settings; explicit confirmation before leaving manual-update mode |
| `forest_settings` | Token-guarded density tile dimensions, direction, seed, caps, update and transform/effect flags |
| `forest_effect_library` | Search/read default, user or custom .eff libraries; metadata, parameter definitions and hashes, code opt-in |

Tools are registered in the full profile and the progressive `itoo_api` toolset.
The full server exposes 207 tools; the playground hides the existing
`inspect_modifier_properties` compatibility alias and shows 206.

## Existing dedicated commands

RailClone: `get_railclone_style`, `set_railclone_style`,
`get_railclone_output`, `railclone_inspect`, `railclone_set_param`,
`railclone_library`, `railclone_from_library`.

Forest Pack: `scatter_forest_pack`, `forest_items`, `forest_edit_items`,
`forest_set_mode`, `forest_validate`, `forest_library`, `forest_from_library`.
The item tools cover create/delete/move, position, rotation, width/height/size,
geometry ID, seed, count and instance-ready transforms.

`itoo_vantage_check` audits both plugins without rebuilding. Generic plugin
inspection and `plugin_patch` expose many additional properties; that is
distinct from having dedicated semantic tools for them.

## Important verified behavior

- RailClone XML and geometry bindings are separate. Bind existing slots using
  `railclone_slots`; graph authoring must still initialize new slot arrays.
- Slot token covers graph, owner/base identity, values and source references.
  All edits validate before beginning a hold. Shared bases, linked styles,
  stale tokens, wrong node kinds, and dependency loops are refused.
- Measured native types: `sfixedsize` is Point3; `sSizeScale` Boolean;
  `szdeform` integer; `sflattop/sflatbottom` float; `smapping` Boolean;
  `smapchans` a channel-list string. Do not infer types from names.
- Slot edits do not turn on autoUpdate or force a rebuild. Metadata edits
  refuse manual-update mode because the description API clears cached output.
- RailClone native instantiation/deletion and cache writes are not claimed
  undoable. Instantiation disables neither the source nor prior items by
  default, preserves material/UVW behavior, and caps output before conversion.
  Native deletion is name-based; renaming objects can break its association.
- Forest effect XML can be accepted without its code affecting the scatter.
  Mutation results say `compile_verified:false`. There is no discovered public
  native compiler-status API. Check actual output using
  `forest_items(refresh_surfaces=true)`; a normal read can return stale items.
- `fpItem.position.z = fpItem.position.z + 10;` moved all four test items
  by 10 scene units. Changing it to 20 moved them by 20; toggling/removing
  returned them to zero. `+=` did not compile in the tested build and is now
  refused before any write. This is the Forest Effects language, not MAXScript.
- Effect additions initialize the six effect-row arrays. Optional arrays
  otherwise fill lazily and immediately invalidate the initial state token.
  Effect edits currently require procedural mode; custom item caches are not
  replaced. Parameterized effects may be read, toggled and have code changed,
  and removal now cleans owned parameter rows.

## Remaining dedicated API gaps

The complete per-interface list is in the captured inventory, including methods
that current documentation omits. Remaining gaps are explicit:

| Area | Remaining work / reason |
|---|---|
| Forest effect parameters | Numeric writes and numeric import initialization are implemented. Object/spline/controller/texture/curve writes and initialization need further live probes. Inspector retains raw reference metadata. |
| Forest effect library | Search/read plus effect move and parameter-free load/export are implemented. Typed parameter import/export remains; add is parameter-free code only. |
| Forest Tools instances | No public native Instantiate method appeared in ForestPack/trees interfaces. Geometry fallback can be built from `forest_items`; billboard, Forest Color and edge behavior need preservation checks. |
| `exportData` | RailClone selection-based XML+FBX and Forest scene-wide XML exports remain unwrapped. Field conventions differ between docs; exports can overwrite files and affect the entire scene. No .vrscene export was attempted. |
| Catalog writes/evaluation | `setMacro`, `evalMacro`, browser open/close/refresh/overlay are not exposed as general commands. Evaluation can run scripts; writes persist catalog state. Stored macro reads are implemented. |
| Compatibility internals | Forest `resetCreatedVersion` and raw setCreatedVersion/upgradeFromVersion remain unwrapped. They change legacy behavior, not ordinary object settings. RailClone's documented reset is wrapped. |
| Renderer integration | RegisterEngine, SetEngineFeatures, getRenderID/data/nodes/raw and clear callbacks take native pointers or alter renderer-owned memory; inventory only. |
| UI/internal state | Rollups, update_ui, setNodesCache and browser UI actions remain inventory only. |
| New graph slot creation | Existing slots can be safely edited. Adding/removing entire parallel slot families needs a separate graph-and-binding transaction. |

This is a coverage audit and tested extension, not a claim that every native
method is a safe scene command or that older plugin binaries were tested.

## Validation and workstation diagnostics

Unit tests: `tests/test_itoo_api.py` covers guards, encoded inputs, native types,
catalog reads, effect storage, confirmation and overwrite refusal. The complete
suite is run with the installed MCP Python runtime. Live evidence and startup
samples live under `%LOCALAPPDATA%/3dsmax-mcp/diagnostics/itoo-api-20261012/`.
The shared MCP_SOLVED log records deployment, hashes and the final counts.

Every launch used an interactive one-shot scheduled task outside the MSIX
process, exact PID routing, and 4 Hz main-thread sampling. Launch-request to
the same final log marker measured 41.77, 39.59 and 39.48 seconds before the
Connecter timeout change. Sampler suspension overhead was 1.49â€“1.75 seconds;
these are sampled timings, not independent uninstrumented benchmarks.

Connecter's absent-localhost connection cost about 2 seconds. The optional
`scripts/connecter_startup_timeout.py` patch bounds only connection setup to
0.25 seconds and restores the original timeout for payload transfer. It keeps
Connecter available. Originals, hashes and rollback are retained separately;
a vendor update can replace this patch. Arnold initialization and the MassFX
debugger connection each cost about 2 seconds in these runs. No plugin was
uninstalled on 2026-10-12, and the user's fullMeshProxy=0 policy was preserved.

A process observer recorded exits with Windows exit codes and confirmed their
PIDs disappeared. The scratch fixture was saved to a new local test file before
normal quit. The old installed bridge caused the shutdown stall; restoring the
fixed binary in both deployment locations passed saved-scene quit in 4.092
seconds with the client retained. On 2026-10-13 another saved-fixture quit took
18.402 seconds, also with the client retained and exit code 0.

## 2026-10-13: Forest Effects file and ordering commands

`forest_effects` now includes `move`, `load` and `export`. Move takes an exact
effect ID and a 1-based destination, with the normal effect-state token. It
reorders all six effect rows while leaving parameter associations keyed by ID.
Live output confirmed the last writer wins: moving the first Z writer to the
end changed all sampled items from Z=20 to Z=10.

Load appends a parameter-free .eff file and requires its SHA256 from a current
`forest_effect_library(action="get")` read plus the destination effects token.
The exact XML metadata is preserved. Export writes only to a new absolute .eff
path and refuses existing files, including a concurrent create. No UI or native
Import command is invoked. The later numeric extension supports numeric parameter initialization and export;
reference/controller/texture/curve import/export remains refused. Nested XML
parameter definitions are now included in library inspection.

Live export/import preserved exact XML and actual Z=10 output. The full suite
passed 1,051 tests; 42 iToo-specific tests pass. The lifecycle observer's stop
signal now uses its configured output directory.

## 2026-10-13: Arnold removal and measurement limits

With explicit user approval, Windows Installer removed MAXtoA for 3ds Max 2027
5.9.4.0 (product E5058F9F-F96F-4A7A-9EE2-A52A2124E718). Exit code 0, no reboot
required; package registration and plugin files are gone. The final sampled
warm launch took 35.490 seconds, including 1.531 seconds of sampler suspension.
Module verification confirms Arnold is absent and Forest, RailClone and Phoenix
are loaded. Read-only Python module inspection found 72 Connecter modules. The
final unmodified scratch quit exited in 4.250 seconds, client retained.

The first launch after today's boot took 96.368 seconds. About 44.55 seconds of
sampled main-thread time was in file-open calls. A later Windows FileIO/Minifilter
trace was highly intrusive (125 million events), so its 70.753-second launch is
not a comparable performance benchmark. A compatible reader recovered 82,614
create/completion pairs, about 2.53 seconds total across threads, which does not
explain the first launch's delay. No antivirus or network cause is established.
The first-launch bottleneck remains open and needs a bounded cold-start trace.

The timing helper now recognizes zero-padded PIDs in Max.log; PID 4260 was
logged as 04260 and its otherwise healthy run exhausted the old recorder timeout.
The observer waits for actual PID disappearance and drains completed exits when
stopped, rather than reporting a pre-exit process snapshot as a lingering process.
Offline observer checks pass. The integrated wt-review suite passes 1,064 tests.
Fresh installed MCP clients see the new effect actions; existing server processes
need their MCP connection restarted to reload the deployed module.

## Numeric Forest Effects parameters — continuation on 2026-10-13

forest_effect_parameters inspects parameters by effect and exact ID or unique
name. set requires a fresh effects_token, enforces native limits and numeric
types, refuses shared/custom bases and stages one undo hold. Percentage bounds
follow the definition; vendor effects may allow signed values or values above
100. XML and native names/types/IDs must agree.

Measured subtype codes: 0=int, 1=real, 2=percentage, 3=unit; numerical parameter
type is 0. Storage uses efpaint*, efpafloat* and efpaunit* arrays. Import initializes
all 26 parameter-row arrays before effect XML, with fresh parameter IDs. Export
writes current numeric values as defaults; remove deletes owned rows, preserving
others. Non-numeric import/export is refused. No UI calls are in implementation.

Live checks verify all four types: import 11, edit 27, export default 27, remove
without orphan rows. Reimport preserves 27. Copies with identical parameter names
remain independently scoped; moving one last changes actual Z from 37 to 31.
Tests are repeated with Forest deselected to exclude modifier-panel effects.
The native editor was used during early investigation, before the user reinforced
the preference for API-only operation; no production command relies on it.
