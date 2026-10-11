"""Forest Pack library catalogue: the index.xml files under the library roots. Python only.

Roots are the values of HKCU\\Software\\Itoo Software\\Forest Pack Pro\\LibraryDir (Forest Pack's own
list, which can include network folders) plus %LOCALAPPDATA%\\Itoo Software\\Forest Pack Pro\\lib.
Each index.xml lists <item> entries: class "maxobject" (a Forest preset or a 3D mesh, merged from
<maxfile> by <object> name) or "tree_billboard" (2D). Matlib paths use $(LIBROOTPATH) (the root)
and $(RENDERER); other relative paths are relative to the index.xml folder.
"""
from __future__ import annotations

import difflib
import ntpath
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

from .forest import ForestError

REGISTRY_KEY = r"Software\Itoo Software\Forest Pack Pro\LibraryDir"
SKIP_DIRS = {"objects", "thumbnails", "maps", "matlibs", "textures", "proxies", "maxfiles", "effects"}
MAX_DEPTH = 6
ROOT_SCAN_BUDGET_S = 10.0
SCAN_CACHE_S = 300.0
TIMEOUT_CACHE_S = 60.0
MAX_INDEX_BYTES = 16_000_000
KINDS = ("presets", "3d", "2d", "all")
_KIND_ORDER = {"presets": 0, "3d": 1, "2d": 2, "other": 3}

_scan_cache: dict[str, tuple[float, list[str], str | None]] = {}
_parse_cache: dict[str, tuple[int, int, list[dict[str, Any]]]] = {}
_renderer_cache: dict[tuple[str, str], list[str]] = {}
_lock = threading.Lock()


def default_root() -> str | None:
    base = os.environ.get("LOCALAPPDATA", "").strip()
    return os.path.join(base, "Itoo Software", "Forest Pack Pro", "lib") if base else None


