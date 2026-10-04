"""Legacy compat tests for hook decorators and llm_tool."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrbot_sdk.capabilities import CapabilitySet
from astrbot_sdk.events import (
    UMO,
    MessageEvent,
    MessageRef,
    MessageType,
    Sender,
)
from astrbot_sdk.message_components import Plain
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.runtime import StdioPluginClient
from astrbot_sdk.tools import ToolCallContext

LEGACY_PLUGIN = """
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest


class LegacyHooksPlugin(Star):
    @filter.on_llm_request()
    async def mutate_request(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest,
    ):
        request.system_prompt = "patched system"
        request.contexts.append({"role": "user", "content": "injected"})

    @filter.on_llm_response()
    async def mutate_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ):
        response.completion_text = response.completion_text + "!"

    @filter.on_decorating_result()
    async def sign_result(self, event: AstrMessageEvent):
        event.get_result().chain.append(Plain(" --signed"))

    @filter.llm_tool(name="get_weather")
    async def get_weather(self, event: AstrMessageEvent, location: str):
        '''查询天气。

        Args:
            location(string): 地点
        '''
        return f"sunny at {location}"

    @filter.on_astrbot_loaded()
    async def boot(self):
        await self.context.put_kv_data("booted", "yes")

    @filter.command("booted")
    async def booted(self, event: AstrMessageEvent):
        value = await self.context.get_kv_data("booted", "no")
        yield event.plain_result(f"booted={value}")
"""


def write_legacy_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_hooks",
                "desc": "legacy hooks compat test plugin",
                "author": "AstrBot",
                "version": "1.0.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(LEGACY_PLUGIN, encoding="utf-8")


def make_event(text: str = "hi") -> MessageEvent:
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
async def test_legacy_hooks_and_tools(tmp_path: Path) -> None:
    plugin_root = tmp_path / "legacy_hooks"
    write_legacy_plugin(plugin_root)

    kv: dict[str, Any] = {}

    async def host_handler(grant, operation, payload):
        if grant.id == "storage.kv":
            if operation == "get":
                return {"value": kv.get(payload["key"], payload.get("default"))}
            if operation == "set":
                kv[payload["key"]] = payload["value"]
                return {}
        raise AssertionError(f"unexpected call: {grant.id} {operation}")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
        legacy=True,
    )
    try:
        handshake = await client.start(
            granted_capabilities=CapabilitySet.from_ids(
                "storage.kv",
                "assets.transfer",
                "message.send",
            ),
        )
        handler_kinds = {h.id: h.kind for h in handshake.handlers}
        assert handler_kinds["mutate_request"].value == "hook.llm_request"
        assert handler_kinds["mutate_response"].value == "hook.llm_response"
        assert handler_kinds["sign_result"].value == "hook.message_result"
        assert handler_kinds["get_weather"].value == "tool"
        # Lifecycle handlers run inside the Runner but still register.
        assert "boot" in handler_kinds
        # on_astrbot_loaded fired during start().
        assert kv == {"booted": "yes"}

        # llm_request hook: writes forwarded as tracked ops.
        tool_handler = next(h for h in handshake.handlers if h.id == "get_weather")
        assert tool_handler.details["name"] == "get_weather"
        assert tool_handler.details["params"] == [
            {
                "name": "location",
                "type": "string",
                "description": "地点",
                "required": True,
            },
        ]

        event = make_event()
        result = await client.invoke_hook(
            "mutate_request",
            event,
            "llm_request",
            {
                "prompt": "hi",
                "system_prompt": "orig",
                "contexts": [{"role": "user", "content": "earlier"}],
                "image_urls": [],
                "audio_urls": [],
                "model": None,
            },
        )
        ops = result["writes"]
        assert {
            "field": "system_prompt",
            "op": "set",
            "value": "patched system",
        } in ops
        append_ops = [op for op in ops if op["field"] == "contexts"]
        assert append_ops[-1]["op"] == "append"
        assert append_ops[-1]["value"] == {"role": "user", "content": "injected"}

        # llm_response hook: completion_text writes map onto content.
        result = await client.invoke_hook(
            "mutate_response",
            event,
            "llm_response",
            {"content": "answer", "reasoning_content": None},
        )
        assert {
            "field": "content",
            "op": "set",
            "value": "answer!",
        } in result["writes"]

        # decorating result: appended compat component becomes an SDK segment.
        result = await client.invoke_hook(
            "sign_result",
            event,
            "message_result",
            {"chain": [{"type": "plain", "text": "hello"}]},
        )
        chain_ops = [op for op in result["writes"] if op["field"] == "chain"]
        assert chain_ops, result["writes"]
        appended = chain_ops[-1]["value"]
        assert appended[-1]["text"] == " --signed"

        # llm_tool invocation through the new tool contract.
        call = ToolCallContext(id="call-1", umo=event.umo, event=event)
        tool_result = await client.invoke_tool(
            "get_weather",
            call,
            {"location": "Shanghai"},
        )
        assert tool_result == "sunny at Shanghai"

        results = [r async for r in client.invoke("booted", make_event("booted"))]
        assert [r.message.text for r in results if r is not None] == [
            "booted=yes",
        ]
    finally:
        await client.close()
