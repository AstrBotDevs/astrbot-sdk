from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from astrbot_sdk.capabilities import CapabilitySet
from astrbot_sdk.errors import CapabilityDenied, InvalidPluginDefinition
from astrbot_sdk.events import (
    UMO,
    MessageEvent,
    MessageRef,
    MessageType,
    Sender,
)
from astrbot_sdk.message_components import Plain
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.results import MessageResult, Propagation
from astrbot_sdk.runtime import load_plugin


def write_plugin(
    plugin_root: Path,
    source: str,
    *,
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
) -> None:
    plugin_root.mkdir()
    metadata = {
        "schema_version": 2,
        "name": f"plugin_{plugin_root.name}",
        "desc": "Test plugin",
        "author": "AstrBot",
        "version": "1.0.0",
        "runtime": {
            "api": "sdk",
            "entrypoint": "main:TestPlugin",
            "sdk_version": ">=0.1,<0.2",
        },
        "capabilities": {
            "required": [{"id": item} for item in required],
            "optional": [{"id": item} for item in optional],
        },
    }
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(metadata),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(source, encoding="utf-8")


def make_event() -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO("platform-1", MessageType.PRIVATE, "user-1"),
        platform_type="webchat",
        message_ref=MessageRef("message-1"),
        message=MessageChain(Plain("hello")),
        sender=Sender("user-1", "Moon"),
        timestamp=datetime.now(UTC),
    )


def test_repository_example_loads() -> None:
    example_root = Path(__file__).parents[1] / "examples" / "hello"

    loaded = load_plugin(example_root)

    assert loaded.metadata.name == "astrbot_plugin_hello_sdk"
    assert loaded.get_handler("hello").spec.path == "hello"


@pytest.mark.asyncio
async def test_loads_plugin_and_preserves_yield_resume_boundary(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "yield_plugin"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, lifecycle, on
from astrbot_sdk.results import Propagation

class TestPlugin(Plugin):
    @lifecycle.startup
    async def startup(self):
        self.steps = ["startup"]

    @on.command("hello")
    async def hello(self, event):
        try:
            self.steps.append("before-first")
            yield event.reply("first")
            self.steps.append("after-first")
            yield event.reply("stop", propagation=Propagation.STOP)
            self.steps.append("after-stop")
        finally:
            self.steps.append("closed")

    @lifecycle.shutdown
    async def shutdown(self, event):
        self.steps.append("shutdown")
""",
    )

    loaded = load_plugin(plugin_root)
    await loaded.start()
    stream = loaded.invoke("hello", make_event())

    first = await anext(stream)
    assert isinstance(first, MessageResult)
    assert first.message.text == "first"
    assert loaded.instance.steps == ["startup", "before-first"]

    second = await anext(stream)
    assert isinstance(second, MessageResult)
    assert second.message.text == "stop"
    assert second.propagation is Propagation.STOP
    assert loaded.instance.steps == [
        "startup",
        "before-first",
        "after-first",
        "closed",
    ]

    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert "after-stop" not in loaded.instance.steps

    await loaded.shutdown()
    assert loaded.instance.steps[-1] == "shutdown"


@pytest.mark.asyncio
async def test_coroutine_message_like_return_is_normalized(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "return_plugin"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("ping")
    async def ping(self):
        return "pong"
""",
    )

    loaded = load_plugin(plugin_root)
    result = await anext(loaded.invoke("ping"))

    assert isinstance(result, MessageResult)
    assert result.message == MessageChain(Plain("pong"))


def test_relative_imports_are_scoped_to_plugin_namespace(tmp_path: Path) -> None:
    plugin_root = tmp_path / "relative_plugin"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on
from .helper import VALUE

class TestPlugin(Plugin):
    @on.command("value")
    async def value(self):
        return VALUE
""",
    )
    (plugin_root / "helper.py").write_text('VALUE = "local"\n', encoding="utf-8")

    loaded = load_plugin(plugin_root)

    assert loaded.get_handler("value").spec.path == "value"


def test_namespace_spec_mirrors_real_package(tmp_path: Path) -> None:
    # Flask/Quart instance-path discovery introspects importlib metadata of the
    # plugin's root module; a spec without origin/search locations breaks it.
    import sys

    from astrbot_sdk.runtime.loader import _install_namespace

    plugin_root = tmp_path / "spec_plugin"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin

class TestPlugin(Plugin):
    pass
""",
    )

    namespace = _install_namespace(plugin_root)
    spec = sys.modules[namespace].__spec__

    assert spec is not None
    assert list(spec.submodule_search_locations or []) == [str(plugin_root)]
    # No __init__.py: the namespace package convention flask understands.
    assert spec.origin == "namespace"

    (plugin_root / "__init__.py").write_text("", encoding="utf-8")
    sys.modules.pop(namespace, None)

    namespace = _install_namespace(plugin_root)
    spec = sys.modules[namespace].__spec__

    assert spec is not None
    assert spec.origin == str(plugin_root / "__init__.py")


def test_required_capability_must_be_granted(tmp_path: Path) -> None:
    plugin_root = tmp_path / "required_capability"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.message()
    async def message(self, event):
        return None
""",
        required=("message.receive",),
    )

    with pytest.raises(CapabilityDenied, match="message.receive"):
        load_plugin(plugin_root)

    loaded = load_plugin(
        plugin_root,
        granted_capabilities=CapabilitySet.from_ids("message.receive"),
    )
    assert loaded.get_handler("message").spec.required_capability == "message.receive"


def test_denied_optional_capability_disables_handler(tmp_path: Path) -> None:
    plugin_root = tmp_path / "optional_capability"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.command("ping")
    async def ping(self):
        return "pong"

    @on.message()
    async def receive(self, event):
        return None
""",
        optional=("message.receive",),
    )

    loaded = load_plugin(plugin_root)

    assert {item.id for item in loaded.registrations} == {"ping"}


def test_handler_capability_must_be_declared(tmp_path: Path) -> None:
    plugin_root = tmp_path / "undeclared_capability"
    write_plugin(
        plugin_root,
        """
from astrbot_sdk import Plugin, on

class TestPlugin(Plugin):
    @on.message()
    async def receive(self, event):
        return None
""",
    )

    with pytest.raises(InvalidPluginDefinition, match="undeclared"):
        load_plugin(plugin_root)


def test_legacy_metadata_reads_display_fields(tmp_path: Path) -> None:
    """Legacy metadata.yaml fields must reach the dashboard card like in-process."""
    from astrbot_sdk.runtime.loader import _load_metadata_for_legacy

    plugin_root = tmp_path / "my_plugin"
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "my_plugin",
                "desc": "Long description",
                "author": "Tester",
                "version": "2.3.4",
                "display_name": "我的插件",
                "short_desc": "Short",
                "repo": "https://example.com/repo",
                "astrbot_version": ">=4.0.0",
                "support_platforms": ["telegram", "webchat"],
                "i18n": {"zh-CN": {"desc": "描述"}},
            }
        ),
        encoding="utf-8",
    )

    metadata = _load_metadata_for_legacy(plugin_root)

    assert metadata.display_name == "我的插件"
    assert metadata.short_desc == "Short"
    assert metadata.repo == "https://example.com/repo"
    assert metadata.astrbot_version == ">=4.0.0"
    assert metadata.support_platforms == ("telegram", "webchat")
    assert metadata.i18n == {"zh-CN": {"desc": "描述"}}
