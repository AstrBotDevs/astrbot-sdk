from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from .protocol_registry import JSONValue, register_protocol_dataclass

if TYPE_CHECKING:
    from .context import PluginContext

_READ_CAPABILITY = "conversation.read"
_WRITE_CAPABILITY = "conversation.write"


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class TextPart:
    """Plain text content of a message."""

    text: str
    type: ClassVar[str] = "text"


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class ImagePart:
    """Image content as an asset reference or public URL."""

    source: Any
    type: ClassVar[str] = "image_url"


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class AudioPart:
    """Audio content as an asset reference or public URL."""

    source: Any
    type: ClassVar[str] = "audio_url"


ContentPart = TextPart | ImagePart | AudioPart


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class Message:
    """One message in a conversation history.

    Mirrors the public data protocol of the AstrBot core agent message (the
    shape accepted by add_message_pair), excluding Host-internal checkpoint
    and persistence machinery. ``content`` may be None for assistant messages
    that only carry tool calls; multimodal content uses part tuples.
    """

    role: str
    content: str | tuple[ContentPart, ...] | None = None
    tool_calls: tuple[Mapping[str, Any], ...] | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.tool_calls is not None:
            object.__setattr__(
                self,
                "tool_calls",
                tuple(dict(call) for call in self.tool_calls),
            )


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class ConversationRevision:
    """The event log revision and the current position in model context."""

    head_seq: int
    leaf_event_id: str | None


@register_protocol_dataclass
@dataclass
class Conversation:
    """The same conversation snapshot fields exposed by AstrBot Core."""

    platform_id: str
    user_id: str
    cid: str
    history: str = ""
    title: str | None = ""
    persona_id: str | None = ""
    created_at: int = 0
    updated_at: int = 0
    token_usage: int = 0
    revision: ConversationRevision | None = None


class ConversationService:
    """Read and manage conversations through AstrBot's plugin API."""

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:
        """Return the selected conversation ID for a session."""
        return await self._ctx._invoke_capability(
            _READ_CAPABILITY,
            "get_curr_conversation_id",
            {"unified_msg_origin": unified_msg_origin},
        )

    async def get_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        create_if_not_exists: bool = False,
    ) -> Conversation | None:
        """Read a conversation, optionally creating one if the ID is missing."""
        return await self._ctx._invoke_capability(
            _READ_CAPABILITY,
            "get_conversation",
            {
                "unified_msg_origin": unified_msg_origin,
                "conversation_id": conversation_id,
                "create_if_not_exists": create_if_not_exists,
            },
        )

    async def get_conversations(
        self,
        unified_msg_origin: str | None = None,
        platform_id: str | None = None,
    ) -> list[Conversation]:
        """List conversations for the specified or current message session."""
        return await self._ctx._invoke_capability(
            _READ_CAPABILITY,
            "get_conversations",
            {
                "unified_msg_origin": (
                    self._ctx._ambient_umo()
                    if unified_msg_origin is None
                    else unified_msg_origin
                ),
                "platform_id": platform_id,
            },
        )

    async def new_conversation(
        self,
        unified_msg_origin: str,
        platform_id: str | None = None,
        content: list[dict] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
    ) -> str:
        """Create and select a conversation, returning its ID."""
        return await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "new_conversation",
            {
                "unified_msg_origin": unified_msg_origin,
                "platform_id": platform_id,
                "content": content,
                "title": title,
                "persona_id": persona_id,
            },
        )

    async def switch_conversation(
        self, unified_msg_origin: str, conversation_id: str
    ) -> None:
        """Select a conversation for the specified session."""
        await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "switch_conversation",
            {
                "unified_msg_origin": unified_msg_origin,
                "conversation_id": conversation_id,
            },
        )

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str | None = None,
        history: list[dict] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
        token_usage: int | None = None,
        *,
        expected_revision: ConversationRevision | None = None,
    ) -> None:
        """Update an explicit or currently selected conversation."""
        await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "update_conversation",
            {
                "unified_msg_origin": unified_msg_origin,
                "conversation_id": conversation_id,
                "history": history,
                "title": title,
                "persona_id": persona_id,
                "token_usage": token_usage,
                "expected_revision": expected_revision,
            },
        )

    async def add_message_pair(
        self, cid: str, user_message: dict, assistant_message: dict
    ) -> None:
        """Append a user message and its assistant reply together."""
        await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "add_message_pair",
            {
                "cid": cid,
                "user_message": user_message,
                "assistant_message": assistant_message,
                "unified_msg_origin": self._ctx._ambient_umo(),
            },
        )

    async def append_message(
        self,
        content: str | list[dict],
        *,
        cid: str,
        message_id: str | None = None,
    ) -> str:
        """Append one user message and return its committed ID."""
        return await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "append_message",
            {
                "cid": cid,
                "content": content,
                "message_id": message_id,
                "unified_msg_origin": self._ctx._ambient_umo(),
            },
        )

    async def append_plugin_event(
        self,
        event_type: str,
        data: dict[str, JSONValue],
        *,
        cid: str,
        event_id: str | None = None,
    ) -> str:
        """Append a custom event in the plugin's namespace and return its ID."""
        return await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "append_plugin_event",
            {
                "cid": cid,
                "event_type": event_type,
                "data": data,
                "event_id": event_id,
                "unified_msg_origin": self._ctx._ambient_umo(),
            },
        )

    async def fork_conversation(
        self, unified_msg_origin: str, source_event_id: str
    ) -> str:
        """Create a separate conversation from a saved context event."""
        return await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "fork_conversation",
            {
                "unified_msg_origin": unified_msg_origin,
                "source_event_id": source_event_id,
            },
        )

    async def delete_conversation(
        self, unified_msg_origin: str, conversation_id: str | None = None
    ) -> None:
        """Delete an explicit or currently selected conversation."""
        await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "delete_conversation",
            {
                "unified_msg_origin": unified_msg_origin,
                "conversation_id": conversation_id,
            },
        )
