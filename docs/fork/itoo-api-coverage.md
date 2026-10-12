# iToo API coverage — Max 2027, 2026-10-12

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

## Eleven added tools

| Tool | Actions or API coverage |
|---|---|
| `itoo_api_inventory` | Installed methods, signatures, product/API versions, property types, tool/missing/UI/internal classification |
| `itoo_catalog` | `getMacroCount`, stored `getMacro`, selected browser item name/property/custom property; never evaluates a macro or opens a browser |
| `railclone_slots` | Inspect/edit base or segment slots by ID or unique name; guarded node bindings and 45 segment settings, including mapping/material IDs/Export to Forest |
| `railclone_metadata` | `setStyleDesc`, explicit confirmed `resetCreatedVersion`; staged base replacement in one undo hold |
| `railclone_proxy` | Inspect; `setProxyMode` disabled/embedded/external, existing-file refusal unless overwrite requested |
| `railclone_instances` | Capped `Instantiate`, `InstantiateEnable`, explicitly confirmed vendor `InstantiateDelete`; restores selection |
| `forest_inspect` | Settings, geometry/surface/area/effect/parameter arrays, bounded readback, `getSelected`; no build |
| `forest_effects` | List/get/add parameter-free effect, change code, toggle, remove; token guard and staged undo |
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
  but are not removed until parameter cleanup is implemented.

## Remaining dedicated API gaps

The complete per-interface list is in the captured inventory, including methods
that current documentation omits. Remaining gaps are explicit:

| Area | Remaining work / reason |
|---|---|
| Forest effect parameters | Numeric/object/controller/texture/curve writes and parameter creation; native storage/type enums need further live probes. Current inspector exposes all arrays. |
| Forest effect library | Search/read is implemented. Import/export and reordering remain; current add is parameter-free code only. |
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
Connecter timeout change. Sampler suspension overhead was 1.49–1.75 seconds;
these are sampled timings, not independent uninstrumented benchmarks.

Connecter's absent-localhost connection cost about 2 seconds. The optional
`scripts/connecter_startup_timeout.py` patch bounds only connection setup to
0.25 seconds and restores the original timeout for payload transfer. It keeps
Connecter available. Originals, hashes and rollback are retained separately;
a vendor update can replace this patch. Arnold initialization and the MassFX
debugger connection each cost about 2 seconds in these runs. No plugin was
uninstalled and the user's fullMeshProxy=0 policy was preserved.

A process observer recorded exits with Windows exit codes and confirmed their
PIDs disappeared. The scratch fixture was saved to a new local test file before
normal quit. A slow shutdown after iToo edits is tracked separately in
MCP_ISSUES, with no attribution beyond measured evidence.
