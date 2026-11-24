from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import pytest

from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.context import PluginContext, PluginInfo, RuntimeMode
from astrbot_sdk.conversations import (
    Conversation,
    ConversationRevision,
    Message,
)
from astrbot_sdk.errors import NotFound, RemoteHostError, map_remote_error
from astrbot_sdk.events import UMO, MessageEvent, MessageRef, MessageType, Sender
from astrbot_sdk.message_components import Plain
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.protocol.codec import decode_value, encode_value
from astrbot_sdk.runtime import StdioPluginClient
from astrbot_sdk.tools import Tool, ToolCallContext, ToolDefinition


def make_context(
    *,
    grants: CapabilitySet | None = None,
    invoker: Any = None,
) -> PluginContext:
    import logging

    return PluginContext(
        plugin=PluginInfo(
            id="test/plugin",
            name="plugin",
            version="1.0.0",
            runtime_mode=RuntimeMode.ISOLATED,
        ),
        config={},
        logger=logging.getLogger("test"),
        capabilities=grants or CapabilitySet(),
        _host_capability_invoker=invoker,
    )


def make_event() -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO("platform-1", MessageType.PRIVATE, "user-1"),
        platform_type="webchat",
        message_ref=MessageRef("message-1"),
        message=MessageChain(Plain("/remember milk")),
        sender=Sender("user-1", "Moon"),
        timestamp=datetime.now(UTC),
    )


def write_plugin(
    plugin_root: Path,
    source: str,
    *,
    required_capabilities: tuple[str, ...] = (),
) -> None:
    import yaml

    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "name": f"plugin_{plugin_root.name}",
                "desc": "conversation test plugin",
                "author": "AstrBot",
                "version": "1.0.0",
                "runtime": {
                    "api": "sdk",
                    "entrypoint": "main:TestPlugin",
                    "sdk_version": ">=0.1,<0.2",
                },
                "capabilities": {
                    "required": [
                        {"id": capability_id} for capability_id in required_capabilities
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(source, encoding="utf-8")


def make_conversation() -> Conversation:
    return Conversation(
        platform_id="platform-1",
        user_id="platform-1:FriendMessage:user-1",
        cid="conv-1",
        title="chat",
        history=json.dumps(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]
        ),
        created_at=1767225600,
        revision=ConversationRevision(3, "event-1"),
    )


def test_generic_dataclass_codec_roundtrip() -> None:
    conversation = make_conversation()
    assert decode_value(encode_value(conversation)) == conversation
    assert decode_value(encode_value([conversation])) == [conversation]


def test_codec_ignores_unknown_fields_for_forward_compat() -> None:
    raw = encode_value(make_conversation())
    raw["value"]["future_field"] = "ignored"
    assert decode_value(raw) == make_conversation()


def test_map_remote_error_translates_known_codes() -> None:
    mapped = map_remote_error(RemoteHostError("NOT_FOUND", "missing"))
    assert isinstance(mapped, NotFound)
    unknown = RemoteHostError("WEIRD", "x")
    assert map_remote_error(unknown) is unknown


@pytest.mark.asyncio
async def test_conversation_service_routes_operations() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    conversation = make_conversation()
    cid = conversation.cid
    umo = make_event().unified_msg_origin
    user_message = {"role": "user", "content": "again"}
    assistant_message = {"role": "assistant", "content": "okay"}

    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        calls.append((capability, operation, payload))
        if operation in {"get_curr_conversation_id", "new_conversation"}:
            return cid
        if operation == "get_conversation":
            return conversation
        if operation == "get_conversations":
            return [conversation]
        if operation in {"append_message", "append_plugin_event", "fork_conversation"}:
            return "new-id"
        return None

    ctx = make_context(
        grants=CapabilitySet.from_ids("conversation.read", "conversation.write"),
        invoker=invoker,
    )
    conversations = ctx.conversations
    assert await conversations.get_curr_conversation_id(umo) == cid
    assert await conversations.get_conversation(umo, cid) == conversation
    assert await conversations.get_conversations(umo, "platform-1") == [conversation]
    assert await conversations.new_conversation(umo, title="t") == cid
    assert await conversations.switch_conversation(umo, cid) is None
    assert (
        await conversations.update_conversation(
            umo, title="t", expected_revision=conversation.revision
        )
        is None
    )
    assert (
        await conversations.add_message_pair(cid, user_message, assistant_message)
        is None
    )
    assert await conversations.append_message("again", cid=cid) == "new-id"
    assert (
        await conversations.append_plugin_event("done", {"ok": True}, cid=cid)
        == "new-id"
    )
    assert await conversations.fork_conversation(umo, "new-id") == "new-id"
    assert await conversations.delete_conversation(umo) is None

    assert [call[1] for call in calls] == [
        "get_curr_conversation_id",
        "get_conversation",
        "get_conversations",
        "new_conversation",
        "switch_conversation",
        "update_conversation",
        "add_message_pair",
        "append_message",
        "append_plugin_event",
        "fork_conversation",
        "delete_conversation",
    ]
    assert calls[0][0] == "conversation.read"
    assert calls[3][0] == "conversation.write"
    assert calls[5][2]["conversation_id"] is None
    assert calls[5][2]["expected_revision"] == conversation.revision
    assert calls[7][2]["cid"] == cid


@pytest.mark.asyncio
async def test_stdio_end_to_end_conversations(tmp_path: Path) -> None:
    plugin_root = tmp_path / "conv_e2e"
    write_plugin(
        plugin_root,
        """
import json

from astrbot_sdk import MessageEvent, Plugin, on

class TestPlugin(Plugin):
    @on.command("remember")
    async def remember(self, event: MessageEvent, note: str):
        umo = event.unified_msg_origin
        cid = await self.ctx.conversations.new_conversation(umo, title="notes")
        await self.ctx.conversations.add_message_pair(
            cid,
            {"role": "user", "content": note},
            {"role": "assistant", "content": "got it"},
        )
        assert await self.ctx.conversations.get_curr_conversation_id(umo) == cid
        current = await self.ctx.conversations.get_conversation(umo, cid)
        yield event.reply(f"{current.title}:{len(json.loads(current.history))}")
""",
        required_capabilities=("conversation.read", "conversation.write"),
    )

    store: dict[str, Conversation] = {}
    umo = make_event().unified_msg_origin

    async def host_handler(
        grant: CapabilityGrant,
        operation: str,
        payload: dict[str, Any],
    ) -> Conversation | str | None:
        if grant.id == "conversation.write" and operation == "new_conversation":
            assert payload["unified_msg_origin"] == umo
            store["conv-9"] = Conversation(
                platform_id="platform-1",
                user_id=umo,
                cid="conv-9",
                title=payload["title"],
                history="[]",
            )
            return "conv-9"
        if grant.id == "conversation.write" and operation == "add_message_pair":
            messages = [payload["user_message"], payload["assistant_message"]]
            store[payload["cid"]].history = json.dumps(messages)
            return None
        if grant.id == "conversation.read" and operation == "get_curr_conversation_id":
            return "conv-9"
        if grant.id == "conversation.read" and operation == "get_conversation":
            return store.get(payload["conversation_id"])
        raise AssertionError(f"unexpected call: {grant.id} {operation}")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
    )
    try:
        await client.start(
            granted_capabilities=CapabilitySet.from_ids(
                "conversation.read",
                "conversation.write",
            ),
        )
        results = [
            result
            async for result in client.invoke("remember", make_event(), note="milk")
        ]
        assert [r.message.text for r in results if r is not None] == ["notes:2"]
        assert json.loads(store["conv-9"].history)[-1]["content"] == "got it"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tools_register_derives_definition_from_handler() -> None:
    registered: list[dict[str, Any]] = []

    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        if operation == "register":
            registered.append(payload)
            return {}
        return {}

    ctx = make_context(
        grants=CapabilitySet.from_ids("llm.tool.register"),
        invoker=invoker,
    )

    async def server_time(zone: Annotated[str, "Timezone name"]) -> str:
        """Get the server time."""
        return f"noon in {zone}"

    ref = await ctx.tools.register(server_time)
    assert ref.name == "server_time"
    assert ref.handler_id.startswith("dynamic:")

    definition = registered[0]["definition"]
    assert isinstance(definition, ToolDefinition)
    assert definition.description == "Get the server time."
    assert definition.params[0].name == "zone"
    assert definition.params[0].description == "Timezone name"
    assert ctx.dynamic_tools[ref.handler_id] is server_time

    await ctx.tools.unregister(ref)
    assert ref.handler_id not in ctx.dynamic_tools


