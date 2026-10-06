"""Unit tests for the legacy manager facades (platform/cron/kb/config/history)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from astrbot_sdk.compat.v1.cron import CronManagerFacade, SchedulerFacade
from astrbot_sdk.compat.v1.errors import IsolationUnsupportedError
from astrbot_sdk.compat.v1.host_snapshot import (
    AstrbotConfigMgrFacade,
    PlatformManagerFacade,
)
from astrbot_sdk.compat.v1.kb import KbManagerFacade
from astrbot_sdk.compat.v1.message_history import MessageHistoryManagerFacade

SNAPSHOT = {
    "platforms": [
        {
            "id": "napcat-1",
            "name": "aiocqhttp",
            "description": "OneBot v11",
            "adapter_display_name": "QQ",
        },
        {
            "id": "webchat-1",
            "name": "webchat",
            "description": "WebChat",
            "adapter_display_name": None,
        },
    ],
    "config": {"timezone": "UTC"},
    "config_profiles": {"uuid-1": {"timezone": "Asia/Shanghai"}},
    "config_routes": {"aiocqhttp::*": "uuid-1"},
    "config_list": [
        {"id": "uuid-1", "name": "group-conf", "path": "abconf_uuid-1.json"},
        {"id": "default", "name": "default", "path": "cmd_config.json"},
    ],
}


class FakeSDKContext:
    """Minimal SDK PluginContext double capturing capability calls."""

    def __init__(self, responses: dict | None = None) -> None:
        self.logger = logging.getLogger("test")
        self.calls: list[tuple[str, str, dict]] = []
        self.cron_handlers: dict = {}
        self._responses = responses or {}

    async def _invoke_capability(self, capability, operation, payload):
        self.calls.append((capability, operation, payload))
        return self._responses.get((capability, operation), {})


class FakeFacadeContext:
    """Minimal legacy Context facade double."""

    def __init__(self, responses: dict | None = None) -> None:
        self._ctx = FakeSDKContext(responses)


def test_platform_manager_lists_snapshot_platforms() -> None:
    facade_ctx = FakeFacadeContext()
    mgr = PlatformManagerFacade(facade_ctx, SNAPSHOT)
    assert [p.meta().id for p in mgr.platform_insts] == ["napcat-1", "webchat-1"]
    assert [p.meta().name for p in mgr.get_insts()] == ["aiocqhttp", "webchat"]
    first = mgr.platform_insts[0]
    assert first.meta().description == "OneBot v11"
    assert first.config == {}
    bot = first.get_client()
    assert bot.api._platform_id == "napcat-1"
    assert bot.api._platform_type == "aiocqhttp"
    with pytest.raises(IsolationUnsupportedError):
        mgr.load_platform({})


@pytest.mark.asyncio
async def test_ucr_route_resolution_and_writes() -> None:
    facade_ctx = FakeFacadeContext()
    mgr = AstrbotConfigMgrFacade(facade_ctx, SNAPSHOT)
    assert mgr.ucr.get_conf_id_for_umop("aiocqhttp:GroupMessage:42") == "uuid-1"
    assert mgr.ucr.get_conf_id_for_umop("webchat:FriendMessage:u1") is None
    assert mgr.ucr.get_conf_id_for_umop("bad") is None
    await mgr.ucr.update_route("webchat:FriendMessage:u1", "uuid-1")
    assert mgr.ucr.umop_to_conf_id["webchat:FriendMessage:u1"] == "uuid-1"
    capability, operation, payload = facade_ctx._ctx.calls[-1]
    assert (capability, operation) == ("config.write", "update_route")
    assert payload == {"umo": "webchat:FriendMessage:u1", "conf_id": "uuid-1"}
    await mgr.ucr.delete_route("webchat:FriendMessage:u1")
    assert "webchat:FriendMessage:u1" not in mgr.ucr.umop_to_conf_id
    assert facade_ctx._ctx.calls[-1][1] == "delete_route"
    with pytest.raises(ValueError, match="umop must be"):
        await mgr.ucr.update_route("bad", "uuid-1")


def test_astrbot_config_mgr_reads() -> None:
    facade_ctx = FakeFacadeContext()
    mgr = AstrbotConfigMgrFacade(facade_ctx, SNAPSHOT)
    assert mgr.default_conf["timezone"] == "UTC"
    assert mgr.confs["default"]["timezone"] == "UTC"
    assert mgr.get_conf("aiocqhttp:GroupMessage:1")["timezone"] == "Asia/Shanghai"
    assert mgr.get_conf(None)["timezone"] == "UTC"
    names = [item["name"] for item in mgr.get_conf_list()]
    assert names == ["group-conf", "default"]
    info = mgr.get_conf_info("aiocqhttp:GroupMessage:1")
    assert info["id"] == "uuid-1"
    assert mgr.get_conf_info("webchat:FriendMessage:u1")["id"] == "default"


@pytest.mark.asyncio
async def test_cron_add_basic_job_registers_and_deletes() -> None:
    responses = {
        ("cron.schedule", "add_basic_job"): {"job": {"job_id": "j1", "name": "n"}},
    }
    facade_ctx = FakeFacadeContext(responses)
    mgr = CronManagerFacade(facade_ctx)

    async def handler(**kwargs):
        return "fired"

    job = await mgr.add_basic_job(
        name="n",
        cron_expression="0 8 * * *",
        handler=handler,
        payload={"a": 1},
    )
    assert job.job_id == "j1"
    capability, operation, payload = facade_ctx._ctx.calls[-1]
    assert (capability, operation) == ("cron.schedule", "add_basic_job")
    assert payload["name"] == "n"
    assert payload["payload"] == {"a": 1}
    handler_id = payload["handler_id"]
    assert facade_ctx._ctx.cron_handlers[handler_id] is handler
    await mgr.delete_job("j1")
    assert handler_id not in facade_ctx._ctx.cron_handlers
    assert facade_ctx._ctx.calls[-1][1] == "delete_job"


@pytest.mark.asyncio
async def test_scheduler_facade_add_remove_job() -> None:
    facade_ctx = FakeFacadeContext()
    scheduler = SchedulerFacade(facade_ctx)
    assert scheduler.running is True

    def tick():
        return None

    job = scheduler.add_job(tick, "interval", seconds=30, id="job-x", name="x")
    assert job.id == "job-x"
    assert scheduler.get_job("job-x") is job
    await asyncio.sleep(0)
    capability, operation, payload = facade_ctx._ctx.calls[-1]
    assert (capability, operation) == ("cron.schedule", "add_scheduler_job")
    assert payload["trigger"] == {"type": "interval", "params": {"seconds": 30}}
    assert payload["options"]["id"] == "job-x"
    assert payload["options"]["name"] == "x"
    handler_id = payload["handler_id"]
    assert facade_ctx._ctx.cron_handlers[handler_id] is tick
    scheduler.remove_job("job-x")
    assert scheduler.get_job("job-x") is None
    await asyncio.sleep(0)
    assert facade_ctx._ctx.calls[-1][1] == "remove_job"


@pytest.mark.asyncio
async def test_kb_manager_facade_calls() -> None:
    responses = {
        ("kb.manage", "get_kb_by_name"): {"kb": {"kb_id": "k1", "kb_name": "docs"}},
        ("kb.manage", "list_kbs"): {"kbs": [{"kb_id": "k1", "kb_name": "docs"}]},
        ("kb.manage", "retrieve"): {"result": {"records": []}},
    }
    facade_ctx = FakeFacadeContext(responses)
    mgr = KbManagerFacade(facade_ctx)
    helper = await mgr.get_kb_by_name("docs")
    assert helper is not None
    assert helper.kb_id == "k1"
    assert helper.kb.kb_name == "docs"
    kbs = await mgr.list_kbs()
    assert kbs[0].kb_id == "k1"
    result = await mgr.retrieve("q", ["docs"])
    assert result == {"records": []}
    capability, operation, payload = facade_ctx._ctx.calls[-1]
    assert (capability, operation) == ("kb.manage", "retrieve")
    assert payload["kb_names"] == ["docs"]
    with pytest.raises(IsolationUnsupportedError):
        await mgr.update_kb("k1")


@pytest.mark.asyncio
async def test_message_history_facade_calls() -> None:
    responses = {
        ("message.history", "insert"): {"record": {"id": 1, "content": {}}},
        ("message.history", "count"): {"count": 7},
        ("message.history", "get"): {"records": [{"id": 1}, {"id": 2}]},
    }
    facade_ctx = FakeFacadeContext(responses)
    mgr = MessageHistoryManagerFacade(facade_ctx)
    record = await mgr.insert("p", "u", {"type": "text"})
    assert record.id == 1
    assert await mgr.count("p", "u") == 7
    records = await mgr.get("p", "u")
    assert [r.id for r in records] == [1, 2]
    await mgr.delete("p", "u")
    capability, operation, payload = facade_ctx._ctx.calls[-1]
    assert (capability, operation) == ("message.history", "delete")
    assert payload["offset_sec"] == 86400
