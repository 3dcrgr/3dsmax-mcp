"""Read-only diagnostics for the Vantage tools: V-Ray output errors in Max.log.

vray_output_errors (#49): after a Live Link start that ran no IPR, the lines this Max wrote to its
Max.log since the start (from the size recorded before it) that say V-Ray couldn't write its
output image, e.g. a raw .vrimg file locked by another process.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

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
