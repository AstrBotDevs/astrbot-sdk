"""Unit tests for the legacy handshake snapshot facades."""

from __future__ import annotations

import asyncio
import logging

import pytest

from astrbot_sdk.compat.v1.errors import IsolationUnsupportedError
from astrbot_sdk.compat.v1.host_snapshot import (
    PersonaManagerFacade,
    copy_global_config,
    resolve_provider_id,
    route_config,
)
from astrbot_sdk.compat.v1.provider import ProviderType
from astrbot_sdk.compat.v1.star import Context
from astrbot_sdk.llm import ProviderKind

SNAPSHOT = {
    "providers": {
        "chat": [
            {"id": "chat-a", "model": "model-a", "type": "openai"},
            {"id": "chat-b", "model": "model-b", "type": "deepseek"},
        ],
        "speech_to_text": [{"id": "stt-a", "model": "whisper", "type": "openai"}],
        "text_to_speech": [],
        "embedding": [{"id": "emb-a", "model": "e5", "type": "local"}],
    },
    "provider_defaults": {
        "chat": "chat-a",
        "speech_to_text": "stt-a",
        "text_to_speech": None,
        "embedding": "emb-a",
    },
    "provider_umo_prefs": {
        "webchat:FriendMessage:u1": {"chat_completion": "chat-b"},
    },
    "personas_v3": [
        {"name": "default", "prompt": "be helpful", "begin_dialogs": []},
        {"name": "tsundere", "prompt": "hmph", "begin_dialogs": []},
    ],
    "personas": [{"persona_id": "p1", "system_prompt": "hi"}],
    "default_personality": {"prompt": "factory", "name": "default"},
    "default_persona_id": "default",
    "umo_session_personas": {"webchat:FriendMessage:u2": "tsundere"},
    "config": {
        "timezone": "UTC",
        "admins_id": ["root"],
        "provider": [{"id": "chat-a", "key": "", "api_base": "https://x"}],
        "provider_stt_settings": {"enable": True, "provider_id": "stt-a"},
        "provider_tts_settings": {"enable": False, "provider_id": ""},
        "agent_runner": {
            "runner_type": "local",
            "config": {
                "model": {"provider_id": "chat-a"},
                "persona": {"persona_id": "default"},
            },
        },
    },
    "config_profiles": {
        "uuid-1": {
            "timezone": "Asia/Shanghai",
            "agent_runner": {
                "runner_type": "local",
                "config": {"model": {"provider_id": "chat-b"}},
            },
        },
    },
    "config_routes": {"aiocqhttp::*": "uuid-1"},
}


