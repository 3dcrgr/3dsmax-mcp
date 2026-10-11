"""Configure the original user's clients without a terminal or external tools."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import tomlkit

from maxmcp.opencode_config import opencode_config_path, resolve_opencode_config, update_opencode

APP = Path(__file__).resolve().parent
SERVER = "3dsmax-mcp"


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".3dsmax-mcp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configuration_plan(home: Path, roaming: Path, local: Path, clients: set[str],
                       codex_home: Path | None = None,
                       opencode_config: Path | None = None) -> dict[Path, bytes]:
    """Parse every selected config before writing anything; malformed files are errors."""
    command = str(APP / "runtime" / "python.exe")
    entry = {"command": command, "args": ["-m", "maxmcp.server"]}
    plan: dict[Path, bytes] = {}
    targets = []
    if "desktop" in clients:
        targets.append(roaming / "Claude" / "claude_desktop_config.json")
        targets.extend(p / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
                       for p in (local / "Packages").glob("Claude_*"))
    if "claude" in clients:
        targets.append(home / ".claude.json")
    if "cursor" in clients:
        targets.append(home / ".cursor" / "mcp.json")
    if "gemini" in clients:
        targets.append(home / ".gemini" / "settings.json")
    for path in targets:
        data = json.loads(path.read_text("utf-8-sig")) if path.exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            raise ValueError(f"Invalid MCP configuration: {path}")
        data.setdefault("mcpServers", {})[SERVER] = entry
        plan[path] = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if "codex" in clients:
        path = (codex_home or home / ".codex") / "config.toml"
        data = tomlkit.parse(path.read_text("utf-8-sig")) if path.exists() else tomlkit.document()
        if "mcp_servers" not in data:
            data["mcp_servers"] = tomlkit.table()
        data["mcp_servers"][SERVER] = entry
        plan[path] = tomlkit.dumps(data).encode("utf-8")
    if "opencode" in clients:
        path = opencode_config or opencode_config_path(home / ".config" / "opencode")
        text = path.read_bytes().decode("utf-8-sig") if path.exists() else "{}\n"
        plan[path] = update_opencode(text, {
            "type": "local", "command": [command, "-m", "maxmcp.server"], "enabled": True,
        })
    source = APP / "runtime" / "Lib" / "site-packages" / "maxmcp"
    config = local / SERVER / "mcp_config.ini"
    if not config.exists():
        plan[config] = (source / "mcp_config.ini").read_bytes()
    for skill in (source / "skills" / "3dsmax-mcp-dev").glob("*.md"):
        for directory in (home / ".agents" / "skills", home / ".claude" / "skills"):
            plan[directory / "3dsmax-mcp-dev" / skill.name] = skill.read_bytes()
    plan[local / SERVER / "mcp-client.json"] = (
        json.dumps({"mcpServers": {SERVER: entry}}, indent=2) + "\n").encode("utf-8")
    return plan


def apply_plan(plan: dict[Path, bytes], backup_dir: Path) -> None:
    before = {p: p.read_bytes() if p.exists() else None for p in plan}
    changed = [p for p in plan if before[p] != plan[p]]
    if not changed:
        return
    # Keep original settings for recovery. No backup or credential contents in logs.
    backup_dir.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="setup-", dir=backup_dir))
    for index, path in enumerate(changed):
        if before[path] is not None:
            (session / f"{index}.bak").write_bytes(before[path])
    (session / "paths.json").write_text(json.dumps([str(p) for p in changed], indent=2), "utf-8")
    written: list[Path] = []
    try:
        for path in changed:
            if (path.read_bytes() if path.exists() else None) != before[path]:
                raise RuntimeError(f"Settings changed during installation; close the client and retry: {path}")
            atomic_write(path, plan[path])
            written.append(path)
    except Exception:
        for path in reversed(written):
            if before[path] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, before[path])
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", default="desktop,claude,codex,cursor,gemini,opencode")
    parser.add_argument("--test-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    home = args.test_root or Path.home()
    roaming = home / "AppData/Roaming" if args.test_root else Path(os.environ["APPDATA"])
    local = home / "AppData/Local" if args.test_root else Path(os.environ["LOCALAPPDATA"])
    codex_home = None if args.test_root else Path(os.environ.get("CODEX_HOME", home / ".codex"))
    opencode_config = None if args.test_root else resolve_opencode_config(home)
    log = local / SERVER / "setup.log"
    try:
        clients = set(filter(None, args.clients.split(",")))
        if clients - {"desktop", "claude", "codex", "cursor", "gemini", "opencode"}:
            raise ValueError("Unknown client selection")
        plan = configuration_plan(home, roaming, local, clients, codex_home, opencode_config)
        apply_plan(plan, local / SERVER / "setup-backups")
        # Older bridges saved this development-only action in each Max profile.
        for macro in (local / "Autodesk/3dsMax").glob("*/*/usermacros/MCP-MCP_ToolSmokeTest.mcr"):
            macro.unlink()
        atomic_write(log, b"Setup completed. Restart 3ds Max and your AI client.\n")
        return 0
    except Exception:
        atomic_write(log, traceback.format_exc().encode("utf-8"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
