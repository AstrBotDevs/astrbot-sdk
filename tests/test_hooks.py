from __future__ import annotations

import pytest

from astrbot_sdk.hooks import (
    HookWriteError,
    LLMRequest,
    build_stage_dto,
)
from astrbot_sdk.message_components import Plain, UnknownSegment
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.protocol.codec import decode_value, encode_value


def test_dto_records_field_and_list_ops() -> None:
    dto = build_stage_dto(
        "llm_request",
        {
            "prompt": "hi",
            "system_prompt": "base",
            "contexts": [{"role": "user", "content": "hi"}],
            "image_urls": [],
            "audio_urls": [],
            "model": "m1",
        },
    )
    dto.system_prompt += " be a cat"
    dto.contexts.append({"role": "user", "content": "more"})
    dto.model = "m2"
    dto.image_urls.clear()

    ops = dto._write_ops()
    assert ops[0] == {"field": "system_prompt", "op": "set", "value": "base be a cat"}
    assert ops[1]["op"] == "append"
    assert ops[2] == {"field": "model", "op": "set", "value": "m2"}
    assert ops[3] == {"field": "image_urls", "op": "clear"}
    # Local state reflects every mutation regardless of forwarding.
    assert dto.system_prompt == "base be a cat"
    assert len(dto.contexts) == 2
    assert dto.image_urls == []


def test_dto_replaces_list_items_as_whole_field_set() -> None:
    dto = LLMRequest(contexts=[{"role": "user", "content": "a"}])
    dto._init_lists()
    dto.contexts[0] = {"role": "user", "content": "b"}
    ops = dto._write_ops()
    assert ops == [
        {
            "field": "contexts",
            "op": "set",
            "value": [{"role": "user", "content": "b"}],
        },
    ]


def test_undeclared_field_fails_fast() -> None:
    dto = build_stage_dto("llm_request", {"prompt": "x"})
    with pytest.raises(HookWriteError):
        dto.temperature = 0.5


def test_observe_only_stages_have_no_dto() -> None:
    assert build_stage_dto("message_sent", {}) is None
    assert build_stage_dto("agent_start", {}) is None


def test_chain_assignment_encodes_segments() -> None:
    dto = build_stage_dto("message_result", {"chain": MessageChain(Plain("a"))})
    dto.chain = [Plain("b"), UnknownSegment(segment_type="Image", data={})]
    ops = dto._write_ops()
    assert ops == [
        {
            "field": "chain",
            "op": "set",
            "value": [
                {"type": "Plain", "text": "b"},
                {"type": "Unknown", "segment_type": "Image", "data": {}},
            ],
        },
    ]


def test_unknown_segment_round_trips_through_hook_payload() -> None:
    chain = MessageChain(
        Plain("hi"),
        UnknownSegment(segment_type="Image", data={"reason": "local"}),
    )
    # The exact host-to-plugin snapshot path used by invoke_hook.
    encoded = encode_value({"chain": chain}, allow_unknown=True)
    decoded = decode_value(encoded)
    assert decoded["chain"] == chain
    ops_dto = build_stage_dto("message_result", decoded)
    # In-place list mutations re-encode the whole chain; unknown segments
    # carried from the snapshot must not break the write ops.
    ops_dto.chain.append(Plain("tail"))
    ops_dto.chain.pop(0)
    ops = ops_dto._write_ops()
    assert ops[0] == {
        "field": "chain",
        "op": "append",
        "value": {"type": "Plain", "text": "tail"},
    }
    assert ops[1]["op"] == "set"
    assert ops[1]["value"] == [
        {"type": "Unknown", "segment_type": "Image", "data": {"reason": "local"}},
        {"type": "Plain", "text": "tail"},
    ]


def test_encode_value_stays_strict_by_default() -> None:
    chain = MessageChain(UnknownSegment(segment_type="Image", data={}))
    with pytest.raises(Exception, match="unsupported outbound message segment"):
        encode_value({"chain": chain})
