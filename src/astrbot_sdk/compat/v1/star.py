"""Legacy Star base class and Context facade."""

from __future__ import annotations

import asyncio
from typing import Any

from .components import MessageChain
from .errors import IsolationUnsupportedError
from .event import AstrMessageEvent  # noqa: F401  (re-exported for plugins)
from .provider import LLMResponse, Provider  # noqa: F401
from .tools import FunctionTool, LegacyFunctionToolAdapter


class MessageSesion:
    """Legacy message session; the historical typo is part of the API."""

    def __init__(
        self,
        platform_name: str,
        message_type: Any,
        session_id: str,
    ) -> None:
        self.platform_name = platform_name
        self.platform_id = platform_name
        self.message_type = message_type
        self.session_id = session_id

    def __str__(self) -> str:
        from ...events import MessageType

        core_type = self.message_type
        if isinstance(core_type, MessageType):
            core_type = {
                MessageType.PRIVATE: "FriendMessage",
                MessageType.GROUP: "GroupMessage",
                MessageType.OTHER: "OtherMessage",
            }[core_type]
        return f"{self.platform_name}:{core_type}:{self.session_id}"

    @staticmethod
    def from_str(session_str: str) -> MessageSesion:
        platform_id, message_type, session_id = session_str.split(":", 2)
        return MessageSesion(platform_id, message_type, session_id)


