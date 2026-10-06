"""Facades over the host handshake snapshot for legacy sync getters.

The host ships a JSON snapshot in the initialize handshake capturing the
state legacy plugins read synchronously (global config, provider registry,
personas, per-session provider preferences). These facades serve the old
sync API surface from that snapshot; it is frozen at load time, so runtime
host edits only reach the plugin after a reload.
"""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import logging
from types import SimpleNamespace
from typing import Any

from ...llm import ProviderInfo, ProviderKind
from .errors import IsolationUnsupportedError
from .provider import (
    EmbeddingProvider,
    Provider,
    ProviderType,
    STTProvider,
    TTSProvider,
)

_KIND_BY_LEGACY_TYPE = {
    ProviderType.CHAT_COMPLETION.value: ProviderKind.CHAT,
    ProviderType.SPEECH_TO_TEXT.value: ProviderKind.SPEECH_TO_TEXT,
    ProviderType.TEXT_TO_SPEECH.value: ProviderKind.TEXT_TO_SPEECH,
    ProviderType.EMBEDDING.value: ProviderKind.EMBEDDING,
}

_SNAPSHOT_KEY_BY_KIND = {
    ProviderKind.CHAT: "chat",
    ProviderKind.SPEECH_TO_TEXT: "speech_to_text",
    ProviderKind.TEXT_TO_SPEECH: "text_to_speech",
    ProviderKind.EMBEDDING: "embedding",
}

# provider_umo_prefs keys are core ProviderType values.
_PREF_KEY_BY_KIND = {
    ProviderKind.CHAT: "chat_completion",
    ProviderKind.SPEECH_TO_TEXT: "speech_to_text",
    ProviderKind.TEXT_TO_SPEECH: "text_to_speech",
    ProviderKind.EMBEDDING: "embedding",
}


def provider_info(entry: dict, kind: ProviderKind) -> ProviderInfo:
    """Convert one snapshot provider entry into the public DTO."""
    return ProviderInfo(
        id=str(entry.get("id") or ""),
        kind=kind,
        model=entry.get("model"),
        provider_type=str(entry.get("type") or ""),
    )


def route_config(snapshot: dict, umo: str | None) -> dict:
    """Replicate UmopConfigRouter + ACM fallback against the snapshot.

    Args:
        snapshot: Host handshake snapshot.
        umo: Session origin to route, or None for the default config.

    Returns:
        The routed (redacted) config dict.
    """
    config = snapshot.get("config") or {}
    if not umo:
        return config
    parts = str(umo).split(":", 2)
    if len(parts) != 3:
        return config
    profiles = snapshot.get("config_profiles") or {}
    for pattern, conf_id in (snapshot.get("config_routes") or {}).items():
        pattern_parts = str(pattern).split(":", 2)
        if len(pattern_parts) != 3:
            continue
        if all(
            p == "" or fnmatch.fnmatchcase(t, p) for p, t in zip(pattern_parts, parts)
        ):
            return profiles.get(conf_id) or config
    return config


def resolve_provider_id(
    snapshot: dict,
    kind: ProviderKind,
    umo: str | None,
) -> str | None:
    """Replicate ProviderManager._resolve_using_provider on the snapshot.

    Args:
        snapshot: Host handshake snapshot.
        kind: Provider kind to resolve.
        umo: Session origin for per-session preferences, or None.

    Returns:
        The resolved provider id, or None when the kind is disabled or no
        provider is available.

    Raises:
        ValueError: The provider kind has no default resolution (matching
            the in-process behavior for embedding/rerank).
    """
    entries = (snapshot.get("providers") or {}).get(_SNAPSHOT_KEY_BY_KIND[kind]) or []
    ids = {entry["id"] for entry in entries}
    if umo:
        pref = ((snapshot.get("provider_umo_prefs") or {}).get(umo) or {}).get(
            _PREF_KEY_BY_KIND[kind]
        )
        if pref in ids:
            return pref
    config = route_config(snapshot, umo)
    if kind is ProviderKind.CHAT:
        agent_runner = config.get("agent_runner") or {}
        provider_id = None
        if agent_runner.get("runner_type") == "local":
            provider_id = ((agent_runner.get("config") or {}).get("model") or {}).get(
                "provider_id"
            )
        if provider_id in ids:
            return provider_id
        return entries[0]["id"] if entries else None
    if kind is ProviderKind.SPEECH_TO_TEXT:
        settings = config.get("provider_stt_settings") or {}
        if not settings.get("enable") or not settings.get("provider_id"):
            return None
        provider_id = settings["provider_id"]
        if provider_id in ids:
            return provider_id
        return entries[0]["id"] if entries else None
    if kind is ProviderKind.TEXT_TO_SPEECH:
        settings = config.get("provider_tts_settings") or {}
        if not settings.get("enable") or not settings.get("provider_id"):
            return None
        provider_id = settings["provider_id"]
        if provider_id in ids:
            return provider_id
        return entries[0]["id"] if entries else None
    raise ValueError(f"Unknown provider type: {kind}")


