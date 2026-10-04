"""Install the astrbot.api compat namespace into sys.modules.

Old plugins import from astrbot.api.*; in the isolated legacy Runner those
imports must resolve to the compat implementation, and imports of the real
astrbot.core.* must fail loudly (isolation would be fake otherwise).
"""

from __future__ import annotations

import logging
import sys
from types import ModuleType
from typing import Any

_installed = False


class _ContextWrapper:
    """Legacy ContextWrapper shell; generic subscription is a no-op."""

    def __init__(self, context: Any = None, **kwargs: Any) -> None:
        self.context = context
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __class_getitem__(cls, item: Any) -> type:
        return cls


class _AstrAgentContext:
    """Legacy AstrAgentContext shell (event/context carrier)."""

    def __init__(self, context: Any = None, event: Any = None, **_: Any) -> None:
        self.context = context
        self.event = event


class _AgentMessage:
    """Legacy agent message shell (OpenAI-format dict behaviour)."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def model_dump(self, **_: Any) -> dict:
        return dict(self.__dict__)


class _AgentPart(_AgentMessage):
    """Legacy agent content-part shell."""


class _ShellRecord:
    """Generic shell for type-only records referenced by legacy plugins."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __class_getitem__(cls, item: Any) -> type:
        return cls


def _namespace_package(name: str) -> ModuleType:
    """Build one empty package placeholder for whitelisted submodules."""
    module = ModuleType(name)
    module.__path__ = []
    return module


