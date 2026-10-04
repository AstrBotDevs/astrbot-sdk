from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

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
from astrbot_sdk.message_components import Plain
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.results import MessageResult, Propagation
from astrbot_sdk.runtime import StdioPluginClient


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
                "desc": "stdio test plugin",
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


def make_event() -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO("platform-1", MessageType.PRIVATE, "user-1"),
        platform_type="webchat",
        message_ref=MessageRef("message-1"),
        message=MessageChain(Plain("hello")),
        sender=Sender("user-1", "Moon"),
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_stdio_runner_preserves_yield_ack_boundary(tmp_path: Path) -> None:
    plugin_root = tmp_path / "stdio_yield"
    marker = tmp_path / "resumed.txt"
    write_plugin(
        plugin_root,
        """
from pathlib import Path

from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("hello")
    async def hello(self, event):
        yield event.reply("first")
        Path(self.config["marker"]).write_text("resumed", encoding="utf-8")
        yield event.reply("second")
""",
    )
    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
    )

    try:
        handshake = await client.start(config={"marker": str(marker)})
        assert handshake.name == "plugin_stdio_yield"
        assert {(handler.id, handler.kind.value) for handler in handshake.handlers} == {
            ("hello", "command")
        }

        stream = client.invoke("hello", make_event())
        first = await anext(stream)
        assert isinstance(first, MessageResult)
        assert first.message.text == "first"
        assert not marker.exists()

        second = await anext(stream)
        assert isinstance(second, MessageResult)
        assert second.message.text == "second"
        assert marker.read_text(encoding="utf-8") == "resumed"

        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_stop_closes_remote_generator_without_ack(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "stdio_stop"
    marker = tmp_path / "after-stop.txt"
    write_plugin(
        plugin_root,
        """
from pathlib import Path

from astrbot_sdk import Plugin, on
from astrbot_sdk.results import Propagation

class TestPlugin(Plugin):
    @on.command("stop")
    async def stop(self, event):
        yield event.reply("stop", propagation=Propagation.STOP)
        Path(self.config["marker"]).write_text("invalid", encoding="utf-8")
""",
    )
    client = StdioPluginClient(plugin_root)

    try:
        await client.start(config={"marker": str(marker)})
        stream = client.invoke("stop", make_event())
        result = await anext(stream)

        assert isinstance(result, MessageResult)
        assert result.propagation is Propagation.STOP
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not marker.exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_surfaces_remote_handler_error(tmp_path: Path) -> None:
    plugin_root = tmp_path / "stdio_error"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("fail")
    async def fail(self):
        raise ValueError("broken handler")
""",
    )
    client = StdioPluginClient(plugin_root)

    try:
        await client.start()
        with pytest.raises(RemotePluginError, match="broken handler") as exc_info:
            await anext(client.invoke("fail"))
        assert exc_info.value.code == "PLUGIN_RUNTIME_ERROR"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_runner_can_call_host_during_handler_invocation(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "stdio_reverse_rpc"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("ask")
    async def ask(self):
        result = await self.ctx._invoke_capability(
            "llm.generate",
            "generate",
            {"prompt": "hello"},
        )
        yield result["text"]
""",
        required_capabilities=("llm.generate",),
    )
    calls: list[tuple[CapabilityGrant, str, dict[str, object]]] = []

    async def invoke_host(
        grant: CapabilityGrant,
        operation: str,
        payload: dict[str, object],
    ) -> dict[str, str]:
        calls.append((grant, operation, payload))
        return {"text": "from Host"}

    grants = CapabilitySet(
        [CapabilityGrant("llm.generate", {"providers": ["default"]})]
    )
    client = StdioPluginClient(plugin_root, capability_handler=invoke_host)

    try:
        await client.start(granted_capabilities=grants)
        stream = client.invoke("ask")
        result = await anext(stream)

        assert isinstance(result, MessageResult)
        assert result.message.text == "from Host"
        assert calls == [
            (
                grants["llm.generate"],
                "generate",
                {"prompt": "hello"},
            )
        ]
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_host_rejects_runner_capability_not_declared_by_plugin(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "stdio_capability_denied"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("escape")
    async def escape(self):
        await self.ctx._host_capability_invoker(
            "llm.agent",
            "run_agent",
            {},
        )
""",
        required_capabilities=("llm.generate",),
    )
    called = False

    async def invoke_host(
        grant: CapabilityGrant,
        operation: str,
        payload: dict[str, object],
    ) -> None:
        nonlocal called
        called = True

    client = StdioPluginClient(plugin_root, capability_handler=invoke_host)
    grants = CapabilitySet.from_ids("llm.generate", "llm.agent")

    try:
        handshake = await client.start(granted_capabilities=grants)
        assert handshake.capabilities == (
            "llm.generate",
            "assets.transfer",
            "storage.kv",
        )
        with pytest.raises(RemotePluginError) as exc_info:
            await anext(client.invoke("escape"))
        assert exc_info.value.code == "CAPABILITY_DENIED"
        assert not called
    finally:
        await client.close()
