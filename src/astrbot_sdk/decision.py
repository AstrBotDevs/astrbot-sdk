"""Structured questions answered by AstrBot's configured decision model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import DecisionError as DecisionError
from .protocol_registry import JSONValue, register_protocol_dataclass

if TYPE_CHECKING:
    from .context import PluginContext


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class DecisionChoiceQuestion:
    instructions: str
    criteria: Mapping[str, str]


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class DecisionBooleanQuestion:
    instructions: str
    criteria: Mapping[str, str] | None = None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class DecisionScoreQuestion:
    instructions: str
    criteria: Sequence[str]


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class DecisionChoiceAnswer:
    choice: str
    probability: float
    probabilities: Mapping[str, float]
    confidence: float


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class DecisionBooleanAnswer:
    probability: float


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class DecisionScoreAnswer:
    score: float
    legend: Mapping[str, str]
    probabilities: Mapping[str, float]
    confidence: float


type DecisionQuestion = (
    DecisionChoiceQuestion | DecisionBooleanQuestion | DecisionScoreQuestion
)
type DecisionAnswer = DecisionChoiceAnswer | DecisionBooleanAnswer | DecisionScoreAnswer
type DecisionState = str | list[JSONValue] | dict[str, JSONValue]


class DecisionService:
    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def ask(
        self,
        questions: Mapping[str, DecisionQuestion],
        state: DecisionState | None = None,
        *,
        provider_id: str | None = None,
    ) -> dict[str, DecisionAnswer]:
        """Ask several questions in one request, preserving their keys."""
        return await self._ctx._invoke_capability(
            "decision.ask",
            "ask",
            {"questions": questions, "state": state, "provider_id": provider_id},
        )

    async def choose(
        self,
        question: str,
        criteria: Mapping[str, str] | Sequence[str],
        *,
        state: DecisionState | None = None,
        provider_id: str | None = None,
    ) -> DecisionChoiceAnswer:
        """Choose one option and return the full probability distribution."""
        return await self._ctx._invoke_capability(
            "decision.ask",
            "choose",
            {
                "question": question,
                "criteria": criteria,
                "state": state,
                "provider_id": provider_id,
            },
        )

    async def yes_no(
        self,
        question: str,
        *,
        state: DecisionState | None = None,
        threshold: float = 0.5,
        provider_id: str | None = None,
        default: bool | None = None,
    ) -> bool:
        """Compare a positive answer's probability with the given threshold."""
        return await self._ctx._invoke_capability(
            "decision.ask",
            "yes_no",
            {
                "question": question,
                "state": state,
                "threshold": threshold,
                "provider_id": provider_id,
                "default": default,
            },
        )
