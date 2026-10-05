from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from ..capabilities import CapabilityGrant, CapabilitySet
from ..errors import (
    AstrBotSDKError,
    HostUnavailable,
    InvalidPluginDefinition,
    InvalidRequest,
    RemoteHostError,
)
from ..events import MessageEvent
from ..protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    AckFrame,
    CancelFrame,
    ErrorFrame,
    ProtocolFrame,
    RequestFrame,
    ResponseFrame,
    YieldFrame,
    decode_frame,
    decode_value,
    encode_frame,
    encode_result,
    encode_value,
)
from ..registration import HandlerKind
from ..results import Propagation
from ..tools import tool_params_schema
from .loader import LoadedPlugin, load_plugin
from .peer import Peer

_SUPPORTED_PARAM_TYPES = {"str": str, "int": int, "float": float, "bool": bool}


def _is_message_event_annotation(annotation: Any) -> bool:
    """Check whether one parameter annotation marks the MessageEvent injection.

    Args:
        annotation: Raw parameter annotation, possibly a postponed string.

    Returns:
        True when the parameter receives the injected MessageEvent.
    """
    if annotation is MessageEvent:
        return True
    return isinstance(annotation, str) and annotation.rsplit(".", 1)[-1] == (
        "MessageEvent"
    )


def _is_event_parameter(param: inspect.Parameter) -> bool:
    """Check whether one parameter is the MessageEvent injection slot.

    Args:
        param: Handler signature parameter.

    Returns:
        True when the parameter receives the injected MessageEvent.

    Raises:
        InvalidPluginDefinition: The reserved name carries another annotation.
    """
    if _is_message_event_annotation(param.annotation):
        return True
    if param.name == "event":
        if param.annotation is inspect.Parameter.empty:
            return True
        raise InvalidPluginDefinition(
            "the 'event' parameter name is reserved for the MessageEvent injection"
        )
    return False


