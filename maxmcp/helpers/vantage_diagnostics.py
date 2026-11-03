"""Read-only diagnostics for the Vantage tools: recent Vantage crashes and V-Ray output errors in Max.log.

recent_crashes (#51): Windows Error Reporting's "Application Error" events (ID 1000) for vantage*.exe
in the Application log, read with one wevtutil query under a 3 s timeout. Any failure gives
state "unknown". A crash in NVIDIA's NGX/DLSS module adds the DLSS Frame Generation hint.

vray_output_errors (#49): after a Live Link start that ran no IPR, the lines this Max wrote to its
Max.log since the start (from the size recorded before it) that say V-Ray couldn't write its
output image, e.g. a raw .vrimg file locked by another process.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Optional

CRASH_WINDOW_HOURS = 24
CRASH_TIMEOUT_S = 3.0
CRASH_CACHE_S = 60.0
CRASH_EVENTS_SHOWN = 5
_MAX_EVENTS_READ = 200
_QUERY = ("*[System[Provider[@Name='Application Error'] and (EventID=1000) and "
          "TimeCreated[timediff(@SystemTime) <= %d]]]")
_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
NGX_MODULES = ("_nvngx.dll", "nvngx_dlssg.dll", "nvngx_dlss.dll")
NGX_HINT = ("Vantage crashed in NVIDIA's NGX/DLSS module (%s). Turn DLSS Frame Generation off in Vantage, or "
            "update the NVIDIA driver.")

_cache: dict[str, Any] = {}


def _run_wevtutil(args: list[str], timeout: float) -> Optional[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def parse_crash_events(xml_text: str) -> list[dict[str, str]]:
    """Vantage crash events, newest first, from wevtutil's /f:xml output (Event elements with no root)."""
    root = ET.fromstring("<Events>" + xml_text.strip().lstrip("﻿") + "</Events>")
    events = []
    for event in root.iter(_NS + "Event"):
        data = {d.get("Name"): (d.text or "") for d in event.iter(_NS + "Data") if d.get("Name")}
        app = data.get("AppName", "")
        if not app.lower().startswith("vantage"):
            continue
        created = event.find("%sSystem/%sTimeCreated" % (_NS, _NS))
        events.append({"time": (created.get("SystemTime") if created is not None else "") or "",
                       "app": app, "app_version": data.get("AppVersion", ""),
                       "module": data.get("ModuleName", ""), "module_version": data.get("ModuleVersion", ""),
                       "exception": data.get("ExceptionCode", ""), "offset": data.get("FaultingOffset", "")})
    events.sort(key=lambda e: e["time"], reverse=True)
    return events


def recent_crashes(hours: int = CRASH_WINDOW_HOURS, timeout: float = CRASH_TIMEOUT_S,
                   runner: Callable[[list[str], float], Optional[str]] = _run_wevtutil,
                   now: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """{"state": "found" | "none" | "unknown", ...}. Read-only, at most `timeout` seconds, cached a minute."""
    cached = _cache.get("crashes")
    if cached and cached[0] == hours and now() - cached[1] < CRASH_CACHE_S:
        return dict(cached[2])
    out: dict[str, Any] = {"state": "unknown", "window_hours": hours}
    if os.name == "nt":
        try:
            text = runner(["wevtutil", "qe", "Application", "/q:" + _QUERY % (hours * 3600 * 1000),
                           "/f:xml", "/rd:true", "/c:%d" % _MAX_EVENTS_READ], timeout)
            if text is not None:
                events = parse_crash_events(text)
                out["state"] = "found" if events else "none"
                if events:
                    out["count"] = len(events)
                    out["events"] = events[:CRASH_EVENTS_SHOWN]
                    modules = sorted({e["module"] for e in events if e["module"].lower() in NGX_MODULES})
                    if modules:
                        out["hint"] = NGX_HINT % ", ".join(modules)
        except Exception:  # noqa: BLE001 - diagnostics only: never fail the status read
            out = {"state": "unknown", "window_hours": hours}
    _cache["crashes"] = (hours, now(), out)
    return dict(out)


def crash_warning(crashes: dict[str, Any]) -> Optional[str]:
    if crashes.get("state") != "found":
        return None
    last = crashes["events"][0]
    text = ("Chaos Vantage crashed %d time(s) in the last %d h (latest %s, in %s %s, exception %s)." % (
        crashes["count"], crashes["window_hours"], last["time"][:19].replace("T", " ") + " UTC", last["module"],
        last["module_version"], last["exception"]))
    return text + (" " + crashes["hint"] if crashes.get("hint") else "")


# ── Max.log ───────────────────────────────────────────────────────────────────

VRAY_OUTPUT_ERRORS = re.compile(r"cannot create output image file|permission denied|"
                                r"(?:cannot|can't|failed to|error) (?:open|create|writ\w*)[^\n]*output", re.IGNORECASE)
LOG_READ_LIMIT = 256 * 1024
LOG_LINES_SHOWN = 5


def parse_log_mark(detail: str) -> Optional[dict[str, Any]]:
    """The start script's "size|pid|path" (taken just before vrayGPUStartIPR); None when absent."""
    size, _, rest = (detail or "").partition("|")
    pid, _, path = rest.partition("|")
    size, pid, path = size.strip().rstrip("L"), pid.strip().rstrip("L"), path.strip()
    if not path or not size.lstrip("-").isdigit():
        return None
    return {"path": path, "size": int(size), "pid": int(pid) if pid.isdigit() else None}


def vray_output_errors(mark: Optional[dict[str, Any]]) -> list[str]:
    """Lines this Max wrote to Max.log since `mark` that report V-Ray output errors. Empty on any doubt."""
    if not mark:
        return []
    try:
        with open(mark["path"], "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            start = mark["size"] if 0 <= mark["size"] <= end else 0  # smaller: Max started a new log
            start = max(start, end - LOG_READ_LIMIT)
            f.seek(start)
            text = f.read(end - start).decode("utf-8", errors="replace")
    except (OSError, KeyError, TypeError, ValueError):
        return []
    tag = "[%d]" % mark["pid"] if mark.get("pid") else ""
    hits = [line.strip() for line in text.splitlines()
            if (not tag or tag in line) and VRAY_OUTPUT_ERRORS.search(line)]
    return hits[-LOG_LINES_SHOWN:]
