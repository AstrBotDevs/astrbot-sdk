from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.errors import InvalidRequest, RemotePluginError
from astrbot_sdk.events import UMO, MessageEvent, MessageRef, MessageType, Sender
from astrbot_sdk.messages import MessageChain, Plain
from astrbot_sdk.protocol import (
    PROTOCOL_VERSION,
    RequestFrame,
    ResponseFrame,
    decode_frame,
    encode_frame,
)
from astrbot_sdk.runtime import (
    StdioPluginClient,
    load_external_credential,
    serve_uds_plugin,
)
from astrbot_sdk.runtime.loader import load_plugin
from astrbot_sdk.runtime.transport import StreamTransport
from astrbot_sdk.tools import ToolCallContext


def _write_service_and_scope_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "metadata.yaml").write_text(
        """
schema_version: 2
name: scoped_services
desc: Tests scoped ctx, direct services, dynamic tools, and capability updates
version: 0.1.0
author: test
runtime:
  api: sdk
  entrypoint: main:ScopedPlugin
  sdk_version: ">=0.1.0"
capabilities:
  required: []
  optional:
    - id: storage.kv
    - id: plugin.inspect
    - id: llm.tool.register
dependencies:
  helper: ">=0.1.0"
""".strip(),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(
        """
from __future__ import annotations

import asyncio
from typing import Any

from astrbot_sdk import DirectContext, MessageEvent, Plugin, on
from astrbot_sdk.errors import CallScopeRetired
from astrbot_sdk.tools import ToolDefinition


class ScopedPlugin(Plugin[dict[str, Any]]):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.leaked_ctxs: list[Any] = []

    @on.tool("leak_scope", description="Capture scoped invocation context")
    async def leak_scope(self, call) -> dict[str, Any]:
        self.leaked_ctxs.append(call.ctx)
        return {"ok": True}

    @on.tool("use_leaked_scope", description="Use retired invocation context")
    async def use_leaked_scope(self) -> dict[str, Any]:
        leaked = self.leaked_ctxs[-1]
        try:
            await leaked.storage.get("k")
        except CallScopeRetired as exc:
            return {"retired": True, "code": exc.code}
        return {"retired": False}

    @on.tool("use_kv", description="Try invoking kv host capability")
    async def use_kv(self) -> dict[str, Any]:
        val = await self.ctx.storage.get("k")
        return {"value": val}

    @on.tool("call_peer", description="Call service on dependency plugin")
    async def call_peer(self, target: str) -> dict[str, Any]:
        peer = await self.ctx.plugins.call_service(target, "ping", {"from": "caller"})
        return {"peer": peer}

    @on.service("echo_host", description="Host-scoped service")
    async def echo_host(self, payload: dict[str, Any]) -> dict[str, Any]:
        val = await self.ctx.storage.get("k")
        return {"echo": payload, "kv": val}

    @on.direct_service("echo_direct", description="Direct service without host scope")
    async def echo_direct(
        self, payload: dict[str, Any], dctx: DirectContext[Any]
    ) -> dict[str, Any]:
        assert not hasattr(dctx, "storage")
        assert not hasattr(dctx, "messages")
        assert not hasattr(dctx, "plugins")
        return {"direct": payload, "cfg": dctx.current_config()}

    @on.tool_discovery
    async def discover(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(name="discovered_tool", description="Discovered at runtime")
        ]

    @on.command("stream_two")
    async def stream_two(self, event: MessageEvent):
        _ = event
        yield "part-1"
        yield "part-2"
""".strip(),
        encoding="utf-8",
    )


async def test_scoped_context_retires_and_direct_context_has_no_host(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "scoped_inproc"
    _write_service_and_scope_plugin(plugin_root)

    async def cap_invoker(cap: str, op: str, payload: dict[str, Any]) -> Any:
        _ = (cap, op, payload)
        return {"value": "ok"}

    async def service_invoker(
        plugin_id: str, service: str, payload: dict[str, Any], call_id: str | None
    ) -> Any:
        return {
            "plugin_id": plugin_id,
            "service": service,
            "payload": payload,
            "call_id": call_id,
        }

    loaded = load_plugin(
        plugin_root,
        config={"mode": "v1"},
        granted_capabilities=CapabilitySet(
            [
                CapabilityGrant("storage.kv"),
                CapabilityGrant("plugin.inspect"),
                CapabilityGrant("llm.tool.register"),
            ]
        ),
        host_capability_invoker=cap_invoker,
        host_service_invoker=service_invoker,
        data_dir=tmp_path / "data",
    )
    await loaded.start()
    try:
        res1 = await loaded.invoke_tool(
            "leak_scope", ToolCallContext(id="c1"), {}, call_id="c1"
        )
        assert res1 == {"ok": True}
        res2 = await loaded.invoke_tool(
            "use_leaked_scope", ToolCallContext(id="c2"), {}, call_id="c2"
        )
        assert res2 == {"retired": True, "code": "CALL_SCOPE_RETIRED"}

        direct_out = await loaded.invoke_service(
            "echo_direct", {"hello": 1}, call_id="s1"
        )
        assert direct_out == {"direct": {"hello": 1}, "cfg": {"mode": "v1"}}

        host_out = await loaded.invoke_service("echo_host", {"hello": 2}, call_id="s2")
        assert host_out["echo"] == {"hello": 2}
        assert host_out["kv"] == "ok"

        discovered = await loaded.discover_tools()
        assert len(discovered) == 1
        assert discovered[0].name == "discovered_tool"
    finally:
        await loaded.shutdown(reason="test")


async def test_wire_services_capability_update_and_tool_discovery(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "scoped_wire"
    _write_service_and_scope_plugin(plugin_root)

    async def cap_handler(
        grant: CapabilityGrant, op: str, payload: dict[str, Any]
    ) -> Any:
        if grant.id == "storage.kv" and op == "get":
            return {"value": f"kv:{payload['key']}"}
        return {}

    async def svc_handler(plugin_id: str, service: str, payload: dict[str, Any]) -> Any:
        return {"to": plugin_id, "service": service, "echo": payload}

    client = StdioPluginClient(
        plugin_root,
        capability_handler=cap_handler,
        service_handler=svc_handler,
    )
    handshake = await client.start(
        config={"env": "dev"},
        granted_capabilities=CapabilitySet(
            [CapabilityGrant("plugin.inspect"), CapabilityGrant("llm.tool.register")]
        ),
    )
    try:
        assert any(
            svc["name"] == "echo_direct" and svc["mode"] == "direct"
            for svc in handshake.services
        )
        assert "live_config_v1" in handshake.features

        # storage.kv not yet granted -> CapabilityDenied.
        with pytest.raises(RemotePluginError) as exc_info:
            await client.invoke_tool("use_kv", ToolCallContext(id="t1"), {})
        assert exc_info.value.code == "CAPABILITY_DENIED"

        # Dynamically grant storage.kv without restarting Runner.
        await client.update_capabilities(
            CapabilitySet(
                [
                    CapabilityGrant("storage.kv"),
                    CapabilityGrant("plugin.inspect"),
                    CapabilityGrant("llm.tool.register"),
                ]
            )
        )
        assert await client.invoke_tool("use_kv", ToolCallContext(id="t2"), {}) == {
            "value": "kv:k"
        }

        # Cross-plugin service routing via Host + exported service invocation.
        peer_res = await client.invoke_tool(
            "call_peer", ToolCallContext(id="t3"), {"target": "helper"}
        )
        assert peer_res == {
            "peer": {"to": "helper", "service": "ping", "echo": {"from": "caller"}}
        }

        svc_res = await client.invoke_service("echo_direct", {"a": 1})
        assert svc_res == {"direct": {"a": 1}, "cfg": {"env": "dev"}}

        tools_snap = await client.discover_tools()
        assert tools_snap["version"] == 1
        assert tools_snap["tools"][0]["name"] == "discovered_tool"

        # Resumable invocation completes both yielded items in order.
        ev = MessageEvent(
            id="e1",
            umo=UMO("test", MessageType.PRIVATE, "u1"),
            platform_type="test",
            message_ref=MessageRef("m1"),
            message=MessageChain(Plain("stream_two")),
            sender=Sender(id="u1", name="Tester"),
            timestamp=datetime.now(timezone.utc),
        )
        chunks = [
            r.message.text
            async for r in client.invoke_resumable("stream_two", ev)
            if r is not None
        ]
        assert chunks == ["part-1", "part-2"]
    finally:
        await client.close()


async def test_uds_credential_validation_and_external_attach(tmp_path: Path) -> None:
    plugin_root = tmp_path / "uds_plugin"
    _write_service_and_scope_plugin(plugin_root)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="ast_uds_") as sock_dir:
        sock_path = Path(sock_dir) / "h.sock"
        assert len(str(sock_path)) < 104

        cred_path = Path(sock_dir) / "cred.json"
        cred_path.write_text(
            json.dumps(
                {
                    "plugin_id": "scoped_services",
                    "credential": "secret-123",
                    "socket_path": str(sock_path),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(cred_path, 0o644)
        with pytest.raises(InvalidRequest, match="0600"):
            load_external_credential(cred_path)

        os.chmod(cred_path, 0o600)
        symlink_cred = Path(sock_dir) / "sym.json"
        symlink_cred.symlink_to(cred_path)
        with pytest.raises(InvalidRequest, match="symlink"):
            load_external_credential(symlink_cred)

        server_transport_ready: asyncio.Future[StreamTransport] = (
            asyncio.get_running_loop().create_future()
        )

        async def _handle_conn(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            tr = StreamTransport(reader, writer)
            line = await asyncio.wait_for(tr.read(), timeout=5.0)
            assert line is not None
            frame = decode_frame(line)
            assert frame.method == "attach"
            assert frame.params["credential"] == "secret-123"
            await tr.write(
                encode_frame(
                    ResponseFrame(
                        id=frame.id,
                        result={
                            "connection_id": uuid.uuid4().hex,
                            "host_epoch": "epoch-1",
                            "resume_token": "tok-1",
                            "features": ["external_attach_v1"],
                        },
                    )
                )
            )
            if not server_transport_ready.done():
                server_transport_ready.set_result(tr)

        uds_server = await asyncio.start_unix_server(_handle_conn, path=str(sock_path))
        runner_task = asyncio.create_task(
            serve_uds_plugin(plugin_root, cred_path, reconnect_grace=0.0)
        )
        host_tr: StreamTransport | None = None
        try:
            host_tr = await asyncio.wait_for(server_transport_ready, timeout=5.0)
            await host_tr.write(
                encode_frame(
                    RequestFrame(
                        id="init-1",
                        method="initialize",
                        params={
                            "protocol_versions": [PROTOCOL_VERSION],
                            "capabilities": [],
                        },
                    )
                )
            )
            raw_init = await asyncio.wait_for(host_tr.read(), timeout=5.0)
            assert raw_init is not None
            init_reply = decode_frame(raw_init)
            assert isinstance(init_reply, ResponseFrame)
            assert init_reply.result["plugin"]["name"] == "scoped_services"

            await host_tr.write(
                encode_frame(
                    RequestFrame(
                        id="stop-1",
                        method="shutdown",
                        params={"reason": "disabled"},
                    )
                )
            )
            raw_stop = await asyncio.wait_for(host_tr.read(), timeout=5.0)
            assert raw_stop is not None
            stop_reply = decode_frame(raw_stop)
            assert isinstance(stop_reply, ResponseFrame)
            await host_tr.close()
            host_tr = None
            await asyncio.wait_for(runner_task, timeout=5.0)
        finally:
            if host_tr is not None:
                with contextlib.suppress(Exception):
                    await host_tr.close()
            if not runner_task.done():
                runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)
            uds_server.close()
            await asyncio.wait_for(uds_server.wait_closed(), timeout=5.0)


async def test_task_group_spawn_detaches_call_scope_and_hook_uploads_local_media(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "task_and_hook"
    plugin_root.mkdir(parents=True, exist_ok=True)
    img_path = tmp_path / "gal.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    (plugin_root / "metadata.yaml").write_text(
        """
schema_version: 2
name: task_and_hook
desc: Regression test for TaskGroup.spawn detachment and hook media upload
version: 0.1.0
author: test
runtime:
  api: sdk
  entrypoint: main:TaskAndHookPlugin
  sdk_version: ">=0.1.0"
capabilities:
  required: []
  optional:
    - id: storage.kv
    - id: assets.transfer
    - id: message.observe
""".strip(),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(
        f"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot_sdk import MessageEvent, Plugin, hooks, on
from astrbot_sdk.hooks import MessageResult
from astrbot_sdk.message_components import Image


class TaskAndHookPlugin(Plugin[dict[str, Any]]):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.bg_done = asyncio.Event()
        self.bg_value: Any = None

    @on.command("spawn_bg")
    async def spawn_bg(self, event: MessageEvent):
        _ = event

        async def _worker() -> None:
            await asyncio.sleep(0.02)
            self.bg_value = await self.ctx.storage.get("after_handler")
            self.bg_done.set()

        self.ctx.tasks.spawn("bg_after_handler", _worker())
        return "queued"

    @hooks.message_result()
    async def render_image(
        self, event: MessageEvent, result: MessageResult
    ) -> None:
        _ = event
        result.chain = [Image.from_file(Path({str(img_path)!r}))]
""".strip(),
        encoding="utf-8",
    )

    from astrbot_sdk.assets import AssetRef

    uploaded_assets: list[dict[str, Any]] = []

    async def cap_invoker(cap: str, op: str, payload: dict[str, Any]) -> Any:
        if cap == "storage.kv" and op == "get":
            return {"value": f"detached:{payload['key']}"}
        if cap == "assets.transfer" and op == "upload":
            uploaded_assets.append(payload)
            return {
                "asset": AssetRef(
                    id="asset-gal-1",
                    filename="gal.jpg",
                    media_type="image/jpeg",
                    size=12,
                )
            }
        return {}

    loaded = load_plugin(
        plugin_root,
        granted_capabilities=CapabilitySet(
            [
                CapabilityGrant("storage.kv"),
                CapabilityGrant("assets.transfer"),
                CapabilityGrant("message.observe"),
            ]
        ),
        host_capability_invoker=cap_invoker,
        data_dir=tmp_path / "data",
    )
    await loaded.start()
    try:
        ev = MessageEvent(
            id="e-bg",
            umo=UMO("test", MessageType.PRIVATE, "u1"),
            platform_type="test",
            message_ref=MessageRef("m-bg"),
            message=MessageChain(Plain("spawn_bg")),
            sender=Sender(id="u1", name="Tester"),
            timestamp=datetime.now(timezone.utc),
        )
        out = [r async for r in loaded.invoke("spawn_bg", ev, call_id="call-bg-1")]
        assert out[0] is not None
        await asyncio.wait_for(loaded.instance.bg_done.wait(), timeout=2.0)
        assert loaded.instance.bg_value == "detached:after_handler"

        hook_out = await loaded.invoke_hook(
            "render_image", ev, {"chain": []}, call_id="call-hook-1"
        )
        assert len(uploaded_assets) == 1
        assert hook_out["writes"] == [
            {
                "field": "chain",
                "op": "set",
                "value": [
                    {
                        "type": "Image",
                        "source": {
                            "kind": "asset",
                            "id": "asset-gal-1",
                            "filename": "gal.jpg",
                            "media_type": "image/jpeg",
                            "size": 12,
                        },
                        "media_type": None,
                    }
                ],
            }
        ]
    finally:
        await loaded.shutdown(reason="test")
