from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.context import PluginContext, PluginInfo, RuntimeMode
from astrbot_sdk.errors import CapabilityDenied, RemotePluginError
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
from astrbot_sdk.runtime.loader import load_plugin


def make_context(
    *,
    grants: CapabilitySet | None = None,
    invoker: Any = None,
    data_dir: Path | None = None,
) -> PluginContext:
    return PluginContext(
        plugin=PluginInfo(
            id="test/plugin",
            name="plugin",
            version="1.0.0",
            runtime_mode=RuntimeMode.ISOLATED,
        ),
        config={},
        logger=__import__("logging").getLogger("test"),
        capabilities=grants or CapabilitySet(),
        data_dir=data_dir or Path("."),
        _host_capability_invoker=invoker,
    )


@pytest.mark.asyncio
async def test_storage_routes_through_host_capability() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        calls.append((capability, operation, payload))
        if operation == "get":
            return {"value": 41}
        return {}

    ctx = make_context(
        grants=CapabilitySet.from_ids("storage.kv"),
        invoker=invoker,
    )
    await ctx.storage.set("answer", 41)
    assert await ctx.storage.get("answer") == 41
    await ctx.storage.delete("answer")
    assert calls == [
        ("storage.kv", "set", {"key": "answer", "value": 41}),
        ("storage.kv", "get", {"key": "answer", "default": None}),
        ("storage.kv", "delete", {"key": "answer"}),
    ]


@pytest.mark.asyncio
async def test_storage_is_denied_without_default_grant() -> None:
    ctx = make_context(grants=CapabilitySet())
    with pytest.raises(CapabilityDenied):
        await ctx.storage.get("key")


@pytest.mark.asyncio
async def test_messages_send_returns_receipt() -> None:
    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        assert capability == "message.send"
        assert operation == "send"
        umo = payload["umo"]
        assert isinstance(umo, UMO)
        assert umo.platform_id == "platform-1"
        assert isinstance(payload["message"], MessageChain)
        assert payload["message"].text == "hi"
        return {"message_id": "m-42"}

    ctx = make_context(
        grants=CapabilitySet.from_ids("message.send"),
        invoker=invoker,
    )
    receipt = await ctx.messages.send(
        UMO("platform-1", MessageType.GROUP, "group-1"),
        "hi",
    )
    assert receipt.message_id == "m-42"


@pytest.mark.asyncio
async def test_messages_send_denied_without_grant() -> None:
    ctx = make_context(grants=CapabilitySet.from_ids("storage.kv"))
    with pytest.raises(CapabilityDenied):
        await ctx.messages.send(UMO("p", MessageType.PRIVATE, "s"), "hi")


def write_plugin(
    plugin_root: Path,
    source: str,
    *,
    required_capabilities: tuple[str, ...] = (),
) -> None:
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "name": f"plugin_{plugin_root.name}",
                "desc": "service test plugin",
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


def test_loader_grants_default_capabilities_and_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "defaults"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("hello")
    async def hello(self):
        return "hi"
""",
    )
    monkeypatch.setenv("ASTRBOT_SDK_DATA_DIR", str(tmp_path / "sdk-data"))
    loaded = load_plugin(plugin_root)
    assert loaded.instance.ctx.capabilities.has("storage.kv")
    expected = tmp_path / "sdk-data" / loaded.metadata.plugin_id.replace("/", "_")
    assert loaded.instance.ctx.storage.data_dir == expected
    assert expected.is_dir()


def make_event() -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO("platform-1", MessageType.PRIVATE, "user-1"),
        platform_type="webchat",
        message_ref=MessageRef("message-1"),
        message=MessageChain(Plain("/greet 2")),
        sender=Sender("user-1", "Moon"),
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_stdio_end_to_end_services_and_typed_params(tmp_path: Path) -> None:
    plugin_root = tmp_path / "services_e2e"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import MessageEvent, Plugin, on

class TestPlugin(Plugin):
    @on.command("greet")
    async def greet(self, event: MessageEvent, times: int, prefix: str = "hi"):
        count = await self.ctx.storage.get("count", 0) + 1
        await self.ctx.storage.set("count", count)
        for index in range(times):
            yield event.reply(f"{prefix} {event.sender.name} #{count}.{index}")
        await self.ctx.messages.send(event.umo, "done")
""",
        required_capabilities=("message.send",),
    )

    kv: dict[str, Any] = {}
    sent: list[tuple[UMO, MessageChain]] = []

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
    )
    try:
        handshake = await client.start(
            granted_capabilities=CapabilitySet.from_ids("message.send"),
        )
        greet = next(h for h in handshake.handlers if h.id == "greet")
        assert greet.details["params"] == [
            {"name": "times", "type": "int", "required": True, "default": None},
            {"name": "prefix", "type": "str", "required": False, "default": "hi"},
        ]

        results = [
            result async for result in client.invoke("greet", make_event(), times=2)
        ]
        assert [r.message.text for r in results if r is not None] == [
            "hi Moon #1.0",
            "hi Moon #1.1",
        ]
        assert kv == {"count": 1}
        assert len(sent) == 1
        assert sent[0][0].session_id == "user-1"
        assert sent[0][1].text == "done"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_rejects_unsupported_command_param_type(tmp_path: Path) -> None:
    plugin_root = tmp_path / "bad_params"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("bad")
    async def bad(self, items: list):
        return "bad"
""",
    )
    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
    )
    with pytest.raises(RemotePluginError):
        await client.start()
    await client.close()
