from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from astrbot_sdk.config import (
    MAX_CONFIG_SCHEMA_BYTES,
    PluginConfig,
    export_config_schema,
    reject_external_schema_refs,
    schema_has_sensitive_fields,
)
from astrbot_sdk.errors import InvalidConfig, RemotePluginError
from astrbot_sdk.events import UMO, MessageEvent, MessageRef, MessageType, Sender
from astrbot_sdk.messages import MessageChain, Plain
from astrbot_sdk.runtime import StdioPluginClient


def _write_config_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "metadata.yaml").write_text(
        """
schema_version: 2
name: cfg_lifecycle_test
desc: Config lifecycle test plugin
version: 0.1.0
author: test
runtime:
  api: sdk
  entrypoint: main:CfgPlugin
  sdk_version: ">=0.1.0"
capabilities:
  required: []
  optional: []
""".strip(),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(
        """
from __future__ import annotations

import asyncio
from pathlib import Path
from pydantic import Field

from astrbot_sdk import MessageEvent, Plugin, PluginConfig, lifecycle, on


class MyConfig(PluginConfig):
    greeting: str = Field(default="hi", min_length=1)
    token: str = Field(
        default="",
        json_schema_extra={"writeOnly": True, "format": "password"},
    )
    trigger_bg_crash: bool = False


class CfgPlugin(Plugin[MyConfig]):
    config_model = MyConfig

    def __init__(self, ctx):
        super().__init__(ctx)
        self.events: list[str] = []

    @lifecycle.validate_config
    async def validate_cfg(self, candidate: MyConfig) -> None:
        if candidate.greeting == "reject_validate":
            raise ValueError("rejected by validator")

    @lifecycle.config_changed
    async def on_changed(self, event: lifecycle.ConfigChangedEvent) -> None:
        if event.current.greeting == "boom_on_apply":
            raise RuntimeError("boom during apply")
        if event.current.trigger_bg_crash:
            async def _crash() -> None:
                raise RuntimeError("background boom")

            self.ctx.tasks.spawn("bg-crash", _crash())
            await asyncio.sleep(0)

    @lifecycle.shutdown
    async def on_stop(self, event: lifecycle.ShutdownEvent) -> None:
        record = Path(self.ctx.data_dir) / "shutdown_reason.txt"
        record.write_text(event.reason, encoding="utf-8")

    @on.command("show")
    async def show(self, event: MessageEvent) -> str:
        _ = event
        return self.ctx.current_config().greeting
""".strip(),
        encoding="utf-8",
    )


def test_config_schema_export_limits_and_sensitive_detection(tmp_path: Path) -> None:
    from pydantic import Field

    class AppConfig(PluginConfig):
        prefix: str = Field(default="[sdk] ")
        api_key: str = Field(
            default="",
            json_schema_extra={"writeOnly": True, "format": "password"},
        )

    out = export_config_schema(AppConfig, tmp_path)
    schema = json.loads(out.read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema_has_sensitive_fields(schema) is True
    assert (
        schema_has_sensitive_fields(
            {"type": "object", "properties": {"a": {"type": "string"}}}
        )
        is False
    )

    with pytest.raises(InvalidConfig):
        reject_external_schema_refs(
            {"type": "object", "$ref": "https://example.com/a.json"}
        )

    class HugeConfig(PluginConfig):
        big: str = Field(default="x", description="y" * (MAX_CONFIG_SCHEMA_BYTES + 100))

    with pytest.raises(InvalidConfig, match="64 KiB"):
        export_config_schema(HugeConfig, tmp_path / "huge")


def test_config_schema_cli_export(tmp_path: Path) -> None:
    mod_file = tmp_path / "my_cfg_mod.py"
    mod_file.write_text(
        """
from astrbot_sdk import PluginConfig


class ExportedCfg(PluginConfig):
    name: str = "ok"
""".strip(),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "astrbot_sdk.config",
            f"{mod_file.stem}:ExportedCfg",
            str(tmp_path),
        ],
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert (tmp_path / "config.schema.json").is_file()


async def test_live_configure_rollback_ping_and_shutdown_reason(tmp_path: Path) -> None:
    plugin_root = tmp_path / "cfg_lifecycle"
    _write_config_plugin(plugin_root)
    client = StdioPluginClient(plugin_root)
    await client.start(config={"greeting": "initial"})
    try:
        # Candidate validation failure leaves current config intact.
        with pytest.raises(RemotePluginError) as exc_info:
            await client.validate_config({"greeting": "reject_validate"})
        assert exc_info.value.code == "INVALID_CONFIG"

        # Runtime failure in on_config_changed rolls back current_config().
        with pytest.raises(RemotePluginError):
            await client.configure({"greeting": "boom_on_apply"})

        ev = MessageEvent(
            id="e1",
            umo=UMO("test", MessageType.PRIVATE, "u1"),
            platform_type="test",
            message_ref=MessageRef("m1"),
            message=MessageChain(Plain("show")),
            sender=Sender(id="u1", name="Tester"),
            timestamp=datetime.now(timezone.utc),
        )
        results = [r async for r in client.invoke("show", ev)]
        assert results[-1] is not None
        assert results[-1].message.text == "initial"

        # Valid live update takes effect without process restart.
        await client.configure({"greeting": "updated"})
        results2 = [r async for r in client.invoke("show", ev)]
        assert results2[-1] is not None
        assert results2[-1].message.text == "updated"

        # Uncaught background task failure surfaces via ping() health check.
        await client.configure({"greeting": "updated", "trigger_bg_crash": True})
        await asyncio.sleep(0.05)
        with pytest.raises(RemotePluginError, match="background boom"):
            await client.ping()
    finally:
        await client.close(reason="reload")
