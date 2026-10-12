"""Bound Connecter TCP connection setup without changing send/response timeouts.

Opt-in reversible workstation fix. The original file and exact hashes are kept
outside the repo. Vendor updates may replace the integration and this patch.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil

OLD = "        s.connect(('127.0.0.1', jsonTcpPort))"
NEW = """        # MCP startup optimization: only connection setup has a short deadline.
        previous_timeout = s.gettimeout()
        s.settimeout(0.25)
        try:
            s.connect(('127.0.0.1', jsonTcpPort))
        finally:
            s.settimeout(previous_timeout)"""


def patched(source: str) -> str:
    tree = ast.parse(source)
    funcs = [f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "sendMessage_TCP"]
    if len(funcs) != 1:
        raise ValueError("Expected one sendMessage_TCP function")
    f = funcs[0]
    lines = source.splitlines(keepends=True)
    body = "".join(lines[f.lineno - 1:f.end_lineno])
    if "MCP startup optimization" in body:
        raise ValueError("Already patched")
    if body.count(OLD) != 1:
        raise ValueError("Vendor function changed; patch refused")
    result = "".join(lines[:f.lineno - 1]) + body.replace(OLD, NEW) + "".join(lines[f.end_lineno:])
    ast.parse(result)
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument("backup_dir", type=Path)
    args = p.parse_args()
    original = args.source.read_bytes()
    text = original.decode("utf-8-sig")
    replacement = patched(text).encode("utf-8")
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    backup = args.backup_dir / args.source.name
    if backup.exists():
        raise SystemExit("Backup already exists; refusing to overwrite it")
    shutil.copy2(args.source, backup)
    # Do not overwrite a vendor update made between validation and the write.
    if args.source.read_bytes() != original:
        raise SystemExit("Concurrent source change; patch refused")
    args.source.write_bytes(replacement)
    if args.source.read_bytes() != replacement:
        raise SystemExit("Patch readback differs")
    result = {"source": str(args.source), "backup": str(backup), "connect_timeout_s": 0.25,
              "old_sha256": hashlib.sha256(original).hexdigest(),
              "new_sha256": hashlib.sha256(replacement).hexdigest()}
    (args.backup_dir / "change.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    escaped_backup, escaped_source = str(backup).replace("'", "''"), str(args.source).replace("'", "''")
    (args.backup_dir / "rollback.ps1").write_text(
        f"Copy-Item -LiteralPath '{escaped_backup}' -Destination '{escaped_source}' -Force\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
