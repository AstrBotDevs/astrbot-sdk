"""Legacy AstrMessageEvent facade and the filter decorator namespace."""

from __future__ import annotations

import enum
from typing import Any

from ...events import MessageEvent, SenderRole
from ...results import Propagation
from .components import (
    EventResultType as EventResultType,
)
from .components import (
    Image,
    MessageChain,
    MessageEventResult,
    Plain,
    from_sdk_segment,
)
from .components import (
    ResultContentType as ResultContentType,
)

# Marker attribute where compat filter decorators accumulate their specs.
_FILTERS_ATTR = "__astrbot_compat_filters__"


def _accumulate(handler: Any, filter_spec: Any) -> Any:
    filters = getattr(handler, _FILTERS_ATTR, None)
    if filters is None:
        filters = []
        setattr(handler, _FILTERS_ATTR, filters)
    filters.append(filter_spec)
    return handler


class CommandFilter:
    def __init__(self, command_name: str, alias: set | None = None) -> None:
        self.command_name = command_name
        self.alias = alias or set()


class RegexFilter:
    def __init__(self, regex: str) -> None:
        self.regex = regex


class EventMessageTypeFilter:
    def __init__(self, event_message_type: Any) -> None:
        self.event_message_type = event_message_type


class PermissionTypeFilter:
    def __init__(self, permission_type: Any, raise_error: bool = True) -> None:
        self.permission_type = permission_type
        self.raise_error = raise_error


class PlatformAdapterTypeFilter:
    def __init__(self, platform_adapter_type_or_str: Any) -> None:
        self.platform_type = platform_adapter_type_or_str


class CustomFilter:
    def __init__(self, custom_filter: Any) -> None:
        self.custom_filter = custom_filter


class _CommandGroup:
    """Legacy command-group handle returned by @filter.command_group.

    Sub-command decorators accumulate full-path CommandFilters
    ("group sub") on the target handler.
    """

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def command(self, name: str, alias: set | None = None, **_: Any) -> Any:
        full = f"{self.prefix} {name}"
        aliases = {f"{self.prefix} {a}" for a in (alias or set())}

        def decorator(handler: Any) -> Any:
            return _accumulate(handler, CommandFilter(full, aliases))

        return decorator

    def command_group(self, name: str, alias: set | None = None, **_: Any) -> Any:
        full = f"{self.prefix} {name}"

        def decorator(handler: Any) -> Any:
            _accumulate(handler, CommandFilter(full, alias))
            return _CommandGroup(full)

        return decorator

    def group(self, name: str, alias: set | None = None, **kwargs: Any) -> Any:
        """Legacy alias of command_group (RegisteringCommandable.group)."""
        return self.command_group(name, alias, **kwargs)


class HookMarker:
    """Marker accumulated by legacy hook decorators (on_llm_request etc.)."""

    def __init__(self, stage: str) -> None:
        self.stage = stage


class EventMessageType(enum.Flag):
    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class PermissionType(enum.Flag):
    ADMIN = enum.auto()
    MEMBER = enum.auto()
    GROUP_ADMIN = enum.auto()
    SHARED_GROUP_ADMIN = enum.auto()


class PlatformAdapterType(enum.Flag):
    AIOCQHTTP = enum.auto()
    QQOFFICIAL = enum.auto()
    QQOFFICIAL_WEBHOOK = enum.auto()
    TELEGRAM = enum.auto()
    WECOM = enum.auto()
    WECOM_AI_BOT = enum.auto()
    LARK = enum.auto()
    DINGTALK = enum.auto()
    DISCORD = enum.auto()
    SLACK = enum.auto()
    KOOK = enum.auto()
    VOCECHAT = enum.auto()
    WEIXIN_OFFICIAL_ACCOUNT = enum.auto()
    SATORI = enum.auto()
    MISSKEY = enum.auto()
    LINE = enum.auto()
    MATRIX = enum.auto()
    WEIXIN_OC = enum.auto()
    MATTERMOST = enum.auto()
    WEBCHAT = enum.auto()
    ALL = enum.auto()


