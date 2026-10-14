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

## Thirteen added tools

| Tool | Actions or API coverage |
|---|---|
| `itoo_api_inventory` | Installed methods, signatures, product/API versions, property types, tool/missing/UI/internal classification |
| `itoo_catalog` | `getMacroCount`, stored `getMacro`, selected browser item name/property/custom property; never evaluates a macro or opens a browser |
| `railclone_slots` | Inspect/edit base or segment slots by ID or unique name; guarded node bindings and 45 segment settings, including mapping/material IDs/Export to Forest |
| `railclone_metadata` | `setStyleDesc`, explicit confirmed `resetCreatedVersion`; staged base replacement in one undo hold |
| `railclone_proxy` | Inspect; `setProxyMode` disabled/embedded/external, existing-file refusal unless overwrite requested |
| `railclone_instances` | Capped `Instantiate`, `InstantiateEnable`, explicitly confirmed vendor `InstantiateDelete`; restores selection |
| `forest_inspect` | Settings, geometry/surface/area/effect/parameter arrays, bounded readback, `getSelected`; no build |
| `forest_effects` | List/get/add, change code, toggle, move, mixed numeric/object/spline import with explicit bindings, definitions export, remove with parameter-row cleanup; token guard and staged undo |
| `forest_effect_parameters` | Inspect parameters; edit numeric values and object/spline bindings with schema, bounds, identity and dependency checks |
| `forest_effect_curves` | Read SDK CurveControl points, tangents, flags and ranges with ownership checks and bounded pagination; `set_points` writes one curve parameter's points on a staged copy in one undo step; no editor or rebuild |
| `railclone_settings` | Token-guarded global/display/instancing settings; explicit confirmation before leaving manual-update mode |
| `forest_settings` | Token-guarded density tile dimensions, direction, seed, caps, update and transform/effect flags; reports Forest X/Y/Z translation extents read-only |
| `forest_effect_library` | Search/read default, user or custom .eff libraries; metadata, parameter definitions and hashes, code opt-in |

Tools are registered in the full profile and the progressive `itoo_api` toolset.
The full server exposes 209 tools; the playground hides the existing
`inspect_modifier_properties` compatibility alias and shows 208.

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
| Forest effect parameters | Numeric/object/spline/texture and verified scalar-controller bindings/imports are implemented. Other controller subtypes and curves need further probes. Controller time sampling remains unresolved. |
| Forest effect library | Search/read, ordering and numeric/reference/texture/scalar-controller load/export are implemented. Other controller subtypes and curve import/export remain; add is parameter-free code only. |
| Forest Tools instances | Covered by `forest_instances` (2026-10-14): no public native Instantiate exists, so it builds nodes from the item transforms. Billboards are skipped, Forest Color tint loss is reported, Edge mode isn't checked. |
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
Offline observer checks pass. The integrated wt-review suite before numeric parameter support passed 1,064 tests; the numeric parameter integration passes 1,071.
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

Validation for the numeric extension: 49 iToo-specific tests, 1,058 isolated
suite tests and 1,071 integrated wt-review tests pass. Fresh installed MCP
clients expose 208 tools and verify parameter edits against actual item output.

The installed MCP also passed a real vendor preset: Stepped Rotation imported
three numeric parameters; changing the step from 90 to 45 degrees changed actual
item rotations to the corresponding increments. No UI interaction was used.
The first scratch launch measured 37.407 s; the later vendor-preset launch
measured 89.968 s, still with Arnold absent. File-open stacks account for about
47.06 sampled seconds in that slower run; MassFX accounts for about 2 seconds.
This variability is logged separately and is not attributed to the new tools.

## Forest Effects object and spline bindings — 2026-10-13

`forest_effect_parameters` now reads and edits existing object (`node`, native
type 1, `efpainode`) and spline (`spline`, native type 5, `efpaspline`) bindings.
Both mappings were measured against actual generated item positions through the
API, with the Forest deselected. Values are canonical `{name, handle}` identities
or null. Writes accept an exact unique name, a guarded identity, or null to clear.
The handle is Max's animation handle, resolved through `getAnimByHandle`.

