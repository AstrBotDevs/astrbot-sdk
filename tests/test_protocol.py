from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astrbot_sdk.assets import AssetRef
from astrbot_sdk.errors import InvalidRequest
from astrbot_sdk.events import (
    UMO,
    CommandInvocation,
    MessageEvent,
    MessageRef,
    MessageType,
    Sender,
    SenderRole,
)
from astrbot_sdk.message_components import (
    At,
    AtAll,
    Image,
    Node,
    Nodes,
    Plain,
    Reply,
    UnknownSegment,
)
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.protocol import (
    MAX_FRAME_BYTES,
    AckFrame,
    RequestFrame,
    decode_frame,
    decode_message_event,
    decode_result,
    encode_frame,
    encode_message_chain,
    encode_message_event,
    encode_result,
)
from astrbot_sdk.results import MessageResult, Propagation


def make_event() -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO("platform-1", MessageType.GROUP, "group-1"),
        platform_type="aiocqhttp",
        message_ref=MessageRef("message-1"),
        message=MessageChain(
            Plain("hello"),
            At("42", "Moon"),
            AtAll(),
            Reply("previous"),
            Image(AssetRef("asset-1", media_type="image/png")),
            Nodes([Node("42", [Plain("nested")], "Moon")]),
            UnknownSegment("PlatformCard", {"id": "card-1"}),
        ),
        sender=Sender("42", "Moon", SenderRole.ADMIN),
        timestamp=datetime.now(UTC),
        command=CommandInvocation("hello", {"count": 2}),
        is_wake=True,
    )


@pytest.mark.parametrize(
    "frame",
    [
        RequestFrame("request-1", "initialize", {"versions": [1]}),
        AckFrame("request-1", 2),
    ],
)
def test_protocol_frame_round_trip(frame: object) -> None:
    assert decode_frame(encode_frame(frame)) == frame


def test_message_event_round_trip() -> None:
    event = make_event()

    decoded = decode_message_event(encode_message_event(event))

    assert decoded == event
    assert decoded.message.text == "hello"


def test_message_event_extras_round_trip() -> None:
    from dataclasses import replace

    event = replace(make_event(), extras={"plugins_name": ["a"], "n": 1})

    decoded = decode_message_event(encode_message_event(event))

    assert dict(decoded.extras) == {"plugins_name": ["a"], "n": 1}


def test_message_result_round_trip() -> None:
    result = MessageResult(
        propagation=Propagation.STOP,
        message=MessageChain(Plain("done")),
        quote=True,
    )

    assert decode_result(encode_result(result)) == result


def test_unknown_segment_is_inbound_only() -> None:
    chain = MessageChain(UnknownSegment("PlatformCard", {"id": "card-1"}))

    with pytest.raises(InvalidRequest, match="unsupported outbound"):
        encode_message_chain(chain)

    encoded = encode_message_chain(chain, allow_unknown=True)
    assert encoded[0]["type"] == "Unknown"


def test_local_media_requires_asset_upload() -> None:
    with pytest.raises(InvalidRequest, match="must be uploaded"):
        encode_message_chain(MessageChain(Image.from_file("image.png")))


def test_invalid_frame_is_rejected() -> None:
    with pytest.raises(InvalidRequest):
        decode_frame(b'{"type":"ack","id":"call","sequence":0}\n')


def test_oversized_frame_is_rejected_before_json_parsing() -> None:
    with pytest.raises(InvalidRequest, match="size limit"):
        decode_frame(b"x" * (MAX_FRAME_BYTES + 1))