def _command_params_schema(registration: Any) -> list[dict[str, Any]]:
    """Build the Host-facing parameter schema for one command handler.

    Args:
        registration: Discovered command handler registration.

    Returns:
        Ordered parameter schema entries for the protocol handshake.

    Raises:
        InvalidPluginDefinition: A user parameter uses an unsupported type.
    """
    parameters = list(inspect.signature(registration.handler).parameters.values())
    # registration.handler is a bound method, so self is usually already
    # omitted; skip it only when it is still present.
    if parameters and parameters[0].name == "self":
        parameters = parameters[1:]
    user_params = parameters
    if user_params and _is_event_parameter(user_params[0]):
        user_params = user_params[1:]

    schema: list[dict[str, Any]] = []
    for param in user_params:
        if param.kind in (
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            continue
        if _is_message_event_annotation(param.annotation):
            raise InvalidPluginDefinition(
                "the MessageEvent injection must be the first handler parameter"
            )
        annotation = param.annotation
        param_type = None
        if annotation in _SUPPORTED_PARAM_TYPES.values():
            param_type = annotation
        elif isinstance(annotation, str):
            param_type = _SUPPORTED_PARAM_TYPES.get(annotation)

        has_default = param.default is not inspect.Parameter.empty
        if param_type is None and has_default:
            default_type = type(param.default)
            if default_type in _SUPPORTED_PARAM_TYPES.values():
                param_type = default_type
        if param_type is None:
            raise InvalidPluginDefinition(
                f"command parameter {param.name!r} of handler {registration.id!r} "
                "must use str, int, float or bool"
            )
        if has_default and not isinstance(param.default, param_type):
            raise InvalidPluginDefinition(
                f"default of command parameter {param.name!r} does not match "
                f"its {param_type.__name__} annotation"
            )
        schema.append(
            {
                "name": param.name,
                "type": param_type.__name__,
                "required": not has_default,
                "default": param.default if has_default else None,
            }
        )
    return schema


def _legacy_data_dir(metadata: Any) -> Path:
    """Resolve the legacy plugin data directory shared with in-process mode.

    Legacy plugins keep their files under the AstrBot data directory at
    plugin_data/{name}; when the Host exports ASTRBOT_DATA_PATH the isolated
    Runner must use the same location so switching runtimes keeps the data.
    """
    from .loader import _default_data_dir

    base = os.environ.get("ASTRBOT_DATA_PATH")
    if not base:
        return _default_data_dir(metadata)
    data_dir = Path(base) / "plugin_data" / metadata.name
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


class StdioPluginServer:
    """Serve one SDK plugin over newline-delimited stdio frames."""

    def __init__(
        self,
        plugin_root: Path,
        *,
        reader: BinaryIO | None = None,
        writer: BinaryIO | None = None,
        legacy: bool = False,
    ) -> None:
        """Initialize the stdio Runner.

        Args:
            plugin_root: Plugin repository root.
            reader: Binary input stream. Defaults to process stdin.
            writer: Binary output stream. Defaults to process stdout.
            legacy: Load the plugin through the legacy compat layer.
        """
        self.plugin_root = plugin_root.resolve()
        self.legacy = legacy
        self.reader = reader or sys.stdin.buffer
        self.writer = writer or sys.stdout.buffer
        self.loaded_plugin: LoadedPlugin | None = None
        self._write_lock = asyncio.Lock()
        self._peer = Peer(
            send=self._send,
            request_handler=self._handle_request,
            request_id_prefix="runner:",
            remote_error_factory=RemoteHostError,
            internal_error_code="PLUGIN_RUNTIME_ERROR",
        )
        self._invocations: dict[
            str,
            tuple[asyncio.Task[None], asyncio.Queue[int]],
        ] = {}
        self._capability_streams: dict[
            str,
            asyncio.Queue[ProtocolFrame | Exception],
        ] = {}
        self._initializing = False
        self._closing = False

    async def serve(self) -> None:
        """Read frames until shutdown or EOF.

        Raises:
            BrokenPipeError: The Host closes stdout while a response is written.
        """
        try:
            while not self._closing:
                line = await asyncio.to_thread(
                    self.reader.readline,
                    MAX_FRAME_BYTES + 1,
                )
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    await self._send(
                        ErrorFrame(
                            id="protocol",
                            code=InvalidRequest.code,
                            message="protocol frame exceeds the size limit",
                        )
                    )
                    break
                frame = None
                try:
                    frame = decode_frame(line)
                    if isinstance(frame, RequestFrame) and frame.method == "invoke":
                        if self.loaded_plugin is None:
                            raise InvalidRequest("Runner is not initialized")
                        if frame.id in self._invocations:
                            raise InvalidRequest(f"duplicate invocation id: {frame.id}")
                        acknowledgements: asyncio.Queue[int] = asyncio.Queue()
                        task = asyncio.create_task(
                            self._run_web_invocation(frame, acknowledgements)
                            if frame.params.get("web") or frame.params.get("views")
                            else self._run_invocation(frame, acknowledgements)
                        )
                        self._invocations[frame.id] = (task, acknowledgements)
                    elif await self._peer.receive(frame):
                        continue
                    elif isinstance(frame, AckFrame):
                        invocation = self._invocations.get(frame.id)
                        if invocation is not None:
                            invocation[1].put_nowait(frame.sequence)
                    elif isinstance(frame, CancelFrame):
                        invocation = self._invocations.get(frame.id)
                        if invocation is not None:
                            invocation[0].cancel()
                    elif isinstance(frame, (YieldFrame, ResponseFrame, ErrorFrame)):
                        queue = self._capability_streams.get(frame.id)
                        if queue is not None:
                            queue.put_nowait(frame)
                        # Late frames for finished streams are dropped.
                    else:
                        raise InvalidRequest(f"Host cannot send {type(frame).__name__}")
                except Exception as exc:
                    code = (
                        exc.code
                        if isinstance(exc, AstrBotSDKError)
                        else "PLUGIN_RUNTIME_ERROR"
                    )
                    await self._send(
                        ErrorFrame(
                            id=getattr(frame, "id", "protocol"),
                            code=code,
                            message=str(exc),
                        )
                    )
        finally:
            await self._stop_plugin()
            await self._peer.close()

    async def _send(
        self,
        frame: ProtocolFrame,
    ) -> None:
        """Write one Runner peer frame atomically.

        Args:
            frame: Frame to send to the Host.
        """
        data = encode_frame(frame)
        async with self._write_lock:
            self.writer.write(data)
            self.writer.flush()

    async def _handle_request(self, frame: RequestFrame) -> Any:
        """Dispatch one generic Host-to-Runner request.

        Args:
            frame: Request to dispatch.

        Returns:
            Request result encoded by the Peer.

        Raises:
            InvalidRequest: The method is unknown or invalid for the current state.
        """
        if frame.method == "ping":
            # Liveness probe for the Host supervisor; also serves as the
            # capability probe for future non-Python runners.
            return {"protocol_version": PROTOCOL_VERSION}
        if frame.method == "initialize":
            return await self._initialize(frame)
        if frame.method == "invoke_tool":
            if self.loaded_plugin is None:
                raise InvalidRequest("Runner is not initialized")
            call = decode_value(frame.params.get("context"))
            args = decode_value(frame.params.get("args", {}))
            if not isinstance(args, Mapping):
                raise InvalidRequest("tool args must be an object")
            result = await self.loaded_plugin.invoke_tool(
                str(frame.params["handler_id"]),
                call,
                args,
            )
            return encode_value(result)
        if frame.method == "invoke_hook":
            if self.loaded_plugin is None:
                raise InvalidRequest("Runner is not initialized")
            event = decode_value(frame.params.get("event"))
            if event is not None and not isinstance(event, MessageEvent):
                raise InvalidRequest("hook event must be a MessageEvent or null")
            payload = decode_value(frame.params.get("payload", {}))
            if not isinstance(payload, Mapping):
                raise InvalidRequest("hook payload must be an object")
            result = await self.loaded_plugin.invoke_hook(
                str(frame.params["handler_id"]),
                event,
                payload,
            )
            return encode_value(result)
        if frame.method == "session_consider":
            event = decode_value(frame.params.get("event"))
            if not isinstance(event, MessageEvent):
                raise InvalidRequest("session_consider event must be a MessageEvent")
            return {"matches": self._session_service()._consider(event)}
        if frame.method == "session_matched":
            event = decode_value(frame.params.get("event"))
            if not isinstance(event, MessageEvent):
                raise InvalidRequest("session_matched event must be a MessageEvent")
            self._session_service()._deliver(str(frame.params["waiter_id"]), event)
            return None
        if frame.method == "session_timeout":
            self._session_service()._timeout(str(frame.params["waiter_id"]))
            return None
        if frame.method == "shutdown":
            await self._stop_plugin()
            self._closing = True
            return None
        raise InvalidRequest(f"unknown Runner method: {frame.method}")

    def _session_service(self) -> Any:
        """Return the loaded plugin's session service.

        Raises:
            InvalidRequest: The Runner or its session service is unavailable.
        """
        loaded = self.loaded_plugin
        if loaded is None:
            raise InvalidRequest("Runner is not initialized")
        ctx = getattr(getattr(loaded, "instance", None), "ctx", None)
        if ctx is None:
            ctx = getattr(loaded, "sdk_ctx", None)
        sessions = getattr(ctx, "sessions", None)
        if sessions is None:
            raise InvalidRequest("plugin context has no session service")
        return sessions

    async def _initialize(self, frame: RequestFrame) -> dict[str, Any]:
        """Load and start the plugin after protocol negotiation.

        Args:
            frame: Initialize request containing config and capability grants.

        Raises:
            InvalidRequest: Initialization is repeated or negotiation fails.
        """
        if self.loaded_plugin is not None or self._initializing:
            raise InvalidRequest("Runner is already initialized")
        self._initializing = True
        try:
            protocol_versions = frame.params.get("protocol_versions")
            if not isinstance(protocol_versions, list) or not any(
                type(version) is int and version == PROTOCOL_VERSION
                for version in protocol_versions
            ):
                raise InvalidRequest("no supported protocol version")

            raw_grants = frame.params.get("capabilities", [])
            if not isinstance(raw_grants, list):
                raise InvalidRequest("capabilities must be a list")
            grants: list[CapabilityGrant] = []
            for item in raw_grants:
                if not isinstance(item, Mapping):
                    raise InvalidRequest("capability grant must be an object")
                scope = decode_value(item.get("scope", {}))
                if not isinstance(scope, Mapping):
                    raise InvalidRequest("capability scope must be an object")
                grants.append(CapabilityGrant(id=str(item["id"]), scope=scope))

            config = decode_value(frame.params.get("config"))
            if self.legacy:
                loaded = self._load_legacy(config, CapabilitySet(grants))
            else:
                loaded = load_plugin(
                    self.plugin_root,
                    config=config,
                    granted_capabilities=CapabilitySet(grants),
                    host_capability_invoker=self._invoke_host_capability,
                    host_capability_stream_invoker=self._invoke_host_capability_stream,
                )
            await loaded.start()
            self.loaded_plugin = loaded

            handlers: list[dict[str, Any]] = []
            for registration in loaded.registrations:
                spec = registration.spec
                details: dict[str, Any] = {}
                if spec.kind is HandlerKind.COMMAND:
                    details = {
                        "path": spec.path,
                        "aliases": list(spec.aliases),
                        "message_types": [
                            message_type.value for message_type in spec.message_types
                        ],
                        "platforms": list(spec.platforms),
                        "roles": [role.value for role in spec.roles],
                        "params": _command_params_schema(registration),
                    }
                elif spec.kind.value.startswith("hook."):
                    details = {
                        "stage": spec.kind.value.removeprefix("hook."),
                    }
                elif spec.kind is HandlerKind.TOOL:
                    precomputed = getattr(
                        registration.handler,
                        "__tool_params__",
                        None,
                    )
                    details = {
                        "name": spec.tool_name,
                        "description": spec.description,
                        "params": precomputed
                        if precomputed is not None
                        else [
                            {
                                "name": param.name,
                                "type": param.type,
                                "description": param.description,
                                "required": param.required,
                            }
                            for param in tool_params_schema(
                                registration.handler,
                                handler_label=f"handler {registration.id!r}",
                            )
                        ],
                    }
                elif spec.kind is HandlerKind.MESSAGE:
                    details = {
                        "message_types": [
                            message_type.value for message_type in spec.message_types
                        ],
                        "platforms": list(spec.platforms),
                        "roles": [role.value for role in spec.roles],
                        "regex": spec.regex,
                        "regex_flags": spec.regex_flags,
                    }
                handlers.append(
                    {
                        "id": registration.id,
                        "kind": spec.kind.value,
                        "description": spec.description,
                        "priority": spec.priority,
                        "details": details,
                    }
                )

            return {
                "protocol_version": PROTOCOL_VERSION,
                "plugin": {
                    "id": loaded.metadata.plugin_id,
                    "name": loaded.metadata.name,
                    "version": loaded.metadata.version,
                    "schema_version": loaded.metadata.schema_version,
                },
                "handlers": handlers,
                "web_routes": [
                    {
                        "route": route.route,
                        "methods": list(route.methods),
                        "description": route.description,
                    }
                    for route in getattr(
                        getattr(loaded.instance, "ctx", None),
                        "web",
                        None,
                    ).routes
                ]
                if getattr(getattr(loaded.instance, "ctx", None), "web", None)
                is not None
                else [],
                "capabilities": list(
                    getattr(loaded.instance, "ctx", None).capabilities
                    if getattr(loaded.instance, "ctx", None) is not None
                    else loaded.sdk_ctx.capabilities
                ),
            }
        finally:
            self._initializing = False

    def _load_legacy(self, config: Any, grants: CapabilitySet) -> Any:
        """Load a legacy plugin through the compat layer."""
        from ..compat.v1.loader import load_legacy_plugin
        from ..context import PluginContext, PluginInfo, RuntimeMode
        from .loader import _load_metadata_for_legacy

        metadata = _load_metadata_for_legacy(self.plugin_root)
        data_dir = _legacy_data_dir(metadata)
        ctx = PluginContext(
            plugin=PluginInfo(
                id=metadata.plugin_id,
                name=metadata.name,
                version=metadata.version,
                runtime_mode=RuntimeMode.ISOLATED,
            ),
            config=config,
            logger=logging.getLogger(f"astrbot.plugin.{metadata.name}"),
            capabilities=grants,
            data_dir=data_dir,
            _host_capability_invoker=self._invoke_host_capability,
            _host_capability_stream_invoker=self._invoke_host_capability_stream,
        )
        return load_legacy_plugin(
            self.plugin_root,
            ctx=ctx,
            config=config,
            logger=logging.getLogger(f"astrbot.plugin.{metadata.name}"),
        )

    async def _invoke_host_capability(
        self,
        capability_id: str,
        operation: str,
        payload: Mapping[str, Any],
    ) -> Any:
        """Invoke one authorized capability through the Host peer.

        Args:
            capability_id: Capability required by the SDK service operation.
            operation: Operation within the capability namespace.
            payload: Operation input DTOs.

        Returns:
            Decoded Host capability result.
        """
        result = await self._peer.request(
            "capability.invoke",
            {
                "capability": capability_id,
                "operation": operation,
                "input": encode_value(dict(payload)),
            },
        )
        return decode_value(result)

    async def _run_web_invocation(
        self,
        frame: RequestFrame,
        acknowledgements: asyncio.Queue[int],
    ) -> None:
        """Invoke one web route and stream the response with backpressure.

        Every yielded item (response info and each body chunk) is
        acknowledged by the Host before the next is produced, so slow
        clients throttle the plugin's generator (SSE, large files).
        """
        try:
            if self.loaded_plugin is None:
                raise InvalidRequest("Runner is not initialized")
            if frame.params.get("views") is not None:
                views_params = frame.params["views"]
                if not isinstance(views_params, Mapping):
                    raise InvalidRequest("views params must be an object")
                operation = str(views_params.get("operation") or "")
                page = views_params.get("page")
                path = views_params.get("path")
                stream = self.loaded_plugin.invoke_views(
                    operation,
                    str(page) if page is not None else None,
                    str(path) if path is not None else None,
                )
            else:
                from ..web import WebRequestInfo

                request = decode_value(frame.params.get("request"))
                if not isinstance(request, WebRequestInfo):
                    raise InvalidRequest("web request must be a WebRequestInfo")
                stream = self.loaded_plugin.invoke_web(request)
            sequence = 0
            async for item in stream:
                sequence += 1
                await self._send(
                    YieldFrame(
                        id=frame.id,
                        sequence=sequence,
                        result=encode_value(item),
                    ),
                )
                acknowledged_sequence = await acknowledgements.get()
                if acknowledged_sequence != sequence:
                    raise InvalidRequest(
                        "web stream acknowledgement sequence does not match"
                    )
            await self._send(ResponseFrame(id=frame.id, result=None))
        except asyncio.CancelledError:
            await self._send(
                ErrorFrame(
                    id=frame.id,
                    code="CANCELLED",
                    message="web invocation was cancelled",
                ),
            )
            raise
        except Exception as exc:
            code = (
                exc.code if isinstance(exc, AstrBotSDKError) else "PLUGIN_RUNTIME_ERROR"
            )
            await self._send(ErrorFrame(id=frame.id, code=code, message=str(exc)))
        finally:
            self._invocations.pop(frame.id, None)

    async def _run_invocation(
        self,
        frame: RequestFrame,
        acknowledgements: asyncio.Queue[int],
    ) -> None:
        """Invoke a handler and suspend after each yielded result.

        Args:
            frame: Invoke request.
            acknowledgements: Sequence acknowledgements from the Host.
        """
        try:
            if self.loaded_plugin is None:
                raise InvalidRequest("Runner is not initialized")
            handler_id = frame.params.get("handler_id")
            raw_args = frame.params.get("args", [])
            raw_kwargs = frame.params.get("kwargs", {})
            if not isinstance(handler_id, str) or not handler_id:
                raise InvalidRequest("handler_id must be a non-empty string")
            if not isinstance(raw_args, list) or not isinstance(raw_kwargs, Mapping):
                raise InvalidRequest("handler args or kwargs are invalid")

            args = [decode_value(value) for value in raw_args]
            kwargs = {
                str(key): decode_value(value) for key, value in raw_kwargs.items()
            }
            sequence = 0
            async for result in self.loaded_plugin.invoke(
                handler_id,
                *args,
                **kwargs,
            ):
                sequence += 1
                await self._send(
                    YieldFrame(
                        id=frame.id,
                        sequence=sequence,
                        result=encode_result(result),
                    )
                )
                if result is not None and result.propagation is Propagation.STOP:
                    continue
                acknowledged_sequence = await acknowledgements.get()
                if acknowledged_sequence != sequence:
                    raise InvalidRequest(
                        "yield acknowledgement sequence does not match"
                    )
            await self._send(ResponseFrame(id=frame.id, result=None))
        except asyncio.CancelledError:
            await self._send(
                ErrorFrame(
                    id=frame.id,
                    code="CANCELLED",
                    message="handler invocation was cancelled",
                )
            )
            raise
        except Exception as exc:
            code = (
                exc.code if isinstance(exc, AstrBotSDKError) else "PLUGIN_RUNTIME_ERROR"
            )
            await self._send(ErrorFrame(id=frame.id, code=code, message=str(exc)))
        finally:
            self._invocations.pop(frame.id, None)

    async def _invoke_host_capability_stream(
        self,
        capability_id: str,
        operation: str,
        payload: Mapping[str, Any],
    ) -> AsyncIterator[Any]:
        """Call a Host capability as a flow-controlled stream.

        The Host sends each item as a YieldFrame; requesting the next item
        from this iterator acknowledges the previous one, so the Host never
        outpaces the plugin.
        """
        stream_id = f"runner-cap-stream:{uuid.uuid4().hex}"
        queue: asyncio.Queue[ProtocolFrame | Exception] = asyncio.Queue()
        self._capability_streams[stream_id] = queue
        try:
            await self._send(
                RequestFrame(
                    id=stream_id,
                    method="capability.invoke",
                    params={
                        "capability": capability_id,
                        "operation": operation,
                        "input": encode_value(payload),
                    },
                ),
            )
            while True:
                frame = await queue.get()
                if isinstance(frame, Exception):
                    raise frame
                if isinstance(frame, YieldFrame):
                    yield decode_value(frame.result)
                    await self._send(
                        AckFrame(id=stream_id, sequence=frame.sequence),
                    )
                elif isinstance(frame, ResponseFrame):
                    return
                elif isinstance(frame, ErrorFrame):
                    raise RemoteHostError(frame.code, frame.message)
                else:
                    raise InvalidRequest(
                        f"unexpected capability stream frame: {type(frame).__name__}"
                    )
        finally:
            self._capability_streams.pop(stream_id, None)

    async def _stop_plugin(self) -> None:
        """Cancel active invocations and stop the loaded plugin."""
        if self.loaded_plugin is not None:
            # Unblock pending session waits before tasks are cancelled so a
            # waiting next() fails with HostUnavailable instead of hanging.
            with contextlib.suppress(Exception):
                self._session_service()._fail_all(
                    HostUnavailable("plugin Runner is shutting down")
                )
        current_task = asyncio.current_task()
        tasks = [
            task for task, _ in self._invocations.values() if task is not current_task
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._invocations.clear()
        if self.loaded_plugin is not None:
            with contextlib.suppress(Exception):
                await self.loaded_plugin.shutdown()
            self.loaded_plugin = None


async def serve_stdio_plugin(plugin_root: Path, *, legacy: bool = False) -> None:
    """Run one plugin Runner on process stdio.

    Args:
        plugin_root: Plugin repository root.
        legacy: Load the plugin through the legacy compat layer.
    """
    await StdioPluginServer(plugin_root, legacy=legacy).serve()
