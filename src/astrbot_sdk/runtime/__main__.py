from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .stdio_server import serve_stdio_plugin


def main() -> None:
    """Run the AstrBot SDK plugin Runner CLI."""
    parser = argparse.ArgumentParser(prog="astrbot-sdk-runner")
    parser.add_argument("--stdio", action="store_true", help="Use stdio transport")
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Load the plugin through the legacy compat layer",
    )
    args = parser.parse_args()
    if not args.stdio:
        parser.error("a transport must be selected")
    asyncio.run(serve_stdio_plugin(args.plugin_root, legacy=args.legacy))


if __name__ == "__main__":
    main()
