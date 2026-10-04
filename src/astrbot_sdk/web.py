"""Web routes: HTTP endpoints served through the capability pipeline.

Plugins register routes with ``ctx.web.route``; the Host dashboard matches
incoming HTTP requests and replays them to the plugin as WebRequestInfo
plus an optional body pull channel. Responses are a WebResponseInfo
followed by a backpressure-controlled chunk stream, so streaming
responses (SSE, large files) work without special cases.
"""

from __future__ import annotations

import inspect
import json as json_module
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .protocol_registry import register_protocol_dataclass

_WEB_CAPABILITY = "web.route"

# Bodies at or below this size travel inline in the request DTO; larger
# bodies are pulled by the plugin through the read_body channel.
INLINE_BODY_LIMIT = 4 * 1024 * 1024


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class WebRequestInfo:
    """One incoming HTTP request, replayed to the plugin."""

    route: str
    method: str
    path: str
    path_params: Mapping[str, str]
    query: tuple[tuple[str, str], ...]
    headers: Mapping[str, str]
    body: bytes | None
    body_size: int
    body_token: str | None
    username: str | None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class WebResponseInfo:
    """Response head; the body follows as stream chunks."""

    status: int
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class WebRouteRegistration:
    """One registered route."""

    route: str
    methods: tuple[str, ...]
    description: str
    handler: Callable[..., Any]


class WebRequest:
    """Request facade handed to route handlers."""

    def __init__(self, info: WebRequestInfo, ctx: Any) -> None:
        self._info = info
        self._ctx = ctx
        self.method = info.method
        self.path = info.path
        self.path_params = dict(info.path_params)
        self.headers = dict(info.headers)
        self.username = info.username
        self.query = _QueryMultiDict(info.query)
        self._body_cache: bytes | None = None

    async def body(self) -> bytes:
        """Read the full request body, pulling it when not inline."""
        if self._body_cache is not None:
            return self._body_cache
        info = self._info
        if info.body is not None:
            self._body_cache = info.body
            return self._body_cache
        if info.body_token is None:
            self._body_cache = b""
            return self._body_cache
        parts = []
        async for chunk in self.stream():
            parts.append(chunk)
        self._body_cache = b"".join(parts)
        return self._body_cache

    async def stream(self) -> AsyncIterator[bytes]:
        """Iterate the request body in chunks."""
        info = self._info
        if info.body is not None:
            yield info.body
            return
        if info.body_token is None:
            return
        offset = 0
        while True:
            result = await self._ctx._invoke_capability(
                _WEB_CAPABILITY,
                "read_body",
                {"token": info.body_token, "offset": offset},
            )
            chunk = result.get("chunk") or b""
            if chunk:
                yield chunk
                offset += len(chunk)
            if result.get("done", True):
                return

    async def text(self, encoding: str = "utf-8") -> str:
        """Read the body as text."""
        return (await self.body()).decode(encoding, errors="replace")

    async def json(self) -> Any:
        """Parse the body as JSON, or None when empty/invalid."""
        raw = await self.body()
        if not raw:
            return None
        try:
            return json_module.loads(raw)
        except json_module.JSONDecodeError:
            return None


class _QueryMultiDict:
    """Minimal multi-value query mapping (last value wins on get)."""

    def __init__(self, pairs: tuple[tuple[str, str], ...]) -> None:
        self._pairs = list(pairs)

    def get(self, key: str, default: Any = None, type: Callable | None = None) -> Any:
        for item_key, item_value in reversed(self._pairs):
            if item_key != key:
                continue
            if type is None:
                return item_value
            try:
                return type(item_value)
            except (TypeError, ValueError):
                return default
        return default

    def getlist(self, key: str) -> list[str]:
        return [value for item_key, value in self._pairs if item_key == key]

    def __contains__(self, key: str) -> bool:
        return any(item_key == key for item_key, _ in self._pairs)

    def __getitem__(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value


class WebResponse:
    """Buffered response returned by route handlers."""

    def __init__(
        self,
        body: bytes | str = b"",
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        content_type: str | None = None,
    ) -> None:
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.headers = dict(headers or {})
        if content_type is not None:
            self.headers.setdefault("content-type", content_type)

    @classmethod
    def json(cls, data: Any, status: int = 200) -> WebResponse:
        """Build a JSON response."""
        return cls(
            json_module.dumps(data, ensure_ascii=False).encode(),
            status=status,
            content_type="application/json",
        )

    @classmethod
    def text(cls, text: str, status: int = 200) -> WebResponse:
        """Build a plain-text response."""
        return cls(text, status=status, content_type="text/plain; charset=utf-8")


async def normalize_web_result(result: Any) -> tuple[WebResponseInfo, AsyncIterator[bytes]]:
    """Normalize one handler result into response info plus a chunk stream."""
    if isinstance(result, WebResponse):
        info = WebResponseInfo(status=result.status, headers=result.headers)

        async def buffered() -> AsyncIterator[bytes]:
            if result.body:
                yield result.body

        return info, buffered()
    if isinstance(result, dict | list):
        return await normalize_web_result(WebResponse.json(result))
    if isinstance(result, str | bytes):
        return await normalize_web_result(WebResponse(result))
    if isinstance(result, tuple) and len(result) in (2, 3):
        body, status = result[0], result[1]
        headers = result[2] if len(result) == 3 else None
        normalized, chunks = await normalize_web_result(body)
        info = WebResponseInfo(
            status=status,
            headers=headers or normalized.headers,
        )

        async def replay() -> AsyncIterator[bytes]:
            async for chunk in chunks:
                yield chunk

        return info, replay()
    if inspect.isasyncgen(result) or (
        inspect.isgenerator(result) or hasattr(result, "__aiter__")
    ):
        info = WebResponseInfo(status=200, headers={})

        async def streaming() -> AsyncIterator[bytes]:
            async for chunk in result:
                if isinstance(chunk, str):
                    yield chunk.encode()
                elif isinstance(chunk, bytes | bytearray):
                    yield bytes(chunk)
                else:
                    yield json_module.dumps(chunk, ensure_ascii=False).encode()

        return info, streaming()
    if result is None:
        async def no_content() -> AsyncIterator[bytes]:
            return
            yield b""

        return WebResponseInfo(status=204, headers={}), no_content()
    raise TypeError(f"unsupported web handler result: {type(result)!r}")


class WebService:
    """Register plugin web routes (``ctx.web``)."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self.routes: list[WebRouteRegistration] = []

    def route(
        self,
        path: str,
        methods: tuple[str, ...] | list[str] = ("GET",),
        description: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register one HTTP route served through the pipeline."""
        if not path.startswith("/"):
            raise ValueError("web route paths must start with '/'")

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.routes.append(
                WebRouteRegistration(
                    route=path,
                    methods=tuple(method.upper() for method in methods),
                    description=description,
                    handler=handler,
                ),
            )
            return handler

        return decorator
