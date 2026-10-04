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

        @self.ctx.web.route("/api/items/{item_id}", methods=["GET"])
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
            "/api/items/{item_id}",
            "/api/echo",
            "/api/stream",
        }

        # dict result with path params, query and username.
        items = [
            item
            async for item in client.invoke_web(
                WebRequestInfo(
                    route="/api/items/{item_id}",
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
