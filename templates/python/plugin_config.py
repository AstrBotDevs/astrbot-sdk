from __future__ import annotations

from pydantic import Field

from astrbot_sdk import PluginConfig


class TemplateConfig(PluginConfig):
    """Typed plugin configuration exported to `config.schema.json`."""

    greeting: str = Field(
        default="Hello from AstrBot SDK!",
        min_length=1,
        description="Greeting returned by the /hello command and tool.",
    )
    api_secret: str = Field(
        default="",
        json_schema_extra={"writeOnly": True, "format": "password"},
        description="Optional sensitive secret (blocked from raw file GET endpoints).",
    )