# PlatformAdapterType member -> core adapter name, mirroring the old
# ADAPTER_NAME_2_TYPE table (used to build platform constraints).
PLATFORM_ADAPTER_NAMES = {
    PlatformAdapterType.AIOCQHTTP: "aiocqhttp",
    PlatformAdapterType.QQOFFICIAL: "qq_official",
    PlatformAdapterType.QQOFFICIAL_WEBHOOK: "qq_official_webhook",
    PlatformAdapterType.TELEGRAM: "telegram",
    PlatformAdapterType.WECOM: "wecom",
    PlatformAdapterType.WECOM_AI_BOT: "wecom_ai_bot",
    PlatformAdapterType.LARK: "lark",
    PlatformAdapterType.DINGTALK: "dingtalk",
    PlatformAdapterType.DISCORD: "discord",
    PlatformAdapterType.SLACK: "slack",
    PlatformAdapterType.KOOK: "kook",
    PlatformAdapterType.VOCECHAT: "vocechat",
    PlatformAdapterType.WEIXIN_OFFICIAL_ACCOUNT: "weixin_official_account",
    PlatformAdapterType.SATORI: "satori",
    PlatformAdapterType.MISSKEY: "misskey",
    PlatformAdapterType.LINE: "line",
    PlatformAdapterType.MATRIX: "matrix",
    PlatformAdapterType.WEIXIN_OC: "weixin_oc",
    PlatformAdapterType.MATTERMOST: "mattermost",
    PlatformAdapterType.WEBCHAT: "webchat",
}


class MessageType(enum.Enum):
    """Legacy platform message type."""

    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"
    OTHER_MESSAGE = "OtherMessage"


class MessageMember:
    """Legacy message sender record."""

    def __init__(self, user_id: str, nickname: str | None = None) -> None:
        self.user_id = user_id
        self.nickname = nickname

    def __str__(self) -> str:
        return f"User ID: {self.user_id}, Nickname: {self.nickname or 'N/A'}"


class AstrBotMessage:
    """Legacy raw message object (event.message_obj)."""

    def __init__(self, **kwargs: Any) -> None:
        self.type: Any = kwargs.get("type")
        self.self_id: str = kwargs.get("self_id", "")
        self.session_id: str = kwargs.get("session_id", "")
        self.message_id: str = kwargs.get("message_id", "")
        self.group_id: str = kwargs.get("group_id", "")
        self.sender: Any = kwargs.get("sender")
        self.message: list = kwargs.get("message", [])
        self.message_str: str = kwargs.get("message_str", "")
        self.raw_message: Any = kwargs.get("raw_message")
        self.timestamp: Any = kwargs.get("timestamp")


class _FilterNamespace:
    """astrbot.api.event.filter compat namespace."""

    EventMessageType = EventMessageType
    PermissionType = PermissionType
    PlatformAdapterType = PlatformAdapterType
    CustomFilter = CustomFilter

    def command(self, command_name: str, alias: set | None = None, **_: Any) -> Any:
        def decorator(handler: Any) -> Any:
            return _accumulate(handler, CommandFilter(command_name, alias))

        return decorator

    def command_group(
        self, command_name: str, alias: set | None = None, **_: Any
    ) -> Any:
        def decorator(handler: Any) -> Any:
            _accumulate(handler, CommandFilter(command_name, alias))
            return _CommandGroup(command_name)

        return decorator

    def regex(self, regex_str: str, **_: Any) -> Any:
        def decorator(handler: Any) -> Any:
            return _accumulate(handler, RegexFilter(regex_str))

        return decorator

    def event_message_type(self, event_message_type: Any, **_: Any) -> Any:
        def decorator(handler: Any) -> Any:
            return _accumulate(handler, EventMessageTypeFilter(event_message_type))

        return decorator

    def permission_type(
        self, permission_type: Any, raise_error: bool = True, **_: Any
    ) -> Any:
        def decorator(handler: Any) -> Any:
            return _accumulate(
                handler,
                PermissionTypeFilter(permission_type, raise_error),
            )

        return decorator

    def platform_adapter_type(self, platform_adapter_type_or_str: Any, **_: Any) -> Any:
        def decorator(handler: Any) -> Any:
            return _accumulate(
                handler,
                PlatformAdapterTypeFilter(platform_adapter_type_or_str),
            )

        return decorator

    def custom_filter(self, custom_filter: Any, *args: Any, **_: Any) -> Any:
        def decorator(handler: Any) -> Any:
            return _accumulate(handler, CustomFilter(custom_filter))

        return decorator

    # ---- Pipeline hooks -----------------------------------------------------

    def _hook(self, stage: str) -> Any:
        def decorator(handler: Any) -> Any:
            return _accumulate(handler, HookMarker(stage))

        return decorator

    def on_llm_request(self, *_args: Any, **_: Any) -> Any:
        return self._hook("llm_request")

    def on_llm_response(self, *_args: Any, **_: Any) -> Any:
        return self._hook("llm_response")

    def on_decorating_result(self, *_args: Any, **_: Any) -> Any:
        return self._hook("message_result")

    def after_message_sent(self, *_args: Any, **_: Any) -> Any:
        return self._hook("message_sent")

    def on_waiting_llm_request(self, *_args: Any, **_: Any) -> Any:
        return self._hook("waiting_llm_request")

    def on_using_llm_tool(self, *_args: Any, **_: Any) -> Any:
        return self._hook("tool_call")

    def on_llm_tool_respond(self, *_args: Any, **_: Any) -> Any:
        return self._hook("tool_result")

    def on_agent_begin(self, *_args: Any, **_: Any) -> Any:
        return self._hook("agent_start")

    def on_agent_done(self, *_args: Any, **_: Any) -> Any:
        return self._hook("agent_end")

    def on_plugin_error(self, *_args: Any, **_: Any) -> Any:
        return self._hook("plugin_error")

    def on_plugin_loaded(self, *_args: Any, **_: Any) -> Any:
        return self._hook("plugin_loaded")

    def on_plugin_unloaded(self, *_args: Any, **_: Any) -> Any:
        return self._hook("plugin_unloaded")

    def on_astrbot_loaded(self, *_args: Any, **_: Any) -> Any:
        return self._hook("lifecycle.startup")

    def on_platform_loaded(self, *_args: Any, **_: Any) -> Any:
        return self._hook("lifecycle.startup")

    def llm_tool(self, name: str | None = None, **_: Any) -> Any:
        def decorator(handler: Any) -> Any:
            return _accumulate(handler, LLMToolMarker(name or handler.__name__))

        return decorator


