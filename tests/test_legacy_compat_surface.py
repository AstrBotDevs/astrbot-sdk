"""Legacy compat tests for api.all, register, StarTools, tools, conversations."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrbot_sdk.capabilities import CapabilitySet
from astrbot_sdk.conversations import Conversation
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
import json
from dataclasses import dataclass, field

from astrbot.api.all import (
    AstrMessageEvent,
    Context,
    MessageChain,
    Plain,
    Star,
    command,
    register,
)
from astrbot.api.star import StarTools
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.star.filter.command import GreedyStr


@dataclass
class WeatherTool(FunctionTool):
    name: str = "get_weather_v2"
    description: str = "query weather"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "city name"},
            },
            "required": ["city"],
        },
    )

    async def call(self, context, **kwargs):
        return f"rainy at {kwargs['city']}"


@register("legacy_surface", "AstrBot", "surface test", "2.0.0")
class LegacySurfacePlugin(Star):
    async def initialize(self):
        self.context.register_llm_tool(WeatherTool)

    @command("args")
    async def args(self, event: AstrMessageEvent, first: str, n: int = 1):
        yield event.plain_result(f"args={first}:{n}:{type(n).__name__}")

    @command("greedy")
    async def greedy(self, event: AstrMessageEvent, rest: GreedyStr):
        yield event.plain_result(f"greedy={rest}")

    @command("datadir")
    async def datadir(self, event: AstrMessageEvent):
        path = StarTools.get_data_dir()
        yield event.plain_result(f"dir={path.name}")

    @command("conv")
    async def conv(self, event: AstrMessageEvent):
        manager = self.context.conversation_manager
        cid = await manager.new_conversation(event.unified_msg_origin)
        await manager.add_message_pair(
            cid,
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        )
        conv = await manager.get_conversation(event.unified_msg_origin, cid)
        history = json.loads(conv.history)
        yield event.plain_result(f"conv={len(history)}:{history[0]['content']}")

    @command("curconv")
    async def curconv(self, event: AstrMessageEvent):
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
        yield event.plain_result(f"cur={cid is not None}")
"""


def write_legacy_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "wrong_name",
                "desc": "yaml desc wins over @register",
                "author": "AstrBot",
                "version": "0.0.1",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(LEGACY_PLUGIN, encoding="utf-8")


