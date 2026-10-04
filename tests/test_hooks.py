from __future__ import annotations

import pytest

from astrbot_sdk.hooks import (
    HookWriteError,
    LLMRequest,
    build_stage_dto,
)


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
