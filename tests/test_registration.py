from __future__ import annotations

import re

import pytest

from astrbot_sdk import Plugin, lifecycle, on
from astrbot_sdk.errors import (
    DuplicateHandlerID,
    InvalidHandlerSignature,
    InvalidPluginDefinition,
)
from astrbot_sdk.registration import HandlerKind, discover_handlers


def test_command_registration_is_static_and_uses_docstring() -> None:
    class TestPlugin(Plugin[None]):
        @on.command("math   add", aliases={"sum"}, priority=5)
        async def add(self, left: int, right: int) -> int:
            """Add two integers.

            More detail is not part of the short description.
            """
            return left + right

    registration = discover_handlers(TestPlugin(None))[0]

    assert registration.id == "add"
    assert registration.spec.kind is HandlerKind.COMMAND
    assert registration.spec.path == "math add"
    assert registration.spec.aliases == ("sum",)
    assert registration.spec.description == "Add two integers."
    assert registration.spec.priority == 5


def test_message_registration_serializes_compiled_regex() -> None:
    class TestPlugin(Plugin[None]):
        @on.message(regex=re.compile(r"^hello$", re.IGNORECASE))
        async def hello(self) -> None:
            return None

    spec = discover_handlers(TestPlugin(None))[0].spec

    assert spec.regex == "^hello$"
    assert spec.regex_flags & re.IGNORECASE
    assert spec.required_capability == "message.receive"


def test_sync_handler_is_rejected() -> None:
    with pytest.raises(InvalidHandlerSignature):

        class TestPlugin(Plugin[None]):
            @on.command("hello")
            def hello(self) -> str:
                return "hello"


def test_multiple_registration_decorators_are_rejected() -> None:
    with pytest.raises(InvalidPluginDefinition, match="one SDK registration"):

        class TestPlugin(Plugin[None]):
            @on.command("hello")
            @on.message()
            async def hello(self) -> None:
                return None


def test_duplicate_handler_id_is_rejected() -> None:
    class TestPlugin(Plugin[None]):
        @on.command("one", id="same")
        async def one(self) -> None:
            return None

        @on.command("two", id="same")
        async def two(self) -> None:
            return None

    with pytest.raises(DuplicateHandlerID):
        discover_handlers(TestPlugin(None))


def test_lifecycle_registration_uses_reserved_kind() -> None:
    class TestPlugin(Plugin[None]):
        @lifecycle.startup
        async def prepare(self) -> None:
            return None

    registration = discover_handlers(TestPlugin(None))[0]

    assert registration.id == "lifecycle.startup"
    assert registration.spec.kind is HandlerKind.LIFECYCLE_STARTUP


def test_lifecycle_signature_is_checked_at_definition_time() -> None:
    with pytest.raises(InvalidHandlerSignature, match="invalid signature"):

        class TestPlugin(Plugin[None]):
            @lifecycle.shutdown
            async def shutdown(self) -> None:
                return None


def test_lifecycle_cannot_yield() -> None:
    with pytest.raises(InvalidHandlerSignature, match="async generator"):

        class TestPlugin(Plugin[None]):
            @lifecycle.startup
            async def startup(self):
                yield None
