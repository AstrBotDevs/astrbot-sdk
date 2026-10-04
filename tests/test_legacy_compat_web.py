"""Legacy web API compat tests: register_web_api with real Quart replay."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrbot_sdk.capabilities import CapabilitySet
from astrbot_sdk.runtime import StdioPluginClient
from astrbot_sdk.web import WebRequestInfo

LEGACY_PLUGIN = """
from quart import g, jsonify, request

from astrbot.api.star import Context, Star


class LegacyWebPlugin(Star):
    async def initialize(self):
        self.context.register_web_api(
            "/legacy/items/{item_id}", self.get_item, ["GET"], "get item")
        self.context.register_web_api(
            "/legacy/echo", self.echo, ["POST"], "echo body")
        self.context.register_web_api(
            "/legacy/whoami", self.whoami, ["GET"], "current user")

    async def get_item(self, item_id):
        q = request.args.get("verbose", "no")
        return jsonify({"id": item_id, "verbose": q})

    async def echo(self):
        body = await request.get_json(silent=True)
        return jsonify({"echo": body}), 201

    async def whoami(self):
        return {"user": g.username}
"""


def write_legacy_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_web",
                "desc": "legacy web compat test plugin",
                "author": "AstrBot",
                "version": "1.0.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(LEGACY_PLUGIN, encoding="utf-8")


def make_request(
    route: str,
    method: str = "GET",
    path: str | None = None,
    path_params: dict | None = None,
    query: tuple = (),
    body: bytes | None = None,
    username: str | None = None,
) -> WebRequestInfo:
    return WebRequestInfo(
        route=route,
        method=method,
        path=path or route,
        path_params=path_params or {},
        query=query,
        headers={"content-type": "application/json"} if body else {},
        body=body,
        body_size=len(body or b""),
        body_token=None,
        username=username,
    )


@pytest.mark.asyncio
async def test_legacy_web_routes(tmp_path: Path) -> None:
    plugin_root = tmp_path / "legacy_web"
    write_legacy_plugin(plugin_root)

    registrations: list[dict[str, Any]] = []

    async def host_handler(grant, operation, payload):
        if grant.id == "web.route" and operation == "register":
            registrations.append(payload)
            return {}
        raise AssertionError(f"unexpected call: {grant.id} {operation}")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
        legacy=True,
    )
    try:
        await client.start(
            granted_capabilities=CapabilitySet.from_ids(
                "storage.kv",
                "assets.transfer",
                "message.send",
                "web.route",
            ),
        )
        # register_web_api flushed during startup.
        assert {r["route"] for r in registrations} == {
            "/legacy/items/{item_id}",
            "/legacy/echo",
            "/legacy/whoami",
        }

        import json

        items = [
            item
            async for item in client.invoke_web(
                make_request(
                    "/legacy/items/{item_id}",
                    path="/legacy/items/abc",
                    path_params={"item_id": "abc"},
                    query=(("verbose", "yes"),),
                ),
            )
        ]
        payload = json.loads(b"".join(c["chunk"] for c in items[1:]))
        assert payload == {"id": "abc", "verbose": "yes"}

        items = [
            item
            async for item in client.invoke_web(
                make_request(
                    "/legacy/echo",
                    method="POST",
                    path="/legacy/echo",
                    body=b'{"hello": 1}',
                ),
            )
        ]
        assert items[0]["info"].status == 201
        payload = json.loads(b"".join(c["chunk"] for c in items[1:]))
        assert payload == {"echo": {"hello": 1}}

        items = [
            item
            async for item in client.invoke_web(
                make_request("/legacy/whoami", path="/legacy/whoami", username="admin"),
            )
        ]
        payload = json.loads(b"".join(c["chunk"] for c in items[1:]))
        assert payload == {"user": "admin"}
    finally:
        await client.close()
