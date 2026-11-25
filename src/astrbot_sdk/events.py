from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .message_components import ButtonInteraction, MessageSegment, UIInteraction
from .messages import MessageChain, MessageLike
from .protocol_registry import JSONValue
from .results import EventResult, MessageResult, Propagation, message_result

if TYPE_CHECKING:
    from .chatui import ViewResult


class MessageType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    OTHER = "other"


class SenderRole(StrEnum):
    MEMBER = "member"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class UMO:
    platform_id: str
    message_type: MessageType
    session_id: str

    def __str__(self) -> str:
        message_type = {
            MessageType.PRIVATE: "FriendMessage",
            MessageType.GROUP: "GroupMessage",
            MessageType.OTHER: "OtherMessage",
        }[self.message_type]
        return f"{self.platform_id}:{message_type}:{self.session_id}"


@dataclass(frozen=True, slots=True)
class MessageRef:
    id: str


@dataclass(frozen=True, slots=True)
class Sender:
    id: str
    name: str
    role: SenderRole = SenderRole.MEMBER


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    path: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "arguments",
            MappingProxyType(dict(self.arguments)),
        )


@dataclass(frozen=True, slots=True)
class MessageEvent:
    id: str
    umo: UMO
    platform_type: str
    message_ref: MessageRef
    message: MessageChain
    sender: Sender
    timestamp: datetime
    command: CommandInvocation | None = None
    is_wake: bool = False
    _source_token: str | None = field(default=None, repr=False, compare=False)
    agent_metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    _message_str: str | None = field(default=None, repr=False)

    @property
    def unified_msg_origin(self) -> str:
        """Return the Core-style session address."""
        return str(self.umo)

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def message_str(self) -> str:
        """Return the text captured from the Core event."""
        return self.text if self._message_str is None else self._message_str

    @property
    def role(self) -> str:
        return self.sender.role.value

    @property
    def is_private(self) -> bool:
        return self.umo.message_type is MessageType.PRIVATE

    @property
    def is_group(self) -> bool:
        return self.umo.message_type is MessageType.GROUP

    def get_message_str(self) -> str:
        return self.message_str

    def get_messages(self) -> list[MessageSegment]:
        return list(self.message)

    def get_sender_id(self) -> str:
        return self.sender.id

    def get_sender_name(self) -> str:
        return self.sender.name

    def is_private_chat(self) -> bool:
        return self.is_private

    def get_message_type(self) -> MessageType:
        return self.umo.message_type

    def get_platform_name(self) -> str:
        return self.platform_type

    def get_platform_id(self) -> str:
        return self.umo.platform_id

    def reply(
        self,
        content: MessageLike,
        *,
        quote: bool = False,
        propagation: Propagation = Propagation.CONTINUE,
    ) -> MessageResult:
        return message_result(content, quote=quote, propagation=propagation)

    def get_button_interaction(self) -> ButtonInteraction | None:
        return next(
            (part for part in self.message if isinstance(part, ButtonInteraction)), None
        )

    def get_ui_interaction(self) -> UIInteraction | None:
        return next(
            (part for part in self.message if isinstance(part, UIInteraction)), None
        )

    def view_result(
        self,
        *,
        data: dict[str, JSONValue],
        name: str | None = None,
        text: str = "",
        title: str = "",
    ) -> ViewResult:
        """Create a named card or replace the card initiating this action."""
        from .chatui import ViewResult

        if name is None and self.get_ui_interaction() is None:
            raise ValueError("updating a view requires a UI interaction")
        return ViewResult(data=data, name=name, text=text, title=title)

    def stop(self) -> EventResult:
        return EventResult(propagation=Propagation.STOP)