def find_provider_entry(
    snapshot: dict, kind: ProviderKind, provider_id: str | None
) -> dict | None:
    """Return the snapshot entry for one provider id, if present."""
    if not provider_id:
        return None
    entries = (snapshot.get("providers") or {}).get(_SNAPSHOT_KEY_BY_KIND[kind]) or []
    return next((e for e in entries if e.get("id") == provider_id), None)


def _legacy_type_from_any(provider_type: Any) -> ProviderKind:
    """Map a legacy/core ProviderType (or its value) onto ProviderKind."""
    value = getattr(provider_type, "value", provider_type)
    kind = _KIND_BY_LEGACY_TYPE.get(str(value))
    if kind is None:
        raise ValueError(f"Unknown provider type: {provider_type}")
    return kind


class GlobalConfig(dict):
    """Read-only facade of the redacted global AstrBot config.

    Attribute access mirrors AstrBotConfig (missing keys yield None).
    Persisting the global config is host-internal and raises.
    """

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError:
            return None

    def save_config(self, *args: Any, **kwargs: Any) -> None:
        """Persisting the global config is host-internal (unsupported)."""
        raise IsolationUnsupportedError(
            "saving the global AstrBot config is unavailable in isolated legacy mode"
        )

    async def save_config_async(self, *args: Any, **kwargs: Any) -> None:
        """Persisting the global config is host-internal (unsupported)."""
        raise IsolationUnsupportedError(
            "saving the global AstrBot config is unavailable in isolated legacy mode"
        )


