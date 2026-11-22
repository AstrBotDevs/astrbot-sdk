from __future__ import annotations

from pathlib import Path

from plugin_config import TemplateConfig

from astrbot_sdk.config import export_config_schema


def main() -> None:
    """Export `config.schema.json` into this plugin package directory."""
    package_root = Path(__file__).resolve().parent
    out = export_config_schema(TemplateConfig, package_root)
    print(f"Exported schema to {out}")


if __name__ == "__main__":
    main()
