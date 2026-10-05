from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from .stdio_server import serve_stdio_plugin
from .ws_runner import RUNNER_TOKEN_ENV, serve_ws_plugin


def main() -> None:
    """Run the AstrBot SDK plugin Runner CLI."""
    parser = argparse.ArgumentParser(prog="astrbot-sdk-runner")
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument(
        "--stdio",
        action="store_true",
        help="Use the stdio transport (host-spawned runners)",
    )
    transport.add_argument(
        "--ws",
        metavar="URL",
        help="Dial out to the Host WebSocket listener (ws:// or wss://)",
    )
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Load the plugin through the legacy compat layer",
    )
    args = parser.parse_args()
    if args.stdio:
        asyncio.run(serve_stdio_plugin(args.plugin_root, legacy=args.legacy))
        return
    asyncio.run(
        serve_ws_plugin(
            args.plugin_root,
            args.ws,
            token=os.environ.get(RUNNER_TOKEN_ENV),
            legacy=args.legacy,
        )
    )


if __name__ == "__main__":
    main()