class Context:
    """Facade over the plugin's new SDK context with the old Context API."""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        self._ctx = ctx
        self._config = config or {}
        self._pending: list[asyncio.Task] = []
        self._tasks: list[asyncio.Task] = []
        self._stars_cache: list | None = None
        self._web_routes: list = []
        self.logger = ctx.logger
        from .conversation import ConversationManager

        self.conversation_manager = ConversationManager(ctx)

    @property
    def persona_manager(self) -> Any:
        """Persona management is host-internal in isolated mode."""
        raise IsolationUnsupportedError(
            "persona_manager is unavailable in isolated legacy mode"
        )

    @property
    def html_renderer(self) -> Any:
        """Legacy HtmlRenderer facade backed by the render capability."""
        from .html import HtmlRendererFacade

        return HtmlRendererFacade()

    def get_config(self, umo: str | None = None) -> Any:
        """Return the global AstrBot configuration.

        The global config is host-internal and cannot cross the isolation
        boundary; plugins needing it must run in-process.
        """
        raise IsolationUnsupportedError(
            "Context.get_config() (global AstrBot config) is unavailable in "
            "isolated legacy mode; the plugin's own config arrives through "
            "the Star constructor"
        )

    # ---- providers ---------------------------------------------------------

    def get_using_provider(self, umo: str | None = None) -> Provider:
        """Return a lazily-resolved facade for the session's chat provider."""
        return Provider(self._ctx, umo=umo)

    async def get_using_provider_async(self, umo: str | None = None) -> Provider:
        """Async variant of get_using_provider (provider metadata resolved)."""
        provider = Provider(self._ctx, umo=umo)
        await provider._resolve()
        return provider

    def get_provider_by_id(self, provider_id: str) -> Provider:
        """Return a facade bound to one explicit provider instance."""
        return Provider(self._ctx, provider_id=provider_id)

    async def get_current_chat_provider_id(self, umo: str) -> str | None:
        """Return the chat provider id currently selected for the session."""
        from ...llm import ProviderKind

        info = await self._ctx.llm.current_provider(ProviderKind.CHAT, umo=umo)
        return info.id if info is not None else None

    async def get_all_providers(self) -> list[Provider]:
        """Return facades for every configured chat provider."""
        from ...llm import ProviderKind

        providers = await self._ctx.llm.list_providers(ProviderKind.CHAT)
        return [Provider(self._ctx, provider_id=info.id) for info in providers]

    async def llm_generate(
        self,
        *,
        chat_provider_id: str,
        prompt: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        tools: Any = None,
        system_prompt: str | None = None,
        contexts: list | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """One-shot completion on an explicit provider (legacy signature)."""
        provider = Provider(self._ctx, provider_id=chat_provider_id)
        return await provider.text_chat(
            prompt=prompt,
            image_urls=image_urls,
            audio_urls=audio_urls,
            contexts=contexts,
            system_prompt=system_prompt or "",
            func_tool=tools,
            **kwargs,
        )

    async def tool_loop_agent(
        self,
        *,
        event: AstrMessageEvent,
        chat_provider_id: str,
        prompt: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        tools: Any = None,
        system_prompt: str | None = None,
        contexts: list | None = None,
        max_steps: int = 128,
        **kwargs: Any,
    ) -> LLMResponse:
        """Run the built-in tool-loop agent (legacy signature)."""
        from ...llm import AgentRequest
        from .provider import build_input_message, contexts_to_messages

        messages = contexts_to_messages(contexts)
        input_message = build_input_message(prompt, image_urls, audio_urls)
        if input_message is not None:
            messages.append(input_message)
        tool_names: tuple[str, ...] | None = None
        if tools is not None:
            tool_names = tuple(
                tool.name for tool in getattr(tools, "tools", []) if tool.name
            )
        request = AgentRequest(
            input=tuple(messages),
            system_prompt=system_prompt or None,
            provider_id=chat_provider_id,
            max_steps=max_steps,
            tools=tool_names,
            event=getattr(event, "_event", None),
        )
        response = await self._ctx.llm.run_agent(request)
        return LLMResponse(
            role="assistant",
            completion_text=response.text,
            reasoning_content=response.reasoning_content,
        )

    # ---- plugin registry ----------------------------------------------------

    async def _prime_stars(self) -> None:
        """Fetch the plugin registry snapshot used by the sync getters."""
        try:
            infos = await self._ctx.plugins.list()
        except Exception:  # noqa: BLE001 - inspection is best-effort
            infos = []
        self._stars_cache = [_star_metadata_shell(info) for info in infos]

    def get_all_stars(self) -> list:
        """Return metadata of installed plugins (startup snapshot)."""
        return list(self._stars_cache or [])

    async def get_all_stars_async(self) -> list:
        """Async variant returning a fresh registry snapshot."""
        await self._prime_stars()
        return self.get_all_stars()

    def get_registered_star(self, star_name: str) -> Any:
        """Return one plugin's metadata by name from the snapshot."""
        for metadata in self.get_all_stars():
            if metadata.name == star_name:
                return metadata
        return None

    # ---- speech and embedding providers -------------------------------------

    def get_using_tts_provider(self, umo: str | None = None) -> Any:
        """Return a lazily-resolved facade for the session's TTS provider."""
        from .provider import TTSProvider

        return TTSProvider(self._ctx, umo=umo)

    async def get_using_tts_provider_async(self, umo: str | None = None) -> Any:
        return self.get_using_tts_provider(umo)

    def get_using_stt_provider(self, umo: str | None = None) -> Any:
        """Return a lazily-resolved facade for the session's STT provider."""
        from .provider import STTProvider

        return STTProvider(self._ctx, umo=umo)

    async def get_using_stt_provider_async(self, umo: str | None = None) -> Any:
        return self.get_using_stt_provider(umo)

    async def get_all_tts_providers(self) -> list:
        from ...llm import ProviderKind
        from .provider import TTSProvider

        infos = await self._ctx.llm.list_providers(ProviderKind.TEXT_TO_SPEECH)
        return [TTSProvider(self._ctx, provider_id=info.id) for info in infos]

    async def get_all_stt_providers(self) -> list:
        from ...llm import ProviderKind
        from .provider import STTProvider

        infos = await self._ctx.llm.list_providers(ProviderKind.SPEECH_TO_TEXT)
        return [STTProvider(self._ctx, provider_id=info.id) for info in infos]

    async def get_all_embedding_providers(self) -> list:
        from ...llm import ProviderKind
        from .provider import EmbeddingProvider

        infos = await self._ctx.llm.list_providers(ProviderKind.EMBEDDING)
        return [EmbeddingProvider(self._ctx, provider_id=info.id) for info in infos]

    # ---- background tasks ---------------------------------------------------

    def register_task(self, task: Any, desc: str = "") -> None:
        """Run a background coroutine for the plugin's lifetime."""
        handle = asyncio.get_running_loop().create_task(task, name=desc or None)
        self._tasks.append(handle)

    # ---- host-internal APIs (fail loudly) ------------------------------------

    def register_commands(self, *args: Any, **kwargs: Any) -> None:
        """Manage host command registrations (unsupported when isolated)."""
        raise IsolationUnsupportedError(
            "register_commands is unavailable in isolated legacy mode"
        )

    def get_llm_tool_manager(self) -> Any:
        """The global tool manager is host-internal (unsupported)."""
        raise IsolationUnsupportedError(
            "get_llm_tool_manager is unavailable in isolated legacy mode"
        )

    def get_event_queue(self) -> Any:
        """The host event queue is host-internal (unsupported)."""
        raise IsolationUnsupportedError(
            "get_event_queue is unavailable in isolated legacy mode"
        )

    def get_platform(self, *args: Any, **kwargs: Any) -> Any:
        """Platform adapters are host-internal (unsupported)."""
        raise IsolationUnsupportedError(
            "get_platform is unavailable in isolated legacy mode"
        )

    def get_platform_inst(self, *args: Any, **kwargs: Any) -> Any:
        """Platform adapters are host-internal (unsupported)."""
        raise IsolationUnsupportedError(
            "get_platform_inst is unavailable in isolated legacy mode"
        )

    def get_db(self) -> Any:
        """The host database is host-internal (unsupported)."""
        raise IsolationUnsupportedError("get_db is unavailable in isolated legacy mode")

    def register_provider(self, *args: Any, **kwargs: Any) -> None:
        """Custom providers are host-internal (unsupported)."""
        raise IsolationUnsupportedError(
            "register_provider is unavailable in isolated legacy mode"
        )

    # ---- dynamic tools -----------------------------------------------------

    def register_llm_tool(self, tool_class: type[FunctionTool]) -> None:
        """Register one legacy FunctionTool class (instantiated internally).

        The old API is synchronous, while isolated registration is an RPC;
        the task is tracked and awaited before plugin startup completes.
        """
        instance = tool_class()
        adapter = LegacyFunctionToolAdapter(instance, self)
        self._pending.append(
            asyncio.get_running_loop().create_task(self._ctx.tools.register(adapter)),
        )

    def add_llm_tools(self, *tools: FunctionTool) -> None:
        """Register already-instantiated legacy tools."""
        for tool in tools:
            adapter = LegacyFunctionToolAdapter(tool, self)
            self._pending.append(
                asyncio.get_running_loop().create_task(
                    self._ctx.tools.register(adapter),
                ),
            )

    def unregister_llm_tool(self, name: str) -> None:
        """Remove one dynamically registered tool by name."""
        self._pending.append(
            asyncio.get_running_loop().create_task(
                self._unregister_llm_tool(name),
            ),
        )

    async def _unregister_llm_tool(self, name: str) -> None:
        from ...tools import ToolRef

        for handler_id, handler in list(self._ctx.dynamic_tools.items()):
            tool_name = getattr(handler, "__self__", handler)
            tool_name = getattr(tool_name, "name", None)
            if tool_name == name:
                await self._ctx.tools.unregister(
                    ToolRef(name=name, handler_id=handler_id),
                )

    def activate_llm_tool(self, name: str) -> bool:
        """Toggle a global tool's active flag (unsupported when isolated)."""
        raise IsolationUnsupportedError(
            "activate_llm_tool is unavailable in isolated legacy mode"
        )

    def deactivate_llm_tool(self, name: str) -> bool:
        """Toggle a global tool's active flag (unsupported when isolated)."""
        raise IsolationUnsupportedError(
            "deactivate_llm_tool is unavailable in isolated legacy mode"
        )

    @property
    def registered_web_apis(self) -> list[tuple[str, Any, list[str], str]]:
        """Read-only view of this plugin's routes, in the core tuple shape."""
        return [
            (entry.route, entry.handler, entry.methods, entry.desc)
            for entry in self._web_routes
        ]

    def register_web_api(
        self,
        route: str,
        view_handler: Any,
        methods: list[str],
        desc: str,
    ) -> None:
        """Register one dashboard web route (legacy signature).

        Registration is an RPC under the hood; the task is tracked and
        awaited before plugin startup completes, same as tool registration.
        """
        from .web import WebRouteEntry

        entry = WebRouteEntry(route, methods, desc, view_handler)
        self._web_routes = [
            item
            for item in self._web_routes
            if not (item.route == route and item.methods == entry.methods)
        ]
        self._web_routes.append(entry)
        self._pending.append(
            asyncio.get_running_loop().create_task(
                self._ctx._invoke_capability(
                    "web.route",
                    "register",
                    {
                        "route": route,
                        "methods": [method.upper() for method in methods],
                        "description": desc,
                    },
                ),
            ),
        )

    async def _drain_pending(self) -> None:
        """Await all scheduled tool registrations (called at startup)."""
        while self._pending:
            task = self._pending.pop()
            await task

    async def put_kv_data(self, key: str, value: Any) -> None:
        await self._ctx.storage.set(key, value)

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        return await self._ctx.storage.get(key, default)

    async def delete_kv_data(self, key: str) -> None:
        await self._ctx.storage.delete(key)

    async def send_message(
        self,
        session: str | MessageSesion,
        message_chain: MessageChain,
    ) -> bool:
        """Proactively send a message chain to a session."""
        from ...events import UMO, MessageType

        if isinstance(session, str):
            session = MessageSesion.from_str(session)
        core_type = session.message_type
        sdk_type = {
            "FriendMessage": MessageType.PRIVATE,
            "private": MessageType.PRIVATE,
            "GroupMessage": MessageType.GROUP,
            "group": MessageType.GROUP,
        }.get(core_type, MessageType.OTHER)
        umo = UMO(session.platform_name, sdk_type, session.session_id)
        chain = (
            message_chain.to_sdk()
            if isinstance(message_chain, MessageChain)
            else message_chain
        )
        await self._ctx.messages.send(umo, chain)
        return True

    @property
    def _inner(self) -> Any:
        """Access the underlying new SDK context (compat escape hatch)."""
        return self._ctx


def _star_metadata_shell(info: Any) -> Any:
    """Convert one PluginInfo into a legacy StarMetadata-shaped object."""
    from types import SimpleNamespace

    return SimpleNamespace(
        name=info.name,
        author=getattr(info, "author", None),
        desc=getattr(info, "desc", None),
        version=info.version,
        repo=None,
        activated=getattr(info, "activated", True),
        module_path=None,
        root_dir_name=None,
    )


class CompatConfig(dict):
    """Plugin config facade: a dict that rejects file persistence loudly.

    The isolated Runner holds a snapshot of the plugin config; writes back
    to data/config need the Host, so save_config is unsupported here.

    Mirrors AstrBotConfig's attribute access: ``config.key`` reads a
    top-level item (missing keys yield None), and attribute assignment
    writes into the in-memory dict without persisting.
    """

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError:
            return None

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    def __delattr__(self, key: str) -> None:
        try:
            del self[key]
        except KeyError:
            raise AttributeError(key) from None

    def save_config(self, *args: Any, **kwargs: Any) -> None:
        """Reject persistence; config files live on the Host."""
        raise IsolationUnsupportedError(
            "config.save_config() is unavailable in isolated legacy mode"
        )


class StarTools:
    """Legacy StarTools utility class bound to the plugin's Context."""

    _context: Context | None = None

    @classmethod
    def initialize(cls, context: Context) -> None:
        cls._context = context

    @classmethod
    def _bound(cls) -> Context:
        if cls._context is None:
            raise ValueError("StarTools not initialized")
        return cls._context

    @classmethod
    def get_data_dir(cls, plugin_name: str | None = None) -> Any:
        """Return data/plugin_data/{name}, creating it when missing."""
        from pathlib import Path

        data_dir = Path(cls._bound()._ctx.data_dir)
        if plugin_name:
            data_dir = data_dir.parent / plugin_name
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    @classmethod
    async def send_message(cls, session: Any, message_chain: MessageChain) -> bool:
        """Send a message chain to a session by unified message origin."""
        return await cls._bound().send_message(session, message_chain)

    @classmethod
    def register_llm_tool(cls, tool_class: type[FunctionTool]) -> None:
        cls._bound().register_llm_tool(tool_class)

    @classmethod
    def unregister_llm_tool(cls, name: str) -> None:
        cls._bound().unregister_llm_tool(name)

    @classmethod
    def activate_llm_tool(cls, name: str) -> bool:
        return cls._bound().activate_llm_tool(name)

    @classmethod
    def deactivate_llm_tool(cls, name: str) -> bool:
        return cls._bound().deactivate_llm_tool(name)


class Star:
    """Legacy plugin base class."""

    name: str

    def __init__(self, context: Context, config: Any = None) -> None:
        self.context = context
        self.logger = context._ctx.logger

    async def initialize(self) -> None:
        """Called once when the plugin is loaded."""

    async def terminate(self) -> None:
        """Called once when the plugin is unloaded."""

    async def text_to_image(
        self,
        text: str,
        return_url: bool = True,
        template_name: str | None = None,
        umo: str | None = None,
    ) -> Any:
        """Convert text to an image via the render capability.

        Returns an AssetRef rather than a URL or path; legacy media
        components accept it directly as their file source.
        """
        return await self.context.html_renderer.render_t2i(
            text,
            return_url=return_url,
            template_name=template_name,
        )

    async def html_render(
        self,
        tmpl: str,
        data: dict,
        return_url: bool = True,
        options: dict | None = None,
        umo: str | None = None,
    ) -> Any:
        """Render a custom Jinja2 HTML template into an image.

        Returns an AssetRef rather than a URL or path; legacy media
        components accept it directly as their file source.
        """
        return await self.context.html_renderer.render_custom_template(
            tmpl,
            data,
            return_url=return_url,
            **(options or {}),
        )

    async def put_kv_data(self, key: str, value: Any) -> None:
        await self.context.put_kv_data(key, value)

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        return await self.context.get_kv_data(key, default)

    async def delete_kv_data(self, key: str) -> None:
        await self.context.delete_kv_data(key)


def register(
    name: str,
    author: str,
    desc: str,
    version: str,
    repo: str = "",
) -> Any:
    """Legacy register decorator declaring plugin metadata on the class."""

    def decorator(cls: type[Star]) -> type[Star]:
        cls.__astrbot_register__ = {
            "name": name,
            "author": author,
            "desc": desc,
            "version": version,
            "repo": repo,
        }
        return cls

    return decorator
