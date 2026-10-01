"""Search, download and import Chaos Cosmos assets through the installed service."""
from typing import Literal

from ..server import client, mcp
from ..helpers import cosmos as impl


@mcp.tool()
def cosmos_search(
    query: str = "",
    kind: Literal["all", "model", "material", "hdri"] = "all",
    downloaded: bool = False,
    limit: int = 10,
    offset: int = 0,
    renderer: Literal["current", "corona", "vray"] = "current",
) -> dict:
    """Find compatible Cosmos assets for the selected Max instance.
    Returns asset IDs, kinds, thumbnails, sizes and download states. Filter models,
    materials or HDRIs with kind. Renderer defaults to the Max's only Cosmos importer
    (else its current scene renderer); usually no Max round-trip is needed.
    Use cosmos_download to cache an asset or cosmos_import to download and import it.
    """
    return impl.search(client, query, kind, downloaded, limit, offset, renderer)


@mcp.tool()
def cosmos_download(
    asset_id: str,
    wait_seconds: int = 20,
    renderer: Literal["current", "corona", "vray"] = "current",
) -> dict:
    """Download a Cosmos model, material or HDRI without importing it.
    Existing downloads are reused. Waits up to 0..60 seconds and returns ready,
    queued or downloading; call again to continue waiting. Requires Cosmos sign-in.
    The asset ID comes from cosmos_search.
    """
    return impl.download(client, asset_id, wait_seconds, renderer)


@mcp.tool()
def cosmos_import(
    asset_id: str,
    wait_seconds: int = 20,
    renderer: Literal["current", "corona", "vray"] = "current",
    settle_seconds: int = impl.SETTLE_SECONDS_DEFAULT,
    restore_medit_renderer: bool = True,
    swap_medit_renderer: bool = False,
) -> dict:
    """Download if needed and import one Cosmos asset into the selected Max instance.
    Returns imported nodes, materials or maps and their file checks; selection is
    preserved. A pending download makes no scene edit. Repeating a completed model
    import creates another instance; if completion is unknown, inspect before retrying.
    First opens the Cosmos browser on Max's main thread (a hidden browser on another
    thread stalled/deadlocked Max); state browser_hung means nothing was imported: follow next.
    swap_medit_renderer (optional; forced on if the browser cannot be opened) uses
    Scanline for the Material Editor during the import, restored later if
    restore_medit_renderer. Waits up to settle_seconds (0-300, at least 8) for Max to
    settle; other calls get IMPORT_SETTLING meanwhile. If safe_to_edit is false,
    follow next and run pending_restore later. Never open or close the Material
    Editor right after.
    """
    return impl.import_asset(client, asset_id, wait_seconds, renderer, settle_seconds, restore_medit_renderer,
                             swap_medit_renderer)