class LLMToolMarker:
    """Marker accumulated by the legacy llm_tool decorator."""

    def __init__(self, name: str) -> None:
        self.name = name


filter = _FilterNamespace()


class _CompatSender:
    def __init__(self, event: MessageEvent) -> None:
        self.user_id = event.sender.id
        self.nickname = event.sender.name


class _CompatMessageObj:
    def __init__(self, event: MessageEvent) -> None:
        self.message = [from_sdk_segment(s) for s in event.message]
        self.sender = _CompatSender(event)
        self.message_id = event.message_ref.id
        self.message_str = event.text
        self.type = {
            "private": MessageType.FRIEND_MESSAGE,
            "group": MessageType.GROUP_MESSAGE,
            "other": MessageType.OTHER_MESSAGE,
        }[event.umo.message_type.value]
        self.self_id = ""
        self.session_id = event.umo.session_id
        self.group_id = (
            event.umo.session_id if self.type is MessageType.GROUP_MESSAGE else ""
        )
        self.raw_message = None
        self.timestamp = event.timestamp


class AstrMessageEvent:
    """Facade over the immutable SDK MessageEvent with the old mutable API."""

    def __init__(self, event: MessageEvent, context: Any) -> None:
        self._event = event
        self._context = context
        self._result: MessageEventResult | None = None
        self._stopped = False
        self._extras: dict[str, Any] = {}
        self.message_obj = _CompatMessageObj(event)
        self.message_str = event.text
        self.is_wake = event.is_wake
        self.role = "admin" if event.sender.role is SenderRole.ADMIN else "member"

    # ---- identity helpers -------------------------------------------------

    @property
    def unified_msg_origin(self) -> str:
        from ...events import MessageType

        core_type = {
            MessageType.PRIVATE: "FriendMessage",
            MessageType.GROUP: "GroupMessage",
            MessageType.OTHER: "OtherMessage",
        }[self._event.umo.message_type]
        return f"{self._event.umo.platform_id}:{core_type}:{self._event.umo.session_id}"

    @property
    def session(self) -> str:
        return self.unified_msg_origin

    @property
    def session_id(self) -> str:
        return self._event.umo.session_id

    def get_sender_id(self) -> str:
        return self._event.sender.id

    def get_sender_name(self) -> str:
        return self._event.sender.name

    def get_message_str(self) -> str:
        return self._event.text

    def get_messages(self) -> list:
        return self.message_obj.message

    def is_private_chat(self) -> bool:
        return self._event.is_private

    def is_at_or_wake_command(self) -> bool:
        return self.is_wake

    def get_message_type(self) -> MessageType:
        """Return the legacy platform MessageType enum."""
        from ...events import MessageType as SDKMessageType

        return {
            SDKMessageType.GROUP: MessageType.GROUP_MESSAGE,
            SDKMessageType.PRIVATE: MessageType.FRIEND_MESSAGE,
            SDKMessageType.OTHER: MessageType.OTHER_MESSAGE,
        }[self._event.umo.message_type]

    def get_platform_name(self) -> str:
        """Return the adapter name (e.g. aiocqhttp)."""
        return self._event.platform_type

    def get_platform_id(self) -> str:
        return self._event.umo.platform_id

    def get_session_id(self) -> str:
        return self._event.umo.session_id

    def get_group_id(self) -> str:
        """Return the group id for group messages, else empty."""
        from ...events import MessageType as SDKMessageType

        if self._event.umo.message_type is SDKMessageType.GROUP:
            return self._event.umo.session_id
        return ""

    def get_self_id(self) -> str:
        """Bot self id is Host-side; unavailable across the boundary."""
        return ""

    def get_message_outline(self) -> str:
        """Return a short outline of the message chain."""
        parts = []
        for segment in self._event.message:
            text = getattr(segment, "text", None)
            if text:
                parts.append(text)
            else:
                parts.append(f"[{getattr(segment, 'type', '?')}]")
        return "".join(parts)

    def is_wake_up(self) -> bool:
        return self.is_wake

    def is_admin(self) -> bool:
        return self.role == "admin"

    # ---- result management -------------------------------------------------

    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult().message(text)

    def chain_result(self, chain: MessageChain | list) -> MessageEventResult:
        return MessageEventResult().set_chain(chain)

    def image_result(self, path_or_url: str) -> MessageEventResult:
        result = MessageEventResult()
        result.chain = [Image(file=path_or_url)]
        return result

    def set_result(self, result: MessageEventResult) -> None:
        if isinstance(result, str):
            result = self.plain_result(result)
        self._result = result

    def get_result(self) -> MessageEventResult | None:
        return self._result

    def clear_result(self) -> None:
        self._result = None

    def make_result(self) -> MessageEventResult:
        """Create an empty result, install it, and return it."""
        result = MessageEventResult()
        self._result = result
        return result

    def stop_event(self) -> None:
        self._stopped = True

    def continue_event(self) -> None:
        self._stopped = False

    def is_stopped(self) -> bool:
        return self._stopped

    # ---- extras and proactive send -----------------------------------------

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    def get_extra(self, key: str | None = None, default: Any = None) -> Any:
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def clear_extra(self) -> None:
        self._extras.clear()

    async def send(self, message: MessageChain | MessageEventResult | str) -> None:
        """Proactively send a message to this event's session."""
        from ...messages import to_message_chain

        if isinstance(message, MessageEventResult):
            chain = MessageChain(chain=message.chain).to_sdk()
        elif isinstance(message, MessageChain):
            chain = message.to_sdk()
        else:
            chain = to_message_chain(message)
        await self._context._ctx.messages.send(self._event.umo, chain)

    def request_llm(
        self,
        prompt: str,
        func_tool_manager: Any = None,
        tool_set: Any = None,
        session_id: str = "",
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        contexts: list | None = None,
        system_prompt: str = "",
        conversation: Any = None,
    ) -> Any:
        """Build a legacy ProviderRequest to yield as the handler result.

        The compat wrapper executes it via the provider facade and yields the
        completion as a plain result.
        """
        from .provider import ProviderRequest

        if contexts and conversation:
            conversation = None
        return ProviderRequest(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls or [],
            audio_urls=audio_urls or [],
            func_tool=tool_set,
            contexts=contexts or [],
            system_prompt=system_prompt,
            conversation=conversation,
        )


def translate_compat_result(
    item: Any,
    event: AstrMessageEvent,
) -> Any:
    """Translate one yielded legacy value into an SDK EventResult-ish value."""
    from ...results import MessageResult

    if item is None:
        return None
    if isinstance(item, MessageEventResult):
        sdk_result = item.to_sdk_result()
        return sdk_result
    if isinstance(item, MessageChain):
        return MessageResult(
            propagation=(
                Propagation.STOP if event.is_stopped() else Propagation.CONTINUE
            ),
            message=item.to_sdk(),
        )
    if isinstance(item, str):
        return MessageResult(
            propagation=(
                Propagation.STOP if event.is_stopped() else Propagation.CONTINUE
            ),
            message=MessageChain(chain=[Plain(item)]).to_sdk(),
        )
    return item
