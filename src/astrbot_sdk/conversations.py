from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from .events import UMO
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
@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """An event in this plugin's conversation event namespace."""

    event_id: str
    seq: int
    event_type: str
    data: dict[str, JSONValue]
    created_at: datetime


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class Conversation:
    """AstrBot LLM conversation owned by one UMO session."""

    id: str
    title: str | None
    persona_id: str | None
    messages: tuple[Message, ...]
    created_at: datetime | None
    updated_at: datetime | None
    revision: ConversationRevision | None = None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class ConversationPatch:
    """Editable conversation fields. None leaves the field unchanged."""

    title: str | None = None
    persona_id: str | None = None
    messages: tuple[Message, ...] | None = None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class ConversationPage:
    """One page of conversations."""

    items: tuple[Conversation, ...]
    next_cursor: str | None


class ConversationService:
    """Read and manage AstrBot conversations for UMO sessions."""

    def __init__(self, ctx: PluginContext) -> None:
        """Initialize the service.

        Args:
            ctx: Owning plugin context used for Host invocation.
        """
        self._ctx = ctx

    async def current(self, umo: UMO) -> Conversation | None:
        """Return the session's currently selected conversation."""
        result = await self._ctx._invoke_capability(
            _READ_CAPABILITY,
            "current",
            {"umo": umo},
        )
        return result.get("conversation")

    async def get(self, umo: UMO, conversation_id: str) -> Conversation | None:
        """Return one conversation by ID."""
        result = await self._ctx._invoke_capability(
            _READ_CAPABILITY,
            "get",
            {"umo": umo, "conversation_id": conversation_id},
        )
        return result.get("conversation")

    async def list(
        self,
        umo: UMO,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> ConversationPage:
        """List the session's conversations, newest first."""
        result = await self._ctx._invoke_capability(
            _READ_CAPABILITY,
            "list",
            {"umo": umo, "cursor": cursor, "limit": limit},
        )
        return result["page"]

    async def create(
        self,
        umo: UMO,
        *,
        title: str | None = None,
        persona_id: str | None = None,
        messages: tuple[Message, ...] = (),
    ) -> Conversation:
        """Create a conversation and select it as the session's current one."""
        result = await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "create",
            {
                "umo": umo,
                "title": title,
                "persona_id": persona_id,
                "messages": list(messages),
            },
        )
        return result["conversation"]

    async def set_current(self, umo: UMO, conversation_id: str) -> None:
        """Select the session's current conversation."""
        await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "set_current",
            {"umo": umo, "conversation_id": conversation_id},
        )

    async def update(
        self,
        umo: UMO,
        conversation_id: str,
        patch: ConversationPatch,
        *,
        expected_revision: ConversationRevision | None = None,
    ) -> Conversation:
        """Apply a patch to one conversation and return the updated value."""
        result = await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "update",
            {
                "umo": umo,
                "conversation_id": conversation_id,
                "patch": patch,
                "expected_revision": expected_revision,
            },
        )
        return result["conversation"]

    async def append(
        self,
        umo: UMO,
        conversation_id: str,
        messages: tuple[Message, ...],
    ) -> None:
        """Append messages to one conversation's history."""
        await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "append",
            {
                "umo": umo,
                "conversation_id": conversation_id,
                "messages": list(messages),
            },
        )

    async def append_message(
        self,
        umo: UMO,
        conversation_id: str,
        content: str | tuple[ContentPart, ...],
        *,
        message_id: str | None = None,
    ) -> str:
        """Append a user message and return its committed ID."""
        return await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "append_message",
            {
                "umo": umo,
                "conversation_id": conversation_id,
                "content": content,
                "message_id": message_id,
            },
        )

    async def append_plugin_event(
        self,
        umo: UMO,
        conversation_id: str,
        event_type: str,
        data: dict[str, JSONValue],
        *,
        event_id: str | None = None,
    ) -> str:
        """Append a private plugin event outside the model context."""
        return await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "append_plugin_event",
            {
                "umo": umo,
                "conversation_id": conversation_id,
                "event_type": event_type,
                "data": data,
                "event_id": event_id,
            },
        )

    async def list_plugin_events(
        self,
        umo: UMO,
        conversation_id: str,
        event_type: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> tuple[ConversationEvent, ...]:
        """Read this plugin's events of one type after an exclusive sequence."""
        result = await self._ctx._invoke_capability(
            _READ_CAPABILITY,
            "list_plugin_events",
            {
                "umo": umo,
                "conversation_id": conversation_id,
                "event_type": event_type,
                "after_seq": after_seq,
                "limit": limit,
            },
        )
        return tuple(result)

    async def fork(self, umo: UMO, source_event_id: str) -> str:
        """Create a new conversation from a saved model-context event."""
        return await self._ctx._invoke_capability(
            _WRITE_CAPABILITY, "fork", {"umo": umo, "source_event_id": source_event_id}
        )

    async def delete(self, umo: UMO, conversation_id: str) -> None:
        """Delete one conversation."""
        await self._ctx._invoke_capability(
            _WRITE_CAPABILITY,
            "delete",
            {"umo": umo, "conversation_id": conversation_id},
        )
