from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from astrbot_sdk.capabilities import CapabilitySet
from astrbot_sdk.context import PluginContext, PluginInfo, RuntimeMode
from astrbot_sdk.errors import HostUnavailable, InvalidRequest, RemoteHostError
from astrbot_sdk.events import (
    UMO,
    MessageEvent,
    MessageRef,
    MessageType,
    Sender,
)
from astrbot_sdk.message_components import Plain
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.services.sessions import (
    DefaultSessionFilter,
    SenderSessionFilter,
    SessionFilter,
    SessionService,
    _Waiter,
)


def make_event(text: str = "hello", sender_id: str = "42") -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO(
            platform_id="platform-1",
            message_type=MessageType.GROUP,
            session_id="group-1",
        ),
        platform_type="aiocqhttp",
        message_ref=MessageRef(id="message-1"),
        message=MessageChain(Plain(text)),
        sender=Sender(id=sender_id, name="Moon"),
        timestamp=datetime.now(UTC),
    )


class _Host:
    """Fake Host invoker recording capability calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.rearm_error: Exception | None = None

    async def __call__(self, capability: str, operation: str, payload: dict[str, Any]):
        self.calls.append((capability, operation, payload))
        if operation == "rearm" and self.rearm_error is not None:
            raise self.rearm_error
        return {"ok": True}


def make_service(
    host: _Host | None = None,
    *,
    grants: tuple[str, ...] = ("message.wait", "message.send"),
) -> tuple[PluginContext, SessionService]:
    ctx = PluginContext(
        plugin=PluginInfo(
            id="test/plugin",
            name="plugin",
            version="1.0.0",
            runtime_mode=RuntimeMode.ISOLATED,
        ),
        config={},
        logger=__import__("logging").getLogger("test"),
        capabilities=CapabilitySet.from_ids(*grants),
        data_dir=Path("."),
        _host_capability_invoker=host,
    )
    return ctx, ctx.sessions


def test_default_filter_scopes_to_umo() -> None:
    event = make_event()
    session_filter = DefaultSessionFilter()
    assert session_filter.umo_scoped is True
    assert session_filter.filter(event) == "platform-1:group:group-1"


def test_sender_filter_appends_sender() -> None:
    event = make_event(sender_id="99")
    session_filter = SenderSessionFilter()
    assert session_filter.umo_scoped is True
    assert session_filter.filter(event) == "platform-1:group:group-1:99"


def test_custom_filter_defaults_to_unscoped() -> None:
    class _Custom(SessionFilter):
        def filter(self, event: MessageEvent) -> str:
            return "custom"

    assert _Custom().umo_scoped is False


@pytest.mark.asyncio
async def test_wait_registers_with_host_payload() -> None:
    host = _Host()
    _, sessions = make_service(host)
    event = make_event()

    async with sessions.wait(event, timeout=30) as session:
        assert session is not None

    register = host.calls[0]
    assert register[0] == "message.wait"
    assert register[1] == "register"
    payload = register[2]
    assert payload["key"] == "platform-1:group:group-1"
    assert payload["timeout"] == 30.0
    assert payload["umo_scoped"] is True
    assert host.calls[-1][1] == "stop"


@pytest.mark.asyncio
async def test_wait_requires_an_event() -> None:
    _, sessions = make_service(_Host())
    with pytest.raises(InvalidRequest):
        sessions.wait()


@pytest.mark.asyncio
async def test_wait_rejects_non_positive_timeout() -> None:
    _, sessions = make_service(_Host())
    with pytest.raises(InvalidRequest):
        sessions.wait(make_event(), timeout=0)


@pytest.mark.asyncio
async def test_wait_uses_ambient_event_by_default() -> None:
    host = _Host()
    ctx, sessions = make_service(host)
    event = make_event()
    token = ctx._bind_ambient_event(event)
    try:
        wait = sessions.wait(timeout=5)
        assert wait._event is event
    finally:
        ctx._reset_ambient_event(token)


@pytest.mark.asyncio
async def test_consider_matches_by_filter_key() -> None:
    host = _Host()
    _, sessions = make_service(host)
    event = make_event()

    async with sessions.wait(event, filter=SenderSessionFilter()):
        waiter_id = next(iter(sessions._waiters))
        matched = sessions._consider(make_event(sender_id="42"))
        assert matched == [
            {"waiter_id": waiter_id, "key": "platform-1:group:group-1:42"}
        ]
        # A different sender must not match the sender-scoped waiter.
        assert sessions._consider(make_event(sender_id="7")) == []


@pytest.mark.asyncio
async def test_consider_treats_failing_filter_as_non_match() -> None:
    _, sessions = make_service(_Host())

    class _Broken(SessionFilter):
        def filter(self, event: MessageEvent) -> str:
            raise RuntimeError("boom")

    # Register the waiter manually because the filter never produces a key.
    sessions._waiters["w1"] = _Waiter(
        id="w1",
        key="k",
        filter=_Broken(),
        timeout=1.0,
        queue=asyncio.Queue(),
    )
    assert sessions._consider(make_event()) == []


@pytest.mark.asyncio
async def test_next_returns_delivered_event_without_rearm_when_queued() -> None:
    host = _Host()
    _, sessions = make_service(host)
    event = make_event()

    async with sessions.wait(event) as session:
        waiter_id = next(iter(sessions._waiters))
        sessions._deliver(waiter_id, make_event(text="answer"))
        got = await session.next()
        assert got.text == "answer"
    # register + stop only; the queued event must not trigger a rearm call.
    assert [call[1] for call in host.calls] == ["register", "stop"]


@pytest.mark.asyncio
async def test_next_rearms_host_clock_with_override() -> None:
    host = _Host()
    _, sessions = make_service(host)
    event = make_event()

    async with sessions.wait(event, timeout=30) as session:
        waiter_id = next(iter(sessions._waiters))
        task = asyncio.create_task(session.next(timeout=2))
        await asyncio.sleep(0)
        sessions._deliver(waiter_id, make_event(text="answer"))
        await task
    rearm = host.calls[1]
    assert rearm[1] == "rearm"
    assert rearm[2]["timeout"] == 2


@pytest.mark.asyncio
async def test_next_raises_timeout_on_host_timeout_push() -> None:
    host = _Host()
    _, sessions = make_service(host)
    event = make_event()

    async with sessions.wait(event) as session:
        waiter_id = next(iter(sessions._waiters))
        sessions._timeout(waiter_id)
        with pytest.raises(TimeoutError):
            await session.next()


@pytest.mark.asyncio
async def test_next_maps_expired_rearm_to_timeout() -> None:
    host = _Host()
    host.rearm_error = RemoteHostError("NOT_FOUND", "waiter gone")
    _, sessions = make_service(host)
    event = make_event()

    async with sessions.wait(event) as session:
        with pytest.raises(TimeoutError):
            await session.next()


@pytest.mark.asyncio
async def test_fail_all_unblocks_waiters() -> None:
    host = _Host()
    _, sessions = make_service(host)
    event = make_event()

    async with sessions.wait(event) as session:
        sessions._fail_all(HostUnavailable("host gone"))
        with pytest.raises(HostUnavailable):
            await session.next()


@pytest.mark.asyncio
async def test_ask_sends_message_then_waits() -> None:
    host = _Host()
    _, sessions = make_service(host)
    event = make_event()

    async with sessions.wait(event) as session:
        waiter_id = next(iter(sessions._waiters))
        task = asyncio.create_task(session.ask("question?"))
        await asyncio.sleep(0)
        sessions._deliver(waiter_id, make_event(text="reply"))
        got = await task
        assert got.text == "reply"

    operations = [call[1] for call in host.calls if call[0] == "message.send"]
    assert operations == ["send"]
