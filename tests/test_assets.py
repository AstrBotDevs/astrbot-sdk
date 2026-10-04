from __future__ import annotations

import base64
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrbot_sdk.assets import AssetRef
from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.context import PluginContext, PluginInfo, RuntimeMode
from astrbot_sdk.events import UMO, MessageEvent, MessageRef, MessageType, Sender
from astrbot_sdk.message_components import (
    Face,
    Forward,
    Image,
    Node,
    Plain,
    UnknownSegment,
)
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.messages import MessageChain as _MC  # noqa: F401
from astrbot_sdk.protocol.codec import decode_message_chain, encode_message_chain
from astrbot_sdk.runtime import StdioPluginClient
from astrbot_sdk.services import prepare_outbound_chain


def make_event() -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO("platform-1", MessageType.PRIVATE, "user-1"),
        platform_type="webchat",
        message_ref=MessageRef("message-1"),
        message=MessageChain(Plain("/pic")),
        sender=Sender("user-1", "Moon"),
        timestamp=datetime.now(UTC),
    )


def write_plugin(plugin_root: Path, source: str) -> None:
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "name": f"plugin_{plugin_root.name}",
                "desc": "assets test plugin",
                "author": "AstrBot",
                "version": "1.0.0",
                "runtime": {
                    "api": "sdk",
                    "entrypoint": "main:TestPlugin",
                    "sdk_version": ">=0.1,<0.2",
                },
            }
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(source, encoding="utf-8")


def make_assets(grants=("assets.transfer",), invoker=None):
    import logging

    ctx = PluginContext(
        plugin=PluginInfo(
            id="test/plugin",
            name="plugin",
            version="1.0.0",
            runtime_mode=RuntimeMode.ISOLATED,
        ),
        config={},
        logger=logging.getLogger("test"),
        capabilities=CapabilitySet.from_ids(*grants),
        _host_capability_invoker=invoker,
    )
    return ctx.assets


def test_face_and_forward_roundtrip() -> None:
    chain = MessageChain(Plain("hi"), Face(id=14), Forward(id="fwd-1"))
    decoded = decode_message_chain(encode_message_chain(chain))
    assert decoded[1] == Face(id=14)
    assert decoded[2] == Forward(id="fwd-1")


def test_unregistered_segment_type_downgrades_on_decode() -> None:
    decoded = decode_message_chain(
        [{"type": "Dice", "value": "6", "title": "roll"}],
    )
    assert isinstance(decoded[0], UnknownSegment)
    assert decoded[0].segment_type == "Dice"
    assert decoded[0].data["title"] == "roll"


@pytest.mark.asyncio
async def test_upload_inline_for_small_payload() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        calls.append((operation, payload))
        if operation == "upload":
            return {"asset": AssetRef(id="ast_1", size=len(payload["data"]))}
        raise AssertionError(operation)

    assets = make_assets(invoker=invoker)
    ref = await assets.upload(b"hello", filename="a.txt")
    assert ref.id == "ast_1"
    assert [op for op, _ in calls] == ["upload"]
    assert base64.b64decode(calls[0][1]["data"]) == b"hello"


@pytest.mark.asyncio
async def test_upload_chunked_for_large_payload() -> None:
    ops: list[str] = []
    received = bytearray()

    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        ops.append(operation)
        if operation == "upload_begin":
            return {"upload_id": "up_1"}
        if operation == "upload_chunk":
            received.extend(base64.b64decode(payload["data"]))
            return {}
        if operation == "upload_commit":
            return {"asset": AssetRef(id="ast_9")}
        raise AssertionError(operation)

    assets = make_assets(invoker=invoker)
    data = bytes(range(256)) * 3000  # ~750 KiB, forces three chunks
    ref = await assets.upload(data, media_type="application/octet-stream")
    assert ref.id == "ast_9"
    assert ops == [
        "upload_begin",
        "upload_chunk",
        "upload_chunk",
        "upload_chunk",
        "upload_commit",
    ]
    assert bytes(received) == data


@pytest.mark.asyncio
async def test_upload_aborts_after_chunk_failure() -> None:
    ops: list[str] = []

    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        ops.append(operation)
        if operation == "upload_begin":
            return {"upload_id": "up_1"}
        if operation == "upload_chunk":
            raise RuntimeError("boom")
        return {}

    assets = make_assets(invoker=invoker)
    with pytest.raises(RuntimeError):
        await assets.upload(b"x" * (300 * 1024))
    assert ops == ["upload_begin", "upload_chunk", "upload_abort"]


@pytest.mark.asyncio
async def test_download_downloads_in_chunks(tmp_path: Path) -> None:
    payload = b"z" * (600 * 1024)

    async def invoker(capability: str, operation: str, params: dict[str, Any]):
        if operation == "stat":
            return {"size": len(payload), "filename": "big.bin"}
        if operation == "read":
            chunk = payload[params["offset"] : params["offset"] + params["length"]]
            return {"data": base64.b64encode(chunk).decode("ascii")}
        raise AssertionError(operation)

    assets = make_assets(invoker=invoker)
    path = await assets.download(AssetRef(id="ast_1"))
    assert path.read_bytes() == payload
    path.unlink()


@pytest.mark.asyncio
async def test_prepare_outbound_replaces_local_sources(tmp_path: Path) -> None:
    async def invoker(capability: str, operation: str, payload: dict[str, Any]):
        if operation == "upload":
            return {"asset": AssetRef(id="ast_1")}
        raise AssertionError(operation)

    local = tmp_path / "note.txt"
    local.write_text("hi", encoding="utf-8")
    chain = MessageChain(
        Plain("look"),
        Image.from_file(local),
        Image.from_url("https://example.com/a.png"),
        Node(sender_id="1", content=[Image.from_bytes(b"raw")]),
    )
    prepared = await prepare_outbound_chain(chain, make_assets(invoker=invoker))
    assert isinstance(prepared[1].source, AssetRef)
    assert prepared[2].source == "https://example.com/a.png"
    assert isinstance(prepared[3].content[0].source, AssetRef)
    # Local sources no longer reach the protocol encoder.
    encode_message_chain(prepared)


@pytest.mark.asyncio
async def test_runner_auto_uploads_bytes_before_yield(tmp_path: Path) -> None:
    plugin_root = tmp_path / "assets_e2e"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import MessageEvent, Plugin, on
from astrbot_sdk.message_components import Image

class TestPlugin(Plugin):
    @on.command("pic")
    async def pic(self, event: MessageEvent):
        yield event.reply(Image.from_bytes(b"fake-png", media_type="image/png"))
""",
    )
    uploads: list[dict[str, Any]] = []

    async def host_handler(grant: CapabilityGrant, operation: str, payload: Any):
        if grant.id == "assets.transfer" and operation == "upload":
            uploads.append(payload)
            return {"asset": AssetRef(id="ast_host_1", size=8)}
        raise AssertionError(f"{grant.id} {operation}")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
    )
    try:
        await client.start()
        results = [r async for r in client.invoke("pic", make_event())]
        message = results[0].message
        assert isinstance(message[0].source, AssetRef)
        assert message[0].source.id == "ast_host_1"
        assert base64.b64decode(uploads[0]["data"]) == b"fake-png"
    finally:
        await client.close()
