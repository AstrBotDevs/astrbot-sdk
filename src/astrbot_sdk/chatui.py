"""Interactive chat view declarations and portable handler results."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from .message_components import UIInteraction as UIInteraction
from .message_components import ViewContext as ViewContext
from .protocol_registry import JSONValue, register_protocol_dataclass
from .results import EventResult, Propagation


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewSpec:
    """Describe one entry in the plugin metadata's views list."""

    name: str
    target: Literal[
        "plugin_page", "chat_message", "chat_workbench", "chat_workbench_with_launcher"
    ] = "plugin_page"
    title: str = ""
    description: str = ""
    version: int = 1
    actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", self.name):
            raise ValueError("invalid view name")
        if self.target not in (
            "plugin_page",
            "chat_message",
            "chat_workbench",
            "chat_workbench_with_launcher",
        ):
            raise ValueError("invalid view target")
        if self.target == "plugin_page" and self.actions:
            raise ValueError("standalone views use Web routes")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("view version must be a positive integer")
        if len(self.title) > 200 or len(self.description) > 500:
            raise ValueError("view metadata is too long")
        if any(not isinstance(action, str) or not action for action in self.actions):
            raise ValueError("view actions must be nonempty strings")


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ViewResult(EventResult):
    """Create a named view or replace the current interaction's display data."""

    data: dict[str, JSONValue]
    name: str | None = None
    text: str = ""
    title: str = ""

    def __post_init__(self) -> None:
        if self.propagation != Propagation.CONTINUE:
            raise ValueError("view results require continue propagation")
        if self.name is not None and (not self.name or not self.text.strip()):
            raise ValueError("creating a view requires a name and fallback text")
        if self.name is None and (self.text or self.title):
            raise ValueError("an update only replaces display data")
        if len(self.title) > 200:
            raise ValueError("view title exceeds 200 characters")
        if not isinstance(self.data, dict):
            raise ValueError("view data must be a JSON object")
        encoded = json.dumps(self.data, allow_nan=False, ensure_ascii=False)
        if len(encoded.encode()) > 1024 * 1024:
            raise ValueError("view data exceeds one MiB")
        object.__setattr__(self, "data", json.loads(encoded))