@pytest.mark.asyncio
async def test_tools_register_tool_class_instance() -> None:
    registered: list[dict[str, Any]] = []

    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        if operation == "register":
            registered.append(payload)
            return {}
        return {}

    ctx = make_context(
        grants=CapabilitySet.from_ids("llm.tool.register"),
        invoker=invoker,
    )

    class WeatherTool(Tool):
        name = "get_weather"
        description = "Get weather for a city"

        def __init__(self) -> None:
            self.calls = 0

        async def call(
            self,
            context: ToolCallContext,
            city: Annotated[str, "城市"],
        ) -> str:
            self.calls += 1
            return f"{city}: sunny #{self.calls}"

    instance = WeatherTool()
    ref = await ctx.tools.register(instance)
    assert ref.name == "get_weather"

    definition = registered[0]["definition"]
    assert isinstance(definition, ToolDefinition)
    assert definition.description == "Get weather for a city"
    assert definition.params[0].name == "city"
    assert definition.params[0].description == "城市"

    # The bound call() is the routed handler; instance state is preserved.
    assert ctx.dynamic_tools[ref.handler_id] == instance.call


def test_content_parts_roundtrip() -> None:
    from astrbot_sdk.assets import AssetRef
    from astrbot_sdk.conversations import AudioPart, ImagePart, TextPart

    message = Message(
        role="user",
        content=(
            TextPart(text="look at this"),
            ImagePart(source=AssetRef(id="ast_1", media_type="image/png")),
            AudioPart(source="https://example.com/a.wav"),
        ),
    )
    decoded = decode_value(encode_value(message))
    assert decoded == message
    assert isinstance(decoded.content[1].source, AssetRef)


def test_normalize_input_accepts_str_and_messages() -> None:
    from astrbot_sdk.llm import _normalize_input

    items = _normalize_input(
        (
            "what is this",
            Message(role="user", content="second"),
            Message(role="assistant", content="third"),
        ),
    )
    assert items[0][0].content == "what is this"
    assert items[0][0].role == "user"
    assert items[1][0].content == "second"
    assert items[2][0].role == "assistant"

    from astrbot_sdk.errors import InvalidRequest
    from astrbot_sdk.message_components import Image

    with pytest.raises(InvalidRequest):
        _normalize_input((Image(source="https://x"),))
