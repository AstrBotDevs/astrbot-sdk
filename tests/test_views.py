"""Views tests: manifest discovery, i18n, and streaming reads."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from astrbot_sdk.runtime import StdioPluginClient
from tests.test_stdio_transport import write_plugin

PLUGIN = """
from astrbot_sdk import Plugin


class TestPlugin(Plugin):
    pass
"""

LEGACY_PLUGIN = """
from astrbot.api.star import Context, Star


class LegacyViewsPlugin(Star):
    pass
"""


def add_views(plugin_root: Path) -> None:
    demo = plugin_root / "views" / "demo"
    demo.mkdir(parents=True)
    (demo / "index.html").write_text("<h1>demo</h1>", "utf-8")
    (demo / "app.js").write_text("console.log(1)", "utf-8")
    ignored = plugin_root / "views" / "empty"
    ignored.mkdir()
    i18n_dir = plugin_root / ".astrbot-plugin" / "i18n"
    i18n_dir.mkdir(parents=True)
    (i18n_dir / "zh-CN.json").write_text(
        '{"metadata": {"display_name": "演示"}}', "utf-8"
    )


def write_legacy_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir()
    (plugin_root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "legacy_views",
                "desc": "views test",
                "author": "AstrBot",
                "version": "1.0.0",
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "main.py").write_text(LEGACY_PLUGIN, encoding="utf-8")


async def _check_views(client: StdioPluginClient) -> None:
    items = [item async for item in client.invoke_views("manifest")]
    info = items[0]["info"]
    assert [page["name"] for page in info["pages"]] == ["demo"]
    files = {f["path"] for f in info["pages"][0]["files"]}
    assert files == {"app.js", "index.html"}
    assert info["i18n"]["zh-CN"] == '{"metadata": {"display_name": "演示"}}'

    items = [item async for item in client.invoke_views("read", "demo", "index.html")]
    assert items[0]["info"]["content_type"] == "text/html"
    assert b"".join(c["chunk"] for c in items[1:]) == b"<h1>demo</h1>"

    # Traversal is rejected.
    from astrbot_sdk.errors import RemotePluginError

    with pytest.raises(RemotePluginError):
        _ = [
            item
            async for item in client.invoke_views("read", "demo", "../../metadata.yaml")
        ]
    with pytest.raises(RemotePluginError):
        _ = [item async for item in client.invoke_views("read", "demo", "missing.txt")]


@pytest.mark.asyncio
async def test_views_sdk_plugin(tmp_path: Path) -> None:
    plugin_root = tmp_path / "viewsdemo"
    write_plugin(plugin_root, PLUGIN)
    add_views(plugin_root)

    client = StdioPluginClient(plugin_root, python_executable=Path(sys.executable))
    try:
        await client.start()
        await _check_views(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_views_legacy_plugin(tmp_path: Path) -> None:
    plugin_root = tmp_path / "legacy_views"
    write_legacy_plugin(plugin_root)
    add_views(plugin_root)

    client = StdioPluginClient(
        plugin_root,
        python_executable=Path(sys.executable),
        legacy=True,
    )
    try:
        await client.start()
        await _check_views(client)
    finally:
        await client.close()
