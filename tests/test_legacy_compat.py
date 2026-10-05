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
    SenderRole,
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
async def test_legacy_command_result_builders(tmp_path: Path) -> None:
    # CommandResult supports the full in-process builder surface: message
    # appends, error() is a deprecated alias, file_image sends a local file
    # through the asset upload pipeline.
    from astrbot_sdk.assets import AssetRef
    from astrbot_sdk.message_components import Image as SDKImage

    plugin_root = tmp_path / "legacy_builders"
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_builders",
                "desc": "builder test",
                "author": "AstrBot",
                "version": "1.0.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "pic.jpg").write_bytes(b"\xff\xd8fake-jpeg")
    (plugin_root / "main.py").write_text(
        f"""
from astrbot.api.all import AstrMessageEvent, CommandResult
from astrbot.api.event import filter
from astrbot.api.star import Context, Star


class BuilderPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("moe")
    async def moe(self, message: AstrMessageEvent):
        yield CommandResult().file_image(r"{plugin_root / "pic.jpg"}")

    @filter.command("combo")
    async def combo(self, event: AstrMessageEvent):
        result = CommandResult().message("a").error("b")
        yield event.plain_result(result.get_plain_text())
""",
        encoding="utf-8",
    )

    uploads: list[dict[str, Any]] = []

    async def host_handler(grant, operation, payload):
        cid = grant if isinstance(grant, str) else grant.id
        if cid == "assets.transfer" and operation == "upload":
            uploads.append(payload)
            return {"asset": AssetRef(id="ast_host_1", size=8)}
        raise AssertionError(f"unexpected call: {cid} {operation}")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
        legacy=True,
    )
    try:
        await client.start(
            granted_capabilities=CapabilitySet.from_ids(
                "storage.kv",
                "assets.transfer",
            ),
        )

        results = [r async for r in client.invoke("moe", make_event("moe"))]
        images = [
            segment
            for r in results
            if r is not None
            for segment in r.message
            if isinstance(segment, SDKImage)
        ]
        assert len(images) == 1
        assert getattr(images[0].source, "id", None) == "ast_host_1"
        assert len(uploads) == 1

        results = [r async for r in client.invoke("combo", make_event("combo"))]
        assert [r.message.text for r in results if r is not None] == ["a b"]
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


GROUP_PLUGIN = """
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("legacy_group", "Tester", "desc", "1.0.0")
class GroupPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command_group("kimi")
    def kimi(self):
        pass

    @kimi.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @kimi.command("login")
    async def kimi_login(self, event: AstrMessageEvent):
        yield event.plain_result("login ok")

    @kimi.command("public")
    async def kimi_public(self, event: AstrMessageEvent):
        yield event.plain_result("public ok")
"""


@pytest.mark.asyncio
async def test_legacy_command_group_custom_filter(tmp_path: Path) -> None:
    # Real-world pattern (astrbot_plugin_kimi_datasource_api): a custom
    # filter attached to the command group gates each sub-command.
    plugin_root = tmp_path / "legacy_group"
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_group",
                "desc": "legacy command group test plugin",
                "author": "AstrBot",
                "version": "1.0.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(GROUP_PLUGIN, encoding="utf-8")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=None,
        legacy=True,
    )
    try:
        handshake = await client.start()
        handler_ids = {h.id for h in handshake.handlers}
        assert handler_ids == {"kimi_login", "kimi_public"}

        # Non-admin senders are rejected by the group custom filter.
        results = [
            r async for r in client.invoke("kimi_login", make_event("kimi login"))
        ]
        assert [r for r in results if r is not None] == []

        # The same command succeeds for admins.
        admin_event = MessageEvent(
            id="event-2",
            umo=UMO("platform-1", MessageType.PRIVATE, "admin-1"),
            platform_type="webchat",
            message_ref=MessageRef("message-2"),
            message=MessageChain(Plain("kimi login")),
            sender=Sender("admin-1", "Root", SenderRole.ADMIN),
            timestamp=datetime.now(UTC),
        )
        results = [r async for r in client.invoke("kimi_login", admin_event)]
        assert [r.message.text for r in results if r is not None] == ["login ok"]

        # Sub-commands without the filter stay open to members.
        results = [
            r async for r in client.invoke("kimi_public", make_event("kimi public"))
        ]
        assert [r.message.text for r in results if r is not None] == ["public ok"]
    finally:
        await client.close()


