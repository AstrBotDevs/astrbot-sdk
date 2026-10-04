from __future__ import annotations

import logging

from .context import PluginContext


class Plugin[ConfigT]:
    def __init__(self, context: PluginContext[ConfigT]) -> None:
        self.ctx = context

    @property
    def config(self) -> ConfigT:
        return self.ctx.config

    @property
    def logger(self) -> logging.Logger:
        return self.ctx.logger
