from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import PluginContext, PluginInfo

_INSPECT_CAPABILITY = "plugin.inspect"


class PluginRegistryService:
    """Read public metadata of installed plugins."""

    def __init__(self, ctx: PluginContext) -> None:
        """Initialize the service.

        Args:
            ctx: Owning plugin context used for Host invocation.
        """
        self._ctx = ctx

    async def get(self, plugin_id: str) -> PluginInfo | None:
        """Return public metadata of one plugin, or None when absent."""
        result = await self._ctx._invoke_capability(
            _INSPECT_CAPABILITY,
            "get",
            {"plugin_id": plugin_id},
        )
        return result.get("plugin")

    async def list(self) -> list[PluginInfo]:
        """Return public metadata of every installed plugin."""
        result = await self._ctx._invoke_capability(
            _INSPECT_CAPABILITY,
            "list",
            {},
        )
        return list(result.get("plugins", []))
