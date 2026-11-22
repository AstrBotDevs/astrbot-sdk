from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import astrbot_sdk
from astrbot_sdk.events import UMO, MessageEvent, MessageRef, MessageType, Sender
from astrbot_sdk.messages import MessageChain, Plain
from astrbot_sdk.runtime import StdioPluginClient
from astrbot_sdk.tools import ToolCallContext


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_rust_echo_binary() -> Path:
    root = _repo_root()
    exe_name = "rust_echo.exe" if __import__("sys").platform == "win32" else "rust_echo"
    binary = root / "target" / "debug" / "examples" / exe_name
    if not binary.is_file():
        subprocess.run(
            ["cargo", "build", "--example", "rust_echo", "-p", "astrbot-worker"],
            cwd=str(root),
            check=True,
        )
    assert binary.is_file()
    return binary


def test_native_extension_bindings_and_worker_wrapper(tmp_path: Path) -> None:
    assert astrbot_sdk.has_native_runtime() is True
    from astrbot_sdk import _native

    schema_path = tmp_path / "config.schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "token": {
                        "type": "string",
                        "writeOnly": True,
                        "format": "password",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    validated_json = _native.validate_config_schema_file_py(str(schema_path))
    assert _native.has_sensitive_schema_fields_py(validated_json) is True

    manifest_dir = tmp_path / "native_manifest"
    manifest_dir.mkdir()
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "id": "test.native_wrap",
                "name": "native_wrap",
                "description": "Native wrapper check",
                "version": "0.1.0",
                "author": "test",
                "command": ["bin/dummy"],
                "dependencies": {"dep_a": ">=1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    manifest = json.loads(_native.load_manifest_json(str(manifest_dir)))
    assert manifest["id"] == "test.native_wrap"
    assert manifest["dependencies"]["dep_a"] == ">=1.0.0"

    worker = astrbot_sdk.Worker.from_dir(manifest_dir)

    async def _echo(payload, ctx):
        _ = ctx
        return {"payload": payload}

    worker.service("echo", "Echo via native worker", _echo)


async def test_stdio_client_spawns_real_rust_echo_binary(tmp_path: Path) -> None:
    binary = _ensure_rust_echo_binary()
    plugin_root = tmp_path / "rust_echo_plugin"
    bin_dir = plugin_root / "bin"
    bin_dir.mkdir(parents=True)
    target_bin = bin_dir / binary.name
    shutil.copy2(binary, target_bin)

    (plugin_root / "plugin.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "id": "astrbot.rust_echo",
                "name": "rust_echo",
                "description": "Echo Rust worker binary",
                "version": "0.1.0",
                "author": "test",
                "command": [f"./bin/{binary.name}", "."],
                "capabilities": {
                    "required": [],
                    "optional": [{"id": "storage.kv"}],
                },
            }
        ),
        encoding="utf-8",
    )

    client = StdioPluginClient(plugin_root)
    handshake = await client.start(config={"prefix": "[rust] "})
    try:
        assert handshake.plugin_id == "astrbot.rust_echo"
        assert any(svc["name"] == "echo" for svc in handshake.services)

        # Invalid candidate config rejected by Rust worker validator.
        with pytest.raises(astrbot_sdk.errors.RemotePluginError) as exc_info:
            await client.validate_config({"prefix": 123})
        assert exc_info.value.code == "INVALID_CONFIG"

        # Invoke Rust command handler.
        ev = MessageEvent(
            id="ev-1",
            umo=UMO("test", MessageType.PRIVATE, "u1"),
            platform_type="test",
            message_ref=MessageRef("m1"),
            message=MessageChain(Plain("hello-rust")),
            sender=Sender(id="u1", name="Tester"),
            timestamp=datetime.now(timezone.utc),
        )
        cmd_results = [r async for r in client.invoke("command:echo", None, ev)]
        assert cmd_results[-1] is not None
        assert cmd_results[-1].message.text == "[rust] hello-rust"

        # Invoke Rust tool and service.
        tool_out = await client.invoke_tool(
            "tool:rust_echo", ToolCallContext(id="tc-1"), {"text": "world"}
        )
        assert tool_out == {"text": "[rust] world"}

        svc_out = await client.invoke_service("echo", {"text": "svc"})
        assert svc_out == {"echo": "[rust] svc"}

        # Live reconfigure changes output without restarting binary.
        await client.configure({"prefix": "[live] "})
        tool_out2 = await client.invoke_tool(
            "tool:rust_echo", ToolCallContext(id="tc-2"), {"text": "ok"}
        )
        assert tool_out2 == {"text": "[live] ok"}
    finally:
        await client.close(reason="reload")
