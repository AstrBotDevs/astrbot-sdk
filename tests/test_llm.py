from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.llm import ChatChunk, ChatResponse, ProviderInfo, ProviderKind
from astrbot_sdk.runtime import StdioPluginClient
from tests.test_conversations import make_context, make_event, write_plugin


@pytest.mark.asyncio
async def test_llm_service_routes_operations() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    info = ProviderInfo(
        id="p1",
        kind=ProviderKind.CHAT,
        model="gpt-x",
        provider_type="openai",
    )

    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        calls.append((operation, payload))
        if operation == "current_provider":
            return {"provider": info}
        if operation == "list_providers":
            return {"providers": [info]}
        if operation == "generate":
            return {"response": ChatResponse(content="hi there")}
        raise AssertionError(operation)

    ctx = make_context(
        grants=CapabilitySet.from_ids("llm.generate"),
        invoker=invoker,
    )
    assert await ctx.llm.current_provider(ProviderKind.CHAT) == info
    assert await ctx.llm.list_providers(ProviderKind.CHAT) == [info]
    response = await ctx.llm.generate("hello", system_prompt="be brief")
    assert response.content == "hi there"
    assert [op for op, _ in calls] == ["current_provider", "list_providers", "generate"]
    assert calls[2][1]["messages"][0].content == "hello"
    assert calls[2][1]["system_prompt"] == "be brief"


@pytest.mark.asyncio
async def test_stdio_end_to_end_llm_generate_and_stream(tmp_path: Path) -> None:
    plugin_root = tmp_path / "llm_e2e"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import MessageEvent, Plugin, on

class TestPlugin(Plugin):
    @on.command("ask")
    async def ask(self, event: MessageEvent, question: str):
        response = await self.ctx.llm.generate(question)
        yield event.reply(f"once:{response.content}")

    @on.command("flow")
    async def flow(self, event: MessageEvent, question: str):
        chunks = []
        async for chunk in self.ctx.llm.stream(question):
            chunks.append(chunk.delta)
        yield event.reply(f"streamed:{''.join(chunks)}")
""",
        required_capabilities=("llm.generate",),
    )

    stream_requests: list[dict[str, Any]] = []

    async def host_handler(grant: CapabilityGrant, operation: str, payload: Any):
        if operation == "generate":
            return {"response": ChatResponse(content="one-shot")}
        if operation == "generate_stream":
            stream_requests.append(payload)

            async def gen():
                for piece in ("hel", "lo", "!"):
                    yield ChatChunk(delta=piece)

            return gen()
        raise AssertionError(operation)

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
    )
    try:
        await client.start(
            granted_capabilities=CapabilitySet.from_ids("llm.generate"),
        )
        results = [r async for r in client.invoke("ask", make_event(), question="q1")]
        assert [r.message.text for r in results if r] == ["once:one-shot"]

        results = [r async for r in client.invoke("flow", make_event(), question="q2")]
        assert [r.message.text for r in results if r] == [
            "streamed:hel lo!".replace(" ", "")
        ]
        assert stream_requests[0]["messages"][0].content == "q2"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ambient_umo_defaults_to_event_session(tmp_path: Path) -> None:
    plugin_root = tmp_path / "llm_ambient"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import MessageEvent, Plugin, on

class TestPlugin(Plugin):
    @on.command("whoami")
    async def whoami(self, event: MessageEvent):
        response = await self.ctx.llm.generate("ping")
        yield event.reply(response.content)
""",
        required_capabilities=("llm.generate",),
    )

    seen: list[dict[str, Any]] = []

    async def host_handler(grant: CapabilityGrant, operation: str, payload: Any):
        if operation == "generate":
            seen.append(payload)
            return {"response": ChatResponse(content="pong")}
        raise AssertionError(operation)

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
    )
    try:
        await client.start(
            granted_capabilities=CapabilitySet.from_ids("llm.generate"),
        )
        results = [r async for r in client.invoke("whoami", make_event())]
        assert [r.message.text for r in results if r] == ["pong"]
        # The event's UMO was bound ambiently into the generate payload.
        umo = seen[0]["umo"]
        assert umo is not None
        assert umo.platform_id == "platform-1"
        assert umo.session_id == "user-1"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_run_agent_requires_generate_capability(tmp_path: Path) -> None:

    plugin_root = tmp_path / "agent_no_generate"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import MessageEvent, Plugin, on
from astrbot_sdk.llm import AgentRequest

class TestPlugin(Plugin):
    @on.command("go")
    async def go(self, event: MessageEvent):
        response = await self.ctx.llm.run_agent(AgentRequest(input="hi"))
        yield event.reply(response.text)
""",
        required_capabilities=("llm.agent",),
    )
    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
    )
    try:
        await client.start(
            granted_capabilities=CapabilitySet.from_ids("llm.agent"),
        )
        from astrbot_sdk.errors import RemotePluginError

        with pytest.raises(RemotePluginError) as exc_info:
            await anext(client.invoke("go", make_event()))
        assert exc_info.value.code == "CAPABILITY_DENIED"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_run_agent_attaches_ambient_event(tmp_path: Path) -> None:
    from astrbot_sdk.llm import AgentResponse

    plugin_root = tmp_path / "agent_ambient"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import MessageEvent, Plugin, on
from astrbot_sdk.llm import AgentRequest

class TestPlugin(Plugin):
    @on.command("go")
    async def go(self, event: MessageEvent):
        response = await self.ctx.llm.run_agent(AgentRequest(input="hi"))
        yield event.reply(response.text)
""",
        required_capabilities=("llm.agent", "llm.generate"),
    )

    seen: list[dict[str, Any]] = []

    async def host_handler(grant: CapabilityGrant, operation: str, payload: Any):
        if operation == "run":
            seen.append(payload)
            return {"response": AgentResponse(text="done")}
        raise AssertionError(operation)

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
    )
    try:
        await client.start(
            granted_capabilities=CapabilitySet.from_ids(
                "llm.agent",
                "llm.generate",
            ),
        )
        results = [r async for r in client.invoke("go", make_event())]
        assert [r.message.text for r in results if r] == ["done"]
        event = seen[0]["event"]
        assert event is not None
        assert event.umo.session_id == "user-1"
        assert seen[0]["tools"] is None
    finally:
        await client.close()