class PersonaDict(dict):
    """Legacy persona record mirroring field writes to the host.

    In-process, personas_v3 entries are shared dicts and assignments mutate
    host state directly. Isolated, each assignment is forwarded through the
    persona.write capability (fire-and-forget, in-memory only on the host).
    """

    def __init__(self, data: dict, *, ctx: Any = None) -> None:
        super().__init__(data)
        self._ctx = ctx

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        ctx = self._ctx
        if ctx is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: nothing can deliver the RPC; the local mirror is
            # still updated for consistency within the runner.
            return
        task = loop.create_task(
            ctx._invoke_capability(
                "persona.write",
                "set_field",
                {"name": self.get("name"), "key": key, "value": value},
            )
        )
        task.add_done_callback(self._log_write_failure)

    @staticmethod
    def _log_write_failure(task: asyncio.Task) -> None:
        """Surface fire-and-forget persona write failures to the log."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logging.getLogger("astrbot.compat").error(
                "persona write failed: %s",
                exc,
            )


class PersonaManagerFacade:
    """Sync persona getters served from the handshake snapshot.

    Persona CRUD mutates the host database and stays host-internal; those
    methods raise IsolationUnsupportedError.
    """

    def __init__(self, ctx: Any, snapshot: dict) -> None:
        """Build the facade from the handshake snapshot."""
        self._ctx = ctx
        self._snapshot = snapshot
        self.personas_v3 = [
            PersonaDict(persona, ctx=ctx)
            for persona in snapshot.get("personas_v3") or []
        ]
        self.personas = [
            SimpleNamespace(
                persona_id=persona.get("persona_id"),
                system_prompt=persona.get("system_prompt"),
            )
            for persona in snapshot.get("personas") or []
        ]
        self.default_persona = snapshot.get("default_persona_id") or "default"
        self._default_personality = PersonaDict(
            snapshot.get("default_personality")
            or {
                "prompt": "You are a helpful and friendly assistant.",
                "name": "default",
            }
        )

    def get_persona_v3_by_id(self, persona_id: str | None) -> PersonaDict | None:
        """Resolve a v3 persona by id (mirrors the in-process semantics)."""
        if not persona_id:
            return None
        persona = next(
            (p for p in self.personas_v3 if p.get("name") == persona_id),
            None,
        )
        if persona is not None:
            return persona
        if persona_id == "default":
            return self._default_personality
        return None

    def _default_persona_id_for(self, umo: str | None) -> str:
        """Compute the configured default persona id for one session."""
        config = route_config(self._snapshot, umo)
        agent_runner = config.get("agent_runner") or {}
        runner_config = agent_runner.get("config") or {}
        if agent_runner.get("runner_type") == "local":
            return (runner_config.get("persona") or {}).get("persona_id", "default")
        return runner_config.get("persona_id", "default")

    async def get_default_persona_v3(self, umo: Any = None) -> PersonaDict:
        """Return the default persona, honoring umo config routing."""
        persona_id = self._default_persona_id_for(str(umo) if umo else None)
        return self.get_persona_v3_by_id(persona_id) or self._default_personality

    async def resolve_selected_persona(
        self,
        *,
        umo: Any,
        conversation_persona_id: str | None,
        platform_name: str,
        provider_settings: dict | None = None,
    ) -> tuple[str | None, PersonaDict | None, str | None, bool]:
        """Resolve the effective persona for a session (legacy semantics).

        Returns:
            Tuple of selected persona id, persona object, force-applied id
            from the session rule, and whether the webchat special default
            applies.
        """
        umo_str = str(umo)
        force_applied_persona_id = (
            self._snapshot.get("umo_session_personas") or {}
        ).get(umo_str)
        persona_id = force_applied_persona_id
        if not persona_id:
            persona_id = conversation_persona_id
            if persona_id == "[%None]":
                pass
            elif persona_id is None:
                persona_id = self._default_persona_id_for(umo_str)
        persona = next(
            (p for p in self.personas_v3 if p.get("name") == persona_id),
            None,
        )
        is_implicit_system_default = (
            force_applied_persona_id is None
            and conversation_persona_id is None
            and persona_id == "default"
        )
        if is_implicit_system_default and platform_name == "webchat":
            persona = None
        use_webchat_special_default = False
        if not persona and platform_name == "webchat" and persona_id != "[%None]":
            persona_id = "_chatui_default_"
            use_webchat_special_default = True
        return (
            persona_id,
            persona,
            force_applied_persona_id,
            use_webchat_special_default,
        )

    async def get_persona(self, persona_id: str) -> Any:
        """Persona CRUD is host-internal (unsupported when isolated)."""
        raise IsolationUnsupportedError(
            "persona CRUD is unavailable in isolated legacy mode"
        )

    async def get_all_personas(self) -> Any:
        """Persona CRUD is host-internal (unsupported when isolated)."""
        raise IsolationUnsupportedError(
            "persona CRUD is unavailable in isolated legacy mode"
        )

    async def create_persona(self, *args: Any, **kwargs: Any) -> Any:
        """Persona CRUD is host-internal (unsupported when isolated)."""
        raise IsolationUnsupportedError(
            "persona CRUD is unavailable in isolated legacy mode"
        )

    async def update_persona(self, *args: Any, **kwargs: Any) -> Any:
        """Persona CRUD is host-internal (unsupported when isolated)."""
        raise IsolationUnsupportedError(
            "persona CRUD is unavailable in isolated legacy mode"
        )

    async def delete_persona(self, *args: Any, **kwargs: Any) -> Any:
        """Persona CRUD is host-internal (unsupported when isolated)."""
        raise IsolationUnsupportedError(
            "persona CRUD is unavailable in isolated legacy mode"
        )


class ProviderManagerFacade:
    """Legacy provider_manager served from the handshake snapshot."""

    def __init__(
        self,
        ctx: Any,
        snapshot: dict,
        persona_mgr: PersonaManagerFacade | None = None,
    ) -> None:
        """Build the facade from the handshake snapshot."""
        self._ctx = ctx
        self._snapshot = snapshot
        providers = snapshot.get("providers") or {}
        self.provider_insts = [
            Provider(
                ctx,
                provider_id=entry["id"],
                info=provider_info(entry, ProviderKind.CHAT),
            )
            for entry in providers.get("chat") or []
        ]
        self.stt_provider_insts = [
            STTProvider(ctx, provider_id=entry["id"])
            for entry in providers.get("speech_to_text") or []
        ]
        self.tts_provider_insts = [
            TTSProvider(ctx, provider_id=entry["id"])
            for entry in providers.get("text_to_speech") or []
        ]
        self.embedding_provider_insts = [
            EmbeddingProvider(ctx, provider_id=entry["id"])
            for entry in providers.get("embedding") or []
        ]
        self.persona_mgr = persona_mgr
        self.personas = persona_mgr.personas_v3 if persona_mgr else []

    @property
    def curr_provider_inst(self) -> Provider | None:
        """Return the default chat provider facade, if one exists."""
        provider_id = (self._snapshot.get("provider_defaults") or {}).get("chat")
        if provider_id:
            found = next(
                (p for p in self.provider_insts if p.get_provider_id() == provider_id),
                None,
            )
            if found is not None:
                return found
        return self.provider_insts[0] if self.provider_insts else None

    def get_using_provider(
        self,
        provider_type: Any,
        umo: str | None = None,
    ) -> Any:
        """Resolve the active provider of one type (legacy sync signature).

        Args:
            provider_type: Legacy ProviderType enum member (or its value).
            umo: Session origin for per-session preferences.

        Returns:
            Provider facade, or None when unavailable/disabled.
        """
        kind = _legacy_type_from_any(provider_type)
        provider_id = resolve_provider_id(self._snapshot, kind, umo)
        if provider_id is None:
            return None
        if kind is ProviderKind.CHAT:
            entry = find_provider_entry(self._snapshot, kind, provider_id)
            return Provider(
                self._ctx,
                provider_id=provider_id,
                info=provider_info(entry, kind) if entry else None,
            )
        if kind is ProviderKind.SPEECH_TO_TEXT:
            return STTProvider(self._ctx, provider_id=provider_id)
        if kind is ProviderKind.TEXT_TO_SPEECH:
            return TTSProvider(self._ctx, provider_id=provider_id)
        raise ValueError(f"Unknown provider type: {provider_type}")


def copy_global_config(snapshot: dict, umo: str | None) -> GlobalConfig:
    """Return a deep-copied routed global config facade."""
    return GlobalConfig(copy.deepcopy(route_config(snapshot, umo)))