def test_compat_config_attribute_access() -> None:
    # AstrBotConfig parity: top-level keys are readable as attributes,
    # missing keys yield None, and attribute writes go to the dict.
    from astrbot_sdk.compat.v1.star import CompatConfig

    config = CompatConfig({"push_time": "08:00"})
    assert config.push_time == "08:00"
    assert config.missing_key is None

    config.new_key = 42
    assert config["new_key"] == 42

    del config.new_key
    assert "new_key" not in config
    with pytest.raises(AttributeError):
        del config.new_key


def test_compat_media_components_accept_asset_ref() -> None:
    # Render/speech capabilities return AssetRef; legacy media constructors
    # must pass it through untouched instead of stringifying it.
    from astrbot_sdk.assets import AssetRef
    from astrbot_sdk.compat.v1.components import Image, to_sdk_segment
    from astrbot_sdk.message_components import Image as SDKImage

    asset = AssetRef(id="ast_test", filename="out.png")
    image = Image.fromFileSystem(asset)
    assert image.file is asset

    segment = to_sdk_segment(Image.fromURL(asset))
    assert isinstance(segment, SDKImage)
    assert segment.source is asset


@pytest.mark.asyncio
async def test_legacy_platform_event_shims() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from astrbot_sdk.compat.v1.api import install
    from astrbot_sdk.compat.v1.event import AstrMessageEvent
    from astrbot_sdk.compat.v1.platform_events import (
        AiocqhttpMessageEvent,
        WebChatMessageEvent,
        build_legacy_event,
    )

    install(host_version="test")

    # The fake source modules must import like the real core ones.
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent as ImportedAiocqhttpMessageEvent,
    )

    assert ImportedAiocqhttpMessageEvent is AiocqhttpMessageEvent

    # isinstance stays honest: the facade class follows the event platform.
    event = make_event()
    aiocqhttp_event = build_legacy_event(
        MessageEvent(
            id="event-2",
            umo=UMO("platform-2", MessageType.GROUP, "group-1"),
            platform_type="aiocqhttp",
            message_ref=MessageRef("message-2"),
            message=MessageChain(Plain("moe")),
            sender=Sender("user-1", "Moon"),
            timestamp=datetime.now(UTC),
        ),
        SimpleNamespace(),
    )
    assert isinstance(aiocqhttp_event, AiocqhttpMessageEvent)
    assert isinstance(aiocqhttp_event, AstrMessageEvent)

    webchat_event = build_legacy_event(event, SimpleNamespace())
    assert isinstance(webchat_event, WebChatMessageEvent)
    assert not isinstance(webchat_event, AiocqhttpMessageEvent)

    unknown_event = build_legacy_event(
        MessageEvent(
            id="event-3",
            umo=UMO("platform-3", MessageType.PRIVATE, "user-1"),
            platform_type="some_community_adapter",
            message_ref=MessageRef("message-3"),
            message=MessageChain(Plain("hi")),
            sender=Sender("user-1", "Moon"),
            timestamp=datetime.now(UTC),
        ),
        SimpleNamespace(),
    )
    assert type(unknown_event) is AstrMessageEvent

    # The raw bot escape hatch routes through the platform.raw capability.
    invoke = AsyncMock(return_value={"result": {"member_count": 1}})
    context = SimpleNamespace(_ctx=SimpleNamespace(_invoke_capability=invoke))
    bot_event = build_legacy_event(
        MessageEvent(
            id="event-4",
            umo=UMO("napcat-1", MessageType.GROUP, "group-1"),
            platform_type="aiocqhttp",
            message_ref=MessageRef("message-4"),
            message=MessageChain(Plain("moe")),
            sender=Sender("user-1", "Moon"),
            timestamp=datetime.now(UTC),
        ),
        context,
    )
    result = await bot_event.bot.get_group_member_list(group_id="group-1")
    assert result == {"result": {"member_count": 1}}
    invoke.assert_awaited_once_with(
        "platform.raw",
        "call_action",
        {
            "platform_id": "napcat-1",
            "platform": "aiocqhttp",
            "action": "get_group_member_list",
            "params": {"group_id": "group-1"},
        },
    )

    # bot.api.call_action is the same channel with an explicit action name.
    invoke.reset_mock()
    await bot_event.bot.api.call_action("set_group_ban", group_id="g", user_id="u")
    assert invoke.await_args.args[2]["action"] == "set_group_ban"
