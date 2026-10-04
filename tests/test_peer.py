from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrbot_sdk.errors import InvalidRequest, RemoteHostError, RemotePluginError
from astrbot_sdk.protocol import ProtocolFrame, RequestFrame
from astrbot_sdk.runtime.peer import Peer


@pytest.mark.asyncio
async def test_peer_supports_nested_bidirectional_requests() -> None:
    host_peer: Peer
    runner_peer: Peer

    async def send_host(frame: ProtocolFrame) -> None:
        assert await runner_peer.receive(frame)

    async def send_runner(frame: ProtocolFrame) -> None:
        assert await host_peer.receive(frame)

    async def handle_host(frame: RequestFrame) -> Any:
        if frame.method != "host.echo":
            raise InvalidRequest("unknown Host method")
        return {"echo": frame.params["value"]}

    async def handle_runner(frame: RequestFrame) -> Any:
        if frame.method != "runner.nested":
            raise InvalidRequest("unknown Runner method")
        return await runner_peer.request(
            "host.echo",
            {"value": frame.params["value"]},
        )

    host_peer = Peer(
        send=send_host,
        request_handler=handle_host,
        request_id_prefix="host:",
        remote_error_factory=RemotePluginError,
    )
    runner_peer = Peer(
        send=send_runner,
        request_handler=handle_runner,
        request_id_prefix="runner:",
        remote_error_factory=RemoteHostError,
    )

    try:
        assert await host_peer.request(
            "runner.nested",
            {"value": "hello"},
        ) == {"echo": "hello"}
    finally:
        await host_peer.close()
        await runner_peer.close()


@pytest.mark.asyncio
async def test_peer_propagates_request_cancellation() -> None:
    host_peer: Peer
    runner_peer: Peer
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def send_host(frame: ProtocolFrame) -> None:
        await runner_peer.receive(frame)

    async def send_runner(frame: ProtocolFrame) -> None:
        await host_peer.receive(frame)

    async def handle_host(frame: RequestFrame) -> Any:
        raise InvalidRequest(f"unknown Host method: {frame.method}")

    async def handle_runner(frame: RequestFrame) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    host_peer = Peer(
        send=send_host,
        request_handler=handle_host,
        request_id_prefix="host:",
        remote_error_factory=RemotePluginError,
    )
    runner_peer = Peer(
        send=send_runner,
        request_handler=handle_runner,
        request_id_prefix="runner:",
        remote_error_factory=RemoteHostError,
    )

    call = asyncio.create_task(host_peer.request("runner.wait", {}))
    try:
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(cancelled.wait(), timeout=1)
    finally:
        await host_peer.close()
        await runner_peer.close()
