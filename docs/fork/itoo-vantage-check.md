# iToo → Chaos Vantage readiness check

`itoo_vantage_check` (fork issue #25, branch `feat/itoo-vantage-check`, 2026-10-11) reports, for each Forest Pack, RailClone and ForestIvy object, how ready it is for Chaos Vantage. It is strictly read-only.

The checks come from the iToo documentation (the knowledge pack's digests A §3.1 and S5, C2 §3.2–3.4, F §2.2, §2.5 and §6.3), from live probes in a scratch 3ds Max 2027.2 with RailClone 7.3.5 and Forest Pack 9.4.3, and from the user's Vantage 3.4 conversion notes. Vantage itself was never rendered by the tool or its tests. Whatever Vantage does beyond what the docs say is marked unverified: Vantage's own log (`%APPDATA%\Chaos\Vantage\debug_log.txt`) decides.

## Calling it

```
itoo_vantage_check(names=None, time_budget_s=30, max_paths=5, details=False)
```

- `names`: exact node names (Max compares node names case-insensitively, so `l25_ivy` finds `L25_ivy` [live]; spaces, quotes, backslashes and non-ASCII characters match as typed). Without them (or with an empty list), every node whose base object is `Forest_Pro`, `RailClone_Pro` or `ForestIvy` is checked. Names that match nothing go to `not_found`; other classes go to `not_itoo`.
- `time_budget_s` (1–300): one MAXScript call does the whole check. Between objects, and while walking materials, it stops at the budget; objects it didn't reach are listed in `not_checked` and `complete` is false. The scratch scene's 263 iToo objects took 1.1 s.
- `max_paths`: example paths kept per finding (`where`); `where_truncated` says when there were more.
- `details`: adds the segment, item, area and modifier lists to each object's `state`.

## What it never does

The script reads properties, walks sub-materials and sub-texmaps (`getSubMtl`, `getSubTexmap`), and calls only `getXMLOutput()`, `getXMLStyle()`, `trees.count()` and `trees.getGeomID`. It never:

- rebuilds anything: no `segmentsUpdate`, `seed = seed`, `gscale = gscale` or `trees.update()`;
- calls RailClone's `getStyleDesc()`, which empties the output export until a rebuild;
- writes a property, a material, a selection or a file; renders; or exports (`.vrscene`, `exportData`).

`tests/test_itoo_vantage.py` lints the generated script: no property assignment (`x.prop = ...`), and none of the calls that write, rebuild, select, render or export.

Checked live on 2026-10-11:

- A `NodeEventCallback` logging every node event saw **no events** from the check in two whole-scene runs and in targeted runs. Those runs covered a moved (stale) RailClone, one with a parameter just written, hidden ones, a disabled one, one whose autoUpdate had been pulsed on, and a Forest with a pending change.
- Export hashes (getXMLOutput and getXMLStyle) and `trees.count()` of the watched objects were unchanged afterwards.
- A moved RailClone stays stale: the check doesn't rebuild it. A Forest with a pending `units_x` change kept its old `trees.count()`.
- One whole-scene run, the first right after the fixtures were created, logged change events on two unnamed nodes. That logger didn't record node names, and the later runs with names logged none. Another session was also using the scratch Max (its objects appeared during the test), so the cause is unknown.
- Getting a RailClone's export can complete a build RailClone already has pending (the probes saw `autoUpdate = true` followed by a read rebuild it), as a viewport redraw would. The check never starts one.
- `save_required_before` and `save_required_after` report `getSaveRequired()` around the check. If it goes from false to true, the result warns `SCENE_MARKED_DIRTY`. Live (L25 run, flag cleared first): the check left it false. But the flag is a weak sensor: property writes sent through the bridge, with undo on or off, didn't set it either, while `maxOps.cloneNodes` did. So the warning catches only operations Max itself marks dirty; the read-only proof is the script lint plus the event and fingerprint checks below.

## Report

```
{
  "complete": bool, "read_only": true,
  "scene": {renderer, max_version, railclone_version, railclone_full_mesh_proxy, save_required_before, save_required_after, elapsed_ms},
  "summary": {objects, kinds{forest,railclone,ivy}, status{error,warn,ok}, findings{error,warn,info}, unknown},
  "objects": [
    {"name", "handle", "kind": "forest"|"railclone"|"ivy", "status": "error"|"warn"|"ok",
     "findings": [{"severity", "code", "count", "where": [...], "where_truncated"?}],
     "unknown": [{"check", "reason"}],
     "state": {...}, "elapsed_ms", "fallback"?: "railclone"|"forest"|"ivy"}
  ],
  "scene_findings": [...], "scene_unknown": [{"check", "reason"}],
  "codes": {CODE: {severity, basis, message, fix?}},   // only the codes that occur
  "fallbacks": {kind: text},                            // for the kinds named by an object
  "basis": {doc, live, inf, memory},
  "notes": [...], "not_found": [...], "not_itoo": [...], "not_checked": [...], "warnings": [...]
}
```

- `status` is `error` if any finding is an error, `warn` if any is a warning, otherwise `ok`. Unknowns don't change it.
- `basis` combines tags with `+`:
  - `doc`: iToo or Chaos documentation.
  - `live`: measured in the scratch Max.
  - `inf`: inferred, not verified.
  - `memory`: the user's own Vantage logs and notes.
- `warnings` can hold:
  - `SCENE_MARKED_DIRTY`;
  - `TIME_BUDGET`: objects not checked;
  - `PARTIAL_READ`: an object's material walk stopped at its cap of 4000 materials and maps or at the budget, or reading it failed part-way.

### Compact `state`

- **Forest:**
  - `viewport_count`, which is `trees.count()` as it is;
  - `vmaxitems`, and `viewport_thinning`: `none`, `unlikely`, `possible` or `unknown`;
  - `rmaxitems`, `max_faces_millions`, `render_mode`, `rmesh`, `vmesh`, `rskip`, `disabled`, `consolidate_materials`;
  - `item_types`, `billboards`, `proxy_items`, `material`, `color_maps`, `opacity_maps`;
  - `estimated_faces`, in Meshes render mode only, with `estimated_faces_partial` when some item types have no face count.
- **RailClone:**
  - `disabled`, `instancing` (`rendermode`), `rmesh`, `vmesh`, `use_segment_material` (`nodemat`), `auto_update`, `proxy_mode`, `created_version`, `material`;
  - counts of `modifiers`, `segments`, `material_operators` and `uvw_operators`;
  - `export`: `{state: present|empty|unreadable, items, estimated_faces, max_segments, max_faces_millions}`;
  - `color_maps`, `opacity_maps`.
- **Ivy:** `render_instancing`, `max_branches`, `max_leaves`, `material`.

## The checks, and what each one reads

### 1. Colour maps (Forest Color, RailClone Color)

- **Where it looks:** every `Forest_Color` and `RailClone_Color` (matched by class name, case-insensitive), wherever it sits in a walked material tree:
  - Forest: `matList[k]`, each item source's material (`cobjlist[k].material`) unless `matList[k]` overrides it, then the node's `forest_automat_*` material. An override replaces the source material inside `forest_automat_*` [live], so an overridden source material doesn't render through the forest and isn't checked (it's still checked through any other item that uses it without an override). `details` shows each item's `material_override`.
  - RailClone: `sobjmtl[k]`, the linked segment sources' materials when Use Segment Material is on, and the node material.
  - ForestIvy: the node material.
- **Slot 1:** Vantage bypasses these maps and renders input slot 1. In the SDK's sub-texmap order, slot 1 is `mapbase`, labelled "Default Map" [live]. The check reads its class, name, file name (`filename` or `HDRIMapName`) and how many of its own inputs are set.
  - `COLORMAP_SLOT1_EMPTY` (error): slot 1 is empty.
  - `COLORMAP_SLOT1_FLAT` (error): slot 1 is a flat colour, either `VRayColor`, or a wrapper with no input (`Color_Correction`, `Output`, `RGB_Tint`, `RGB_Multiply`, `Mix`, `Mask`, `CompositeTexturemap`, `VRayCompTex`).
  - `COLORMAP_SLOT1` (info): what slot 1 holds. `COLORMAP_SLOT1_NESTED` (info) when slot 1 is another iToo colour map.
- **Lost variation:** `COLORMAP_VARIATION_LOST` (warn) is raised for every map: tint and random variation are always lost. Its `where` shows the settings:
  - `maplist` slots set, and enabled (`maponlist`, whose PB2 label is "Use Color"; read here as the slot's enable box [inf]);
  - `mapidmode`, `tintmode`, `tintmin`/`tintmax`, `override`.
- **GPU tint modes:** `FORESTCOLOR_GPU_TINT_FROM_MAP` (warn) applies to Forest Color only. It fires when the tint that applies is "Get color from map" (`tintmode` 1 [inf, UI order]) and the tint isn't off (`tintmin`/`tintmax` not both 0).
  - V-Ray GPU doesn't support that tint in 'Random Values' or 'As Texture on Surface' mode [doc]. `tintmapmode` 0 is Random Values and 1 is As Texture [inf].
  - The tint that applies is the map's own when `override` is on, and the Forest object's (`tintmode`, `tintmapmode`, `tintmin`, `tintmax`) when it's off.

### 2. Opacity maps

- **What it reads:** every material slot whose name contains "opacity" or "cutout" and has a map.
- `OPACITY_MAPPED` (info): Vantage has Opacity Maps off by default. Turn on Renderer > Advanced > Opacity Maps (Clip Mode); it can't be set from a script.
- `OPACITY_MAP_NOT_BITMAP` (warn): the slot holds something other than `Bitmaptexture` or `VRayBitmap`. Vantage's docs list procedural and wrapper maps as unsupported in opacity [memory].

### 3. CPU-only features

- **Forest Material:** `CPU_ONLY_FOREST_MATERIAL` (warn) for any `Forest_Material` in the walk. The docs list it for V-Ray (CPU) and Scanline only.
- **Billboards:** `CPU_ONLY_BILLBOARD` (warn) for items with `geomlist` 1 (2D template [live]).
  - `XSHADOW_BILLBOARDS` (warn): billboards while `usefakeshadows` is on.
  - Also flagged: `GEOMLIST_CRASH_RISK` (error) for `geomlist` 3 [live], and `GEOMLIST_UNKNOWN` (warn) for other values.
- **Edge mode:** read from `arboundchecklist` per area, with `pf_aractivelist`.
  - `EDGE_MODE_ON_PROXIES` (warn): an active area has the value 2 and an item source's base object is `VRayProxy`.
  - `EDGE_MODE_UNVERIFIED` (info): Edge mode without proxies.
  - The value meanings (0 Point, 1 Size, 2 Edge) weren't confirmed live: writes of 0–3 changed nothing visible.
- **ForestIvy:** `IVY_RENDER_INSTANCING` (warn) when `renderinstancing` is on. It's documented for V-Ray CPU and Arnold, so on GPU/Vantage expect one mesh. The finding shows `maxbranches` and `maxleaves`.
- **XShadow:** `XSHADOW_LIGHT` (warn, scene finding) for any light whose `shadowGenerator` is an XShadow.

### 4. Material IDs under `forest_automat_*`

- **ID-driven maps:** maps in a forest's walk that can be keyed on face material ID. With Consolidate Materials (`consmat`) on, each one gives `ID_MAP_IN_FOREST` (warn): Forest renumbers face IDs into `forest_automat_*` [doc+live], so the map picks other entries or loses its variation. With consolidation off it gives `ID_MAP_UNCONSOLIDATED` (info).
  - `VRayMultiSubTex` with `from_id` 0, "Face material ID" [doc: V-Ray SDK].
  - `VRayMultiSubTex` with `from_id` 12 (random by composite seed) and `random_by_face_mtl_id` ("By face MtlID") on. If that flag can't be read, `multisubtex_mode` is unknown.
  - Any map with `randByMat` ("Random by material id") on: `BerconTile` and `BerconGradient` [live: PB2 labels].
  - `MultiTexture` has `rnd_by` ("Randomize by", default 0), and its dialog offers Material ID and Object [live], but which value means Material ID wasn't confirmed. So a `MultiTexture` in a consolidated forest goes to the `id_map` unknown with its `rnd_by` value, not to a finding.
  - Max's native maps have no ID switch on this build (only OSL maps, which Vantage doesn't support), so none is checked.
- **FPColor materials:** `FPCOLOR_HARMLESS` (info) counts the "FPColor ID n" Standard materials. They only drive the viewport Color ID display, and Forest rebuilds them. The walk doesn't enter them.
- **Node material:**
  - `FOREST_NO_AUTOMATERIAL` (info): the node has no material yet.
  - `FOREST_NODE_MATERIAL_REPLACED` (warn): the node's material isn't `forest_automat_*` while consolidation is on.
  - `FOREST_NODE_MATERIAL` (info): the same with consolidation off.

### 5. RailClone settings

- `RC_DISABLED` (info): `disabled` is on. The object renders nothing in Max either, so Vantage matches Max.
- **Modifiers** (`node.modifiers`; reading them fires no events [live]):
  - `RC_MODIFIERS` (warn): any modifier enabled in renders. The docs say modifiers are incompatible with native instancing.
  - `RC_MODIFIERS_OFF` (info): all of them are off in renders.
- **Instancing and render mode:**
  - `RC_INSTANCING_OFF` (info): `rendermode` (Use Instancing Engine) is off.
  - `RC_RENDER_BOXES` (warn): `rmesh` 1, which renders boxes.
  - Toggling either changed nothing in the viewport or the export [live]: render-time only.
- **V-Ray-only features under the instancing engine** (per the docs; Vantage unverified):
  - `RC_SEGMENT_MATERIAL` (warn): `nodemat`, Use Segment Material.
  - `RC_MATERIAL_OPERATOR` (warn): enabled `baseClass="Operator" class="Material"` nodes in `getXMLStyle()`. Nodes with `on="0"` are skipped [inf: the node's enable flag].
  - `RC_SEGMENT_MAPPING` (warn): `smapping` on a segment.
  - With the instancing engine off, the last two are the info codes `RC_MATERIAL_OPERATOR_MESH` and `RC_SEGMENT_MAPPING_MESH`: they then work in every renderer.
- **Proxy cache:**
  - `RC_PROXY_CACHE_EMBEDDED` (info): `proxymode` 1.
  - `RC_PROXY_CACHE_FILE` (info): `proxymode` 2. `RC_PROXY_FILE_MISSING` (error) when `doesFileExist proxyfile` is false.
  - `proxyfile` stays set after mode 0 [live], so only `proxymode` counts.
- **V-Ray proxy segments:** `RC_PROXY_MESH_REVERTABLE` (info) when a segment's `sobjref` or linked `sobjnode` base object is `VRayProxy`, instancing is off, and the RailClone plugin is 7 or later.
  - Live: after a proxy was linked as a segment (`sobjnode[k]`), `sobjref[k]` read `Editable_mesh`, not `VRayProxy`, so the linked node is what identifies it. A proxy segment with no linked node left would read as a mesh, with its preview faces.
  - Since RailClone 7, proxies no longer need full mesh mode for GPU/Vantage.
  - The version is the file version of the loaded `railclone*.dlo` module. It's read only when needed, because enumerating the modules takes about a second.
- **fullMeshProxy:** RailClone's registry value `HKCU\Software\Itoo Software\RailClone Pro\fullMeshProxy` (RC 7.1.4 and later; 1, the default, converts V-Ray Proxy segments to full mesh).
  - The check reads it inside 3ds Max with the .NET registry, read-only, once per call and only when a RailClone is checked. A Max started from inside an MSIX-packaged app (such as Claude Desktop) sees a private registry where iToo's keys can be hidden; then the value is unknown, never assumed fine.
  - `FULL_MESH_PROXY_ON` (warn, scene finding, once): the value is 1, or not set (the default applies), on RailClone 7 or later.
  - `RC_PROXY_FULL_MESH` (info) on each RailClone that actually has V-Ray Proxy segments while it's on.
  - **Policy (user, 2026-10-11):** the setting is allowed, but any suggestion to turn it ON, from the user or an agent, gets a warning first. It's not recommended on RailClone 7+: no longer needed for V-Ray GPU/Vantage, and it costs memory and translation time. Change it only in RailClone's settings; this tool never writes the registry.
- **Segment materials:** `RC_SOBJMTL_NOT_ONE_SLOT_MULTI` (warn) for `sobjmtl` entries that aren't one-slot Multimaterials (`getNumSubMtls` 1).
  - Vantage 3.4 logged "Unsupported nested MtlSingleBRDF" for them, and the one-slot wrap fixed it [memory].
  - Writing `sobjmtl` changed no face IDs in the viewport [live].

### 6. Caps

- **RailClone: the export.** Counts come from `getXMLOutput()` as it is: `<item` elements, plus `source_segment` per segment.
  - Over `maxseg` or `maxFaces`, RailClone generates nothing (0 items), and `disabled` empties the export too [live].
  - So an empty export of an enabled object is `RC_EXPORT_EMPTY` (warn), and the caps go to unknown as "no current export".
  - `RC_AUTOUPDATE_OFF` (info) notes that with `autoUpdate` off the export may be older than the last change.
- **RailClone: segments.** `RC_MAXSEG_NEAR` (warn) at 80% or more of `maxseg`. `RC_MAXSEG_EXCEEDED` (error) means the export predates the cap. 0 gives `RC_MAXSEG_UNLIMITED` (live: `maxseg` 0 with `maxFaces` 0 kept all 131 items of a fence).
- **RailClone: faces.** The estimate is items per segment × each segment's faces.
  - The faces come from `sobjref[k].mesh.numfaces`, or else from the linked source node's evaluated mesh (so a linked source with modifiers is counted: its `sobjref` turns into a DummyClass [live], and the node's mesh is what RailClone uses). V-Ray proxy segments are unknown, and so is an embedded segment whose `sobjref` is a DummyClass with no linked node to fall back on.
  - It is compared with `maxFaces` (in millions) as `RC_MAXFACES_NEAR` (warn) or `RC_MAXFACES_EXCEEDED` (error); 0 gives `RC_MAXFACES_UNLIMITED`. Instanced items count too [live].
  - Live, the estimate for a 176-item fence was 12,112 faces. `maxFaces` 0.0122 kept the output and 0.0121 emptied it, so the estimate was exact there.
  - Live (L25 run), on the stock Pantile roof (a clipping area, 12 segments, Material operators, 430 items): the estimate was 517,878 faces, and RailClone's own cutoff was exactly there (0.517878 kept the output, 0.517877 emptied it), although the built mesh had 509,708 faces after clipping. So the estimate follows RailClone's cap arithmetic (items × segment faces), not the final mesh.
  - Sliced or bent segments can add faces, so treat it as an estimate.
  - A proxy-cached RailClone exports items without `source_segment`, so its faces are unknown.
- **Forest: viewport thinning.** `trees.count()` is read as it is. The viewport Max Items limit (`vmaxitems`, default 50,000) thins it [live]: with a full count N over the limit, Forest keeps every k-th grid cell, k = ceil(sqrt(N / vmaxitems)). Live: 143,700 items gave a count of 35,800.
  - Thinning needs count > vmaxitems / 4, so it's `unlikely` below that and `possible` above it.
  - When possible, the check warns `FOREST_VIEWPORT_THINNING` and lists the render count as unknown. Reading it needs `vmaxitems` 0 and a rebuild, which this check never does.
  - In one probed state (after a distribution map was assigned and cleared) thinning was harsher than the rule, so "unlikely" isn't a proof.
- **Forest: render items.** Against `rmaxitems` (Max Items render; over it Forest renders an "F" icon [doc]):
  - `FOREST_RENDER_MAX_ITEMS_EXCEEDED` (error): count > rmaxitems.
  - `FOREST_RENDER_MAX_ITEMS_RISK` (warn): thinning possible and 4 × count > rmaxitems.
  - `FOREST_RENDER_MAX_ITEMS_NEAR` (warn): 80% or more.
  - `FOREST_RENDER_MAX_ITEMS_UNLIMITED` (info): 0.
- **Forest: render mode and faces.** Max Faces is ignored by V-Ray in Automatic render mode [doc].
  - When `rendermode` isn't 0 (Automatic, the default; Meshes otherwise [inf]), the check warns `FOREST_MESH_RENDER_MODE`.
  - It then counts items per type with `trees.getGeomID` (up to 200,000 items) and multiplies by each source's `mesh.numfaces`, for `FOREST_MAX_FACES_NEAR` or `FOREST_MAX_FACES_EXCEEDED`. Billboards and V-Ray proxies have no face count: a proxy node's mesh is its viewport preview (12 faces live, for the armadillo sample in box preview), so it's never read, and those item types go to the `faces` unknown.
- **Forest: other.**
  - `FOREST_DISABLED` (info): disabled, so it renders nothing in Max either.
  - `FOREST_NO_ITEMS` (warn): count 0.
  - `FOREST_RENDER_DISPLAY` (warn): `rmesh` not 0.
  - `FOREST_RENDER_SIMPLIFICATION` (info): `rskip` > 0.

### 7. Fallback

Objects with a finding that Vantage may not translate get `fallback` (their kind); `fallbacks` has the text, and the scene finding `FALLBACK` (info) points to it. Those findings are the colour-map errors (`COLORMAP_SLOT1_EMPTY`, `COLORMAP_SLOT1_FLAT`), the CPU-only features (`CPU_ONLY_FOREST_MATERIAL`, `CPU_ONLY_BILLBOARD`, `EDGE_MODE_ON_PROXIES`, `IVY_RENDER_INSTANCING`) and `ID_MAP_IN_FOREST`.

Other errors get no fallback, because converting can't fix them: their fix is in Max. A disabled object or one over `maxseg`/`maxFaces` generates nothing, so Instantiate has nothing to convert [live]. The same goes for a missing proxy file, `rmaxitems` or Max Faces exceeded, and `geomlist` 3.

- **RailClone:** RailClone Tools `RailClone_Pro.Instantiate mode layer autoDelete separatedMeshes forceInstances disableAtEnd`. It works on the selection. Use `forceInstances=false`, because true ignores the Material and UVW operators.
- **Forest:** Forest Tools > Instantiate (UI only). The instances keep their source material IDs.
- **ForestIvy:** render instancing off, or convert the ivy to a mesh.

All of these change the scene, so ask the user first. Never export a `.vrscene`: it crashed Max.

### Scene findings

`XSHADOW_LIGHT`, `RENDERER_NOT_VRAY` (info: the current renderer isn't V-Ray), `FULL_MESH_PROXY_ON` (warn) and `FALLBACK`. Scene-level unknowns are in `scene_unknown`.

## What stays unknown, and why

| `unknown.check` | Why |
|---|---|
| `caps` | No current RailClone export (empty or unreadable). Only a rebuild would refresh it. |
| `faces` | A segment or item type without a face count: a V-Ray proxy (its mesh is only the preview), an embedded segment whose source was modified (`sobjref` is a DummyClass and no source node is linked), a billboard, or a proxy-cached RailClone whose export doesn't name segments. A modified *linked* source is counted from the source node's mesh. |
| `id_map` | A `MultiTexture` in a consolidated forest (which `rnd_by` value means Material ID isn't confirmed), or a map whose `randByMat` couldn't be read. |
| `render_item_count` | `trees.count()` may be thinned by the viewport limit. The full count needs `vmaxitems` 0 and a rebuild. |
| `histogram` | Meshes render mode with over 200,000 items, or the budget ran out while counting items per type. |
| `boundary_check` | `arboundchecklist` missing or unreadable. |
| `proxy_items` | V-Ray proxy items are found by class only. Whether a UI-picked proxy gets another `geomlist` value wasn't probed. |
| `railclone_version` | The loaded RailClone module's version couldn't be read, so `RC_PROXY_MESH_REVERTABLE` can't be decided. |
| `full_mesh_proxy` (in `scene_unknown`) | This Max can't see `HKCU\Software\Itoo Software\RailClone Pro` (an MSIX-packaged parent hides it), the read failed, the value is unexpected, or it's on but the RailClone version is unknown. |
| `forest_color_gpu_mode`, `multisubtex_mode`, `material_operator`, `rmesh`, `proxymode` | A value that couldn't be read, or isn't a documented value. |
| any property name | That property doesn't exist on this build ("no such property"). |
| `materials`, `read` | The material walk stopped at its cap or the budget, or reading the object failed part-way (`PARTIAL_READ`). |

## Not tested live

- A RailClone *built* with a V-Ray proxy segment: the L25 run linked the armadillo proxy with `autoUpdate` off, so RailClone never generated with it (see below).
- A missing external proxy cache file (`RC_PROXY_FILE_MISSING`): an existing one was tested; removing the file under a mode-2 RailClone might open a dialog in the shared Max.
- MultiTexture's `rnd_by` values, and whether Vantage renders BerconTile's random by material ID.
- ForestIvy with surfaces.
- Forest's Max Faces / Max Items (render) cutoffs ('F' icon): render-time only. `snapshotAsMesh renderMesh:true` returned the viewport display mesh, so it can't show them.
- Every Vantage-side claim: what Vantage renders for slot 1, Opacity Maps, Use Segment Material, the Material operator, segment mapping, Edge mode. The tool says which ones are documented and which are unverified; nothing was rendered.

## Live test (2026-10-11, scratch Max 2027.2, PID 35448)

- **Fixtures** (prefix `IVK_`, under `livetests-2027\runs-1011\vcheck\` on the share):
  - copies of a stock fence with segment materials, Use Segment Material, mapping, instancing off, Render Boxes, a RailClone Color, an Output opacity map and a UVW modifier off in renders;
  - a disabled copy, an embedded proxy cache and a caps copy;
  - a forest in Meshes render mode with a billboard, a `VRayMultiSubTex` by ID, an opacity bitmap, a Forest Color with an empty Color Correction in slot 1, `rmaxitems` 100, tint from map and Edge on the Surface Area;
  - a forest thinned to 35,800 of 143,700 items;
  - a ForestIvy, and an Omni light with XShadow.
- **Other session's objects:** the check was also run, read-only, on that session's `VC_` objects. It found the empty slot 1 inside a Mask on the stock Grass preset, flagged in the user's notes.
- **Fixture report:** `tests/itoo_vantage_live_report.txt` is the raw report of one run, and the unit tests check its analysis.
- **Whole scene:** 263 objects in 1.1 s, with no node events and unchanged exports. The report was 266 KB, mostly the compact `state` of 245 RailClones; name the objects to keep it small.
- **Review fixes** (later the same day, under `runs-1011\vcheck-fix\`):
  - `IVF_forest` overrides item 1's material with `matList`; `IVF_forest_ctrl` is the same forest without the override. The override replaced the source material in `forest_automat_*`. Only the control reported that source material's empty Forest Color slot 1.
  - Item 2's material holds a `VRayMultiSubTex` (from_id 12, By face MtlID), a `BerconTile` (randByMat) and a `MultiTexture`. Both forests reported the first two as `ID_MAP_IN_FOREST` and the MultiTexture as the `id_map` unknown.
  - The proxy forest's estimate dropped from 17,244 faces (1,437 × the 12-face preview) to 0, marked partial, with the proxy under `faces`.
  - Whole scene, 290 objects in 1.2 s: 10 objects name a fallback and 5 are errors (all colour maps). In the earlier whole-scene run, 236 objects were errors with a fallback, and 229 of them were only disabled stock RailClones.

### L25 live test (2026-10-11, later, under `runs-1011\live-vcheck\`)

- **Fixtures** (prefix `L25_`, left in the scratch Max at x ≈ 450,000):
  - RailClones from the stock Metal Fence 1 (merged with its path, undo off) and the stock Pantile roof (merged, given a clipping rectangle): a plain control, a UVW modifier on in renders, a Bend off in renders with caps 0, render flags with segment materials and mapping and `autoUpdate` off with a pending change, embedded and file proxy caches, caps near and over, disabled, moved without a rebuild, and a fence with the armadillo `VRayProxy` linked as segment 3 (instancing and `autoUpdate` off).
  - RailClone Color maps with a bitmap, a `VRayColor`, an empty slot 1, and a nested Forest Color.
  - Forests: one scatter with Forest Colors (bitmap, flat with tint from map, empty), bitmap and Noise opacity, a `VRayMultiSubTex` by face ID in a Multi/Sub, a Forest Material and a billboard; Meshes mode over Max Faces with `rmesh` 1 and `rskip`; thinned; a proxy item with Edge mode; unconsolidated with its own material; a replaced node material; disabled; no surface; billboard with fake shadows off; and the stock "fallen leaves oak" preset merged with its sources. Plus a ForestIvy and an XShadow Omni.
  - The `.rail` item (Baseboard 002) was skipped: a `.rail` is iToo's own chunked format, loadable only through `loadLibraryItemByPath`.
- **Read-only:** a fingerprint of all 562 nodes (transform, parent, hidden/frozen, material, modifiers), every property of the 356 iToo objects, each RailClone's export (items and hash) and style hash, `trees.count()`, and every property of the 1,462 materials and maps they use, plus the selection and the dirty flag. Two fingerprints with nothing in between matched; a changed seed and a changed `units_x` showed up. The whole-scene run (356 objects, 2.4 s), the 24-name run, a 1 s budget run and an awkward-names run changed nothing and fired no node events.
- **Findings:** every planted condition was reported with the expected code, severity and count, and nothing else; the names run matched the whole-scene run object for object. Newly live: `EDGE_MODE_ON_PROXIES`, `RC_PROXY_MESH_REVERTABLE` with the version read (`7,3,5,687`), `RC_PROXY_CACHE_FILE`, `FOREST_RENDER_DISPLAY`, `FOREST_MAX_FACES_EXCEEDED`, `RC_MAXSEG_NEAR`/`RC_MAXFACES_NEAR`, the unlimited caps and the nested colour map.
- **Through FastMCP:** the tool is registered with the four parameters, and `names` passed as a JSON string, a list or one string all work.
