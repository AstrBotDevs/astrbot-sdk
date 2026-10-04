"""P2 legacy compat tests: provider facade, config, and constructor shapes."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.errors import RemotePluginError
from astrbot_sdk.events import (
    UMO,
    MessageEvent,
    MessageRef,
    MessageType,
    Sender,
)
from astrbot_sdk.llm import AgentResponse, ChatResponse, ProviderInfo, ProviderKind
from astrbot_sdk.message_components import Plain
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.runtime import StdioPluginClient

LEGACY_PLUGIN = """
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.provider import LLMResponse, ProviderRequest


class LegacyProviderPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config

    @filter.command("config")
    async def show_config(self, event: AstrMessageEvent):
        yield event.plain_result(f"cfg={self.config['greeting']}")

    @filter.command("chat")
    async def chat(self, event: AstrMessageEvent):
        provider = self.context.get_using_provider(event.unified_msg_origin)
        resp = await provider.text_chat(prompt="hello", system_prompt="be nice")
        assert isinstance(resp, LLMResponse)
        yield event.plain_result(f"chat={resp.completion_text}")

    @filter.command("gen")
    async def gen(self, event: AstrMessageEvent):
        resp = await self.context.llm_generate(
            chat_provider_id="explicit-1",
            prompt="hi",
            contexts=[{"role": "user", "content": "earlier"}],
        )
        yield event.plain_result(f"gen={resp.completion_text}")

    @filter.command("agent")
    async def agent(self, event: AstrMessageEvent):
        resp = await self.context.tool_loop_agent(
            event=event,
            chat_provider_id="explicit-1",
            prompt="do stuff",
        )
        yield event.plain_result(f"agent={resp.completion_text}")

    @filter.command("globalconf")
    async def globalconf(self, event: AstrMessageEvent):
        self.context.get_config()
        yield event.plain_result("unreachable")
"""


def write_legacy_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_provider",
                "desc": "legacy provider compat test plugin",
                "author": "AstrBot",
                "version": "1.0.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(LEGACY_PLUGIN, encoding="utf-8")


def make_event(text: str = "chat") -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO("platform-1", MessageType.PRIVATE, "user-1"),
        platform_type="webchat",
        message_ref=MessageRef("message-1"),
        message=MessageChain(Plain(text)),
        sender=Sender("user-1", "Moon"),
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_legacy_provider_facade(tmp_path: Path) -> None:
    plugin_root = tmp_path / "legacy_provider"
    write_legacy_plugin(plugin_root)

    generate_calls: list[dict[str, Any]] = []
    agent_calls: list[dict[str, Any]] = []

    async def host_handler(
        grant: CapabilityGrant,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if grant.id == "llm.generate":
            if operation == "current_provider":
                return {
                    "provider": ProviderInfo(
                        id="current-1",
                        kind=ProviderKind.CHAT,
                        model="model-x",
                        provider_type="openai",
                    ),
                }
            if operation == "list_providers":
                return {
                    "providers": [
                        ProviderInfo(
                            id="explicit-1",
                            kind=ProviderKind.CHAT,
                            model="model-y",
                            provider_type="openai",
                        ),
                    ],
                }
            if operation == "generate":
                generate_calls.append(payload)
                return {"response": ChatResponse(content="llm says hi")}
        if grant.id == "llm.agent" and operation == "run":
            agent_calls.append(payload)
            return {"response": AgentResponse(text="agent done")}
        raise AssertionError(f"unexpected call: {grant.id} {operation}")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
        legacy=True,
    )
    try:
        await client.start(
            config={"greeting": "hi"},
            granted_capabilities=CapabilitySet.from_ids(
                "storage.kv",
                "assets.transfer",
                "message.send",
                "llm.generate",
                "llm.agent",
            ),
        )

        results = [r async for r in client.invoke("show_config", make_event())]
        assert [r.message.text for r in results if r is not None] == ["cfg=hi"]

        results = [r async for r in client.invoke("chat", make_event())]
        assert [r.message.text for r in results if r is not None] == [
            "chat=llm says hi",
        ]
        # Provider resolved from the ambient/session umo, system prompt passed.
        assert generate_calls[-1]["system_prompt"] == "be nice"
        assert generate_calls[-1]["provider_id"] == "current-1"

        results = [r async for r in client.invoke("gen", make_event())]
        assert [r.message.text for r in results if r is not None] == [
            "gen=llm says hi",
        ]
        messages = generate_calls[-1]["messages"]
        assert [m.role for m in messages] == ["user", "user"]
        assert messages[0].content == "earlier"
        assert messages[1].content == "hi"

        results = [r async for r in client.invoke("agent", make_event())]
        assert [r.message.text for r in results if r is not None] == [
            "agent=agent done",
        ]
        assert agent_calls[-1]["provider_id"] == "explicit-1"
        assert agent_calls[-1]["tools"] is None

        with pytest.raises(RemotePluginError, match="global AstrBot config"):
            _ = [r async for r in client.invoke("globalconf", make_event())]
    finally:
        await client.close()