XML/native type, ID and name must agree. Missing, ambiguous or stale sources,
non-shape spline sources and dependency cycles are refused before adoption.
All writes stage a copied base under the existing fresh-token, shared-base,
custom-mode and busy-state guards, then adopt it in one undo hold. No UI or
automatic build is needed. Use `forest_items(refresh_surfaces=true)` to verify
actual output; storage readback alone does not certify compilation.

Reference imports/exports remain refused pending explicit portable binding
semantics. Controller, texture and curve parameter writes remain unsupported.
This extends an existing command; the MCP tool count stays 208.

Validation: 53 focused iToo tests, 1,062 isolated tests and 1,075 integrated
tests pass. Installed JSON-RPC calls changed actual Z to 7 and restored the
guarded object identity and Z=19. Spline rebinding changed the four distances
from 2.6–48.2 to 480.4–548.2 scene units. Tests cover null clearing, stale tokens,
stale handles, duplicate names, wrong shapes, invalid batches and a source
parented under the Forest. The last case requires checking dependencies against
the target node as well as its base; a base-only check misses parent links.

Scratch PID 16000 started in 53.537 seconds (221 samples; 2.874 seconds of total
sampler suspension). Inclusive sampled stacks included USD 4.76 seconds and
V-Ray 4.50 seconds; these overlapping measurements are not removable-cost
benchmarks. Its saved reference fixture quit normally in 7.144 seconds with the
client retained. The observer recorded exit code 0 and PID disappearance.
Evidence: diagnostics/itoo-references-20261013/ on the shared repository.

## Forest Effects reference preset portability — 2026-10-13

`forest_effects(action="load")` now accepts mixed numerical, object and spline
definitions. `bindings` must identify every reference parameter by its file ID
or unique name, using an exact unique node name, `{name, handle}` identity or
explicit null. Extra, duplicate and missing bindings are refused. References
are resolved and checked for shape type and dependency cycles before adoption;
all 26 parallel arrays are initialized before XML, with fresh parameter IDs.
No source geometry is created from the preset.

Reference export requires `export_reference_definitions=true`. The `.eff` file
contains definitions, not scene node bindings. The response's `required_bindings`
lists each ID/name/kind for explicit binding on reload. Numeric values are
exported as current defaults. Missing opt-in or an existing destination refuses
the export without writing a file. Cross-scene users must resolve new source
identities rather than reuse scene handles. Controller, texture and curve
definitions remain refused until their native semantics are measured.

Round-trip verification evaluates each effect separately: effects execute in
sequence, and a second effect can depend on item positions changed by the first.
Running both simultaneously is not a valid portability comparison.
Procedural item indices may reorder on rebuild; compare complete transforms
independently of list order instead of assuming index stability.

Validation: 58 focused tests, 1,067 isolated tests and 1,080 integrated tests
pass. API-only live checks verify mixed imports, missing/extra bindings, invalid
sources and cycles, export opt-in, distinct IDs on repeated imports and cleanup
that preserves original rows. A fresh installed JSON-RPC client verifies the
complete transform set on reload, explicit null bindings on a disabled effect,
normalized UUID keys and restoration of the original effect. Tool count remains
208. Current installed module SHA256:
`7a5ec297406196c1964268f63b7cb3709402dffa09c1af0e35c6562fd2113f11`.

Scratch PID 32800 started in 43.458 seconds; 181 samples, 1.951 seconds total
sampler suspension. It saved its own fixture and quit normally in 5.202 seconds,
with the client retained. Observer recorded exit code 0 and PID disappearance.
Evidence: diagnostics/itoo-reference-files-20261013/ on the shared repository.

## Forest Effects texture map bindings — 2026-10-14

API-only probes measured texture parameter type 3, XML type `texture`, storage
`efpatexmap`. `evaluateTexture` returned normalized RGB in the measured Checker
fixture: red 0.25 and 0.75 produced Z=25 and Z=75 when multiplied by 100.
`forest_effect_parameters` reads and edits existing map bindings. Canonical
identity is `{name, handle, class}`; the handle is resolved by `getAnimByHandle`.
Null explicitly clears a binding. Maps are reused, never recreated or edited.

