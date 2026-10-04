"""Runner environment whitelist tests."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from astrbot_sdk.runtime.env import runner_env
from tests.test_stdio_transport import write_plugin


def test_runner_env_filters_and_overrides() -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "OPENAI_API_KEY": "sk-secret",
        "DATABASE_URL": "postgres://secret",
        "PYTHONPATH": "/evil",
        "HTTP_PROXY": "http://proxy:8080",
    }
    env = runner_env(base=base)
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/test"
    assert env["HTTP_PROXY"] == "http://proxy:8080"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert "OPENAI_API_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "PYTHONPATH" not in env

    overridden = runner_env(
        {"ASTRBOT_DATA_PATH": "/data", "PATH": "/custom"},
        base=base,
    )
    assert overridden["ASTRBOT_DATA_PATH"] == "/data"
    assert overridden["PATH"] == "/custom"


def make_event() -> MessageEvent:
    return MessageEvent(
        id="event-1",
        umo=UMO("platform-1", MessageType.PRIVATE, "user-1"),
        platform_type="webchat",
        message_ref=MessageRef("message-1"),
        message=MessageChain(Plain("env")),
        sender=Sender("user-1", "Moon"),
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_runner_receives_whitelisted_env_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "envcheck"
    write_plugin(
        plugin_root,
        """
import os

from astrbot_sdk import Plugin


class TestPlugin(Plugin):
    from astrbot_sdk import on

    @on.command("envcheck")
    async def envcheck(self, event):
        keys = [
            "OPENAI_API_KEY",
            "PYTHONPATH",
            "PYTHONNOUSERSITE",
            "PATH",
            "MARKER_VAR",
        ]
        report = {key: os.environ.get(key) for key in keys}
        yield event.reply(str(sorted(report.items())))
""",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("PYTHONPATH", "/evil")

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        env={"MARKER_VAR": "marker"},
    )
    try:
        await client.start()
        results = [r async for r in client.invoke("envcheck", make_event())]
        text = results[-1].message.text
        assert "'OPENAI_API_KEY', None" in text
        assert "'PYTHONPATH', None" in text
        assert "'PYTHONNOUSERSITE', '1'" in text
        assert "'MARKER_VAR', 'marker'" in text
        # PATH survives (plugins spawn subprocesses).
        assert "'PATH', None" not in text
    finally:
        await client.close()
