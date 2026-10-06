"""Run one 3dsmax-mcp tool from the current source, in a fresh process.

The MCP server keeps the Python code it started with. This script imports
maxmcp anew on every run and calls the tool through the same argument
validation and envelope wrapper as the server's progressive dispatch, over the
same named pipe, so a Python tool change is testable without restarting the
server. --pid or --pipe pins one 3ds Max, so parallel agents can each drive
their own instance. Keep mutating calls to one Max sequential.

    python scripts/mcpcall.py query_scene '{"action": "overview"}'
    python scripts/mcpcall.py --pid 2112 reload_native_core
    python scripts/mcpcall.py create_object @local/args.json
    python scripts/mcpcall.py create_object - < args.json
    python scripts/mcpcall.py --list [substring]
    python scripts/mcpcall.py --schema create_object

Prints the ToolEnvelope as JSON and a timing line on stderr. Exit status: 0
when the envelope is ok, 1 when it is not, 2 for usage errors.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_arguments(text: str | None) -> dict[str, Any]:
    """Tool arguments from JSON text, @file or - (stdin); none means {}."""
    if text is None:
        return {}
    if text == "-":
        text = sys.stdin.read()
    elif text.startswith("@"):
        text = Path(text[1:]).read_text(encoding="utf-8-sig")
    value = json.loads(text) if text.strip() else {}
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object")
    return value


class Tools:
    """The server's tools, imported from source in the progressive profile."""

    def __init__(self) -> None:
        # Progressive imports only the module that defines the called tool.
        os.environ["MCP_TOOL_PROFILE"] = "progressive"
        sys.path.insert(0, str(ROOT))
        from maxmcp import server
        from maxmcp.tool_discovery import META_TOOL_NAMES, ProgressiveToolCatalog

        logging.getLogger().setLevel(logging.WARNING)
        self.server = server
        self.catalog = ProgressiveToolCatalog(
            package="maxmcp",
            tools_dir=ROOT / "maxmcp" / "tools",
            hidden_mcp=server._progressive_mcp,
            allowed_modules=server.CORE_TOOL_MODULES + server.SPECIALTY_TOOL_MODULES,
        )
        # Instance routing tools are always registered on the public server.
        self.public = {
            tool.name: tool
            for tool in server.mcp._tool_manager.list_tools()
            if tool.name not in META_TOOL_NAMES
        }

    def names(self) -> dict[str, str]:
        names = dict(self.catalog.tool_modules)
        names.update({name: "routing" for name in self.public})
        return names

    def schema(self, name: str) -> dict[str, Any] | None:
        if name in self.public:
            tool = self.public[name]
            return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
        module = self.catalog.tool_modules.get(name)
        if module is None:
            return None
        toolset = next(spec.name for spec in self.catalog.toolsets if module in spec.modules)
        return next(t for t in self.catalog.describe_toolset(toolset)["tools"] if t["name"] == name)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.public:
            return self.catalog.call_tool(name, arguments)
        # The same validation as progressive dispatch; tool.fn is the
        # structured wrapper that returns a ToolEnvelope dict.
        from maxmcp.tool_response import ToolEnvelope

        tool = self.public[name]
        try:
            prepared = tool.fn_metadata.pre_parse_json(arguments)
            kwargs = tool.fn_metadata.arg_model.model_validate(prepared).model_dump_one_level()
            result = tool.fn(**kwargs)
            return ToolEnvelope.model_validate(result).model_dump(mode="json", exclude_none=True)
        except Exception as exc:
            return {"ok": False, "error": {"type": exc.__class__.__name__, "code": "BAD_PARAM",
                                           "message": f"Could not invoke {name!r}: {exc}",
                                           "retryable": False}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tool", nargs="?", help="tool name, or a name filter with --list")
    parser.add_argument("arguments", nargs="?", help="JSON object, @file or - for stdin (default {})")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--pid", type=int, help="3ds Max process to target")
    target.add_argument("--pipe", help="named pipe to target")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list tool names and their modules")
    mode.add_argument("--schema", action="store_true", help="print the tool's description and input schema")
    parser.add_argument("--compact", action="store_true", help="print the envelope on one line")
    args = parser.parse_args(argv)

    if args.pid is not None:
        if args.pid <= 0:
            parser.error("--pid must be a positive process ID")
        os.environ.pop("MCP_MAX_PIPE", None)
        os.environ["MCP_MAX_PID"] = str(args.pid)
    elif args.pipe:
        os.environ["MCP_MAX_PIPE"] = args.pipe
    if not args.list and not args.tool:
        parser.error("a tool name is required")
    try:
        arguments = load_arguments(args.arguments)
    except (OSError, ValueError) as exc:
        parser.error(f"bad arguments: {exc}")

    sys.stdout.reconfigure(encoding="utf-8")
    tools = Tools()
    if args.list:
        needle = (args.tool or "").lower()
        for name, module in sorted(tools.names().items()):
            if needle in name.lower():
                print(f"{name}  ({module})")
        return 0
    if args.schema:
        schema = tools.schema(args.tool)
        if schema is None:
            parser.error(f"unknown tool {args.tool!r}; see --list")
        print(json.dumps(schema, indent=2, ensure_ascii=False))
        return 0

    started = time.perf_counter()
    envelope = tools.call(args.tool, arguments)
    elapsed = (time.perf_counter() - started) * 1000
    print(json.dumps(envelope, ensure_ascii=False, indent=None if args.compact else 2, default=str))
    ok = envelope.get("ok") is True
    print(f"mcpcall: {args.tool} {'ok' if ok else 'failed'} in {elapsed:.0f} ms", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
