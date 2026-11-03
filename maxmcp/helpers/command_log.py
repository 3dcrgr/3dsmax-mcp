"""Rolling on-disk log of the commands this MCP server sent to 3ds Max (#50).

When Max crashes, its dump can be matched to the command that was running: each
command gets a "sent" line just before it is written to the bridge, and a "done"
line when its reply (or error) comes back. A "sent" line without a "done" line
was still running, or never answered.

File: %LOCALAPPDATA%\\3dsmax-mcp\\diagnostics\\recent-commands.jsonl, the folder
capture_hang_diagnostics saves to. Size-capped: once it holds more than
2 * KEEP_LINES lines it is rewritten with the newest KEEP_LINES (about the last
200 commands). Several MCP servers may append to it, each line names its server
PID. Scripts are cut to PREVIEW_CHARS, values after password, token, secret
or key style names are replaced by <redacted>, and file paths by <path> plus
their extension.

Logging never raises: a full disk or a locked file only loses log lines.
Set MCP_COMMAND_LOG=0 to turn it off.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

FILE_NAME = "recent-commands.jsonl"
KEEP_LINES = 400  # a "sent" and a "done" line per command: about the last 200 commands
PREVIEW_CHARS = 200
_SENT_IDS_KEPT = 256

_lock = threading.Lock()
_lines_estimate: int | None = None
_sent_ids: dict[str, float] = {}

_SECRET = re.compile(
    r"""(?ix)
    \b(password|passwd|pwd|passphrase|token|secret|api[_-]?key|apikey|access[_-]?key|auth(?:orization)?|
       credential|bearer)\b
    (\s*[:=]\s*|\s+)
    ("[^"]*"|'[^']*'|@"[^"]*"|[^\s,;)]+)""")
_WHITESPACE = re.compile(r"\s+")
# File paths name users, clients and projects: keep only the extension. A quoted
# string that holds a drive path (C:\..., C:/...) or a UNC path (\\server\...) is
# replaced whole, spaces included. Unquoted paths are cut at the first space.
_PATH_START = re.compile(r"""(?:\b[A-Za-z]:[\\/]|\\\\[^\\\s"'])""")
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"|@"[^"]*"|\'[^\']*\'')
_BARE_PATH = re.compile(r"""(?:\b[A-Za-z]:[\\/]|\\\\(?=[^\\\s"']))[^"'\s,;)\]]*""")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}(?=[\"']?$)")


def _path_stub(text: str) -> str:
    ext = _EXT.search(text)
    return "<path>" + (ext.group(0) if ext else "")


def _redact_paths(text: str) -> str:
    def quoted(match: re.Match) -> str:
        s = match.group(0)
        if not _PATH_START.search(s):
            return s
        quote = '@"' if s.startswith('@"') else s[0]
        return quote + _path_stub(s) + s[-1]
    text = _QUOTED.sub(quoted, text)
    return _BARE_PATH.sub(lambda m: _path_stub(m.group(0)), text)


def enabled() -> bool:
    return os.environ.get("MCP_COMMAND_LOG", "1").strip().lower() not in ("0", "false", "off", "no")


def log_dir() -> Path:
    """Same folder as capture_hang_diagnostics (tools/diagnostics.diagnostics_dir)."""
    root = os.environ.get("LOCALAPPDATA")
    base = Path(root) if root else Path.home() / "AppData" / "Local"
    return base / "3dsmax-mcp" / "diagnostics"


def log_path() -> Path:
    return log_dir() / FILE_NAME


def preview(text: Any, limit: int = PREVIEW_CHARS) -> str:
    """One line, secrets redacted, at most `limit` characters."""
    flat = _WHITESPACE.sub(" ", str(text or "")).strip()
    flat = _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", flat)
    flat = _redact_paths(flat)
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _command_text(request: str) -> tuple[str, str | None, str | None]:
    """(command, cmd_type, request_id) from the JSON request line."""
    try:
        data = json.loads(request)
    except (TypeError, ValueError):
        return str(request), None, None
    if not isinstance(data, dict):
        return str(request), None, None
    return str(data.get("command", "")), data.get("type"), data.get("requestId")


def _current_tool() -> str | None:
    try:
        from ..max_client import _call_guard
    except Exception:
        return None
    return getattr(_call_guard, "tool", None)


def _append(entry: dict[str, Any]) -> None:
    global _lines_estimate
    path = log_path()
    line = json.dumps(entry, ensure_ascii=True) + "\n"
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if _lines_estimate is None:
                try:
                    with path.open("rb") as handle:
                        _lines_estimate = sum(1 for _ in handle)
                except FileNotFoundError:
                    _lines_estimate = 0
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
            _lines_estimate += 1
            if _lines_estimate > 2 * KEEP_LINES:
                _trim(path)
        except OSError:
            pass


def _trim(path: Path) -> None:
    global _lines_estimate
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    keep = lines[-KEEP_LINES:]
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text("".join(keep), encoding="utf-8", newline="")
    os.replace(tmp, path)
    _lines_estimate = len(keep)


def _stamp() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def record_sent(request: str, request_id: str | None, cmd_type: str | None, max_pid: int | None,
                transport: str) -> None:
    """Log a command just before it is written to the bridge."""
    if not enabled():
        return
    try:
        command, parsed_type, parsed_id = _command_text(request)
        rid = request_id or parsed_id
        entry = {"t": _stamp(), "event": "sent", "server_pid": os.getpid(), "max_pid": max_pid,
                 "tool": _current_tool(), "cmd_type": cmd_type or parsed_type, "request_id": rid,
                 "transport": transport, "chars": len(command), "command": preview(command)}
        if rid:
            with _lock:
                while len(_sent_ids) >= _SENT_IDS_KEPT:
                    _sent_ids.pop(next(iter(_sent_ids)))
                _sent_ids[rid] = time.perf_counter()
        _append(entry)
    except Exception:
        pass


def was_sent(request_id: str) -> bool:
    with _lock:
        return request_id in _sent_ids


def record_done(request_id: str, cmd_type: str | None, started: float, *, ok: bool,
                code: str | None = None) -> None:
    """Log the end of a sent command: its reply, or the error that ended the wait."""
    if not enabled():
        return
    try:
        with _lock:
            _sent_ids.pop(request_id, None)
        entry = {"t": _stamp(), "event": "done", "server_pid": os.getpid(), "request_id": request_id,
                 "cmd_type": cmd_type, "ok": ok, "elapsed_ms": round((time.perf_counter() - started) * 1000.0)}
        if code:
            entry["code"] = code
        _append(entry)
    except Exception:
        pass


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """The newest `limit` log entries (oldest first), for diagnostics and tests."""
    try:
        lines = log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
