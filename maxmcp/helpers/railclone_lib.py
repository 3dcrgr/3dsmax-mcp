"""RailClone helpers: XML sanitising, output geometry, exposed parameters, library index.

Pure Python; nothing here talks to 3ds Max. The facts behind it come from live
tests on RailClone Pro 7.1.1 (``RailClone_Pro.version()`` = 400) in 3ds Max 2026;
see docs/fork/railclone.md.
"""
from __future__ import annotations

import difflib
import itertools
import math
import ntpath
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any, Iterable


class RailCloneError(RuntimeError):
    """A structured RailClone failure; ``code`` reaches the MCP error envelope."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = code in {"USER_BUSY", "STALE_STYLE", "STALE_PARAMS"}
        self.details = details or None


# ---------------------------------------------------------------- style XML

# RailClone 7.x getXMLStyle() writes some attributes twice on graph nodes, e.g.
# x="-298.322" y="-206.678" y="-206.678" (7 of 43 stock items, styles v210/v300).
# The duplicates seen live were always identical; keep the first.
DUP_ATTR_PATTERN = r'(\s([\w:.-]+)="([^"]*)")(?:\s\2="[^"]*")+'
DUP_ATTR = re.compile(DUP_ATTR_PATTERN)
_ATTR = re.compile(r'\s([\w:.-]+)="([^"]*)"')


def sanitize_xml(text: str) -> tuple[str, int, int]:
    """Return (text without repeated attributes, attributes removed, removed values that differed)."""
    removed = conflicts = 0

    def keep_first(match: re.Match) -> str:
        nonlocal removed, conflicts
        first = match.group(3)
        for extra in _ATTR.finditer(match.group(0), len(match.group(1))):
            removed += 1
            if extra.group(2) != first:
                conflicts += 1
        return match.group(1)

    return DUP_ATTR.sub(keep_first, text), removed, conflicts


def graph_summary(root: ET.Element, param_ids: Iterable[Any] = ()) -> dict[str, Any]:
    """Count graph nodes by type and list exposed-parameter connectors (_exp_*)."""
    index = {str(pid).lower(): i for i, pid in enumerate(param_ids, 1) if pid is not None}
    nodes = list(root.iter("node"))
    by_type = Counter(f"{n.get('baseClass', '?')}/{n.get('class', '?')}" for n in nodes)
    exposed = []
    for node in nodes:
        for conn in node.findall("connector"):
            cid = conn.get("id", "")
            if not cid.startswith("_exp_"):
                continue
            target = conn.get("target_id", "")
            exposed.append({
                "node_id": node.get("id"),
                "node": node.get("name"),
                "node_type": f"{node.get('baseClass', '?')}/{node.get('class', '?')}",
                "connector": cid,
                "input": conn.get("name"),
                "param_id": target,
                "param_name": conn.get("target_name"),
                "param_index": index.get(target.lower()),
            })
    return {
        "scene": dict(root.attrib),
        "node_count": len(nodes),
        "by_type": dict(sorted(by_type.items())),
        "exposed": exposed,
    }


# ---------------------------------------------------------------- base roles

# What a base feeds, read from the style XML (live on RailClone 7.3.5, probes p12/p13/p19): the connectors
# whose target_id is the base id. batype can't tell a path from a clipping area (it is 0 for every spline base),
# and base names are no guide: 12 stock InputMacros named "Clipping area" are spline inputs.
GENERATOR_INPUTS = {"spline": "path", "spline_x": "path", "spline_y": "path", "clipping_area": "clip",
                    "base_surface": "surface"}
# A macro input's connector id is "{macroId}{inputMacroId}"; the InputMacro's <parameter id="type"> is
# 0 segment, 1 spline (followed inside the macro), 2 surface, 3 clipping area.
INPUT_MACRO_ROLES = {0: "segment", 2: "surface", 3: "clip"}
ROLE_STEPS = 500
# tools/railclone.py has the same walk in MAXScript (rcBaseRoles) for railclone_from_library; keep them alike.


def combine_roles(found: set[str]) -> str:
    """One role from everything a base feeds: path, clip, surface, segment, mixed, unused or unknown."""
    if not found:
        return "unused"
    if "path" in found and "clip" in found:
        return "mixed"
    for role in ("path", "clip", "surface"):
        if role in found:
            return role
    return "segment" if found == {"segment"} else "unknown"


def base_roles(root: ET.Element, base_ids: Iterable[Any]) -> list[str]:
    """The role of each base (in base_ids order), following operators and macro inputs to a generator input."""
    connectors = []
    macros: dict[str, int] = {}
    for node in root.iter("node"):
        owner = (node.get("id") or "").lower()
        owner_class = node.get("baseClass") or ""
        # A node's inputs are its own <connector>s and those in its <groupConnectors> lists (an operator's
        # variable inputs, e.g. Basic Ops' "Splines": new_spline). Live: 12 stock .rail styles reach their only
        # path through such a list (Glass Railing 03, the Industrial pipes and ducts).
        for conn in node.findall("connector") + node.findall("groupConnectors/connector"):
            target = (conn.get("target_id") or "").lower()
            if target:
                connectors.append(((conn.get("id") or "").lower(), target, owner, owner_class))
        if node.get("class") == "InputMacro":
            kind = -1
            for param in node.findall("parameter"):
                if param.get("id") == "type":
                    try:
                        kind = int(param.get("value") or "")
                    except ValueError:
                        kind = -1
            macros[owner] = kind
    roles = []
    for base_id in base_ids:
        found: set[str] = set()
        queue, seen, steps = [str(base_id).lower()], set(), 0
        while queue and steps < ROLE_STEPS:
            steps += 1
            target = queue.pop()
            if target in seen:
                continue
            seen.add(target)
            for cid, tgt, owner, owner_class in connectors:
                if tgt != target:
                    continue
                if cid in GENERATOR_INPUTS:
                    found.add(GENERATOR_INPUTS[cid])
                    continue
                if cid.startswith("_exp_"):
                    continue
                cut = cid.find("}{")
                inner = cid[cut + 1:] if cut >= 0 and cid.startswith("{") else ""
                if inner and inner in macros:
                    kind = macros[inner]
                    if kind in INPUT_MACRO_ROLES:
                        found.add(INPUT_MACRO_ROLES[kind])
                    else:
                        queue.append(inner)
                elif owner_class == "Operator":
                    queue.append(owner)
                else:
                    found.add("unknown")
        roles.append(combine_roles(found))
    return roles


# ---------------------------------------------------------------- output XML

# Placeholder items carry an inverted box at about +/-2.147e9 (INT_MAX).
SENTINEL = 1.0e9
_VEC = re.compile(r"\[([^\]]*)\]")


def parse_vectors(text: Any) -> list[list[float]] | None:
    if not isinstance(text, str):
        return None
    vectors = []
    for body in _VEC.findall(text):
        parts = body.split(",")
        if len(parts) != 3:
            return None
        try:
            vectors.append([float(p) for p in parts])
        except ValueError:
            return None
    return vectors or None


def parse_box(text: Any) -> tuple[list[float], list[float]] | None:
    vectors = parse_vectors(text)
    return (vectors[0], vectors[1]) if vectors and len(vectors) == 2 else None


def parse_matrix(text: Any) -> list[list[float]] | None:
    """Parse 12 comma-separated floats (row1..row4 of a Max matrix3)."""
    if not isinstance(text, str):
        return None
    try:
        values = [float(v) for v in text.split(",")]
    except ValueError:
        return None
    if len(values) != 12 or not all(math.isfinite(v) for v in values):
        return None
    return [values[i:i + 3] for i in range(0, 12, 3)]


def transform_point(p: list[float], rows: list[list[float]]) -> list[float]:
    """Max row-vector convention: p * M = x*row1 + y*row2 + z*row3 + row4."""
    return [p[0] * rows[0][k] + p[1] * rows[1][k] + p[2] * rows[2][k] + rows[3][k] for k in range(3)]


def is_placeholder(item: dict[str, Any]) -> bool:
    """No source_segment, or an inverted/sentinel box: a slot that places nothing."""
    if not item.get("source_segment"):
        return True
    box = parse_box(item.get("box"))
    if box is None:
        return False
    lo, hi = box
    return any(a > b for a, b in zip(lo, hi)) or any(abs(c) >= SENTINEL for c in (*lo, *hi))


def _grow(bounds: list[list[float]] | None, point: list[float]) -> list[list[float]]:
    if bounds is None:
        return [list(point), list(point)]
    for k in range(3):
        bounds[0][k] = min(bounds[0][k], point[k])
        bounds[1][k] = max(bounds[1][k], point[k])
    return bounds


def _bounds_out(bounds: list[list[float]] | None) -> dict[str, list[float]] | None:
    if bounds is None:
        return None
    return {"min": [round(v, 6) for v in bounds[0]], "max": [round(v, 6) for v in bounds[1]]}


def summarize_items(items: list[dict[str, Any]], node_rows: list[list[float]] | None = None) -> dict[str, Any]:
    """Counts by source segment and object/world bounds of the placed items."""
    placed = 0
    by_segment: Counter = Counter()
    local = world = None
    for item in items:
        if is_placeholder(item):
            continue
        placed += 1
        by_segment[item.get("source_segment")] += 1
        box = parse_box(item.get("box"))
        if box is None:
            continue
        local = _grow(local, box[0])
        local = _grow(local, box[1])
        if node_rows is not None:
            for corner in itertools.product(*zip(box[0], box[1])):
                world = _grow(world, transform_point(list(corner), node_rows))
    return {
        "total": len(items),
        "placed": placed,
        "placeholders": len(items) - placed,
        "by_segment": dict(sorted(by_segment.items())),
        "bounds_object": _bounds_out(local),
        "bounds_world": _bounds_out(world),
    }


# ---------------------------------------------------------------- exposed parameters

# patype -> meaning and the array the engine reads (verified by output changes). 1 is a plain float: stock
# styles use it for angles only, but macro parameters with the same enum use it for any float (RC 7.3.5 survey
# of 226 .max and 179 .rail styles). 6 is String (pastrval) per the docs; no stock style uses it, so its value is
# read but never written.
PARAM_TYPES: dict[int, dict[str, Any]] = {
    0: {"name": "int", "array": "paintval", "limits": ("paintmin", "paintmax")},
    1: {"name": "float", "array": "pafloatval", "limits": ("pafloatmin", "pafloatmax")},
    2: {"name": "percent", "array": "pafloatval", "limits": ("pafloatmin", "pafloatmax")},
    3: {"name": "distance", "array": "paunitval", "limits": ("paunitmin", "paunitmax")},
    4: {"name": "bool", "array": "paboolval", "limits": None},
    5: {"name": "selector", "array": "pafloatval", "limits": None},
    6: {"name": "string", "array": "pastrval", "limits": None, "read_only": True},
    7: {"name": "separator", "array": None, "limits": None},
}
PARAM_FIELDS = ("paid", "paname", "patype", "paintval", "pafloatval", "paunitval", "paboolval", "pastrval",
                "paselector", "palimit", "paintmin", "paintmax", "pafloatmin", "pafloatmax", "paunitmin",
                "paunitmax", "padesc")
BASE_FIELDS = ("baid", "baname", "batype", "banode", "bastart", "balength", "bafull", "badesc")
SEGMENT_FIELDS = ("sid", "sname", "sobjnode", "sobjref", "sobjmtl", "sinstance", "sslice", "sbend", "srenderable")
WRITABLE_ARRAYS = frozenset({"paintval", "pafloatval", "paunitval", "paboolval"})

_INT_TEXT = re.compile(r"^\s*[-+]?\d+\s*$")
_UNIT_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "km": 1.0e6, "in": 25.4, "ft": 304.8}
_UNIT_VALUE = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(mm|cm|m|km|in|ft)?\s*$")


def parse_selector(text: Any) -> list[dict[str, Any]]:
    """Parse paselector "Label|value;Label|value" into options with 0-based indices."""
    options: list[dict[str, Any]] = []
    if not isinstance(text, str) or not text:
        return options
    for chunk in text.split(";"):
        if not chunk:
            continue
        label, sep, raw = chunk.rpartition("|")
        if not sep:
            label, raw = chunk, ""
        value: Any = raw
        try:
            value = int(raw) if _INT_TEXT.match(raw) else float(raw)
        except ValueError:
            pass
        options.append({"label": label, "value": value, "index": len(options)})
    return options


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def param_view(raw: dict[str, Any], index: int, upm: float | None) -> dict[str, Any]:
    """One exposed parameter as JSON, with the value read from its typed array."""
    ptype = raw.get("patype")
    spec = PARAM_TYPES.get(ptype) if isinstance(ptype, int) else None
    view: dict[str, Any] = {
        "index": index,
        "id": raw.get("paid"),
        "name": raw.get("paname"),
        "type": ptype,
        "type_name": spec["name"] if spec else "unknown",
        "array": spec["array"] if spec else None,
    }
    if spec and spec["array"]:
        view["value"] = raw.get(spec["array"])
    if ptype == 3 and upm and _number(view.get("value")) is not None:
        view["value_mm"] = round(view["value"] / upm, 6)
    if spec and spec["limits"]:
        lo, hi = (raw.get(k) for k in spec["limits"])
        view["limit"] = {"on": bool(raw.get("palimit")), "min": lo, "max": hi}
        value = _number(view.get("value"))
        if raw.get("palimit") and value is not None and (
                (_number(lo) is not None and value < lo) or (_number(hi) is not None and value > hi)):
            view["out_of_limits"] = True
    if ptype == 5:
        options = parse_selector(raw.get("paselector"))
        view["options"] = options
        view["ui_index"] = raw.get("paintval")
        current = _number(raw.get("pafloatval"))
        match = [o for o in options if _number(o["value"]) is not None and current is not None
                 and abs(float(o["value"]) - current) < 1e-6]
        view["selected"] = {"label": match[0]["label"], "index": match[0]["index"]} if match else None
    if spec and spec.get("read_only"):
        view["read_only"] = True
    if ptype not in PARAM_TYPES:
        view["raw"] = {k: raw.get(k) for k in ("paintval", "pafloatval", "paunitval", "paboolval", "pastrval")}
    if raw.get("padesc"):
        view["description"] = raw.get("padesc")
    return view


def _mxs_float(value: float) -> str:
    return repr(float(value))


def _check_limits(raw: dict[str, Any], spec: dict[str, Any], value: float, label: str) -> None:
    if not raw.get("palimit"):
        return
    lo, hi = (_number(raw.get(k)) for k in spec["limits"])
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ValueError(f"{label}: {value!r} is outside the parameter limits [{lo}, {hi}]; "
                         "RailClone does not clamp scripted values")


def coerce_param_value(raw: dict[str, Any], value: Any, upm: float | None, label: str) -> dict[str, Any]:
    """Validate one requested value; return the typed value and the array writes."""
    ptype = raw.get("patype")
    spec = PARAM_TYPES.get(ptype) if isinstance(ptype, int) else None
    if spec is None:
        raise ValueError(f"{label}: parameter type {ptype!r} is not supported (verified types: 0-5, 7)")
    if ptype == 7:
        raise ValueError(f"{label}: is a separator/label row and has no value")
    if spec.get("read_only"):
        raise ValueError(f"{label}: is a {spec['name']} parameter (type {ptype}); the docs say it is stored in "
                         f"{spec['array']}, but no stock style uses this type, so writing it is untested and refused")
    if ptype == 4:
        if isinstance(value, bool):
            flag = value
        elif isinstance(value, int) and value in (0, 1):
            flag = bool(value)
        else:
            raise ValueError(f"{label}: boolean parameter needs true or false")
        return {"value": flag, "writes": [("paboolval", "true" if flag else "false")]}
    if ptype == 5:
        options = parse_selector(raw.get("paselector"))
        if not options:
            raise ValueError(f"{label}: selector has no options")
        if isinstance(value, str):
            chosen = [o for o in options if o["label"] == value] or \
                     [o for o in options if o["label"].strip() == value.strip()]
        elif _number(value) is not None:
            chosen = [o for o in options if _number(o["value"]) is not None
                      and abs(float(o["value"]) - float(value)) < 1e-9]
        else:
            chosen = []
        if len(chosen) != 1:
            labels = ", ".join(f"{o['label']!r}={o['value']}" for o in options)
            raise ValueError(f"{label}: {value!r} is not exactly one selector option ({labels})")
        option = chosen[0]
        number = _number(option["value"])
        if number is None:
            raise ValueError(f"{label}: option {option['label']!r} has a non-numeric value {option['value']!r}")
        return {"value": option["value"], "label": option["label"], "option_index": option["index"],
                "writes": [("pafloatval", _mxs_float(number)), ("paintval", str(option["index"]))]}
    if ptype == 0:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or float(value) != int(value):
            raise ValueError(f"{label}: integer parameter needs a whole number")
        number = int(value)
        if abs(number) > 2**31 - 1:
            raise ValueError(f"{label}: integer out of range")
        _check_limits(raw, spec, number, label)
        return {"value": number, "writes": [("paintval", str(number))]}
    if ptype == 3 and isinstance(value, str):
        match = _UNIT_VALUE.match(value)
        if not match:
            raise ValueError(f"{label}: distance must be a number in scene units or a string like '60cm'")
        number = float(match.group(1))
        if match.group(2):
            if not upm:
                raise ValueError(f"{label}: scene unit scale unknown; pass scene units")
            # 7 significant digits: upm comes from a float32 ("20cm" gave 20.0000002 cm live)
            number = float(f"{number * _UNIT_MM[match.group(2)] * upm:.7g}")
    else:
        number = _number(value)
        if number is None:
            raise ValueError(f"{label}: needs a finite number")
    _check_limits(raw, spec, number, label)
    result = {"value": number, "writes": [(spec["array"], _mxs_float(number))]}
    if ptype == 3 and upm:
        result["value_mm"] = round(number / upm, 6)
    return result


def plan_param_writes(params: list[dict[str, Any]], requested: dict[str, Any], upm: float | None) -> list[dict]:
    """Resolve keys (exact paname or paid) and validate values; nothing is clamped."""
    if not isinstance(requested, dict) or not requested:
        raise ValueError("params must be a non-empty object of parameter name or id -> value")
    if len(requested) > 100:
        raise ValueError("at most 100 parameters per call")
    by_id = {str(p.get("paid")).lower(): i for i, p in enumerate(params) if p.get("paid") is not None}
    plan, seen = [], {}
    for key, value in requested.items():
        if not isinstance(key, str) or not key:
            raise ValueError("parameter keys must be non-empty strings (exact name or id)")
        if key.lower() in by_id:
            pos = by_id[key.lower()]
        else:
            hits = [i for i, p in enumerate(params) if p.get("paname") == key]
            if not hits:
                names = sorted({str(p.get("paname")) for p in params if p.get("patype") != 7})
                raise ValueError(f"PARAM_NOT_FOUND: no exposed parameter named {key!r}; available: {names}")
            if len(hits) > 1:
                ids = [params[i].get("paid") for i in hits]
                raise ValueError(f"AMBIGUOUS_PARAM: {len(hits)} parameters are named {key!r}; use an id: {ids}")
            pos = hits[0]
        if pos in seen:
            raise ValueError(f"{key!r} and {seen[pos]!r} name the same parameter")
        seen[pos] = key
        raw = params[pos]
        coerced = coerce_param_value(raw, value, upm, repr(key))
        for array, _ in coerced["writes"]:
            if array not in WRITABLE_ARRAYS:
                raise ValueError(f"internal: refusing to write {array}")
        spec = PARAM_TYPES[raw["patype"]]
        plan.append({"key": key, "index": pos + 1, "id": raw.get("paid"), "name": raw.get("paname"),
                     "type": raw["patype"], "type_name": spec["name"], "array": spec["array"],
                     "requested": value, **coerced})
    return plan


# ---------------------------------------------------------------- library

LIBRARY_KEY = r"Software\Itoo Software\RailClone Pro\LibraryDir"
MAX_INDEX_BYTES = 16 * 1024 * 1024
DEFAULT_INDEX_CAP = 5000
DEFAULT_SCAN_SECONDS = 20.0
PROBE_SECONDS = 3.0
_MACRO = re.compile(r"\$\((\w+)\)")
_RENDERER_MACROS = frozenset({"RENDERER", "SELECTEDRENDERER"})


def library_roots() -> tuple[list[dict[str, str]], list[str]]:
    """Library browser roots: HKCU LibraryDir values, name -> folder (the name is the route root)."""
    try:
        import winreg  # noqa: PLC0415 - Windows only
    except ImportError:
        return [], ["NO_REGISTRY: the RailClone library is only readable on Windows"]
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, LIBRARY_KEY)
    except OSError:
        return [], [f"NO_LIBRARY_ROOTS: HKCU\\{LIBRARY_KEY} not found (open the RailClone library browser once)"]
    roots = []
    with key:
        for i in itertools.count():
            try:
                name, data, _kind = winreg.EnumValue(key, i)
            except OSError:
                break
            if name and isinstance(data, str) and data.strip():
                roots.append({"name": name, "path": os.path.expandvars(data.strip())})
    return roots, []


def _same_dir(a: str, b: str) -> bool:
    def key(path: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.expandvars(path.strip()))).rstrip("\\/")
    return key(a) == key(b)


def root_differences(server: list[dict[str, str]], max_side: list[dict[str, str]]) -> list[str]:
    """How the server's root list differs from 3ds Max's: roots only on one side or with another folder."""
    theirs = {r["name"]: r["path"] for r in max_side}
    ours = {r["name"]: r["path"] for r in server}
    out = [f"{n!r} is only in 3ds Max ({p})" for n, p in theirs.items() if n not in ours]
    out += [f"{n!r} is not in 3ds Max (this server sees {p})" for n, p in ours.items() if n not in theirs]
    out += [f"{n!r} is {theirs[n]} in 3ds Max, {p} here" for n, p in ours.items()
            if n in theirs and not _same_dir(p, theirs[n])]
    return out


def probe_dir(path: str, timeout: float = PROBE_SECONDS) -> str | None:
    """None when path is a reachable folder, else why not (unreachable shares can block for long)."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["ok"] = os.path.isdir(path)
        except OSError as exc:  # pragma: no cover - isdir rarely raises
            outcome["error"] = str(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return f"no answer within {timeout:g} s"
    if outcome.get("error"):
        return outcome["error"]
    return None if outcome.get("ok") else "folder not found"


def find_index_files(root_dir: str, deadline: float, cap: int) -> tuple[list[str], bool]:
    """index.xml paths under root_dir, sorted; False when the cap or deadline stopped the walk."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames.sort()
        if time.monotonic() > deadline or len(found) >= cap:
            return sorted(found), False
        if any(f.lower() == "index.xml" for f in filenames):
            name = next(f for f in filenames if f.lower() == "index.xml")
            found.append(os.path.join(dirpath, name))
    return sorted(found), True


def _expand(text: str, macros: dict[str, str]) -> str:
    return _MACRO.sub(lambda m: macros.get(m.group(1).upper(), "$(" + m.group(1).upper() + ")"), text)


def library_relative(path: str) -> str | None:
    """The index-folder-relative part of a library path, or None for an absolute path.

    index.xml paths are relative to the folder that holds the index.xml (live: 686 of 687 stock RailClone and
    all 511 Forest Pack maxfiles). The docs' template writes them with a leading backslash
    (\\objects\\x.max), which Windows reads as the drive root, so one leading slash is dropped. A UNC path
    (\\\\server\\share) or a drive path stays absolute.
    """
    if path[:2] in ("\\\\", "//") or ntpath.splitdrive(path)[0]:
        return None
    return path.lstrip("\\/")


def _resolve(text: str, macros: dict[str, str], cat_dir: str) -> str:
    if not text:
        return ""
    path = _expand(text.strip(), macros).replace("/", "\\")
    if path.startswith("$("):
        return os.path.normpath(path)
    rel = library_relative(path)
    return os.path.normpath(path if rel is None else os.path.join(cat_dir, rel))


def _thumbnail(text: str, macros: dict[str, str], cat_dir: str) -> str:
    """<bitmap>: a bare file name is in the index folder's thumbnails\\ (live: 683 of 683 stock thumbnails)."""
    if not text:
        return ""
    path = _expand(text.strip(), macros).replace("/", "\\")
    rel = library_relative(path)
    if rel is None or path.startswith("$("):
        return os.path.normpath(path)
    candidates = [os.path.join(cat_dir, rel)]
    if not rel.lower().startswith("thumbnails\\"):
        candidates.insert(0, os.path.join(cat_dir, "thumbnails", rel))
    found = next((c for c in candidates if os.path.isfile(c)), candidates[0])
    return os.path.normpath(found)


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower() if path else ""


def unresolved_macros(path: str) -> list[str]:
    return sorted({m.upper() for m in _MACRO.findall(path or "")})


def read_xml_file(path: str) -> ET.Element:
    with open(path, "rb") as handle:
        data = handle.read(MAX_INDEX_BYTES + 1)
    if len(data) > MAX_INDEX_BYTES:
        raise ValueError("file is larger than 16 MB")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise ValueError("DTDs and entity declarations are not supported")
    return ET.fromstring(data)


def parse_index(path: str, root_name: str, root_dir: str) -> list[dict[str, Any]]:
    """Items of one index.xml; raises ValueError/ET.ParseError/OSError on a bad file."""
    cat_dir = os.path.dirname(path)
    rel = os.path.relpath(cat_dir, root_dir)
    category = "" if rel == "." else rel.replace("/", "\\")
    library = read_xml_file(path)
    macros = {"LIBROOTPATH": root_dir, "LIBFULLPATH": cat_dir}
    hooks = bool(library.findall("maxscript"))
    items = []
    for element in library.iter("item"):
        fields: dict[str, str] = {}
        names: list[tuple[str | None, str]] = []
        for child in element:
            text = (child.text or "").strip()
            if child.tag == "name":
                if text:
                    names.append((child.get("lang"), text))
            elif child.tag not in fields:
                fields[child.tag] = text
        if not names:
            continue
        english = [n for lang, n in names if (lang or "").lower() == "english"]
        name = english[0] if english else names[0][1]
        maxfile = _resolve(fields.get("maxfile", ""), macros, cat_dir)
        matlib = _resolve(fields.get("matlib", ""), macros, cat_dir)
        thumb = _thumbnail(fields.get("bitmap", ""), macros, cat_dir)
        route = "\\" + root_name + ("\\" + category if category else "") + "\\" + name
        item = {
            "route": route,
            "root": root_name,
            "category": category,
            "name": name,
            "family": fields.get("family") or None,
            "description": fields.get("description") or None,
            "available": fields.get("available") or None,
            "maxfile": maxfile or None,
            "maxfile_ext": _ext(maxfile),
            "object": fields.get("object") or None,
            "material": fields.get("material") or None,
            "matlib": matlib or None,
            "matlib_ext": _ext(matlib),
            "uuid": fields.get("uuid") or None,
            "thumbnail": thumb or None,
            "class": element.get("class"),
            "category_dir": cat_dir,
        }
        if len(names) > 1:
            item["names"] = [n for _, n in names]
        if hooks:
            item["library_scripts"] = True
        needs_route = item["maxfile_ext"] == ".rail" or item["matlib_ext"] == ".pmat" or not item["object"]
        item["load_method"] = "route" if needs_route else "merge"
        items.append(item)
    return items


def scan_library(roots: list[dict[str, str]], *, deadline: float, cap: int = DEFAULT_INDEX_CAP,
                 probe_timeout: float = PROBE_SECONDS) -> dict[str, Any]:
    """Walk every reachable root; unreachable roots are skipped with a warning."""
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    report = []
    complete = True
    for root in roots:
        info: dict[str, Any] = {"name": root["name"], "path": root["path"]}
        report.append(info)
        problem = probe_dir(root["path"], probe_timeout)
        if problem:
            info["reachable"] = False
            warnings.append(f"ROOT_UNREACHABLE: {root['name']!r} ({root['path']}): {problem}; skipped")
            continue
        info["reachable"] = True
        files, done = find_index_files(root["path"], deadline, cap)
        info["index_files"] = len(files)
        if not done:
            complete = False
            warnings.append(f"SCAN_INCOMPLETE: {root['name']!r} stopped after {len(files)} index.xml files "
                            "(time or file cap)")
        count = 0
        for path in files:
            try:
                found = parse_index(path, root["name"], root["path"])
            except (OSError, ValueError, ET.ParseError) as exc:
                warnings.append(f"INDEX_UNREADABLE: {path}: {exc}")
                continue
            items.extend(found)
            count += len(found)
        info["items"] = count
    return {"items": items, "roots": report, "warnings": warnings, "complete": complete}


def match_items(items: list[dict[str, Any]], query: str = "", category: str = "") -> list[dict[str, Any]]:
    terms = [t for t in (query or "").lower().split() if t]
    cat = (category or "").replace("/", "\\").strip("\\").lower()
    out = []
    for item in items:
        if cat and cat not in item["category"].lower():
            continue
        if terms:
            hay = " ".join(str(item.get(k) or "") for k in
                           ("name", "category", "family", "description", "object", "route")).lower()
            hay += " " + " ".join(item.get("names") or []).lower()
            if not all(t in hay for t in terms):
                continue
        out.append(item)
    phrase = " ".join(terms)

    def rank(item: dict[str, Any]) -> tuple[int, str]:
        # exact name, then the whole phrase in the name, then every word in the name, then elsewhere
        names = [str(item.get("name") or "").lower()] + [str(n).lower() for n in item.get("names") or []]
        if not terms:
            level = 0
        elif phrase in names:
            level = 0
        elif any(phrase in n for n in names):
            level = 1
        elif any(all(t in n for t in terms) for n in names):
            level = 2
        else:
            level = 3
        return level, item["route"].lower()

    return sorted(out, key=rank)


def public_item(item: dict[str, Any], *, check_files: bool = True) -> dict[str, Any]:
    out = {k: v for k, v in item.items() if k != "category_dir" and v is not None}
    if check_files:
        if item.get("maxfile"):
            out["maxfile_exists"] = os.path.isfile(item["maxfile"])
        if item.get("matlib") and not unresolved_macros(item["matlib"]):
            out["matlib_exists"] = os.path.isfile(item["matlib"])
    if unresolved_macros(item.get("matlib") or ""):
        out["matlib_macros"] = unresolved_macros(item["matlib"])
    return out


def _exact_child(parent: str, name: str) -> tuple[bool, str | None]:
    """(exists with this exact case, the on-disk spelling when only the case differs)."""
    try:
        entries = os.listdir(parent)
    except OSError:
        return False, None
    if name in entries:
        return True, None
    folded = [e for e in entries if e.lower() == name.lower()]
    return False, folded[0] if folded else None


def resolve_route(route: str, roots: list[dict[str, str]]) -> dict[str, Any]:
    """Validate a library route exactly (registry root, folder names, index.xml <name>)."""
    if "/" in route:
        raise RailCloneError("ROUTE_INVALID", "routes use backslashes: \\<root>\\<category>\\<item>")
    parts = route[1:].split("\\")
    if len(parts) < 2 or any(not p for p in parts):
        raise RailCloneError("ROUTE_INVALID", f"route {route!r} must be \\<root>\\<category path>\\<item name>")
    root_name, cat_parts, item_name = parts[0], parts[1:-1], parts[-1]
    root = next((r for r in roots if r["name"] == root_name), None)
    if root is None:
        folded = [r["name"] for r in roots if r["name"].lower() == root_name.lower()]
        hint = f"; the registry spells it {folded[0]!r} (case must match)" if folded else \
            f"; known roots: {[r['name'] for r in roots]}"
        raise RailCloneError("ROUTE_INVALID", f"library root {root_name!r} is not registered{hint}")
    problem = probe_dir(root["path"])
    if problem:
        raise RailCloneError("ROOT_UNREACHABLE", f"library root {root_name!r} ({root['path']}): {problem}")
    folder = root["path"]
    for part in cat_parts:
        exact, spelled = _exact_child(folder, part)
        if not exact:
            hint = f"; on disk it is {spelled!r} (case must match)" if spelled else ""
            raise RailCloneError("ROUTE_INVALID", f"category folder {part!r} not found under {folder}{hint}")
        folder = os.path.join(folder, part)
    index = os.path.join(folder, "index.xml")
    if not os.path.isfile(index):
        raise RailCloneError("ROUTE_INVALID", f"no index.xml in {folder}")
    try:
        items = parse_index(index, root_name, root["path"])
    except (OSError, ValueError, ET.ParseError) as exc:
        raise RailCloneError("INDEX_UNREADABLE", f"{index}: {exc}") from exc
    hits = [it for it in items if it["name"] == item_name]
    if len(hits) != 1:
        names = sorted(it["name"] for it in items)
        what = "no item" if not hits else f"{len(hits)} items"
        raise RailCloneError("ROUTE_INVALID", f"{what} named {item_name!r} in {index}",
                             {"items_in_category": names[:50]})
    return hits[0]


def resolve_item(item: str, roots: list[dict[str, str]], *, deadline: float,
                 cap: int = DEFAULT_INDEX_CAP) -> tuple[dict[str, Any], list[str]]:
    """A route (starts with a backslash) or an exact item name/uuid; never guesses."""
    if not isinstance(item, str) or not item.strip() or "\x00" in item:
        raise ValueError("item must be a library route or an exact item name")
    if not roots:
        raise RailCloneError("NO_LIBRARY_ROOTS", "no RailClone library roots are registered")
    if item.startswith(("\\", "/")):
        return resolve_route(item, roots), []
    if "\\" in item:
        raise RailCloneError("ROUTE_INVALID", "a route starts with a backslash: \\<root>\\<category>\\<item>")
    scan = scan_library(roots, deadline=deadline, cap=cap)
    hits = [it for it in scan["items"]
            if it["name"] == item or item in (it.get("names") or []) or (it.get("uuid") and it["uuid"] == item)]
    if len(hits) == 1:
        return hits[0], scan["warnings"]
    if hits:
        raise RailCloneError("AMBIGUOUS_ITEM", f"{len(hits)} library items are named {item!r}; pass a route",
                             {"routes": [h["route"] for h in hits[:20]]})
    close = set(difflib.get_close_matches(item.lower(), [it["name"].lower() for it in scan["items"]], n=20,
                                          cutoff=0.6))
    near = [it["route"] for it in scan["items"]
            if it["name"].lower() in close or item.lower() in it["name"].lower()][:20]
    suffix = "" if scan["complete"] else " (the scan was incomplete)"
    raise RailCloneError("ITEM_NOT_FOUND", f"no library item is named exactly {item!r}{suffix}",
                         {"similar": near, "warnings": scan["warnings"][:10]})


def matlib_template(item: dict[str, Any]) -> tuple[str, str | None]:
    """(matlib with only renderer macros left, reason it cannot be loaded by merge or None)."""
    path = item.get("matlib") or ""
    if not path or not item.get("material"):
        return "", "the item names no material library or material"
    if item.get("matlib_ext") == ".pmat":
        return "", ".pmat material libraries load only through a route load (method='route')"
    if item.get("matlib_ext") != ".mat":
        return "", f"unsupported material library type {item.get('matlib_ext')!r}"
    left = set(unresolved_macros(path)) - _RENDERER_MACROS
    if left:
        return "", f"material library path has unresolved macros {sorted(left)}"
    return path, None