def make_event(text: str = "args") -> MessageEvent:
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
async def test_legacy_surface(tmp_path: Path) -> None:
    plugin_root = tmp_path / "legacy_surface"
    write_legacy_plugin(plugin_root)

    conversations: dict[str, dict[str, Any]] = {}
    current: dict[str, str] = {}
    registered_tools: list[dict[str, Any]] = []

    def conv_key(umo: UMO, cid: str) -> str:
        return f"{umo.platform_id}:{cid}"

    async def host_handler(grant, operation, payload):
        if grant.id == "conversation.write":
            umo = payload["umo"]
            if operation == "create":
                cid = f"cid-{len(conversations) + 1}"
                conversations[conv_key(umo, cid)] = {
                    "messages": list(payload.get("messages", [])),
                    "title": payload.get("title"),
                }
                current[umo.session_id] = cid
                return {
                    "conversation": Conversation(
                        id=cid,
                        title=None,
                        persona_id=None,
                        messages=(),
                        created_at=None,
                        updated_at=None,
                    ),
                }
            if operation == "set_current":
                current[umo.session_id] = payload["conversation_id"]
                return {}
            if operation == "update":
                record = conversations[conv_key(umo, payload["conversation_id"])]
                patch = payload["patch"]
                if patch.messages is not None:
                    record["messages"] = list(patch.messages)
                return {
                    "conversation": Conversation(
                        id=payload["conversation_id"],
                        title=record["title"],
                        persona_id=None,
                        messages=tuple(record["messages"]),
                        created_at=None,
                        updated_at=None,
                    ),
                }
            if operation == "append":
                record = conversations[conv_key(umo, payload["conversation_id"])]
                record["messages"].extend(payload["messages"])
                return {}
        if grant.id == "conversation.read":
            umo = payload["umo"]
            if operation == "current":
                cid = current.get(umo.session_id)
                record = (
                    conversations.get(conv_key(umo, cid)) if cid is not None else None
                )
                return {
                    "conversation": (
                        Conversation(
                            id=cid,
                            title=record["title"],
                            persona_id=None,
                            messages=tuple(record["messages"]),
                            created_at=None,
                            updated_at=None,
                        )
                        if record is not None
                        else None
                    ),
                }
            if operation == "get":
                cid = payload["conversation_id"]
                record = conversations.get(conv_key(umo, cid))
                return {
                    "conversation": (
                        Conversation(
                            id=cid,
                            title=record["title"],
                            persona_id=None,
                            messages=tuple(record["messages"]),
                            created_at=None,
                            updated_at=None,
                        )
                        if record is not None
                        else None
                    ),
                }
        if grant.id == "llm.tool.register" and operation == "register":
            registered_tools.append(payload)
            return {}
        raise AssertionError(f"unexpected call: {grant.id} {operation}")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
        legacy=True,
        env={"ASTRBOT_DATA_PATH": str(tmp_path)},
    )
    try:
        handshake = await client.start(
            granted_capabilities=CapabilitySet.from_ids(
                "storage.kv",
                "assets.transfer",
                "message.send",
                "conversation.read",
                "conversation.write",
                "llm.tool.register",
            ),
        )
        # metadata.yaml takes precedence over the @register decorator,
        # matching the in-process loader.
        assert handshake.name == "wrong_name"
        assert handshake.version == "0.0.1"

        # FunctionTool registered during initialize() with JSON-schema params.
        assert len(registered_tools) == 1
        definition = registered_tools[0]["definition"]
        assert definition.name == "get_weather_v2"
        assert definition.params[0].name == "city"
        handler_id = registered_tools[0]["handler_id"]
        from astrbot_sdk.tools import ToolCallContext

        result = await client.invoke_tool(
            handler_id,
            ToolCallContext(id="c-1", umo=make_event().umo),
            {"city": "Tokyo"},
        )
        assert result == "rainy at Tokyo"

        # Legacy positional args with defaults and type conversion.
        results = [r async for r in client.invoke("args", make_event("/args hello 3"))]
        assert [r.message.text for r in results if r is not None] == [
            "args=hello:3:int",
        ]
        results = [r async for r in client.invoke("args", make_event("args hi"))]
        assert [r.message.text for r in results if r is not None] == [
            "args=hi:1:int",
        ]

        # GreedyStr captures the remainder.
        results = [
            r
            async for r in client.invoke(
                "greedy",
                make_event("/greedy some long text here"),
            )
        ]
        assert [r.message.text for r in results if r is not None] == [
            "greedy=some long text here",
        ]

        # StarTools.get_data_dir lands on the AstrBot plugin_data path.
        results = [r async for r in client.invoke("datadir", make_event("datadir"))]
        assert [r.message.text for r in results if r is not None] == [
            "dir=wrong_name",
        ]
        assert (tmp_path / "plugin_data" / "wrong_name").is_dir()

        # Conversation manager facade end to end.
        results = [r async for r in client.invoke("conv", make_event("conv"))]
        assert [r.message.text for r in results if r is not None] == [
            "conv=2:hi",
        ]
        results = [r async for r in client.invoke("curconv", make_event("curconv"))]
        assert [r.message.text for r in results if r is not None] == ["cur=True"]
    finally:
        await client.close()


LEGACY_PLUGIN_V2 = """
import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class LegacyV2Plugin(Star):
    async def initialize(self):
        self.context.register_task(self._background(), "bg")

    async def _background(self):
        await self.context.put_kv_data("bg_ran", "yes")

    @filter.command("stars")
    async def stars(self, event: AstrMessageEvent):
        names = [m.name for m in self.context.get_all_stars()]
        yield event.plain_result(f"stars={','.join(names)}")

    @filter.command("llmreq")
    async def llmreq(self, event: AstrMessageEvent):
        yield event.request_llm(prompt="say hi")

    @filter.command("ev")
    async def ev(self, event: AstrMessageEvent):
        outline = event.get_message_outline()
        admin = event.is_admin()
        gid = event.get_group_id()
        yield event.plain_result(
            f"outline={outline} admin={admin} gid={gid!r} sid={event.get_session_id()}"
        )

    @filter.command("ttsstt")
    async def ttsstt(self, event: AstrMessageEvent):
        audio = await self.context.get_using_tts_provider().get_audio("hello")
        text = await self.context.get_using_stt_provider().get_text(audio)
        vec = self.context.get_all_embedding_providers()
        dim = await vec[0].get_embedding("x")
        yield event.plain_result(f"tts={text} dim={len(dim)}")

    @filter.command("bgcheck")
    async def bgcheck(self, event: AstrMessageEvent):
        value = await self.context.get_kv_data("bg_ran", "no")
        yield event.plain_result(f"bg={value}")
"""


