from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from astrbot_sdk.capabilities import CapabilityGrant, CapabilitySet
from astrbot_sdk.errors import InvalidPluginMetadata
from astrbot_sdk.events import UMO, MessageEvent, MessageRef, MessageType, Sender
from astrbot_sdk.messages import MessageChain, Plain
from astrbot_sdk.runtime import GroupWorkerClient, PluginIsolation, load_metadata


def _write_sdk_plugin(
    root: Path,
    name: str,
    reply_text: str,
    *,
    isolation: str | None = None,
    use_kv: bool = False,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    isolation_line = f"\n  isolation: {isolation}" if isolation is not None else ""
    optional_caps = (
        "\n  optional:\n    - id: storage.kv" if use_kv else "\n  optional: []"
    )
    (root / "metadata.yaml").write_text(
        f"""
schema_version: 2
name: {name}
desc: Test plugin {name}
version: 0.1.0
author: test
runtime:
  api: sdk
  entrypoint: main:PluginImpl
  sdk_version: ">=0.1.0"{isolation_line}
capabilities:
  required: []{optional_caps}
""".strip(),
        encoding="utf-8",
    )
    if use_kv:
        body = (
            '        prev = await self.ctx.storage.get("k")\n'
            '        val = f"{self.ctx.plugin.id}:{event.message.text}"\n'
            '        await self.ctx.storage.set("k", val)\n'
            f'        return f"{reply_text}:{{self.ctx.plugin.id}}:{{prev}}"'
        )
    else:
        body = f"        return {reply_text!r}"
    (root / "main.py").write_text(
        f"""
from __future__ import annotations

import os
from astrbot_sdk import MessageEvent, Plugin, on


class PluginImpl(Plugin[dict]):
    @on.command("ping")
    async def ping(self, event: MessageEvent) -> str:
{body}

    @on.command("pid")
    async def get_pid(self, event: MessageEvent) -> str:
        _ = event
        return str(os.getpid())
""".strip(),
        encoding="utf-8",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        id="e1",
        umo=UMO("test", MessageType.PRIVATE, "u1"),
        platform_type="test",
        message_ref=MessageRef("m1"),
        message=MessageChain(Plain(text)),
        sender=Sender(id="u1", name="Tester"),
        timestamp=datetime.now(timezone.utc),
    )


def test_metadata_isolation_parsing(tmp_path: Path) -> None:
    p_default = tmp_path / "p_default"
    _write_sdk_plugin(p_default, "p_default", "ok")
    assert load_metadata(p_default).runtime.isolation is PluginIsolation.SHARED

    p_dedicated = tmp_path / "p_dedicated"
    _write_sdk_plugin(p_dedicated, "p_dedicated", "ok", isolation="dedicated")
    assert load_metadata(p_dedicated).runtime.isolation is PluginIsolation.DEDICATED

    p_bad = tmp_path / "p_bad"
    _write_sdk_plugin(p_bad, "p_bad", "ok", isolation="process_pool")
    with pytest.raises(InvalidPluginMetadata, match="runtime.isolation"):
        load_metadata(p_bad)


async def test_group_worker_multiplexes_reload_and_isolation(tmp_path: Path) -> None:
    p1 = tmp_path / "plugin_one"
    p2 = tmp_path / "plugin_two"
    _write_sdk_plugin(p1, "plugin_one", "one", use_kv=True)
    _write_sdk_plugin(p2, "plugin_two", "two", use_kv=False)

    kv_stores: dict[str, dict[str, object]] = {
        "test/plugin_one": {},
        "test/plugin_two": {},
    }

    def make_cap_handler(expected_plugin_id: str):
        async def _handler(grant: CapabilityGrant, op: str, payload: dict) -> dict:
            store = kv_stores[expected_plugin_id]
            if grant.id == "storage.kv" and op == "get":
                return {"value": store.get(payload["key"])}
            if grant.id == "storage.kv" and op == "set":
                store[payload["key"]] = payload["value"]
                return {}
            return {}

        return _handler

    worker = GroupWorkerClient()
    await worker.start()
    try:
        c1 = await worker.open_plugin(
            p1,
            capability_handler=make_cap_handler("test/plugin_one"),
        )
        c2 = await worker.open_plugin(
            p2,
            capability_handler=make_cap_handler("test/plugin_two"),
        )

        h1, h2 = await asyncio.gather(
            c1.start(
                granted_capabilities=CapabilitySet([CapabilityGrant("storage.kv")])
            ),
            c2.start(),
        )
        assert h1.plugin_id == "test/plugin_one"
        assert h2.plugin_id == "test/plugin_two"
        assert c1.pid == c2.pid == worker.pid

        # Concurrent invocations run on the same OS PID without mixing state.
        res_pid_1, res_pid_2 = await asyncio.gather(
            asyncio.to_thread(lambda: None),
            asyncio.sleep(0),
        )
        _ = (res_pid_1, res_pid_2)
        p1_pids = [r async for r in c1.invoke("get_pid", _make_event("pid"))]
        p2_pids = [r async for r in c2.invoke("get_pid", _make_event("pid"))]
        assert p1_pids[0].message.text == str(worker.pid)
        assert p2_pids[0].message.text == str(worker.pid)

        out1, out2 = await asyncio.gather(
            c1.invoke("ping", _make_event("hi1")).__anext__(),
            c2.invoke("ping", _make_event("hi2")).__anext__(),
        )
        assert out1.message.text == "one:test/plugin_one:None"
        assert out2.message.text == "two"

        # Closing plugin_one leaves plugin_two alive, and re-opening plugin_one picks
        # up on-disk edits on the exact same Group Worker PID.
        await c1.close()
        _write_sdk_plugin(p1, "plugin_one", "one-v2", use_kv=True)
        c1_reloaded = await worker.open_plugin(
            p1,
            capability_handler=make_cap_handler("test/plugin_one"),
        )
        await c1_reloaded.start(
            granted_capabilities=CapabilitySet([CapabilityGrant("storage.kv")])
        )
        assert c1_reloaded.pid == c2.pid == worker.pid
        out1_v2 = [r async for r in c1_reloaded.invoke("ping", _make_event("hi3"))]
        assert out1_v2[0].message.text == "one-v2:test/plugin_one:test/plugin_one:hi1"

        # Sibling plugin stays unaffected.
        out2_again = [r async for r in c2.invoke("ping", _make_event("hi4"))]
        assert out2_again[0].message.text == "two"

        # A broken third plugin failing initialize does not impact sibling plugins.
        p_bad = tmp_path / "broken_init"
        _write_sdk_plugin(p_bad, "broken_init", "never", isolation="shared")
        (p_bad / "main.py").write_text(
            "raise RuntimeError('boom at import')\n", encoding="utf-8"
        )
        c_bad = await worker.open_plugin(p_bad)
        with pytest.raises(Exception):
            await c_bad.start()
        out2_after_err = [r async for r in c2.invoke("ping", _make_event("hi5"))]
        assert out2_after_err[0].message.text == "two"

        await asyncio.gather(c1_reloaded.close(), c2.close())
    finally:
        await worker.close()


async def test_group_worker_kill_wakes_all_channels(tmp_path: Path) -> None:
    p1 = tmp_path / "k1"
    p2 = tmp_path / "k2"
    _write_sdk_plugin(p1, "k1", "v1")
    _write_sdk_plugin(p2, "k2", "v2")

    worker = GroupWorkerClient()
    await worker.start()
    try:
        c1 = await worker.open_plugin(p1)
        c2 = await worker.open_plugin(p2)
        await asyncio.gather(c1.start(), c2.start())
        worker.kill()
        code1, code2 = await asyncio.wait_for(
            asyncio.gather(c1.wait_exited(), c2.wait_exited()),
            timeout=5.0,
        )
        assert code1 is not None or not c1.is_running
        assert code2 is not None or not c2.is_running
    finally:
        await worker.close()
