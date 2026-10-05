"""Legacy session_waiter compat tests on top of the message.wait capability."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.events import (
    UMO,
    MessageEvent,
    MessageRef,
    MessageType,
    Sender,
)
from astrbot_sdk.message_components import Plain
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.runtime import StdioPluginClient

LEGACY_PLUGIN = """
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.session_waiter import SessionController, session_waiter


class LegacyWaiterPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)

    @filter.command("quiz")
    async def quiz(self, event: AstrMessageEvent):
        yield event.plain_result("Q1: 2+2=?")

        @session_waiter(timeout=30)
        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            if evt.message_str.strip() == "4":
                await evt.send(evt.plain_result("win"))
                controller.stop()
            else:
                await evt.send(evt.plain_result("wrong"))
                controller.keep(30, reset_timeout=True)

        await waiter(event)
"""


def write_legacy_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_waiter",
                "author": "tester",
                "desc": "legacy waiter plugin",
                "version": "1.0.0",
            }
        ),
        "utf-8",
    )
    (plugin_root / "main.py").write_text(LEGACY_PLUGIN, "utf-8")


def make_event(text: str = "quiz", message_id: str = "message-1") -> MessageEvent:
    return MessageEvent(
        id=message_id,
        umo=UMO("platform-1", MessageType.PRIVATE, "user-1"),
        platform_type="webchat",
        message_ref=MessageRef(message_id),
        message=MessageChain(Plain(text)),
        sender=Sender("user-1", "Moon"),
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_legacy_session_waiter_round_trip(tmp_path: Path) -> None:
    plugin_root = tmp_path / "legacy_waiter"
    write_legacy_plugin(plugin_root)

    wait_calls: list[tuple[str, dict[str, Any]]] = []
    sent: list[dict[str, Any]] = []

    async def host_handler(
        grant: CapabilityGrant,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if grant.id == "message.wait":
            wait_calls.append((operation, payload))
            return {"ok": True}
        if grant.id == "message.send" and operation == "send":
            sent.append(payload)
            return {"message_id": "out-1"}
        raise AssertionError(f"unexpected call: {grant.id} {operation}")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
        legacy=True,
    )
    try:
        await client.start(
            config={},
            granted_capabilities=CapabilitySet.from_ids(
                "storage.kv",
                "assets.transfer",
                "message.send",
                "message.wait",
            ),
        )

        stream = client.invoke("quiz", make_event())
        first = await stream.__anext__()
        assert first is not None and first.message.text == "Q1: 2+2=?"

        # The rest of the generator blocks inside the session wait until the
        # legacy handler stops the controller.
        rest = asyncio.create_task(_collect(stream))
        for _ in range(100):
            if wait_calls:
                break
            await asyncio.sleep(0.05)
        assert wait_calls and wait_calls[0][0] == "register"
        register = wait_calls[0][1]
        assert register["key"] == "platform-1:FriendMessage:user-1"
        waiter_id = register["waiter_id"]

        # A wrong turn keeps the session alive and answers through evt.send.
        wrong = make_event(text="5", message_id="message-2")
        consider = await client.session_consider(wrong)
        assert consider["matches"] == [
            {"waiter_id": waiter_id, "key": "platform-1:FriendMessage:user-1"}
        ]
        await client.session_matched(waiter_id, wrong)
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.05)
        assert sent[0]["message"].text == "wrong"
        assert not rest.done()

        # The right answer stops the controller and finishes the command.
        right = make_event(text="4", message_id="message-3")
        await client.session_matched(waiter_id, right)
        results = await asyncio.wait_for(rest, timeout=10)
        assert all(r is None or r.message.text for r in results)
        assert sent[-1]["message"].text == "win"

        # The Host claim was released when the wait ended.
        assert wait_calls[-1][0] == "stop"
        assert wait_calls[-1][1]["waiter_id"] == waiter_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_session_waiter_timeout_unblocks(tmp_path: Path) -> None:
    plugin_root = tmp_path / "legacy_waiter"
    write_legacy_plugin(plugin_root)

    wait_calls: list[tuple[str, dict[str, Any]]] = []

    async def host_handler(
        grant: CapabilityGrant,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if grant.id == "message.wait":
            wait_calls.append((operation, payload))
            return {"ok": True}
        if grant.id == "message.send" and operation == "send":
            return {"message_id": "out-1"}
        raise AssertionError(f"unexpected call: {grant.id} {operation}")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        capability_handler=host_handler,
        legacy=True,
    )
    try:
        await client.start(
            config={},
            granted_capabilities=CapabilitySet.from_ids(
                "storage.kv",
                "assets.transfer",
                "message.send",
                "message.wait",
            ),
        )

        stream = client.invoke("quiz", make_event())
        await stream.__anext__()
        rest = asyncio.create_task(_collect(stream))
        for _ in range(100):
            if wait_calls:
                break
            await asyncio.sleep(0.05)
        assert wait_calls and wait_calls[0][0] == "register"
        waiter_id = wait_calls[0][1]["waiter_id"]

        # The Host clock fired: the legacy handler surfaces the old 等待超时
        # TimeoutError out of the command handler.
        await client.session_timeout(waiter_id)
        with pytest.raises(Exception, match="等待超时"):
            await asyncio.wait_for(rest, timeout=10)
    finally:
        await client.close()


async def _collect(stream: Any) -> list[Any]:
    return [r async for r in stream]