class _BlockedModule(ModuleType):
    """Module placeholder that fails loudly when touched."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.__path__ = []  # mark as package so submodule imports reach us

    def __getattr__(self, name: str) -> Any:
        raise ImportError(
            f"{self.__name__} is unavailable in isolated legacy mode; "
            "plugins importing it must run in-process"
        )


class _SharedPreferences:
    """Legacy sp facade backed by plugin-scoped KV storage."""

    _ctx: Any = None

    @classmethod
    def initialize(cls, ctx: Any) -> None:
        """Bind the plugin context (called once by the compat loader)."""
        cls._ctx = ctx

    def _storage(self) -> Any:
        if _SharedPreferences._ctx is None:
            raise ValueError("sp not initialized")
        return _SharedPreferences._ctx.storage

    async def get(self, scope: str, key: str, default: Any = None) -> Any:
        return await self._storage().get(f"sp:{scope}:{key}", default)

    async def put(self, scope: str, key: str, value: Any) -> None:
        await self._storage().set(f"sp:{scope}:{key}", value)

    async def remove(self, scope: str, key: str) -> None:
        await self._storage().delete(f"sp:{scope}:{key}")

    async def session_get(
        self,
        umo: str,
        key: str,
        default: Any = None,
        **_: Any,
    ) -> Any:
        return await self._storage().get(f"sp:session:{umo}:{key}", default)

    async def session_put(self, umo: str, key: str, value: Any) -> None:
        await self._storage().set(f"sp:session:{umo}:{key}", value)


def _unsupported(feature: str) -> Any:
    def raise_loud(*args: Any, **kwargs: Any) -> Any:
        from .errors import IsolationUnsupportedError

        raise IsolationUnsupportedError(
            f"{feature} is unavailable in isolated legacy mode; "
            "plugins needing it must run in-process"
        )

    return raise_loud


def _unsupported_agent(*args: Any, **kwargs: Any) -> Any:
    return _unsupported("custom agent registration (@agent)")(*args, **kwargs)


def install() -> None:
    """Install the compat astrbot.api namespace and the core blocker."""
    global _installed
    if _installed:
        return

    from . import components, event, html, loader, po, provider, star, tools, web

    astrbot_mod = ModuleType("astrbot")
    astrbot_mod.__path__ = []
    astrbot_mod.logger = logging.getLogger("astrbot.compat")

    api_mod = ModuleType("astrbot.api")
    api_mod.__path__ = []
    api_mod.logger = astrbot_mod.logger
    api_mod.AstrBotConfig = dict
    api_mod.FunctionTool = tools.FunctionTool
    api_mod.ToolSet = tools.ToolSet
    api_mod.BaseFunctionToolExecutor = type("BaseFunctionToolExecutor", (), {})
    api_mod.llm_tool = event.filter.llm_tool
    api_mod.agent = _unsupported_agent
    api_mod.html_renderer = html.HtmlRendererFacade()
    api_mod.sp = _SharedPreferences()

    event_mod = ModuleType("astrbot.api.event")
    event_mod.__path__ = []
    event_mod.filter = event.filter
    event_mod.AstrMessageEvent = event.AstrMessageEvent
    event_mod.MessageChain = components.MessageChain
    event_mod.MessageEventResult = components.MessageEventResult
    event_mod.ResultContentType = event.ResultContentType

    filter_mod = ModuleType("astrbot.api.event.filter")
    for name in dir(event.filter):
        if not name.startswith("_"):
            setattr(filter_mod, name, getattr(event.filter, name))
    filter_mod.GreedyStr = loader.GreedyStr

    star_mod = ModuleType("astrbot.api.star")
    star_mod.Star = star.Star
    star_mod.Context = star.Context
    star_mod.MessageSesion = star.MessageSesion
    star_mod.StarTools = star.StarTools
    star_mod.register = star.register

    tools_mod = ModuleType("astrbot.core.agent.tool")
    tools_mod.FunctionTool = tools.FunctionTool
    tools_mod.ToolSet = tools.ToolSet
    tools_mod.ToolExecResult = Any

    star_context_mod = ModuleType("astrbot.core.star.context")
    star_context_mod.Context = star.Context

    provider_register_mod = ModuleType("astrbot.core.provider.register")
    provider_register_mod.register_provider = _unsupported("register_provider")
    provider_register_mod.llm_tools = type("llm_tools", (), {})()

    custom_filter_mod = ModuleType("astrbot.core.star.filter.custom_filter")
    custom_filter_mod.CustomFilter = event.CustomFilter

    provider_provider_mod = ModuleType("astrbot.core.provider.provider")
    provider_provider_mod.Provider = provider.Provider
    provider_provider_mod.STTProvider = provider.Provider
    provider_provider_mod.TTSProvider = provider.Provider
    provider_provider_mod.EmbeddingProvider = provider.Provider

    platform_metadata_mod = ModuleType("astrbot.core.platform.platform_metadata")
    platform_metadata_mod.PlatformMetadata = _ShellRecord

    platform_platform_mod = ModuleType("astrbot.core.platform.platform")
    platform_platform_mod.Platform = _ShellRecord

    config_default_mod = ModuleType("astrbot.core.config.default")
    config_default_mod.DEFAULT_CONFIG = {}
    config_default_mod.CONFIG_METADATA_2 = {}

    platform_adapter_type_mod = ModuleType(
        "astrbot.core.star.filter.platform_adapter_type",
    )
    platform_adapter_type_mod.PlatformAdapterType = event.PlatformAdapterType
    platform_adapter_type_mod.PlatformAdapterTypeFilter = (
        event.PlatformAdapterTypeFilter
    )
    platform_adapter_type_mod.ADAPTER_NAME_2_TYPE = {
        name: member for member, name in event.PLATFORM_ADAPTER_NAMES.items()
    }

    star_star_mod = ModuleType("astrbot.core.star.star")
    star_star_mod.Star = star.Star
    star_star_mod.StarMetadata = _ShellRecord
    # Registry introspection is Host-internal; isolated plugins see empty
    # registries (degraded but import-safe).
    star_star_mod.star_map = {}
    star_star_mod.star_registry = []

    run_context_mod = ModuleType("astrbot.core.agent.run_context")
    run_context_mod.ContextWrapper = _ContextWrapper

    agent_context_mod = ModuleType("astrbot.core.astr_agent_context")
    agent_context_mod.AstrAgentContext = _AstrAgentContext

    po_mod = ModuleType("astrbot.core.db.po")
    po_mod.Conversation = po.Conversation
    po_mod.Personality = po.Personality
    po_mod.CronJob = po.CronJob

    register_mod = ModuleType("astrbot.core.star.register")
    register_mod.register_star = star.register
    register_mod.register_llm_tool = event.filter.llm_tool
    register_mod.register_command = event.filter.command
    register_mod.register_command_group = event.filter.command_group
    register_mod.register_regex = event.filter.regex
    register_mod.register_event_message_type = event.filter.event_message_type
    register_mod.register_permission_type = event.filter.permission_type
    register_mod.register_platform_adapter_type = event.filter.platform_adapter_type
    register_mod.register_custom_filter = event.filter.custom_filter
    register_mod.register_agent = _unsupported_agent
    for hook_name in (
        "on_llm_request",
        "on_llm_response",
        "on_decorating_result",
        "after_message_sent",
        "on_waiting_llm_request",
        "on_using_llm_tool",
        "on_llm_tool_respond",
        "on_agent_begin",
        "on_agent_done",
        "on_plugin_error",
        "on_plugin_loaded",
        "on_plugin_unloaded",
        "on_astrbot_loaded",
        "on_platform_loaded",
    ):
        setattr(
            register_mod,
            f"register_{hook_name}",
            getattr(event.filter, hook_name),
        )

    entities_mod = ModuleType("astrbot.core.provider.entities")
    entities_mod.LLMResponse = provider.LLMResponse
    entities_mod.ProviderRequest = provider.ProviderRequest
    entities_mod.ProviderMetaData = provider.ProviderMetaData
    entities_mod.ProviderType = provider.ProviderType
    entities_mod.EmbeddingProvider = provider.Provider
    entities_mod.TokenUsage = _ShellRecord

    conversation_mgr_mod = ModuleType("astrbot.core.conversation_mgr")
    from .conversation import ConversationManager

    conversation_mgr_mod.ConversationManager = ConversationManager
    conversation_mgr_mod.Conversation = po.Conversation

    agent_hooks_mod = ModuleType("astrbot.core.agent.hooks")
    agent_hooks_mod.BaseAgentRunHooks = _ShellRecord

    regex_filter_mod = ModuleType("astrbot.core.star.filter.regex")
    regex_filter_mod.RegexFilter = event.RegexFilter

    message_result_mod = ModuleType("astrbot.core.message.message_event_result")
    message_result_mod.MessageEventResult = components.MessageEventResult
    message_result_mod.MessageChain = components.MessageChain
    message_result_mod.CommandResult = components.MessageEventResult
    message_result_mod.EventResultType = event.EventResultType
    message_result_mod.ResultContentType = event.ResultContentType

    platform_mod = ModuleType("astrbot.core.platform")
    platform_mod.__path__ = []
    platform_mod.AstrMessageEvent = event.AstrMessageEvent
    platform_mod.MessageType = event.MessageType
    platform_mod.MessageMember = event.MessageMember
    platform_mod.AstrBotMessage = event.AstrBotMessage
    platform_mod.MessageSesion = star.MessageSesion

    command_filter_mod = ModuleType("astrbot.core.star.filter.command")
    command_filter_mod.GreedyStr = loader.GreedyStr
    command_filter_mod.CommandFilter = event.CommandFilter

    permission_mod = ModuleType("astrbot.core.star.filter.permission")
    permission_mod.PermissionType = event.PermissionType
    permission_mod.PermissionTypeFilter = event.PermissionTypeFilter

    event_message_type_mod = ModuleType(
        "astrbot.core.star.filter.event_message_type",
    )
    event_message_type_mod.EventMessageType = event.EventMessageType
    event_message_type_mod.EventMessageTypeFilter = event.EventMessageTypeFilter

    message_components_mod = ModuleType("astrbot.core.message.components")
    message_components_mod.BaseMessageComponent = components.BaseMessageComponent
    for name in (
        "Plain",
        "At",
        "AtAll",
        "Reply",
        "Image",
        "Record",
        "Video",
        "Face",
        "Forward",
        "File",
        "Node",
        "Nodes",
        "Poke",
        "Json",
        "Share",
    ):
        setattr(message_components_mod, name, getattr(components, name))

    config_mod = _namespace_package("astrbot.core.config")
    config_mod.AstrBotConfig = dict
    astrbot_config_mod = ModuleType("astrbot.core.config.astrbot_config")
    astrbot_config_mod.AstrBotConfig = dict

    core_star_mod = _namespace_package("astrbot.core.star")
    core_star_mod.Star = star.Star
    core_star_mod.Context = star.Context
    core_star_mod.StarTools = star.StarTools
    core_star_mod.register = star.register

    astrbot_message_mod = ModuleType("astrbot.core.platform.astrbot_message")
    astrbot_message_mod.AstrBotMessage = event.AstrBotMessage
    astrbot_message_mod.MessageMember = event.MessageMember
    astrbot_message_mod.MessageType = event.MessageType
    astrbot_message_mod.MessageSesion = star.MessageSesion
    astrbot_message_mod.MessageSession = star.MessageSesion

    message_session_mod = ModuleType("astrbot.core.platform.message_session")
    message_session_mod.MessageSesion = star.MessageSesion
    message_session_mod.MessageSession = star.MessageSesion

    agent_message_mod = ModuleType("astrbot.core.agent.message")
    agent_message_mod.Message = _AgentMessage
    agent_message_mod.UserMessageSegment = _AgentMessage
    agent_message_mod.AssistantMessageSegment = _AgentMessage
    agent_message_mod.SystemMessageSegment = _AgentMessage
    agent_message_mod.ToolCallMessageSegment = _AgentMessage
    agent_message_mod.TextPart = _AgentPart
    agent_message_mod.ImageURLPart = _AgentPart
    agent_message_mod.AudioURLPart = _AgentPart
    agent_message_mod.ToolCall = _AgentPart

    api_platform_mod = ModuleType("astrbot.api.platform")
    api_platform_mod.AstrBotMessage = event.AstrBotMessage
    api_platform_mod.AstrMessageEvent = event.AstrMessageEvent
    api_platform_mod.MessageMember = event.MessageMember
    api_platform_mod.MessageType = event.MessageType
    api_platform_mod.Platform = _ShellRecord
    api_platform_mod.PlatformMetadata = _ShellRecord
    api_platform_mod.register_platform_adapter = _unsupported(
        "register_platform_adapter"
    )
    for name in (
        "BaseMessageComponent",
        "Plain",
        "At",
        "AtAll",
        "Reply",
        "Image",
        "Record",
        "Video",
        "Face",
        "Forward",
        "File",
        "Node",
        "Nodes",
        "Poke",
        "Json",
        "Share",
        "MessageChain",
    ):
        setattr(api_platform_mod, name, getattr(components, name))

    command_group_mod = ModuleType("astrbot.core.star.filter.command_group")
    command_group_mod.CommandGroupFilter = event.CommandFilter

    message_type_mod = ModuleType("astrbot.core.platform.message_type")
    message_type_mod.MessageType = event.MessageType

    astr_message_event_mod = ModuleType(
        "astrbot.core.platform.astr_message_event",
    )
    astr_message_event_mod.AstrMessageEvent = event.AstrMessageEvent
    astr_message_event_mod.MessageSesion = star.MessageSesion
    astr_message_event_mod.MessageSession = star.MessageSesion

    star_tools_mod = ModuleType("astrbot.core.star.star_tools")
    star_tools_mod.StarTools = star.StarTools

    star_handler_mod = ModuleType("astrbot.core.star.star_handler")
    # Registry introspection is Host-internal; isolated plugins see an empty
    # registry (degraded but import-safe).
    star_handler_mod.star_handlers_registry = []
    star_handler_mod.StarHandlerMetadata = _ShellRecord
    star_handler_mod.EventType = _ShellRecord

    astrbot_path_mod = ModuleType("astrbot.core.utils.astrbot_path")

    def _data_path() -> str:
        import os

        return os.environ.get("ASTRBOT_DATA_PATH", ".")

    astrbot_path_mod.get_astrbot_data_path = _data_path

    provider_mod = ModuleType("astrbot.api.provider")
    provider_mod.LLMResponse = provider.LLMResponse
    provider_mod.Personality = provider.Personality
    provider_mod.Provider = provider.Provider
    provider_mod.ProviderMetaData = provider.ProviderMetaData
    provider_mod.ProviderRequest = provider.ProviderRequest
    provider_mod.ProviderType = provider.ProviderType
    provider_mod.STTProvider = provider.Provider

    components_mod = ModuleType("astrbot.api.message_components")
    for name in (
        "ComponentType",
        "BaseMessageComponent",
        "Plain",
        "At",
        "AtAll",
        "Reply",
        "Image",
        "Record",
        "Video",
        "Face",
        "Forward",
        "File",
        "Node",
        "Nodes",
        "Poke",
        "Json",
        "Share",
        "MessageChain",
    ):
        setattr(components_mod, name, getattr(components, name))

    message_mod = ModuleType("astrbot.api.message")
    message_mod.MessageEventResult = components.MessageEventResult
    message_mod.MessageChain = components.MessageChain

    web_mod = ModuleType("astrbot.api.web")
    web_mod.request = web.ApiWebRequestProxy()
    web_mod.PluginMultiDict = web.PluginMultiDict
    web_mod.PluginUploadFile = web.PluginUploadFile
    try:
        from starlette.responses import (
            FileResponse,
            JSONResponse,
            StreamingResponse,
        )

        web_mod.FileResponse = FileResponse
        web_mod.JSONResponse = JSONResponse
        web_mod.StreamingResponse = StreamingResponse
    except ImportError:
        pass

    all_mod = ModuleType("astrbot.api.all")
    all_mod.AstrBotConfig = dict
    all_mod.logger = astrbot_mod.logger
    # The old api.all leaks os into its namespace; plugins star-import it.
    import os as _os

    all_mod.os = _os
    all_mod.html_renderer = html.HtmlRendererFacade()
    all_mod.llm_tool = event_mod.filter.llm_tool
    all_mod.MessageEventResult = components.MessageEventResult
    all_mod.MessageChain = components.MessageChain
    all_mod.CommandResult = components.MessageEventResult
    all_mod.EventResultType = event.EventResultType
    all_mod.AstrMessageEvent = event.AstrMessageEvent
    all_mod.command = event_mod.filter.command
    all_mod.command_group = event_mod.filter.command_group
    all_mod.event_message_type = event_mod.filter.event_message_type
    all_mod.regex = event_mod.filter.regex
    all_mod.platform_adapter_type = event_mod.filter.platform_adapter_type
    all_mod.EventMessageType = event.EventMessageType
    all_mod.EventMessageTypeFilter = event.EventMessageTypeFilter
    all_mod.PlatformAdapterType = event.PlatformAdapterType
    all_mod.PlatformAdapterTypeFilter = event.PlatformAdapterTypeFilter
    all_mod.register = star.register
    all_mod.Context = star.Context
    all_mod.Star = star.Star
    all_mod.Provider = provider.Provider
    all_mod.ProviderMetaData = provider.ProviderMetaData
    all_mod.Personality = po.Personality
    all_mod.AstrBotMessage = event.AstrBotMessage
    all_mod.MessageMember = event.MessageMember
    all_mod.MessageType = event.MessageType
    for name in (
        "BaseMessageComponent",
        "Plain",
        "At",
        "AtAll",
        "Reply",
        "Image",
        "Record",
        "Video",
        "Face",
        "Forward",
        "File",
        "Node",
        "Nodes",
        "Poke",
        "Json",
        "Share",
        "MessageChain",
    ):
        setattr(all_mod, name, getattr(components_mod, name))

    astrbot_mod.api = api_mod
    api_mod.event = event_mod
    api_mod.star = star_mod
    api_mod.provider = provider_mod
    api_mod.message_components = components_mod
    api_mod.message = message_mod
    api_mod.all = all_mod

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.event.filter"] = filter_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.provider"] = provider_mod
    sys.modules["astrbot.api.message_components"] = components_mod
    sys.modules["astrbot.api.message"] = message_mod
    sys.modules["astrbot.api.all"] = all_mod
    sys.modules["astrbot.api.web"] = web_mod

    # Type-only astrbot.core submodules are safe to expose; everything else
    # under astrbot.core stays blocked (live Host internals).
    sys.modules["astrbot.core.agent"] = _namespace_package("astrbot.core.agent")
    sys.modules["astrbot.core.agent.tool"] = tools_mod
    sys.modules["astrbot.core.star.context"] = star_context_mod
    sys.modules["astrbot.core.provider.register"] = provider_register_mod
    sys.modules["astrbot.core.star.filter.custom_filter"] = custom_filter_mod
    sys.modules["astrbot.core.provider.provider"] = provider_provider_mod
    sys.modules["astrbot.core.platform.platform_metadata"] = platform_metadata_mod
    sys.modules["astrbot.core.platform.platform"] = platform_platform_mod
    sys.modules["astrbot.core.config.default"] = config_default_mod
    sys.modules["astrbot.core.star.filter.platform_adapter_type"] = (
        platform_adapter_type_mod
    )
    sys.modules["astrbot.core.star.star"] = star_star_mod
    sys.modules["astrbot.core.agent.run_context"] = run_context_mod
    sys.modules["astrbot.core.astr_agent_context"] = agent_context_mod
    sys.modules["astrbot.core.db"] = _namespace_package("astrbot.core.db")
    sys.modules["astrbot.core.db.po"] = po_mod
    core_provider_pkg = _namespace_package("astrbot.core.provider")
    core_provider_pkg.Provider = provider.Provider
    core_provider_pkg.STTProvider = provider.Provider
    core_provider_pkg.TTSProvider = provider.Provider
    core_provider_pkg.EmbeddingProvider = provider.Provider
    core_provider_pkg.ProviderMetaData = provider.ProviderMetaData
    sys.modules["astrbot.core.provider"] = core_provider_pkg
    sys.modules["astrbot.core.provider.entities"] = entities_mod
    # Some plugins import through the historical "entites" typo path.
    sys.modules["astrbot.core.provider.entites"] = entities_mod
    sys.modules["astrbot.core.conversation_mgr"] = conversation_mgr_mod
    sys.modules["astrbot.core.agent.hooks"] = agent_hooks_mod
    sys.modules["astrbot.core.star.filter.regex"] = regex_filter_mod
    sys.modules["astrbot.core.message"] = _namespace_package("astrbot.core.message")
    sys.modules["astrbot.core.message.message_event_result"] = message_result_mod
    sys.modules["astrbot.core.platform"] = platform_mod
    sys.modules["astrbot.core.platform.astrbot_message"] = astrbot_message_mod
    sys.modules["astrbot.core.platform.message_session"] = message_session_mod
    sys.modules["astrbot.core.platform.message_type"] = message_type_mod
    sys.modules["astrbot.core.platform.astr_message_event"] = astr_message_event_mod
    sys.modules["astrbot.core.agent.message"] = agent_message_mod
    sys.modules["astrbot.api.platform"] = api_platform_mod
    sys.modules["astrbot.core.star"] = core_star_mod
    sys.modules["astrbot.core.star.star_tools"] = star_tools_mod
    sys.modules["astrbot.core.star.star_handler"] = star_handler_mod
    sys.modules["astrbot.core.star.filter"] = _namespace_package(
        "astrbot.core.star.filter",
    )
    sys.modules["astrbot.core.star.filter.command"] = command_filter_mod
    sys.modules["astrbot.core.star.filter.command_group"] = command_group_mod
    sys.modules["astrbot.core.star.register"] = register_mod
    sys.modules["astrbot.core.star.filter.permission"] = permission_mod
    sys.modules["astrbot.core.star.filter.event_message_type"] = event_message_type_mod
    sys.modules["astrbot.core.message.components"] = message_components_mod
    sys.modules["astrbot.core.config"] = config_mod
    sys.modules["astrbot.core.config.astrbot_config"] = astrbot_config_mod
    sys.modules["astrbot.core.utils"] = _namespace_package("astrbot.core.utils")
    sys.modules["astrbot.core.utils.astrbot_path"] = astrbot_path_mod

    # Loudly block the real Host internals; the Runner shares the core venv,
    # so astrbot.core would import successfully without this.
    blocked_core = _BlockedModule("astrbot.core")
    # Type-only top-level re-exports stay importable.
    blocked_core.AstrBotConfig = dict
    sys.modules["astrbot.core"] = blocked_core

    _installed = True
