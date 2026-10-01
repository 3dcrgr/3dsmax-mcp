"""OS-level diagnostics for a hung 3ds Max (no bridge, no debugger).

Names are resolved lazily so `python -m maxmcp.diagnostics.stackdump` runs
without importing the module twice.
"""

__all__ = ["capture_stacks", "format_text", "identify_main_thread", "summarize"]


def __getattr__(name):
    if name in __all__:
        from . import stackdump
        return getattr(stackdump, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
