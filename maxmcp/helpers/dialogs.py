"""Dialogs holding 3ds Max's main thread, read without touching it."""

from __future__ import annotations

from typing import Any

from ..max_client import real_dialogs


def open_main_dialogs(client) -> list[dict[str, Any]]:
    """Dialogs holding Max's main thread, from the bridge's dialog monitor (window-manager
    state only, over the control channel; nothing reaches Max's main thread). [] when the
    bridge has no monitor, or reports that Max's main thread stopped pumping (busy or hung
    whatever dialog is open; the OS-level checks cover that). Cosmos browser, Material
    Editor and viewport windows never count."""
    try:
        status = client._dialog_control("status")
        if status.get("main_thread_pumping") is False:
            return []
        return real_dialogs([d for d in status.get("dialogs", []) if isinstance(d, dict) and d.get("main_thread")])
    except Exception:
        return []
