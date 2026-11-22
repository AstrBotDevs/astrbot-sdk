"""Unit tests covering full cross-process SDK and legacy compat v1 parity."""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from astrbot_sdk import Plugin, PluginContext, ViewResult, ViewSpec, on
from astrbot_sdk.assets import AssetRef
from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.context import PluginInfo, RuntimeMode
from astrbot_sdk.errors import InvalidRequest
from astrbot_sdk.events import (
    UMO,
    MessageEvent,
    MessageRef,
    MessageType,
    Sender,
    SenderRole,
)
from astrbot_sdk.message_components import (
    ActionRow,
    Button,
    ButtonInteraction,
    Json,
    Location,
    Music,
    Plain,
    Poke,
    Share,
    UIInteraction,
    UnknownSegment,
    ViewContext,
)
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.protocol.codec import (
    decode_message_chain,
    decode_result,
    decode_value,
    encode_message_chain,
    encode_result,
    encode_value,
)
from astrbot_sdk.registration import HandlerKind, discover_handlers
from astrbot_sdk.results import Propagation
from astrbot_sdk.runtime.loader import load_plugin
from astrbot_sdk.runtime.metadata import parse_metadata


class _RecordingInvoker:
    def __init__(self, responses: dict[tuple[str, str], Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._responses = responses or {}

    async def __call__(
        self, capability_id: str, operation: str, payload: dict[str, Any]
    ) -> Any:
        self.calls.append((capability_id, operation, dict(payload)))
        return self._responses.get((capability_id, operation), {})


def _make_context(
    tmp_path: Path,
    *cap_ids: str,
    responses: dict[tuple[str, str], Any] | None = None,
) -> tuple[PluginContext, _RecordingInvoker]:
    invoker = _RecordingInvoker(responses)
    ctx = PluginContext(
        plugin=PluginInfo(
            id="tester/demo",
            name="demo",
            version="1.0.0",
            runtime_mode=RuntimeMode.ISOLATED,
        ),
        config={},
        logger=logging.getLogger("test.full_parity"),
        capabilities=CapabilitySet([CapabilityGrant(id=c) for c in cap_ids]),
        data_dir=tmp_path,
        _host_capability_invoker=invoker,
    )
    return ctx, invoker


def _sample_event(*segments: Any) -> MessageEvent:
    return MessageEvent(
        id="evt-1",
        umo=UMO("webchat", MessageType.PRIVATE, "u-1"),
        platform_type="webchat",
        message_ref=MessageRef(id="msg-1"),
        message=MessageChain(*segments),
        sender=Sender(id="u-1", name="Alice", role=SenderRole.ADMIN),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        is_wake=True,
    )


def test_rich_segments_and_view_result_roundtrip() -> None:
    btn_cb = Button.callback("OK", "approve", data={"step": 1}, style="primary")
    btn_url = Button.link("Open", "https://example.com", emoji="🔗")
    row = ActionRow([btn_cb, btn_url])
    ui = UIInteraction(
        action_id="refresh",
        interaction_id="int-1",
        view=ViewContext(id="v-1", name="card", version=2, revision=3, data={"n": 1}),
        data={"delta": 2},
    )
    bi = ButtonInteraction(
        action_id="approve",
        interaction_id="b-1",
        data={"x": 9},
        source_message_id="msg-1",
        callback_data="tok-1",
    )
    chain = MessageChain(
        Plain("hi"),
        Poke(poke_type="126", user_id="u-1"),
        Json(data={"a": 1}),
        Share(url="https://example.com", title="T", content="C", image="img"),
        Location(lat=31.2, lon=121.5, title="SH", content="Bund"),
        Music(music_type="163", id=42, title="Song", url="https://music.example"),
        row,
        bi,
        ui,
    )
    encoded = encode_message_chain(chain)
    decoded = decode_message_chain(encoded)
    assert decoded[0] == Plain("hi")
    assert decoded[1] == Poke(poke_type="126", user_id="u-1")
    assert isinstance(decoded[2], Json) and dict(decoded[2].data) == {"a": 1}
    assert decoded[3] == Share(
        url="https://example.com", title="T", content="C", image="img"
    )
    assert decoded[4] == Location(lat=31.2, lon=121.5, title="SH", content="Bund")
    assert decoded[5] == Music(
        music_type="163", id=42, title="Song", url="https://music.example"
    )
    assert decoded[6] == row
    assert decoded[7] == bi
    assert isinstance(decoded[8], UIInteraction)
    assert decoded[8].view.name == "card"
    assert dict(decoded[8].view.data) == {"n": 1}

    with pytest.raises(InvalidRequest):
        encode_message_chain(MessageChain(UnknownSegment("custom", {"a": 1})))

    vr = ViewResult(
        propagation=Propagation.STOP,
        name="card",
        data={"count": 5},
        text="fallback",
        title="Card",
    )
    res_wire = encode_result(vr)
    decoded_vr = decode_result(res_wire)
    assert isinstance(decoded_vr, ViewResult)
    assert decoded_vr.propagation is Propagation.STOP
    assert decoded_vr.name == "card"
    assert dict(decoded_vr.data) == {"count": 5}
    assert decoded_vr.text == "fallback"
    assert decoded_vr.title == "Card"

    # Tool-return path uses encode_value/decode_value on registered dataclasses.
    assert decode_value(encode_value(vr)) == vr


def test_button_ui_decorators_event_helpers_and_views_metadata(
    tmp_path: Path,
) -> None:
    class _Demo(Plugin):
        @on.button_action("approve")
        async def on_btn(self, event: MessageEvent):
            yield event.reply(f"clicked:{event.button_interaction.action_id}")

        @on.ui_action("refresh")
        async def on_ui(self, event: MessageEvent):
            yield event.view_result(data={"k": "v"})

    ctx, _ = _make_context(tmp_path)
    regs = {r.method_name: r for r in discover_handlers(_Demo(ctx))}
    assert regs["on_btn"].spec.kind is HandlerKind.MESSAGE
    assert regs["on_btn"].spec.button_action is True
    assert regs["on_btn"].spec.button_action_id == "approve"
    assert regs["on_ui"].spec.kind is HandlerKind.MESSAGE
    assert regs["on_ui"].spec.ui_action is True
    assert regs["on_ui"].spec.ui_action_id == "refresh"

    ev = _sample_event(
        ButtonInteraction(action_id="approve", interaction_id="i1"),
        UIInteraction(
            action_id="refresh",
            interaction_id="i2",
            view=ViewContext(id="v1", name="card", data={}),
        ),
    )
    assert ev.is_button_interaction is True
    assert (
        ev.button_interaction is not None
        and ev.button_interaction.action_id == "approve"
    )
    assert ev.ui_interaction is not None and ev.ui_interaction.action_id == "refresh"
    upd = ev.view_result(data={"updated": True})
    assert upd.name is None and dict(upd.data) == {"updated": True}

    meta = parse_metadata(
        {
            "schema_version": 2,
            "name": "demo_views",
            "desc": "demo",
            "author": "tester",
            "version": "1.0.0",
            "runtime": {
                "api": "sdk",
                "entrypoint": "main:DemoPlugin",
                "sdk_version": ">=0.1",
            },
            "views": [
                {"name": "card", "target": "chat_message", "actions": ["refresh"]}
            ],
        },
        plugin_root=tmp_path,
    )
    assert len(meta.pages) == 1 and meta.pages[0]["name"] == "card"
    spec = ViewSpec(name="card", target="chat_message", actions=("refresh",))
    assert spec.entry == Path("views/card/index.html")


@pytest.mark.asyncio
async def test_modern_host_service_clients_and_cron(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "modern_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "metadata.yaml").write_text(
        """
schema_version: 2
name: modern_plugin
desc: modern
author: tester
version: 1.0.0
runtime:
  api: sdk
  entrypoint: main:ModernPlugin
  sdk_version: ">=0.1"
capabilities:
  required:
    - id: file.token
    - id: event.state
    - id: platform.raw
    - id: cron.schedule
    - id: kb.manage
    - id: message.history
    - id: assets.transfer
""",
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        """
from astrbot_sdk import Plugin
class ModernPlugin(Plugin):
    pass
""",
        encoding="utf-8",
    )

    asset_ref = AssetRef(id="ast-1", filename="hi.txt")
    invoker = _RecordingInvoker(
        {
            ("assets.transfer", "upload"): {"asset": asset_ref},
            ("file.token", "register"): {"token": "tok-123"},
            ("event.state", "stream_open"): {"stream_id": "str-1"},
            ("event.state", "call_method"): {"result": {"group_id": "g-1"}},
            ("platform.raw", "call_action"): {"result": {"ok": True}},
            ("cron.schedule", "add_basic_job"): {"job": {"job_id": "j-basic"}},
            ("cron.schedule", "add_scheduler_job"): {"job_id": "j-once"},
            ("cron.schedule", "list_jobs"): {"jobs": [{"job_id": "j-basic"}]},
            ("kb.manage", "create_kb"): {"kb": {"kb_id": "kb-1", "kb_name": "docs"}},
            ("kb.manage", "list_kbs"): {"kbs": [{"kb_id": "kb-1"}]},
            ("kb.manage", "get_kb"): {"kb": {"kb_id": "kb-1"}},
            ("kb.manage", "get_kb_by_name"): {"kb": {"kb_id": "kb-1"}},
            ("kb.manage", "delete_kb"): {"deleted": True},
            ("message.history", "insert"): {"record": {"id": 1}},
            ("message.history", "insert_message_chain"): {"record": {"id": 2}},
            ("message.history", "get"): {"records": [{"id": 1}, {"id": 2}]},
            ("message.history", "count"): {"count": 2},
        }
    )
    loaded = load_plugin(
        plugin_dir,
        granted_capabilities=CapabilitySet.from_ids(
            "file.token",
            "event.state",
            "platform.raw",
            "cron.schedule",
            "kb.manage",
            "message.history",
            "assets.transfer",
        ),
        host_capability_invoker=invoker,
        data_dir=tmp_path / "data",
    )
    ctx = loaded.instance._ctx
    ev = _sample_event(Plain("hi"))

    tok = await ctx.assets.issue_download_token(b"hello", filename="hi.txt")
    assert tok == "tok-123"

    await ctx.events.set_call_llm(ev, False)
    await ctx.events.send_typing(ev)
    await ctx.events.stop_typing(ev)
    await ctx.events.react(ev, "👍")
    assert await ctx.events.get_group(ev) == {"group_id": "g-1"}

    async def _chunks():
        yield "part1"
        yield Plain("part2")

    await ctx.messages.send_streaming(ev, _chunks(), use_fallback=True)
    assert await ctx.platforms.call_action("webchat", "ping", x=1) == {"ok": True}

    fired: list[str] = []

    async def _cron_cb(tag: str = "none") -> str:
        fired.append(tag)
        return tag

    job = await ctx.cron.add_basic_job("c1", "* * * * *", _cron_cb)
    assert job["job_id"] == "j-basic"
    handler_id = next(iter(ctx.cron_handlers))
    assert await loaded.invoke_cron(handler_id, {"args": ["ok"], "kwargs": {}}) == "ok"
    assert fired == ["ok"]
    once_id = await ctx.cron.add_once_job(
        "c2", datetime(2026, 1, 1, tzinfo=UTC), _cron_cb
    )
    assert once_id == "j-once"
    assert await ctx.cron.list_jobs() == [{"job_id": "j-basic"}]
    await ctx.cron.delete_job("j-basic")

    created_kb = await ctx.kb.create("docs", embedding_provider_id="emb-1")
    assert created_kb["kb_id"] == "kb-1"
    assert len(await ctx.kb.list()) == 1
    assert (await ctx.kb.get("kb-1"))["kb_id"] == "kb-1"
    assert (await ctx.kb.get_by_name("docs"))["kb_id"] == "kb-1"
    assert await ctx.kb.delete("kb-1") is True

    await ctx.history.insert("webchat", "u-1", {"text": "hi"})
    await ctx.history.insert_chain("webchat", "u-1", "there")
    assert await ctx.history.count("webchat", "u-1") == 2
    assert len(await ctx.history.get("webchat", "u-1")) == 2
    await ctx.history.update(1, content={"text": "updated"})
    await ctx.history.delete("webchat", "u-1")


@pytest.mark.asyncio
async def test_legacy_compat_full_category_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrbot_sdk.compat.v1.api import install
    from astrbot_sdk.compat.v1.errors import IsolationUnsupportedError
    from astrbot_sdk.compat.v1.loader import load_legacy_plugin

    monkeypatch.delenv("ASTRBOT_SDK_LEGACY_RUNNER", raising=False)
    monkeypatch.delenv("ASTRBOT_HOST_VERSION", raising=False)
    monkeypatch.delenv("ASTRBOT_SDK_VIRTUAL_PLUGIN_PARENTS", raising=False)
    install(host_version="4.99.0")
    plugin_dir = tmp_path / "legacy_parity"
    plugin_dir.mkdir()
    (plugin_dir / "metadata.yaml").write_text(
        "name: legacy_parity\nauthor: tester\nversion: 1.0.0\ndesc: parity\n",
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        """
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.chatui import ViewResult, ViewSpec
from astrbot.api.agents import AgentRuntime, PlainOutput, chain_from_content
from astrbot.core import file_token_service
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.api.message_components import (
    BaseMessageComponent, Button, ActionRow, Music, Plain, Image
)

@register("legacy_parity", "tester", "parity", "1.0.0")
class LegacyParityPlugin(Star):
    async def initialize(self) -> None:
        self.context.register_commands(
            "legacy_parity", "dyn_hello", "dynamic", 1, self._dyn_cmd
        )

    async def _dyn_cmd(self, event: AstrMessageEvent):
        yield event.plain_result("from-dyn")

    @filter.button_action("btn_ok")
    async def on_btn(self, event: AstrMessageEvent):
        inter = event.get_button_interaction()
        yield event.plain_result(f"btn:{inter.action_id}")

    @filter.ui_action("card_bump")
    async def on_ui(self, event: AstrMessageEvent):
        yield event.view_result(data={"clicks": 2})

    @filter.command("card")
    async def on_card(self, event: AstrMessageEvent):
        yield event.view_result(
            name="c1", data={"clicks": 0}, text="fallback", title="T"
        )
""",
        encoding="utf-8",
    )

    asset_ref = AssetRef(id="ast-ft", filename="img.png")
    invoker = _RecordingInvoker(
        {
            ("assets.transfer", "upload"): {"asset": asset_ref},
            ("file.token", "register"): {"token": "tok-file"},
            ("handler.register", "register"): {"registered": True},
            ("event.inject", "inject"): {"queued": True},
            ("event.state", "stream_open"): {"stream_id": "s-1"},
            ("db.read", "get_umo_aliases"): {
                "aliases": [{"umo": "webchat:FriendMessage:u1", "user_alias": "Al"}]
            },
            ("db.read", "get_conversations"): {
                "conversations": [{"conversation_id": "cid-1", "user_id": "u1"}]
            },
            ("subagents.reload", "reload"): {"handoffs": [{"name": "helper"}]},
            ("plugin.lifecycle", "turn_off"): {"done": True},
            ("plugin.lifecycle", "turn_on"): {"done": True},
        }
    )
    sdk_ctx = PluginContext(
        plugin=PluginInfo(
            id="tester/legacy_parity",
            name="legacy_parity",
            version="1.0.0",
            runtime_mode=RuntimeMode.ISOLATED,
        ),
        config={},
        logger=logging.getLogger("test.legacy_parity"),
        capabilities=CapabilitySet.from_ids(
            "assets.transfer",
            "file.token",
            "handler.register",
            "event.inject",
            "event.state",
            "db.read",
            "subagents.reload",
            "plugin.lifecycle",
            "message.send",
        ),
        data_dir=tmp_path / "data_legacy",
        _host_capability_invoker=invoker,
    )
    try:
        loaded = load_legacy_plugin(
            plugin_dir,
            ctx=sdk_ctx,
            host_info={
                "version": "4.99.0",
                "snapshot": {
                    "config": {"callback_api_base": "http://localhost:6185"},
                    "subagent_handoffs": [{"name": "initial"}],
                    "handlers": [
                        {
                            "event_type": "AdapterMessageEvent",
                            "handler_full_name": "p_cmd",
                            "handler_name": "cmd",
                        }
                    ],
                },
            },
        )
        await loaded.start()

        from astrbot.api.agents import AgentRuntime, PlainOutput, chain_from_content
        from astrbot.api.chatui import ViewResult as LegacyViewResult
        from astrbot.api.chatui import ViewSpec as LegacyViewSpec
        from astrbot.api.message_components import (
            ActionRow as LegacyActionRow,
        )
        from astrbot.api.message_components import (
            BaseMessageComponent,
        )
        from astrbot.api.message_components import (
            Button as LegacyButton,
        )
        from astrbot.api.message_components import (
            Image as LegacyImage,
        )
        from astrbot.api.message_components import (
            Music as LegacyMusic,
        )
        from astrbot.api.message_components import (
            Plain as LegacyPlain,
        )
        from astrbot.core import file_token_service
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import star_handlers_registry

        reg_names = {h.handler_name for h in star_handlers_registry}
        assert star_handlers_registry[0].handler_full_name == "p_cmd"
        assert {"on_btn", "on_card", "on_ui"}.issubset(reg_names)
        assert isinstance(LegacyButton.callback("Go", "go"), BaseMessageComponent)
        assert isinstance(
            LegacyActionRow([LegacyButton.link("Link", "https://example.com")]),
            BaseMessageComponent,
        )
        assert isinstance(LegacyMusic(title="M"), BaseMessageComponent)
        assert isinstance(
            chain_from_content([PlainOutput("hi")]).chain[0], BaseMessageComponent
        )
        with pytest.raises(IsolationUnsupportedError):
            AgentRuntime()
        spec = LegacyViewSpec(name="c1", target="chat_message", actions=["card_bump"])
        assert str(spec.entry) == "views/c1/index.html"
        assert LegacyViewResult(name="c1", data={"a": 1}, text="fb").name == "c1"

        # Invoke dynamically registered command via CompatLoadedPlugin.invoke
        dyn_id = next(iter(loaded.instance.context._dynamic_commands))
        dyn_results = [
            r async for r in loaded.invoke(dyn_id, _sample_event(Plain("/dyn_hello")))
        ]
        assert dyn_results[0].message.text == "from-dyn"

        # Verify @filter.button_action, @filter.ui_action, and ViewResult
        btn_ev = _sample_event(
            ButtonInteraction(action_id="btn_ok", interaction_id="i1")
        )
        btn_out = [r async for r in loaded.invoke("on_btn", btn_ev)]
        assert btn_out[0].message.text == "btn:btn_ok"

        ui_ev = _sample_event(
            UIInteraction(
                action_id="card_bump",
                interaction_id="i2",
                view=ViewContext(id="v1", name="c1", data={}),
            )
        )
        ui_out = [r async for r in loaded.invoke("on_ui", ui_ev)]
        assert dict(ui_out[0].data) == {"clicks": 2} and ui_out[0].name is None

        card_out = [
            r async for r in loaded.invoke("on_card", _sample_event(Plain("/card")))
        ]
        assert card_out[0].name == "c1" and dict(card_out[0].data) == {"clicks": 0}

        # Verify Category 2 host service facades on Context & StarTools
        ctx_facade = loaded.instance.context
        assert (
            await ctx_facade.get_db().get_umo_aliases(["webchat:FriendMessage:u1"])
        )[0].user_alias == "Al"
        assert (await ctx_facade.get_db().get_conversations(user_id="u1"))[
            0
        ].conversation_id == "cid-1"
        with pytest.raises(IsolationUnsupportedError):
            ctx_facade.get_db()()

        sample_file = tmp_path / "sample.bin"
        sample_file.write_bytes(b"pngdata")
        assert await file_token_service.register_file(sample_file) == "tok-file"
        img_url = await LegacyImage.fromFileSystem(
            sample_file
        ).register_to_file_service()
        assert img_url == "http://localhost:6185/api/file/tok-file"

        from astrbot.api.star import StarTools

        await StarTools.create_event(
            await StarTools.create_message(
                type="FriendMessage",
                self_id="bot",
                session_id="u1",
                sender=type("_S", (), {"user_id": "u1", "nickname": "A"})(),
                message=[LegacyPlain("inj")],
                message_str="inj",
            )
        )
        await ctx_facade.get_event_queue().put(
            type(
                "_E",
                (),
                {
                    "unified_msg_origin": "webchat:FriendMessage:u1",
                    "get_messages": lambda self: [LegacyPlain("q")],
                    "get_message_str": lambda self: "q",
                    "get_sender_id": lambda self: "u1",
                    "get_sender_name": lambda self: "A",
                    "get_self_id": lambda self: "bot",
                    "is_wake_up": lambda self: True,
                    "get_extra": lambda self: {"k": 1},
                },
            )()
        )
        assert (
            await ctx_facade.subagent_orchestrator.reload_from_config({"enable": True})
        )[0].name == "helper"
        await ctx_facade._star_manager.turn_off_plugin("peer")
        await ctx_facade._star_manager.turn_on_plugin("peer")

        # send_streaming + process_buffer
        from astrbot_sdk.compat.v1.platform_events import build_legacy_event

        leg_ev = build_legacy_event(_sample_event(Plain("hi")), ctx_facade)
        rem = await leg_ev.process_buffer("hello\nworld\ntail", re.compile(r"[^\n]+\n"))
        assert rem == "tail"

        async def _leg_chunks():
            from astrbot.api.message_components import MessageChain as LChain

            yield LChain(chain=[LegacyPlain("s1")])

        await leg_ev.send_streaming(_leg_chunks())
        assert leg_ev._has_send_oper is True
        assert ("event.state", "stream_close") in [(c[0], c[1]) for c in invoker.calls]
        assert isinstance(star_map, dict)

        star_handlers_registry.clear()
        load_legacy_plugin(
            plugin_dir,
            ctx=sdk_ctx,
            host_info={"version": "4.99.0", "snapshot": {}},
        )
        assert {h.handler_name for h in star_handlers_registry} >= {
            "on_btn",
            "on_card",
            "on_ui",
        }
    finally:
        os.environ.pop("ASTRBOT_SDK_LEGACY_RUNNER", None)
        os.environ.pop("ASTRBOT_HOST_VERSION", None)
        os.environ.pop("ASTRBOT_SDK_VIRTUAL_PLUGIN_PARENTS", None)
        for name in [
            item
            for item in tuple(sys.modules)
            if item == "data" or item.startswith("data.")
        ]:
            sys.modules.pop(name, None)
        sys.path[:] = [p for p in sys.path if not str(p).startswith(str(tmp_path))]
