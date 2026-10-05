from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
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

LEGACY_PLUGIN = """
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.message_components import MessageChain, Plain


class LegacyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.booted = False

    async def initialize(self):
        self.booted = True

    @filter.command("hello")
    async def hello(self, event: AstrMessageEvent):
        name = event.get_sender_name()
        await self.context.put_kv_data("last", name)
        yield event.plain_result(f"hello {name}")
        yield event.chain_result(MessageChain(chain=[Plain("second")]))

    @filter.command("count")
    async def count(self, event: AstrMessageEvent):
        value = await self.context.get_kv_data("last", "none")
        return event.plain_result(f"last={value}")

    @filter.command("proactive")
    async def proactive(self, event: AstrMessageEvent):
        await self.context.send_message(
            event.unified_msg_origin,
            MessageChain(chain=[Plain("proactive hello")]),
        )
        yield event.plain_result("sent")

    @filter.regex(r"^ping$")
    async def on_ping(self, event: AstrMessageEvent):
        yield event.plain_result("pong")

    async def terminate(self):
        pass
"""


def write_legacy_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_hello",
                "desc": "legacy compat test plugin",
                "author": "AstrBot",
                "version": "1.0.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(LEGACY_PLUGIN, encoding="utf-8")


def make_event(text: str = "hello") -> MessageEvent:
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
async def test_legacy_register_decorator_does_not_override_yaml_metadata(
    tmp_path: Path,
) -> None:
    # Some plugins pass empty strings to the deprecated register decorator
    # (e.g. an empty version). The in-process loader prioritizes
    # metadata.yaml, so the handshake must carry the yaml metadata.
    plugin_root = tmp_path / "legacy_empty_register"
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_empty_register",
                "desc": "yaml desc",
                "author": "YamlAuthor",
                "version": "v1.1.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(
        """
from astrbot.api.star import Context, Star, register


@register("legacy_empty_register", "DecoratorAuthor", "", "", "")
class EmptyRegisterPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
""",
        encoding="utf-8",
    )

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=None,
        legacy=True,
    )
    try:
        handshake = await client.start(
            granted_capabilities=CapabilitySet.from_ids("storage.kv"),
        )
        assert handshake.name == "legacy_empty_register"
        assert handshake.version == "v1.1.0"
        assert handshake.plugin_id == "yamlauthor/legacy_empty_register"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_command_event_param_name_is_positional(tmp_path: Path) -> None:
    # The legacy CommandFilter treats the first parameter after self as the
    # event whatever its name; plugins may call it message, ctx, etc.
    plugin_root = tmp_path / "legacy_event_name"
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_event_name",
                "desc": "event param name test",
                "author": "AstrBot",
                "version": "1.0.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(
        """
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class OddEventNamePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("moe")
    async def get_moe(self, message: AstrMessageEvent):
        yield message.plain_result("moe!")

    @filter.command("greet")
    async def greet(self, ctx: AstrMessageEvent, name: str = "anon"):
        yield ctx.plain_result(f"hi {name}")
""",
        encoding="utf-8",
    )

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=None,
        legacy=True,
    )
    try:
        await client.start(granted_capabilities=CapabilitySet.from_ids("storage.kv"))

        results = [r async for r in client.invoke("get_moe", make_event("moe"))]
        assert [r.message.text for r in results if r is not None] == ["moe!"]

        results = [r async for r in client.invoke("greet", make_event("greet"))]
        assert [r.message.text for r in results if r is not None] == ["hi anon"]

        results = [r async for r in client.invoke("greet", make_event("greet Moon"))]
        assert [r.message.text for r in results if r is not None] == ["hi Moon"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_plugin_runs_unmodified(tmp_path: Path) -> None:
    plugin_root = tmp_path / "legacy_hello"
    write_legacy_plugin(plugin_root)

    kv: dict[str, Any] = {}
    sent: list[tuple[Any, Any]] = []

    async def host_handler(
        grant: CapabilityGrant,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if grant.id == "storage.kv":
            if operation == "get":
                return {"value": kv.get(payload["key"], payload.get("default"))}
            if operation == "set":
                kv[payload["key"]] = payload["value"]
                return {}
            if operation == "delete":
                kv.pop(payload["key"], None)
                return {}
        if grant.id == "message.send" and operation == "send":
            sent.append((payload["umo"], payload["message"]))
            return {"message_id": "m-1"}
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
        assert handshake.schema_version == 1
        handler_ids = {h.id for h in handshake.handlers}
        assert handler_ids == {"hello", "count", "proactive", "on_ping"}

        results = [r async for r in client.invoke("hello", make_event())]
        texts = [r.message.text for r in results if r is not None]
        assert texts == ["hello Moon", "second"]
        assert kv == {"last": "Moon"}

        results = [r async for r in client.invoke("count", make_event("count"))]
        assert [r.message.text for r in results if r is not None] == [
            "last=Moon",
        ]

        results = [r async for r in client.invoke("proactive", make_event("proactive"))]
        assert [r.message.text for r in results if r is not None] == ["sent"]
        assert len(sent) == 1
        assert sent[0][0].session_id == "user-1"
        assert sent[0][1].text == "proactive hello"

        results = [r async for r in client.invoke("on_ping", make_event("ping"))]
        assert [r.message.text for r in results if r is not None] == ["pong"]
    finally:
        await client.close()