@pytest.mark.asyncio
async def test_legacy_surface_v2(tmp_path: Path) -> None:
    plugin_root = tmp_path / "legacy_surface_v2"
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_surface_v2",
                "desc": "v2 surface test",
                "author": "AstrBot",
                "version": "1.0.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(LEGACY_PLUGIN_V2, encoding="utf-8")

    kv: dict[str, Any] = {}
    from astrbot_sdk.context import PluginInfo, RuntimeMode
    from astrbot_sdk.llm import (
        ChatResponse,
        EmbeddingResponse,
        Transcript,
    )

    async def host_handler(grant, operation, payload):
        if grant.id == "storage.kv":
            if operation == "get":
                return {"value": kv.get(payload["key"], payload.get("default"))}
            if operation == "set":
                kv[payload["key"]] = payload["value"]
                return {}
        if grant.id == "plugin.inspect" and operation == "list":
            return {
                "plugins": [
                    PluginInfo(
                        id="a/one",
                        name="one",
                        version="1",
                        runtime_mode=RuntimeMode.ISOLATED,
                    ),
                    PluginInfo(
                        id="a/two",
                        name="two",
                        version="1",
                        runtime_mode=RuntimeMode.ISOLATED,
                    ),
                ],
            }
        if grant.id == "llm.generate" and operation == "generate":
            return {"response": ChatResponse(content="hi there")}
        if grant.id == "llm.generate" and operation == "current_provider":
            return {"provider": None}
        if grant.id == "llm.generate" and operation == "list_providers":
            from astrbot_sdk.llm import ProviderInfo, ProviderKind

            return {
                "providers": [
                    ProviderInfo(
                        id="emb-1",
                        kind=ProviderKind.EMBEDDING,
                        model="emb-model",
                        provider_type="openai",
                    ),
                ],
            }
        if grant.id == "speech.synthesize" and operation == "synthesize":
            return {"asset": payload["text"]}
        if grant.id == "speech.transcribe" and operation == "transcribe":
            return {"transcript": Transcript(text=str(payload["audio"]))}
        if grant.id == "llm.embed" and operation == "embed":
            return {
                "response": EmbeddingResponse(
                    embeddings=((0.1, 0.2, 0.3),) * len(payload["input"]),
                ),
            }
        raise AssertionError(f"unexpected call: {grant.id} {operation}")

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
                "message.send",
                "plugin.inspect",
                "llm.generate",
                "speech.synthesize",
                "speech.transcribe",
                "llm.embed",
            ),
            host_info={
                "snapshot": {
                    "providers": {
                        "chat": [],
                        "speech_to_text": [
                            {"id": "stt-1", "model": "whisper", "type": "openai"},
                        ],
                        "text_to_speech": [
                            {"id": "tts-1", "model": "tts", "type": "openai"},
                        ],
                        "embedding": [
                            {"id": "emb-1", "model": "e5", "type": "local"},
                        ],
                    },
                    "provider_defaults": {
                        "chat": None,
                        "speech_to_text": "stt-1",
                        "text_to_speech": "tts-1",
                        "embedding": "emb-1",
                    },
                    "provider_umo_prefs": {},
                    "config": {
                        "provider_stt_settings": {
                            "enable": True,
                            "provider_id": "stt-1",
                        },
                        "provider_tts_settings": {
                            "enable": True,
                            "provider_id": "tts-1",
                        },
                    },
                    "config_profiles": {},
                    "config_routes": {},
                },
            },
        )

        # register_task ran during initialize.
        results = [r async for r in client.invoke("bgcheck", make_event("bg"))]
        assert [r.message.text for r in results if r is not None] == ["bg=yes"]

        # Registry snapshot from start().
        results = [r async for r in client.invoke("stars", make_event("stars"))]
        assert [r.message.text for r in results if r is not None] == [
            "stars=one,two",
        ]

        # Yielded ProviderRequest executes through the provider facade.
        results = [r async for r in client.invoke("llmreq", make_event("llmreq"))]
        assert [r.message.text for r in results if r is not None] == ["hi there"]

        # Event facade getters.
        results = [r async for r in client.invoke("ev", make_event("ev hi"))]
        assert [r.message.text for r in results if r is not None] == [
            "outline=ev hi admin=False gid='' sid=user-1",
        ]

        # TTS/STT/embedding facades.
        results = [r async for r in client.invoke("ttsstt", make_event("tts"))]
        assert [r.message.text for r in results if r is not None] == [
            "tts=hello dim=3",
        ]
    finally:
        await client.close()
