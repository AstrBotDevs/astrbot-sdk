from __future__ import annotations

from datetime import UTC, datetime

from astrbot_sdk.events import (
    UMO,
    MessageEvent,
    MessageRef,
    MessageType,
    Sender,
)
from astrbot_sdk.message_components import At, Plain, Record
from astrbot_sdk.messages import MessageChain, to_message_chain
from astrbot_sdk.results import Propagation


def make_event() -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO(
            platform_id="platform-1",
            message_type=MessageType.GROUP,
            session_id="group-1",
        ),
        platform_type="aiocqhttp",
        message_ref=MessageRef(id="message-1"),
        message=MessageChain(Plain("hello"), At("42")),
        sender=Sender(id="42", name="Moon"),
        timestamp=datetime.now(UTC),
    )


def test_message_chain_preserves_segments_and_plain_text() -> None:
    chain = MessageChain(Plain("a"), At("42"), Plain("b"))

    assert tuple(chain) == (Plain("a"), At("42"), Plain("b"))
    assert chain.text == "ab"


def test_message_like_converts_to_immutable_chain() -> None:
    assert to_message_chain("hello") == MessageChain(Plain("hello"))
    assert to_message_chain([Plain("a"), At("42")]) == MessageChain(
        Plain("a"),
        At("42"),
    )


def test_event_reply_and_stop() -> None:
    event = make_event()

    reply = event.reply("hello", quote=True)
    assert reply.message == MessageChain(Plain("hello"))
    assert reply.quote is True
    assert reply.propagation is Propagation.CONTINUE
    assert event.stop().propagation is Propagation.STOP
    assert event.is_group is True
    assert event.text == "hello"


def test_record_keeps_runner_local_source() -> None:
    record = Record.from_file("voice.wav", media_type="audio/wav")

    assert str(record.source) == "voice.wav"
    assert record.media_type == "audio/wav"
