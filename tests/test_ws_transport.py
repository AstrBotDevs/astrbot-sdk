from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest
import websockets

from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.results import MessageResult
from astrbot_sdk.runtime import RUNNER_TOKEN_ENV, WSPluginClient, WSPluginListener
from astrbot_sdk.runtime.env import runner_env
from astrbot_sdk.runtime.transport import WebSocketTransport
from tests.test_stdio_transport import make_event, write_plugin


async def spawn_ws_runner(
    plugin_root: Path,
    url: str,
    token: str,
) -> asyncio.subprocess.Process:
    """Spawn one real plugin Runner process dialed into the test listener."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "astrbot_sdk.runtime",
        "--ws",
        url,
        "--plugin-root",
        str(plugin_root),
        env=runner_env({RUNNER_TOKEN_ENV: token}),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


class ListenerHarness:
    """Run a listener that hands accepted transports to the test."""

    def __init__(self, token: str) -> None:
        """Configure the harness with the one accepted bearer token."""
        self.token = token
        self.transports: asyncio.Queue[WebSocketTransport] = asyncio.Queue()
        self.listener = WSPluginListener(
            "127.0.0.1",
            0,
            authenticate=lambda presented: presented == token,
            on_connection=self._own_connection,
        )

    async def _own_connection(
        self,
        transport: WebSocketTransport,
        _token: str,
    ) -> None:
        await self.transports.put(transport)
        await transport.wait_closed()

    async def __aenter__(self) -> ListenerHarness:
        await self.listener.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.listener.close()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.listener.port}"

    async def next_client(self, **kwargs: object) -> WSPluginClient:
        """Wait for the next Runner connection and wrap it in a client."""
        transport = await asyncio.wait_for(self.transports.get(), timeout=10)
        return WSPluginClient(transport, **kwargs)


@pytest.mark.asyncio
async def test_ws_runner_round_trip_and_graceful_exit(tmp_path: Path) -> None:
    plugin_root = tmp_path / "ws_echo"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("hello")
    async def hello(self, event):
        yield event.reply("first")
        yield event.reply("second")
""",
    )
    async with ListenerHarness("secret-token") as harness:
        process = await spawn_ws_runner(plugin_root, harness.url, "secret-token")
        try:
            client = await harness.next_client()
            try:
                handshake = await client.start()
                assert handshake.name == "plugin_ws_echo"
                assert {(h.id, h.kind.value) for h in handshake.handlers} == {
                    ("hello", "command")
                }

                stream = client.invoke("hello", make_event())
                first = await anext(stream)
                assert isinstance(first, MessageResult)
                assert first.message.text == "first"
                second = await anext(stream)
                assert isinstance(second, MessageResult)
                assert second.message.text == "second"
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)
            finally:
                await client.close()

            # The shutdown request ends the serve loop, so the Runner
            # process exits on its own without a terminate/kill.
            await asyncio.wait_for(process.wait(), timeout=10)
            assert process.returncode == 0
        finally:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()


@pytest.mark.asyncio
async def test_ws_runner_can_call_host_during_handler_invocation(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "ws_reverse_rpc"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("ask")
    async def ask(self):
        result = await self.ctx._invoke_capability(
            "llm.generate",
            "generate",
            {"prompt": "hello"},
        )
        yield result["text"]
""",
        required_capabilities=("llm.generate",),
    )
    calls: list[tuple[CapabilityGrant, str, dict[str, object]]] = []

    async def invoke_host(
        grant: CapabilityGrant,
        operation: str,
        payload: dict[str, object],
    ) -> dict[str, str]:
        calls.append((grant, operation, payload))
        return {"text": "from Host"}

    async with ListenerHarness("secret-token") as harness:
        process = await spawn_ws_runner(plugin_root, harness.url, "secret-token")
        try:
            client = await harness.next_client(capability_handler=invoke_host)
            grants = CapabilitySet(
                [CapabilityGrant("llm.generate", {"providers": ["default"]})]
            )
            try:
                await client.start(granted_capabilities=grants)
                stream = client.invoke("ask")
                result = await anext(stream)
                assert isinstance(result, MessageResult)
                assert result.message.text == "from Host"
                assert calls == [
                    (grants["llm.generate"], "generate", {"prompt": "hello"})
                ]
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)
            finally:
                await client.close()
        finally:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()


@pytest.mark.asyncio
async def test_ws_listener_rejects_missing_and_wrong_tokens(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "ws_unauthorized"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin

class TestPlugin(Plugin):
    pass
""",
    )
    async with ListenerHarness("secret-token") as harness:
        for headers in (
            None,
            {"Authorization": "Bearer wrong-token"},
            {"Authorization": "Basic c2VjcmV0"},
        ):
            with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
                async with websockets.connect(
                    harness.url,
                    additional_headers=headers,
                    proxy=None,
                ):
                    pass
            assert exc_info.value.response.status_code == 401
        assert harness.transports.empty()
