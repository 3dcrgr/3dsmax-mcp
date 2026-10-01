# Texture paths missing from Revit (Autodesk) materials

Fork issue #5. Commits `95b17f7`, `3b11fc8` and `c89028e`.

## What happened

`material_roles(scan_scene=true)` was run on materials imported from Revit/ATF (`Autodesk_Material`). It reported every `Autodesk Bitmap` slot with `file: null` and `exists: null`, even for materials that clearly had textures (`Generic_Image`, `Parameters_Color_Map`, `Bump_Image`, `Wood_Image`). It also didn't mark the audit incomplete, so a texture audit of a Revit scene looked clean when it had read nothing.

## Why

The material graph reader (native `inspect_material_network`, which `material_roles` uses) only collected file paths from plain filename parameters. An Autodesk Bitmap keeps its image in a `TYPE_BITMAP` parameter (a bitmap asset), not in a filename string. So nothing was found. And because the Python side had no concept of "this map should have a file", an empty result passed silently.

## What changed

Native (`material_network_handlers.cpp`):
- Paths are also read from `TYPE_BITMAP` parameters (the PBBitmap's asset or name) and from asset-backed `TYPE_FILENAME` parameters. These candidates are used **only when the map has no plain path** (`3b11fc8`).
- Parameters with an empty internal name are never treated as texture sources (`c89028e`).
  - Cosmos VRayBitmaps carry a hidden, unnamed paramblock parameter that holds a package-relative copy of the texture path (`<guid>/textures/Diff_4k_srgb.tx`). MAXScript can't see it: `showProperties`, `getPropNames` and `enumerateFiles` don't list it.
  - The graph used to report it as a second file with parameter `""`, next to the real `HDRIMapName` path, which raised a false `FILE_MISSING`. This predates the fork.
  - Such parameters are now named `param_<id>`, and their value is still reported when values are requested.
- Relative paths are resolved through Max's search paths, with a cache that lives for one request.
- A leaf map with no file parameter at all falls back to its own aux files. This enumeration is non-recursive, uses the shallowest reference level only, and is guarded against ambiguity.
- Graph nodes carry:
  - `classInternal` when the class name is localized;
  - `fileUnset` to tell an empty slot apart from one that couldn't be read.
- Material replication's verification no longer reads asset files, which matches its plan.

Python (`material_roles`):
- A file-bearing map with no path now gives a `FILE_PATH_UNREADABLE` warning and `complete: false`. File-bearing here means Bitmap, Autodesk Bitmap, VRayBitmap/HDRI, CoronaBitmap, aiImage, or an OSL image loader with no file feeding it.
- An empty slot gives `FILE_NOT_ASSIGNED`.

## How it was verified

Live on 3ds Max 2026, 2026-10-01:
- **An Autodesk Bitmap on a Revit furniture fabric** now reports its path, from parameter `Parameters_Source`: `C:\Program Files (x86)\Common Files\Autodesk Shared\Materials\Textures\1\Mats\Furnishings.Fabrics.Linen.White.jpg`. It also reports `exists: false`, which is correct, because that texture library isn't installed on the machine.
- **Unset Autodesk Metal relief and cutout bitmaps** give `FILE_PATH_UNREADABLE`, and the audit reports `complete: false`.
- **Cosmos VRayBitmaps:** the duplicate package-relative row was found live at 11:20 and traced to the unnamed parameter. The fix (`c89028e`) is pending live verification.

Unit tests: `tests/test_material_roles_files.py`.

## Upstream status

Not fixed upstream as of 2026-10-01. `material_roles` came from EdgecraftStudio's upstream PR 29, which was tested on Corona scenes, so this is a natural follow-up to raise with them.
