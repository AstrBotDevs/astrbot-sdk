from __future__ import annotations

from typing import Annotated

from plugin_config import TemplateConfig

from astrbot_sdk import MessageEvent, Plugin, lifecycle, on


class TemplatePlugin(Plugin[TemplateConfig]):
    """Starter plugin demonstrating typed config, live reload, and tools."""

    config_model = TemplateConfig

    @lifecycle.validate_config
    async def validate(self, candidate: TemplateConfig) -> None:
        if "forbidden" in candidate.greeting.lower():
            raise ValueError("greeting must not contain the word 'forbidden'")

    @lifecycle.config_changed
    async def on_config_changed(self, event: lifecycle.ConfigChangedEvent) -> None:
        self.ctx.logger.info("Greeting updated to %s", event.current.greeting)

    @on.command("hello", description="Return the configured greeting")
    async def hello(self, event: MessageEvent) -> str:
        _ = event
        return self.ctx.current_config().greeting

    @on.tool("greet", description="Greet someone using the configured greeting")
    async def greet(
        self,
        name: Annotated[str, "Name of the person to greet"],
    ) -> dict[str, str]:
        return {"message": f"{self.ctx.current_config().greeting} ({name})"}
