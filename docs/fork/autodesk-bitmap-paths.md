# Texture paths missing from Revit (Autodesk) materials

Fork issue #5. Commits `95b17f7`, `3b11fc8` and `c89028e`.

## What happened

`material_roles(scan_scene=true)` was run on materials imported from Revit/ATF (`Autodesk_Material`). It reported every `Autodesk Bitmap` slot with `file: null` and `exists: null`, even for materials that clearly had textures (`Generic_Image`, `Parameters_Color_Map`, `Bump_Image`, `Wood_Image`). It also didn't mark the audit incomplete, so a texture audit of a Revit scene looked clean when it had read nothing.

## Why

The material graph reader (native `inspect_material_network`, which `material_roles` uses) only collected file paths from string and filename parameters whose value looked like a path. An Autodesk Bitmap keeps its image in a `TYPE_BITMAP` parameter (a bitmap asset), not in a filename string. So nothing was found. And because the Python side had no concept of "this map should have a file", an empty result passed silently.

## What changed

Native (`material_network_handlers.cpp`):
- Paths are also read from `TYPE_BITMAP` parameters (the PBBitmap's asset or name) and from asset-backed `TYPE_FILENAME` parameters. These candidates are used **only when the map has no plain path** (`3b11fc8`).
- Parameters with an empty internal name are never treated as texture sources (`c89028e`).
  - Cosmos VRayBitmaps carry a hidden, unnamed paramblock parameter that holds a package-relative copy of the texture path (`<guid>/textures/Diff_4k_srgb.tx`). MAXScript can't see it: `showProperties`, `getPropNames` and `enumerateFiles` don't list it.
  - The graph used to report it as a second file with parameter `""`, next to the real `HDRIMapName` path, which raised a false `FILE_MISSING`. This predates the fork.
  - Such parameters are now named `param_<id>`, and their value is still reported when values are requested.
- Asset-backed paths (`TYPE_BITMAP`, asset-backed filename parameters and aux files) that aren't an existing absolute file are resolved through Max's search paths, with a cache that lives for one request. If the lookup fails, the raw path is still reported, so it shows `exists: false` and a `FILE_MISSING` issue. Plain string and filename paths are reported as stored, without resolution.
- A leaf map (no connected sub-maps) that still has no path falls back to its own aux files, whether or not it has a file parameter. This is meant for an Autodesk Bitmap whose bitmap parameter gives nothing.
  - Each object lists only its own files, but the search also looks up to two levels into the map's helper references (at most 32 objects). It never goes into materials or scene nodes.
  - Only the shallowest level that has an image counts. The path is reported with parameter `auxFiles`.
  - If that level holds more than one distinct image, none is attributed. The graph gets an `AUX_FILES_AMBIGUOUS` warning, and in `material_roles` a file-bearing map stays `FILE_PATH_UNREADABLE`.
- Graph nodes carry:
  - `classInternal` when the class name is localized;
  - `fileUnset` to tell an empty slot apart from one that couldn't be read.
- Material replication (plan and verification) doesn't read asset-backed paths or aux files. It remaps only string and filename paths, so Autodesk Bitmap textures are not remapped.

Python (`material_roles`):
- A file-bearing map with no path now gives a `FILE_PATH_UNREADABLE` warning and `complete: false`. File-bearing here means Bitmap, Autodesk Bitmap, VRayBitmap/HDRI, CoronaBitmap, aiImage, or an OSL image loader with no file feeding it.
- A file-bearing map whose file parameters are all empty (`fileUnset`) gives `FILE_NOT_ASSIGNED` instead, and the audit stays complete. Autodesk Bitmap is the exception: empty parameters prove nothing there, so an unset one still gives `FILE_PATH_UNREADABLE` and `complete: false`.

## How it was verified

Live on 3ds Max 2026, 2026-10-01:
- **An Autodesk Bitmap on a Revit furniture fabric** now reports its path, from parameter `Parameters_Source`: `C:\Program Files (x86)\Common Files\Autodesk Shared\Materials\Textures\1\Mats\Furnishings.Fabrics.Linen.White.jpg`. It also reports `exists: false`, which is correct, because that texture library isn't installed on the machine.
- **Unset Autodesk Metal relief and cutout bitmaps** give `FILE_PATH_UNREADABLE`, and the audit reports `complete: false`.
- **Cosmos VRayBitmaps:** the duplicate package-relative row first showed up in the 10:46 live check. `3b11fc8`, deployed at 11:02 to remove it, didn't (checked at 11:06). At 11:20 the row was traced to the unnamed parameter. The fix (`c89028e`) was deployed with the rebuilt bridge at 12:22. At 12:30 `material_roles` on the Cosmos material `Tiles_B_130cm` gave one row per VRayBitmap (`HDRIMapName`, `exists: true`), no package-relative rows, and `complete: true`.

Unit tests: `tests/test_material_roles_files.py`, for the Python warnings. The native path reading has no automated tests.

## Upstream status

Not fixed upstream as of 2026-10-01. `material_roles` came from EdgecraftStudio's upstream PR 29, which was tested on Corona scenes, so this is a natural follow-up to raise with them.
