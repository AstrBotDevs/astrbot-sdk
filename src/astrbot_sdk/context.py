from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .capabilities import CapabilitySet
from .conversations import ConversationService
from .errors import CapabilityDenied, HostUnavailable, RemoteError, map_remote_error
from .llm import LLMService
from .plugins import PluginRegistryService
from .protocol_registry import register_protocol_dataclass
from .render import RenderService
from .services import AssetService, MessageService, PluginStorage, SessionService
from .tools import ToolService
from .web import WebService

_AMBIENT_UMO: ContextVar[Any] = ContextVar("ambient_umo", default=None)
_AMBIENT_EVENT: ContextVar[Any] = ContextVar("ambient_event", default=None)

HostCapabilityInvoker = Callable[
    [str, str, Mapping[str, Any]],
    Awaitable[Any],
]
HostCapabilityStreamInvoker = Callable[
    [str, str, Mapping[str, Any]],
    AsyncIterator[Any],
]


class RuntimeMode(StrEnum):
    ISOLATED = "isolated"
    IN_PROCESS = "in_process"


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class PluginInfo:
    id: str
    name: str
    version: str
    runtime_mode: RuntimeMode
    author: str | None = None
    desc: str | None = None
    activated: bool = True


@dataclass(slots=True)
class PluginContext[ConfigT]:
    plugin: PluginInfo
    config: ConfigT
    logger: logging.Logger
    capabilities: CapabilitySet
    data_dir: Path = Path(".")
    storage: PluginStorage = field(init=False)
    messages: MessageService = field(init=False)
    conversations: ConversationService = field(init=False)
    assets: AssetService = field(init=False)
    plugins: PluginRegistryService = field(init=False)
    render: RenderService = field(init=False)
    sessions: SessionService = field(init=False)
    tools: ToolService = field(init=False)
    llm: LLMService = field(init=False)
    web: WebService = field(init=False)
    _host_capability_invoker: HostCapabilityInvoker | None = field(
        default=None,
        repr=False,
    )
    _host_capability_stream_invoker: HostCapabilityStreamInvoker | None = field(
        default=None,
        repr=False,
    )
    dynamic_tools: dict[str, Callable[..., Awaitable[Any]]] = field(
        default_factory=dict,
        repr=False,
    )
    cron_handlers: dict[str, Callable[..., Any]] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        # Build stable Host service facades. Services never disappear from the
        # context; missing grants surface as CapabilityDenied at call time.
        self.storage = PluginStorage(self, self.data_dir)
        self.messages = MessageService(self)
        self.conversations = ConversationService(self)
        self.assets = AssetService(self)
        self.plugins = PluginRegistryService(self)
        self.render = RenderService(self)
        self.sessions = SessionService(self)
        self.tools = ToolService(self)
        self.llm = LLMService(self)
        self.web = WebService(self)

    def _register_dynamic_tool(
        self,
        handler_id: str,
        handler: Callable[..., Awaitable[Any]],
    ) -> None:
        """Register one dynamic tool handler on this context."""
        self.dynamic_tools[handler_id] = handler

    def _unregister_dynamic_tool(self, handler_id: str) -> None:
        """Remove one dynamic tool handler from this context."""
        self.dynamic_tools.pop(handler_id, None)

    async def _invoke_capability(
        self,
        capability_id: str,
        operation: str,
        payload: Mapping[str, Any],
    ) -> Any:
        """Invoke a Host capability for an SDK service implementation.

        Args:
            capability_id: Capability required by the operation.
            operation: Operation within the capability namespace.
            payload: Operation input DTOs.

        Returns:
            Decoded Host response.

        Raises:
            CapabilityDenied: The plugin was not granted the capability.
            HostUnavailable: No Host invocation channel is available.
        """
        if not self.capabilities.has(capability_id):
            raise CapabilityDenied(f"capability was not granted: {capability_id}")
        if self._host_capability_invoker is None:
            raise HostUnavailable("Host capability channel is unavailable")
        try:
            return await self._host_capability_invoker(
                capability_id,
                operation,
                payload,
            )
        except RemoteError as exc:
            raise map_remote_error(exc) from exc

    def _bind_ambient_umo(self, umo: Any) -> Token:
        """Bind a UMO to the current invocation for provider selection."""
        return _AMBIENT_UMO.set(umo)

    def _reset_ambient_umo(self, token: Token) -> None:
        """Reset the ambient UMO binding."""
        _AMBIENT_UMO.reset(token)

    def _ambient_umo(self) -> Any:
        """Return the UMO bound to the current invocation, if any."""
        return _AMBIENT_UMO.get()

    def _bind_ambient_event(self, event: Any) -> Token:
        """Bind the triggering event to the current invocation."""
        return _AMBIENT_EVENT.set(event)

    def _reset_ambient_event(self, token: Token) -> None:
        """Reset the ambient event binding."""
        _AMBIENT_EVENT.reset(token)

    def _ambient_event(self) -> Any:
        """Return the event bound to the current invocation, if any."""
        return _AMBIENT_EVENT.get()

    def _invoke_capability_stream(
        self,
        capability_id: str,
        operation: str,
        payload: Mapping[str, Any],
    ) -> AsyncIterator[Any]:
        """Invoke a Host capability as a flow-controlled stream.

        Args:
            capability_id: Capability required by the operation.
            operation: Operation within the capability namespace.
            payload: Operation input DTOs.

        Returns:
            Async iterator of decoded Host stream items.

        Raises:
            CapabilityDenied: The plugin was not granted the capability.
            HostUnavailable: No Host streaming channel is available.
        """
        if not self.capabilities.has(capability_id):
            raise CapabilityDenied(f"capability was not granted: {capability_id}")
        if self._host_capability_stream_invoker is None:
            raise HostUnavailable("Host capability streaming channel is unavailable")
        return self._host_capability_stream_invoker(
            capability_id,
            operation,
            payload,
        )