`forest_effects` loads texture definitions with explicit bindings and exports
definitions with the existing opt-in and `required_bindings` contract. All 26
parameter arrays are initialized before effect XML. Wrong resource types, stale
names/classes/handles and dependency cycles are refused before adoption.
Effect state tokens now include texture handle/name/class so distinct maps are
not conflated by their display string. Tokens cover binding identity, not map
content: editing a referenced map is a separate operation.

Live tests verify 25→75→25, null clearing, invalid identities without state
changes, export/reimport with identical complete transforms, and removal that
preserves the original parameter row. Controller and curve definitions remain
unsupported. No computer-use calls were used in this phase.

Validation: 63 focused tests in the feature checkout (64 in the integrated
checkout), 1,072 isolated tests, and 1,088 integrated tests (5 skipped).
Fresh installed JSON-RPC verifies texture rebind, export/import and restoration
of the original map. Installed module SHA256:
`63f869edf7cd57fbcd616800a9e1dc8dae3220cf38dea9cdac86979a264e0d19`.
The integrated deployment preserves the newer Forest translation-extents change.

Scratch PID 15684 started in 73.418 seconds; 293 samples and 2.847 seconds total
sampler suspension. NtCreateFile/NtOpenFile account for about 37.04 sampled
seconds; this does not identify the file, caller or external source of delay.
Saved own fixture, normal quit 8.910 seconds, client retained, observer exit
code 0 and PID disappearance. Evidence: diagnostics/itoo-textures-20261014/.

## Scalar controller bindings and reference preservation — 2026-10-14

Measured controller parameter type 2 and the scalar storage case with native
`efpacontype=0`. `efpacontref` holds the track's parameter-block owner, while
`efpacontanim` is the zero-based subanim index. A direct controller or the scene
node's base object did not evaluate the measured height track. The correct
parameter-block/index binding returned 13.5.

`forest_effect_parameters` now exposes canonical owner/track/controller identity
and can rebind verified FloatController tracks or explicitly clear them. Public
`subanim` is one-based; native storage is zero-based. Fresh handles, owner and
controller class guards, type checks and dependency checks apply. Tokens include
the selected controller identity, so replacing the controller invalidates them.
No controller values, keys or expressions are written by the binding tools.

`forest_effects` imports scalar controller definitions with required explicit
bindings and exports definitions using the existing opt-in contract. Extra
controller XML attributes are refused until measured. Other controller subtype
writes and curve definitions remain unsupported.

**Fixed bug:** `copy base` cloned linked parameter blocks during staging and
broke source linkage. Both effect edit paths now restore the four external
reference arrays before modifying the staged copy: object, spline, texture and
controller owner. Live checks preserve canonical controller and texture identity
through load, toggle, move, parameter set and remove. Scalar rebind 13.5→29.5,
null clearing, portable round trips and replacing the source controller passed.

**Animation limitation:** after setting the source track to 47.5 at frame 10,
`forest_items(refresh_surfaces=true)` returned frame-zero Z=31.5. Reapplying the
binding did not change this. The cause is unresolved; do not claim animated
output works. Responses explicitly say `controller_animation_verified=false`.
Storage/binding checks and scalar output checks do not establish time evaluation.

The separate `max.time` probe returned 1600 ticks at frame 10, while the controller
parameter returned 31.5 rather than its source's 47.5. This narrows the problem to
controller parameter sampling, not all Forest effect time evaluation.

Validation: 71 focused feature tests, 1,080 isolated tests, and 1,096 integrated
tests (5 skipped). Fresh installed JSON-RPC verifies Z=29.5 then restoration to
31.5 and exact source identity, plus portable import/export. Tool count remains
208. Installed integrated module SHA256:
`c59c877222a83bd635cc11edf0ea4ad2fd21baf03e6053b12eb74895a2c2f685`.

Scratch PID 30044 startup 40.175 seconds; 165 samples, 1.844 seconds total sampler
suspension. Saved own fixture, normal shutdown 5.547 seconds with client retained;
observer exit code 0 and PID gone. Evidence: diagnostics/itoo-controllers-20261014/.

## Native curve inspection and time-sampling follow-up — 2026-10-14

`Effect_Curves` is an SDK CurveControl with no MAXScript properties or methods.
The new `forest_effect_curves` tool uses ICurveCtl/ICurve directly through the
native main-thread inspector. It returns points, tangent coordinates, raw flags,
out-of-range type, X/Y ranges and current time. Curve indices are zero-based;
pagination and point truncation are explicit. No UI, build, curve edits or curve
parameter-to-index mapping are performed.

