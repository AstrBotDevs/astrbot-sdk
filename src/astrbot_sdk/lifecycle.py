from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .registration import HandlerKind, _lifecycle


@dataclass(frozen=True, slots=True)
class ConfigChangedEvent[ConfigT]:
    previous: ConfigT
    current: ConfigT


@dataclass(frozen=True, slots=True)
class ShutdownEvent:
    deadline: datetime | None = None


startup = _lifecycle(HandlerKind.LIFECYCLE_STARTUP)
config_changed = _lifecycle(HandlerKind.LIFECYCLE_CONFIG_CHANGED)
shutdown = _lifecycle(HandlerKind.LIFECYCLE_SHUTDOWN)

__all__ = [
    "ConfigChangedEvent",
    "ShutdownEvent",
    "config_changed",
    "shutdown",
    "startup",
]