class FakeCtx:
    """Minimal SDK context double capturing capability calls."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("test")
        self.calls: list[tuple[str, str, dict]] = []

    async def _invoke_capability(self, capability, operation, payload):
        self.calls.append((capability, operation, payload))
        return {}


def test_route_config_default_and_profile() -> None:
    assert route_config(SNAPSHOT, None)["timezone"] == "UTC"
    routed = route_config(SNAPSHOT, "aiocqhttp:GroupMessage:42")
    assert routed["timezone"] == "Asia/Shanghai"
    assert route_config(SNAPSHOT, "webchat:FriendMessage:u1")["timezone"] == "UTC"
    # Malformed umo and malformed patterns fall back to the default config.
    assert route_config(SNAPSHOT, "not-a-umo")["timezone"] == "UTC"


def test_resolve_provider_id_preference_and_defaults() -> None:
    # Per-session preference wins.
    assert (
        resolve_provider_id(SNAPSHOT, ProviderKind.CHAT, "webchat:FriendMessage:u1")
        == "chat-b"
    )
    # Default config's agent_runner provider_id.
    assert resolve_provider_id(SNAPSHOT, ProviderKind.CHAT, None) == "chat-a"
    # umo routed to a profile selecting chat-b.
    assert (
        resolve_provider_id(SNAPSHOT, ProviderKind.CHAT, "aiocqhttp:GroupMessage:9")
        == "chat-b"
    )
    # Unknown preference falls through to default resolution.
    snapshot = {
        **SNAPSHOT,
        "provider_umo_prefs": {"webchat:FriendMessage:u1": {"chat_completion": "gone"}},
    }
    assert (
        resolve_provider_id(snapshot, ProviderKind.CHAT, "webchat:FriendMessage:u1")
        == "chat-a"
    )


def test_resolve_provider_id_stt_tts_embedding() -> None:
    assert resolve_provider_id(SNAPSHOT, ProviderKind.SPEECH_TO_TEXT, None) == "stt-a"
    # TTS disabled in config resolves to None.
    assert resolve_provider_id(SNAPSHOT, ProviderKind.TEXT_TO_SPEECH, None) is None
    # Embedding has no default resolution in-process either.
    with pytest.raises(ValueError, match="Unknown provider type"):
        resolve_provider_id(SNAPSHOT, ProviderKind.EMBEDDING, None)
    # STT disabled resolves to None.
    snapshot = {
        **SNAPSHOT,
        "config": {**SNAPSHOT["config"], "provider_stt_settings": {"enable": False}},
    }
    assert resolve_provider_id(snapshot, ProviderKind.SPEECH_TO_TEXT, None) is None


def test_context_sync_provider_getters() -> None:
    context = Context(FakeCtx(), snapshot=SNAPSHOT)
    providers = context.get_all_providers()
    assert [p.get_provider_id() for p in providers] == ["chat-a", "chat-b"]
    meta = providers[0].meta()
    assert meta.id == "chat-a"
    assert meta.model == "model-a"
    assert meta.provider_type is ProviderType.CHAT_COMPLETION

    using = context.get_using_provider("webchat:FriendMessage:u1")
    assert using is not None
    assert using.get_provider_id() == "chat-b"
    assert context.get_using_provider(None).get_provider_id() == "chat-a"

    assert context.get_using_stt_provider(None) is not None
    assert context.get_using_tts_provider(None) is None

    assert [p.get_provider_id() for p in context.get_all_embedding_providers()] == [
        "emb-a",
    ]

    empty = Context(FakeCtx(), snapshot={})
    assert empty.get_using_provider(None) is None
    assert empty.get_all_providers() == []

    by_id = context.get_provider_by_id("chat-b")
    assert by_id.meta().model == "model-b"
    assert context.get_provider_by_id("missing") is None


def test_provider_manager_facade() -> None:
    context = Context(FakeCtx(), snapshot=SNAPSHOT)
    manager = context.provider_manager
    assert [p.get_provider_id() for p in manager.provider_insts] == [
        "chat-a",
        "chat-b",
    ]
    assert manager.curr_provider_inst.get_provider_id() == "chat-a"
    resolved = manager.get_using_provider(
        ProviderType.CHAT_COMPLETION, "webchat:FriendMessage:u1"
    )
    assert resolved.get_provider_id() == "chat-b"
    with pytest.raises(ValueError, match="Unknown provider type"):
        manager.get_using_provider(ProviderType.EMBEDDING)
    # personas are shared with the persona manager facade.
    assert manager.personas is context.persona_manager.personas_v3
    assert manager.persona_mgr is context.persona_manager


def test_get_config_facade() -> None:
    context = Context(FakeCtx(), snapshot=SNAPSHOT)
    config = context.get_config()
    assert config["timezone"] == "UTC"
    assert config.admins_id == ["root"]
    assert config.missing_key is None
    routed = context.get_config("aiocqhttp:GroupMessage:42")
    assert routed["timezone"] == "Asia/Shanghai"
    # Mutating the returned copy must not affect the snapshot.
    config["timezone"] = "changed"
    assert context.get_config()["timezone"] == "UTC"
    with pytest.raises(IsolationUnsupportedError):
        config.save_config()

    empty = Context(FakeCtx(), snapshot={})
    with pytest.raises(IsolationUnsupportedError):
        empty.get_config()


def test_persona_manager_facade() -> None:
    context = Context(FakeCtx(), snapshot=SNAPSHOT)
    manager = context.persona_manager
    assert manager.get_persona_v3_by_id("tsundere")["prompt"] == "hmph"
    assert manager.get_persona_v3_by_id(None) is None
    assert manager.get_persona_v3_by_id("default")["prompt"] == "be helpful"
    # Falls back to the built-in default personality when unnamed.
    empty = PersonaManagerFacade(
        FakeCtx(), {"default_personality": {"prompt": "x", "name": "default"}}
    )
    assert empty.get_persona_v3_by_id("default")["prompt"] == "x"
    assert empty.get_persona_v3_by_id("ghost") is None


@pytest.mark.asyncio
async def test_persona_manager_async_resolution() -> None:
    context = Context(FakeCtx(), snapshot=SNAPSHOT)
    manager = context.persona_manager
    default = await manager.get_default_persona_v3()
    assert default["prompt"] == "be helpful"

    persona_id, persona, force, webchat = await manager.resolve_selected_persona(
        umo="webchat:FriendMessage:u2",
        conversation_persona_id=None,
        platform_name="webchat",
    )
    assert (persona_id, persona["prompt"], force, webchat) == (
        "tsundere",
        "hmph",
        "tsundere",
        False,
    )

    persona_id, persona, force, webchat = await manager.resolve_selected_persona(
        umo="webchat:FriendMessage:u1",
        conversation_persona_id=None,
        platform_name="webchat",
    )
    # Implicit system default on webchat uses the ChatUI special default.
    assert (persona_id, persona, force, webchat) == (
        "_chatui_default_",
        None,
        None,
        True,
    )

    with pytest.raises(IsolationUnsupportedError):
        await manager.create_persona("x")


@pytest.mark.asyncio
async def test_persona_dict_write_forwards_rpc() -> None:
    ctx = FakeCtx()
    manager = PersonaManagerFacade(ctx, SNAPSHOT)
    persona = manager.personas_v3[1]
    persona["prompt"] = "new prompt"
    assert persona["prompt"] == "new prompt"
    await asyncio.sleep(0)
    assert ctx.calls == [
        (
            "persona.write",
            "set_field",
            {"name": "tsundere", "key": "prompt", "value": "new prompt"},
        ),
    ]


def test_global_config_copy_is_independent() -> None:
    config = copy_global_config(SNAPSHOT, None)
    config["admins_id"].append("other")
    assert SNAPSHOT["config"]["admins_id"] == ["root"]