The backend checks the exact CurveControl class ID and verifies bounded reference
ownership under the freshly resolved Forest base. It refuses stale base bindings,
foreign controls, invalid limits and nonfinite point coordinates. Old native
bridges return BRIDGE_CAPABILITY_MISSING through the Python wrapper.

An optional private SDK fixture seeded two runtime curves for testing; it is not
part of the production tool or installer. The user approved one temporary scratch
launch with Python enabled. Safe-mode config was restored before fixture execution;
the launcher initially misread the fast-ping response and delayed that restoration.
The helper now reads regular response meta.safeMode. The authorized instance is
closed; a later normal instance verified native safeMode=true and curve inspection
without Python. Inspection left getSaveRequired=false.

Tests verify empty controls, actual runtime points/tangents, ranges, paging, limits
and ownership refusal. After saving/reloading the synthetic fixture, the second
curve reset from three points to two and Y range returned to 0..1. The inspector
correctly reads the resulting data. This is not a verified curve editing/persistence
route; do not claim efpacurve serialization or CurveControl-to-parameter mapping.

75 focused feature tests, 1,084 isolated tests and 1,100 integrated tests (5 skipped)
pass. Fresh installed JSON-RPC sees 209 tools and reads populated curves in normal
safe mode. Native SHA256: 2f033113935c1bcdb690d487dca9941221d00e87bc6dd58dde46402c078dcb24.
The reviewed manifest supersedes the prior shutdown-safe bridge; both installed
copies match. Python itoo_api SHA256: f1143f49e0c4e5958b27fc4ec8a6c040be54b32885c4a2a7874748ca3419920c.

Before the repair below, controller issue 43 still reproduced after scene reload. A direct controller with
index -1 also returned zero; no unsupported controller subtype is promoted.

Measured scratch starts: 42.223, 37.943, 35.906, 34.036 and 35.884 seconds.
Normal exits with retained clients: 4.615, 4.734, 4.192, 4.133 and 4.273 seconds.
Each observer exit has code 0 and PID disappearance. Evidence is stored under
diagnostics/itoo-controller-time-20261014/; timing variability is not an isolated
removable-plugin cost measurement.

### Curve binding probe follow-up, 2026-10-14

Safe-mode scratch PID 33352 started in 42.083 seconds (173 samples, 1.890 seconds
sampler suspension). A private raw-array probe of native type 4 evaluated the
existing first curve to 0.25 at X=0.5. Changing efpacurve strings did not change
that result. This does not establish a binding map: changing expression/XML can
leave prior evaluated output cached.

Inserting a numeric parameter before two curve parameters and refreshing items
terminated Max. Minidump: access violation 0xc0000005 in ForestPackPro.dlo+0x14f63f,
read address 0x20. Observer recorded exit code -1 and PID disappearance. No public
curve write tool was used. At this point production curve imports were refused with the
explicit CURVE_WRITES_UNSUPPORTED message. Do not repeat raw curve-row changes in
a production scene. Mapping and persistence had not yet been implemented.
Evidence: diagnostics/itoo-curve-mapping-20261014/.

### Controller sampling and curve-slot repair implementation

Scalar bindings now use an internal `Float_Expression` on Forest's animatable
`efpafloatval` track. Its `MCP_Source` scalar target references the original
FloatController. This avoids the native controller-parameter path's observed
frame-zero sampling while retaining the source controller and its keys.
`forest_effect_parameters(action="repair_animation", expected_effects=...)`
migrates existing scalar bindings; setting or importing a scalar binding creates
this route automatically. Inspector rows expose `sampling_route`, driver/target
identity and `sampling_valid`; replacing the source controller invalidates that
confirmation until rebound. Staging rebuilds the driver against the original
source reference, preventing `copy base` from leaving a cloned source target.
Export writes portable controller definitions requiring explicit source bindings,
without serializing the internal driver or source keys.