def registry_roots() -> list[tuple[str, str]]:
    """(label, path) pairs from Forest Pack's LibraryDir key; empty off Windows or without the key."""
    try:
        import winreg
    except ImportError:
        return []
    roots: list[tuple[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            index = 0
            while True:
                try:
                    label, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if isinstance(value, str) and value.strip():
                    roots.append((label or "Library", value.strip()))
    except OSError:
        pass
    return roots


def library_roots() -> list[dict[str, str]]:
    candidates = [(label, path, "registry") for label, path in registry_roots()]
    fallback = default_root()
    if fallback:
        candidates.append(("Forest Library", fallback, "default"))
    roots: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, path, source in candidates:
        key = os.path.normcase(os.path.normpath(path))
        if key not in seen:
            seen.add(key)
            roots.append({"label": label, "path": path, "source": source})
    return roots


def _walk(root: str, deadline: float) -> tuple[list[str], bool]:
    """index.xml paths under root, and whether the walk finished before the deadline."""
    found: list[str] = []
    stack = [(root, 0)]
    while stack:
        if time.monotonic() >= deadline:
            return sorted(found), False
        path, depth = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            if path == root:
                raise
            continue
        for entry in entries:
            name = entry.name
            try:
                if entry.is_file() and name.lower() == "index.xml":
                    found.append(entry.path)
                elif (depth < MAX_DEPTH and not name.startswith(".") and name.lower() not in SKIP_DIRS
                      and entry.is_dir(follow_symlinks=False)):
                    stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return sorted(found), True


def scan_root(root: str, budget_s: float = ROOT_SCAN_BUDGET_S) -> tuple[list[str], str | None]:
    """index.xml paths under root, scanned on a daemon thread so a slow network share can't stall the call.

    A scan cut short keeps what it found. Results are cached for SCAN_CACHE_S; a root that didn't answer at
    all is retried after TIMEOUT_CACHE_S.
    """
    with _lock:
        cached = _scan_cache.get(root)
        if cached and time.monotonic() < cached[0]:
            return list(cached[1]), cached[2]
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            files, complete = _walk(root, time.monotonic() + budget_s)
            if not complete:
                box["error"] = f"partial: the scan stopped after {budget_s:g} s"
            box["files"] = files
        except OSError as exc:
            box["error"] = f"not readable: {exc.strerror or exc}"
            box["files"] = []

    worker = threading.Thread(target=run, name="forest-library-scan", daemon=True)
    worker.start()
    worker.join(budget_s + 1.0)
    if "files" in box:
        files, error, ttl = box["files"], box.get("error"), SCAN_CACHE_S
    else:
        files, error, ttl = [], f"timed out after {budget_s:g} s", TIMEOUT_CACHE_S
    with _lock:
        _scan_cache[root] = (time.monotonic() + ttl, list(files), error)
    return list(files), error


def _read_xml(path: str) -> ET.Element:
    with open(path, "rb") as fh:
        data = fh.read(MAX_INDEX_BYTES + 1)
    if len(data) > MAX_INDEX_BYTES:
        raise ValueError("index.xml is too large")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise ValueError("DTDs and entity declarations are not supported")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", data.decode("cp1252", errors="replace"))
        return ET.fromstring(text)


def _text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _english_name(element: ET.Element) -> str:
    names = element.findall("name")
    for candidate in names:
        if (candidate.get("lang") or "").lower() == "english" and (candidate.text or "").strip():
            return candidate.text.strip()
    for candidate in names:
        if (candidate.text or "").strip():
            return candidate.text.strip()
    return ""


def _kind(parts: list[str], item_class: str) -> str:
    lowered = [p.lower() for p in parts]
    for kind in ("presets", "3d", "2d"):
        if kind in lowered:
            return kind
    return "2d" if item_class == "tree_billboard" else "other"


def resolve_path(template: str, index_dir: str, root: str, renderer: str | None = None) -> str | None:
    """A library path with $(LIBROOTPATH) (and $(RENDERER) when given) resolved; relative to index_dir."""
    if not template:
        return None
    path = re.sub(r"\$\(LIBROOTPATH\)", lambda _: root, template, flags=re.I)
    if renderer is not None:
        path = re.sub(r"\$\(RENDERER\)", lambda _: renderer, path, flags=re.I)
    if not ntpath.isabs(path):
        path = ntpath.join(index_dir, path)
    return ntpath.normpath(path)


def matlib_renderers(template: str, index_dir: str, root: str) -> list[str]:
    """Renderer names for which the item's material library file exists."""
    if not template or not re.search(r"\$\(RENDERER\)", template, re.I):
        return []
    pattern = resolve_path(template, index_dir, root, renderer="\x00")
    folder, base = ntpath.split(pattern or "")
    if "\x00" in folder or "\x00" not in base:
        return []
    key = (folder.lower(), base.lower())
    with _lock:
        if key in _renderer_cache:
            return list(_renderer_cache[key])
    prefix, suffix = base.split("\x00", 1)
    found: list[str] = []
    try:
        for name in os.listdir(folder):
            low = name.lower()
            if (len(name) > len(prefix) + len(suffix) and low.startswith(prefix.lower())
                    and low.endswith(suffix.lower())):
                found.append(name[len(prefix):len(name) - len(suffix)])
    except OSError:
        pass
    found.sort()
    with _lock:
        _renderer_cache[key] = found
    return list(found)


def parse_index(path: str, root: dict[str, str]) -> list[dict[str, Any]]:
    """The items of one index.xml (cached by modification time and size)."""
    stat = os.stat(path)
    with _lock:
        cached = _parse_cache.get(path)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
    tree = _read_xml(path)
    index_dir = os.path.dirname(path)
    rel = os.path.relpath(index_dir, root["path"])
    parts = [] if rel in (".", "") else re.split(r"[\\/]+", rel)
    items: list[dict[str, Any]] = []
    for element in tree.iter("item"):
        item_class = element.get("class") or ""
        maxfile_rel = _text(element, "maxfile")
        maxfile = resolve_path(maxfile_rel, index_dir, root["path"]) if maxfile_rel else None
        template = _text(element, "matlib") or _text(element, "library")
        bitmap = _text(element, "bitmap")
        thumbnail = None
        if bitmap:
            for candidate in (os.path.join(index_dir, "thumbnails", bitmap), os.path.join(index_dir, bitmap)):
                if os.path.isfile(candidate):
                    thumbnail = candidate
                    break
        name = _english_name(element) or _text(element, "object")
        description = " ".join(_text(element, "description").split())
        record = {
            "name": name,
            "kind": _kind(parts, item_class),
            "library": parts[0] if parts else "",
            "category": parts[-1] if parts else os.path.basename(index_dir),
            "family": _text(element, "family"),
            "available": _text(element, "available"),
            "uuid": _text(element, "uuid"),
            "class": item_class,
            "object": _text(element, "object"),
            "maxfile": maxfile,
            "maxfile_exists": bool(maxfile and os.path.isfile(maxfile)),
            "matlib": resolve_path(template, index_dir, root["path"]) if template else None,
            "matlib_renderers": matlib_renderers(template, index_dir, root["path"]),
            "material": _text(element, "material"),
            "thumbnail": thumbnail,
            "description": description[:200],
            "root": root["label"],
            "_index_dir": index_dir,
            "_root_path": root["path"],
            "_matlib_template": template,
        }
        record["_hay"] = " ".join(str(record[k]) for k in (
            "name", "object", "family", "category", "library", "uuid", "description", "kind")).lower()
        items.append(record)
    with _lock:
        _parse_cache[path] = (stat.st_mtime_ns, stat.st_size, items)
    return items


def catalogue() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """All items, the per-root scan summary and warnings."""
    items: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = []
    warnings: list[str] = []
    for root in library_roots():
        files, error = scan_root(root["path"])
        info: dict[str, Any] = {"label": root["label"], "path": root["path"], "source": root["source"],
                                "index_files": len(files)}
        if error:
            info["error"] = error
        count = 0
        for path in files:
            try:
                parsed = parse_index(path, root)
            except (OSError, ValueError, ET.ParseError) as exc:
                warnings.append(f"{path}: {exc}")
                continue
            items.extend(parsed)
            count += len(parsed)
        info["items"] = count
        roots.append(info)
    return items, roots, warnings


def public(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def _score(item: dict[str, Any], query: str) -> int:
    name, obj = item["name"].lower(), item["object"].lower()
    if not query:
        return 3
    if query in (name, obj, item["uuid"].lower()):
        return 0
    if name.startswith(query) or obj.startswith(query):
        return 1
    if query in name or query in obj:
        return 2
    return 3


def search(query: str, kind: str, limit: int) -> dict[str, Any]:
    items, roots, warnings = catalogue()
    q = " ".join(query.lower().split())
    tokens = q.split()
    matched = [it for it in items if (kind == "all" or it["kind"] == kind) and all(t in it["_hay"] for t in tokens)]
    matched.sort(key=lambda it: (_score(it, q), _KIND_ORDER.get(it["kind"], 9), it["library"].lower(),
                                 it["category"].lower(), it["name"].lower()))
    return {"query": query, "kind": kind, "total": len(matched), "returned": min(limit, len(matched)),
            "items": [public(it) for it in matched[:limit]], "roots": roots, "warnings": warnings}


def find_item(item: str) -> dict[str, Any]:
    """One library item by uuid, object name or English name (case-insensitive)."""
    items, roots, _ = catalogue()
    key = item.strip().lower()
    hits = [it for it in items if it["uuid"].lower() == key]
    if not hits:
        hits = [it for it in items if key in (it["object"].lower(), it["name"].lower())]
    unique = {((it["maxfile"] or "").lower(), it["object"].lower()): it for it in hits}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        options = "; ".join(f"{it['uuid']} ({it['name']}, {it['library']}/{it['category']})" for it in hits[:8])
        raise ForestError("AMBIGUOUS", f"{len(unique)} library items match '{item}'; pass a uuid: {options}")
    names = sorted({it["name"] for it in items} | {it["object"] for it in items if it["object"]})
    close = difflib.get_close_matches(item, names, n=5, cutoff=0.5)
    where = ", ".join(r["path"] for r in roots) or "no library roots"
    hint = f" Close matches: {', '.join(close)}." if close else " Use forest_library to search."
    raise ForestError("NOT_FOUND", f"no library item matches '{item}' (searched {where}).{hint}")
