"""Legacy hook facades: adapt old hook signatures onto tracked stage DTOs."""

from __future__ import annotations

import inspect
from collections.abc import MutableSequence
from types import SimpleNamespace
from typing import Any

from ...hooks import HookDTO, build_stage_dto
from ...registration import HandlerKind
from .components import (
    MessageEventResult,
    from_sdk_segment,
    to_sdk_segment,
)
from .event import AstrMessageEvent

# Legacy filter-decorator stage -> new hook HandlerKind.
HOOK_STAGE_KINDS: dict[str, HandlerKind] = {
    "llm_request": HandlerKind.HOOK_LLM_REQUEST,
    "llm_response": HandlerKind.HOOK_LLM_RESPONSE,
    "tool_call": HandlerKind.HOOK_TOOL_CALL,
    "tool_result": HandlerKind.HOOK_TOOL_RESULT,
    "message_result": HandlerKind.HOOK_MESSAGE_RESULT,
    "message_sent": HandlerKind.HOOK_MESSAGE_SENT,
    "agent_start": HandlerKind.HOOK_AGENT_START,
    "agent_end": HandlerKind.HOOK_AGENT_END,
    "waiting_llm_request": HandlerKind.HOOK_WAITING_LLM_REQUEST,
    "plugin_error": HandlerKind.HOOK_PLUGIN_ERROR,
    "plugin_loaded": HandlerKind.HOOK_PLUGIN_LOADED,
    "plugin_unloaded": HandlerKind.HOOK_PLUGIN_UNLOADED,
}

_LLM_REQUEST_FIELDS = frozenset(
    {"prompt", "system_prompt", "contexts", "image_urls", "audio_urls", "model"},
)


class _HookProviderRequest:
    """Legacy ProviderRequest facade writing through to the tracked DTO."""

    def __init__(self, dto: HookDTO) -> None:
        object.__setattr__(self, "_dto", dto)
        object.__setattr__(self, "_local", {"func_tool": None, "conversation": None})

    def __getattr__(self, name: str) -> Any:
        if name in _LLM_REQUEST_FIELDS:
            return getattr(object.__getattribute__(self, "_dto"), name)
        return object.__getattribute__(self, "_local").get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _LLM_REQUEST_FIELDS:
            setattr(object.__getattribute__(self, "_dto"), name, value)
        else:
            object.__getattribute__(self, "_local")[name] = value


class _HookLLMResponse:
    """Legacy LLMResponse facade writing through to the tracked DTO."""

    def __init__(self, dto: HookDTO) -> None:
        object.__setattr__(self, "_dto", dto)
        object.__setattr__(self, "_local", {"role": "assistant", "result_chain": None})

    def __getattr__(self, name: str) -> Any:
        if name == "completion_text":
            return getattr(object.__getattribute__(self, "_dto"), "content")
        if name == "reasoning_content":
            return getattr(object.__getattribute__(self, "_dto"), "reasoning_content")
        return object.__getattribute__(self, "_local").get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "completion_text":
            setattr(object.__getattribute__(self, "_dto"), "content", value)
        elif name == "reasoning_content":
            setattr(object.__getattribute__(self, "_dto"), "reasoning_content", value)
        else:
            object.__getattribute__(self, "_local")[name] = value


class _TrackedChainProxy(MutableSequence):
    """Compat-component view over the tracked SDK segment list."""

    def __init__(self, tracked: list) -> None:
        self._tracked = tracked

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [from_sdk_segment(s) for s in self._tracked[index]]
        return from_sdk_segment(self._tracked[index])

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            self._tracked[index] = [to_sdk_segment(v) for v in value]
        else:
            self._tracked[index] = to_sdk_segment(value)

    def __delitem__(self, index: Any) -> None:
        del self._tracked[index]

    def __len__(self) -> int:
        return len(self._tracked)

    def insert(self, index: int, value: Any) -> None:
        self._tracked.insert(index, to_sdk_segment(value))


def _tool_namespace(payload: dict[str, Any]) -> SimpleNamespace:
    """Build the legacy FunctionTool-shaped object for tool hooks."""
    return SimpleNamespace(
        name=payload.get("name", ""),
        description=payload.get("description", ""),
        parameters=payload.get("parameters", {}),
    )


async def invoke_compat_hook(
    plugin: Any,
    method: Any,
    stage: str,
    event: Any,
    stage_payload: dict[str, Any],
) -> dict[str, Any]:
    """Invoke one legacy hook handler against a tracked stage DTO.

    The legacy facade writes through to the tracked DTO where the stage
    supports modification; recorded write ops are returned for the Host.
    """
    from ...events import MessageEvent

    payload = dict(stage_payload or {})
    dto = (
        build_stage_dto(stage, payload) if not stage.startswith("lifecycle.") else None
    )
    context = plugin.context

    facade_event: AstrMessageEvent | None = None
    if isinstance(event, MessageEvent):
        facade_event = AstrMessageEvent(event, context)

    args: list[Any] = []
    if stage == "llm_request":
        args = [facade_event, _HookProviderRequest(dto)]
    elif stage == "llm_response":
        args = [facade_event, _HookLLMResponse(dto)]
    elif stage == "message_result":
        if facade_event is not None and dto is not None:
            result = MessageEventResult()
            result.chain = _TrackedChainProxy(dto.chain)
            facade_event.set_result(result)
        args = [facade_event]
    elif stage == "tool_call":
        args = [facade_event, _tool_namespace(payload), getattr(dto, "args", {})]
    elif stage == "tool_result":
        args = [
            facade_event,
            _tool_namespace(payload),
            payload.get("args", {}),
            getattr(dto, "content", None),
        ]
    elif stage == "agent_start":
        args = [facade_event, SimpleNamespace(**payload)]
    elif stage == "agent_end":
        args = [
            facade_event,
            SimpleNamespace(**payload),
            _HookLLMResponse(dto) if dto is not None else None,
        ]
    elif stage == "plugin_error":
        error = dto
        args = [
            facade_event,
            getattr(error, "plugin_name", ""),
            getattr(error, "handler_name", ""),
            getattr(error, "error", ""),
            getattr(error, "traceback", ""),
        ]
    elif stage in {"plugin_loaded", "plugin_unloaded"}:
        args = [dto]
    elif stage in {"message_sent", "waiting_llm_request"}:
        args = [facade_event]
    elif stage.startswith("lifecycle."):
        args = []
    else:
        raise ValueError(f"unsupported legacy hook stage: {stage}")

    outcome = method(*args)
    if inspect.isasyncgen(outcome):
        await outcome.aclose()
        raise ValueError("legacy hook handler cannot yield results")
    await outcome

    # A replaced result chain (event.set_result(...)) must be copied back.
    if stage == "message_result" and facade_event is not None and dto is not None:
        result = facade_event.get_result()
        if result is not None and not isinstance(result.chain, _TrackedChainProxy):
            dto.chain = [to_sdk_segment(c) for c in result.chain]

    return {
        "writes": dto._write_ops() if isinstance(dto, HookDTO) else [],
    }