Curve definition import initializes all native CurveControl slots on the detached
Forest copy before adoption. The live probe established curve indices equal the
full parameter-row index, including numeric rows. Existing static curve points
are moved by parameter ID when preceding rows are removed; animated point
reordering is refused. New curves start flat at `(0,1)` to `(1,1)` with pinned endpoints, matching the measured Forest default. Custom curve
payloads and curve export/edit were unsupported at this point; see "Forest Effect curve points" below. Slot preparation and scalar
driver assignment require the updated native bridge and reject attached owners.
All live changes still use one guarded undo hold for adopting the prepared copy.

Private route probes verified source/numeric/Forest values 31.5, 39.5 and 47.5 at
frames 0, 5 and 10, source-key preservation, and save/reload linkage. A separate
allocated `[numeric, curve, curve]` probe produced X=33/Z=22 and retained points
and output after reload. The subsequent public implementation passed deployed animation, save/reload, numeric-before-curves, static-shape reindexing and malformed-slot refusal checks. See [deployed verification](forest-effects-verification-2026-10-14.md) for exact scope and hashes.

## Forest Effect curve points — 2026-10-14 (#44)

Status: implemented, deployed (bridge `04815449…`, from `538a241`) and verified live on a scratch
Max 2027 on 2026-10-14 by an independent session (MCP issue fixes; evidence in the dev folder's
`docs\livetests-2027\runs-1014-curve-points\`, confirmation in `MCP_SOLVED.md`):
- a changed curve evaluates at once (interior 0.9 gave Z=90; a five-point curve gave Z=70);
- bezier tangents are relative to their point (measured 0.5331/0.6116/0.7194/0.8494 at
  x=0.6/0.7/0.8/0.9, as predicted); a bezier-corner point (flags 3) persists and evaluates the same;
- save and reload give identical points and build; export then load gives identical points on new
  slots, and removing the loaded effect restores everything;
- every refusal leaves the curve and build unchanged; the primitive on the live, attached Forest
  gives `STAGING_REQUIRED`; #45 shipped-effect loads are unchanged; no crash.

Not tested: undo (max undo isn't run through the MCP), the animated-curve refusal, and Forest's own `.eff` loader.

- `forest_effect_curves(action="set_points", parameter=, points=, expected_effects=)` writes one
  curve parameter. The curve slot is the parameter's global row (`curve_index`), verified live in
  #45. The edit runs on a copied Forest base (`copy base` deep-clones the CurveControl) through
  the new native primitive `mcpForestSetCurvePoints`, then the copy is adopted in one undo hold.
  The live base's slots are validated first (`mcpForestValidateCurveSlots`).
- The primitive accepts only an unattached staged Forest, a CurveControl it owns, and a slot whose
  row is a curve (native type 4). It refuses animated curves (`ANIMATED_CURVE_UNSUPPORTED`), flag
  bits outside corner, bezier, the two locks, endpoint and no-X-constraint, X values that don't
  increase, endpoints off the control's X range, and values outside the X/Y range
  (`CURVE_VALUE_OUT_OF_RANGE`). Every point is read back natively, then again by the Python tool.
  After writing, the staged CurveControl notifies its dependents (only the staged base) so no cached
  evaluation survives; the adopted Forest evaluates the new shape at once (verified live).
- Points: `[x, y]` pairs (corner), `{x, y, type corner|bezier|bezier_corner, in, out, lock_x, lock_y}`
  objects (tangents relative to the point, per the SDK), or the rows that
  `inspect` returns. The first and last points are always endpoints; the editor's selection bit is
  dropped.
- Persistence: Forest saves each slot into `efpacurve[r]` and rebuilds the slot from it on load
  (verified in #45), so edited points travel with the scene.
- `.eff`: a curve definition may hold one `<curves><curve><point px py inx iny outx outy flags/>`
  child. This is MCP-specific: it reuses the XML Forest writes into `efpacurve` inside scenes, and
  shipped `.eff` files carry no points. Load applies the points after the slots are
  prepared and stores the definition without the child (Forest's stored effect XML never holds
  points). Export reads each curve parameter's points and writes that child (`exported_curves`).
  iToo doesn't document curve data in `.eff` files, and Forest's own `.eff` loader is untested with
  this child.
- The effects token doesn't cover curve shapes (they live in the native slot, not in the `efpa*`
  arrays), so two concurrent curve edits aren't detected as stale.
