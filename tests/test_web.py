"""Web route tests: registration, handshake, invocation, streaming."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from astrbot_sdk.runtime import StdioPluginClient
from astrbot_sdk.web import WebRequestInfo
from tests.test_stdio_transport import write_plugin

PLUGIN = """
from astrbot_sdk import Plugin
from astrbot_sdk.web import WebResponse


class TestPlugin(Plugin):
    from astrbot_sdk import on

    def __init__(self, ctx):
        super().__init__(ctx)

        @self.ctx.web.route("/api/items/<item_id>", methods=["GET"])
        async def get_item(request, item_id: str):
            return {
                "id": item_id,
                "q": request.query.get("verbose", "no"),
                "user": request.username,
            }

        @self.ctx.web.route("/api/echo", methods=["POST"])
        async def echo(request):
            body = await request.json()
            return WebResponse.json({"echo": body}, status=201)

        @self.ctx.web.route("/api/stream", methods=["GET"])
        async def stream(request):
            async def events():
                for i in range(3):
                    yield f"data: {i}\\n\\n"

            return events()
"""


@pytest.mark.asyncio
async def test_web_routes_end_to_end(tmp_path: Path) -> None:
    plugin_root = tmp_path / "webby"
    write_plugin(plugin_root, PLUGIN)

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
    )
    try:
        handshake = await client.start()
        routes = getattr(handshake, "web_routes", None)
        assert routes is not None
        assert {r["route"] for r in routes} == {
            "/api/items/<item_id>",
            "/api/echo",
            "/api/stream",
        }

        # dict result with path params, query and username.
        items = [
            item
            async for item in client.invoke_web(
                WebRequestInfo(
                    route="/api/items/<item_id>",
                    method="GET",
                    path="/api/items/abc",
                    path_params={"item_id": "abc"},
                    query=(("verbose", "yes"),),
                    headers={},
                    body=None,
                    body_size=0,
                    body_token=None,
                    username="admin",
                ),
            )
        ]
        info = items[0]["info"]
        assert info.status == 200
        import json

        payload = json.loads(b"".join(c["chunk"] for c in items[1:]))
        assert payload == {"id": "abc", "q": "yes", "user": "admin"}

        # POST json echo with a WebResponse.
        items = [
            item
            async for item in client.invoke_web(
                WebRequestInfo(
                    route="/api/echo",
                    method="POST",
                    path="/api/echo",
                    path_params={},
                    query=(),
                    headers={"content-type": "application/json"},
                    body=b'{"hello": 1}',
                    body_size=11,
                    body_token=None,
                    username=None,
                ),
            )
        ]
        assert items[0]["info"].status == 201
        payload = json.loads(b"".join(c["chunk"] for c in items[1:]))
        assert payload == {"echo": {"hello": 1}}

        # Async generator streams chunk by chunk.
        items = [
            item
            async for item in client.invoke_web(
                WebRequestInfo(
                    route="/api/stream",
                    method="GET",
                    path="/api/stream",
                    path_params={},
                    query=(),
                    headers={},
                    body=None,
                    body_size=0,
                    body_token=None,
                    username=None,
                ),
            )
        ]
        chunks = [c["chunk"] for c in items[1:]]
        assert chunks == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"]

        # Method not allowed and unknown route fail clearly.
        from astrbot_sdk.errors import RemotePluginError

        with pytest.raises(RemotePluginError, match="not allowed"):
            _ = [
                item
                async for item in client.invoke_web(
                    WebRequestInfo(
                        route="/api/echo",
                        method="GET",
                        path="/api/echo",
                        path_params={},
                        query=(),
                        headers={},
                        body=None,
                        body_size=0,
                        body_token=None,
                        username=None,
                    ),
                )
            ]
    finally:
        await client.close()


HELPER_PLUGIN = """
from astrbot_sdk import Plugin
from astrbot_sdk.web import (
    WebResponse,
    error_response,
    file_response,
    json_response,
    stream_response,
)


class TestPlugin(Plugin):
    def __init__(self, ctx):
        super().__init__(ctx)

        @self.ctx.web.route("/api/helpers/json", methods=["GET"])
        async def json_helper(request):
            return json_response({"ok": True})

        @self.ctx.web.route("/api/helpers/error", methods=["GET"])
        async def error_helper(request):
            return error_response("nope", status_code=403)

        @self.ctx.web.route("/api/helpers/sse", methods=["GET"])
        async def sse_helper(request):
            return stream_response(["data: a\\n\\n", "data: b\\n\\n"])

        @self.ctx.web.route("/api/helpers/file", methods=["GET"])
        async def file_helper(request):
            return file_response(self.ctx.data_dir / "hello.txt")

        @self.ctx.web.route("/api/helpers/starlette", methods=["GET"])
        async def starlette_helper(request):
            from starlette.responses import JSONResponse

            return JSONResponse({"via": "starlette"}, status_code=202)

        @self.ctx.web.route("/api/helpers/starlette-stream", methods=["GET"])
        async def starlette_stream_helper(request):
            from starlette.responses import StreamingResponse

            async def gen():
                yield b"x"
                yield b"y"

            return StreamingResponse(gen())
"""


def make_request(route: str, path: str | None = None) -> WebRequestInfo:
    return WebRequestInfo(
        route=route,
        method="GET",
        path=path or route,
        path_params={},
        query=(),
        headers={},
        body=None,
        body_size=0,
        body_token=None,
        username=None,
    )


@pytest.mark.asyncio
async def test_web_helpers_and_starlette(tmp_path: Path) -> None:
    plugin_root = tmp_path / "webhelpers"
    write_plugin(plugin_root, HELPER_PLUGIN)
    data_dir = plugin_root / ".data"

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        env={"ASTRBOT_SDK_DATA_DIR": str(data_dir)},
    )
    try:
        await client.start()
        import json

        async def call(route: str):
            items = [item async for item in client.invoke_web(make_request(route))]
            return items[0]["info"], b"".join(c["chunk"] for c in items[1:])

        info, body = await call("/api/helpers/json")
        assert info.status == 200
        assert json.loads(body) == {"ok": True}

        info, body = await call("/api/helpers/error")
        assert info.status == 403
        assert json.loads(body) == {"status": "error", "message": "nope", "data": None}

        info, body = await call("/api/helpers/sse")
        assert info.headers["content-type"] == "text/event-stream"
        assert body == b"data: a\n\ndata: b\n\n"

        # file_response streams the file written by the plugin into its data dir.
        (data_dir / "astrbot_plugin_webhelpers" / "hello.txt").parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        (data_dir / "astrbot_plugin_webhelpers" / "hello.txt").write_text(
            "file body",
            "utf-8",
        )
        info, body = await call("/api/helpers/file")
        assert body == b"file body"
        assert info.headers["content-type"].startswith("text/plain")

        info, body = await call("/api/helpers/starlette")
        assert info.status == 202
        assert json.loads(body) == {"via": "starlette"}

        info, body = await call("/api/helpers/starlette-stream")
        assert body == b"xy"
    finally:
        await client.close()


def test_route_pattern_validation() -> None:
    from astrbot_sdk.errors import InvalidPluginDefinition
    from astrbot_sdk.web import WebService

    service = WebService(None)
    service._validate_route("/api/items/<item_id>")
    service._validate_route("/api/files/<path:file_path>")
    with pytest.raises(InvalidPluginDefinition):
        service._validate_route("/api/items/<path:rest>/extra")
    with pytest.raises(InvalidPluginDefinition):
        service._validate_route("/api/items/<broken")
    with pytest.raises(InvalidPluginDefinition):
        service._validate_route("/api/items/{item_id}")
