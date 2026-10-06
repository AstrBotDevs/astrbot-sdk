"""Legacy plugins import under their real in-process dotted path.

In-process AstrBot imports plugins as ``data.plugins.<dir>.main``. The Runner
mirrors that whenever the plugin lives below a recognizable AstrBot data
directory, so module identity (__name__, ModuleSpec, __package__) is
byte-for-byte identical. Framework introspection (Flask/Quart instance paths,
importlib.resources) and spawned multiprocessing children rely on it.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_sdk.compat.v1.loader import load_legacy_plugin

PLUGIN_NAME = "fake_identity"


def _write_plugin(root: Path, value: int = 1) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.yaml").write_text(
        f"name: {PLUGIN_NAME}\ndesc: t\nauthor: t\nversion: 0.0.1\n",
        encoding="utf-8",
    )
    (root / "helper.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    (root / "main.py").write_text(
        "from .helper import VALUE\n"
        "from astrbot.api.star import Context, Star, register\n\n"
        f"@register('{PLUGIN_NAME}', 't', 't', '0.0.1')\n"
        "class FakeIdentity(Star):\n"
        "    pass\n",
        encoding="utf-8",
    )


@pytest.fixture()
def plugin_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Plugin under a real data/plugins layout with cleanup of import state."""
    data_dir = tmp_path / "data"
    plugin_root = data_dir / "plugins" / PLUGIN_NAME
    monkeypatch.setenv("ASTRBOT_DATA_PATH", str(data_dir))
    # Let the loader set these itself; monkeypatch restores the original
    # (absent) values at teardown.
    monkeypatch.delenv("ASTRBOT_SDK_LEGACY_RUNNER", raising=False)
    monkeypatch.delenv("ASTRBOT_HOST_VERSION", raising=False)
    ctx = SimpleNamespace(logger=logging.getLogger("test"))
    yield plugin_root, ctx
    # monkeypatch.delenv registers no undo when the var was absent at setup,
    # so the loader's own os.environ.setdefault would leak into this process.
    os.environ.pop("ASTRBOT_SDK_LEGACY_RUNNER", None)
    os.environ.pop("ASTRBOT_HOST_VERSION", None)
    for name in [
        item
        for item in tuple(sys.modules)
        if item == "data" or item.startswith("data.")
    ]:
        sys.modules.pop(name, None)
    sys.path[:] = [p for p in sys.path if not str(p).startswith(str(tmp_path))]


def test_imports_under_real_dotted_path(plugin_env) -> None:
    plugin_root, ctx = plugin_env
    _write_plugin(plugin_root)

    load_legacy_plugin(plugin_root, ctx=ctx)

    package = f"data.plugins.{PLUGIN_NAME}"
    module = sys.modules[f"{package}.main"]
    assert module.VALUE == 1
    # Relative import resolved through the real package.
    assert sys.modules[f"{package}.helper"].VALUE == 1
    # A fresh interpreter with the same sys.path can resolve the plugin by
    # name (this is what multiprocessing spawn children do).
    assert importlib.util.find_spec(package) is not None


def test_reload_reexecutes_package_code(plugin_env) -> None:
    plugin_root, ctx = plugin_env
    _write_plugin(plugin_root, value=1)
    load_legacy_plugin(plugin_root, ctx=ctx)

    _write_plugin(plugin_root, value=2)
    # Same-size rewrites within one mtime tick would reuse the stale .pyc.
    for file in plugin_root.glob("*.py"):
        stat = file.stat()
        os.utime(file, (stat.st_atime, stat.st_mtime + 10))
    load_legacy_plugin(plugin_root, ctx=ctx)

    assert sys.modules[f"data.plugins.{PLUGIN_NAME}.main"].VALUE == 2


def test_spawn_child_bootstrap_installs_legacy_shims(tmp_path: Path) -> None:
    """multiprocessing spawn children reinstall the shims via __main__ fixup."""
    code = (
        "import os; "
        "os.environ['ASTRBOT_SDK_LEGACY_RUNNER'] = '1'; "
        "import astrbot_sdk.runtime.__main__; "
        "import astrbot.api; "
        # Compat shims are dynamically created ModuleType objects without a
        # __file__, unlike the real core package.
        "print('shim' if not getattr(astrbot.api, '__file__', None) else 'real')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        # Away from the repo root so `astrbot/` is not importable from cwd.
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "shim" in result.stdout


def test_shims_are_not_installed_without_runner_flag(tmp_path: Path) -> None:
    """The legacy shims must not leak into unrelated interpreters."""
    code = (
        "import astrbot_sdk.runtime.__main__; "
        "import astrbot.api; "
        "print('shim' if not getattr(astrbot.api, '__file__', None) else 'real')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "real" in result.stdout
