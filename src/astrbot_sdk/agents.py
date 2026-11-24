"""Run and observe the configured AstrBot Agent for a conversation."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from .assets import AssetRef
from .errors import InvalidRequest
from .events import UMO, MessageEvent
from .message_components import File, Image, Plain, Record, Video
from .messages import MessageChain
from .protocol_registry import JSONValue, register_protocol_dataclass

if TYPE_CHECKING:
    from .context import PluginContext


class SessionState(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    CLOSED = "closed"


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class PlainInput:
    text: str


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class ImageInput:
    source: AssetRef | str
    text: str | None = None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True)
class FileInput:
    source: AssetRef | str
    text: str | None = None


type InputComponent = PlainInput | ImageInput | FileInput


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class PlainOutput:
    type: ClassVar[str] = "plain"
    text: str


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ReasoningOutput:
    type: ClassVar[str] = "reasoning"
    text: str | None = None
    redacted: bool = False


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ImageOutput:
    type: ClassVar[str] = "image"
    source: AssetRef | str | None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class FileOutput:
    type: ClassVar[str] = "file"
    source: AssetRef | str | None
    name: str | None = None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class AudioOutput:
    type: ClassVar[str] = "audio"
    source: AssetRef | str | None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class VideoOutput:
    type: ClassVar[str] = "video"
    source: AssetRef | str | None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ExtensionOutput:
    type: ClassVar[str] = "extension"
    namespace: str
    name: str
    value: JSONValue


type OutputComponent = (
    PlainOutput
    | ReasoningOutput
    | ImageOutput
    | FileOutput
    | AudioOutput
    | VideoOutput
    | ExtensionOutput
)


class TurnStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSessionRef:
    """Persistent address of an agent, independent of UMO selection."""

    runtime: str
    session_id: str


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeErrorInfo:
    """Stable error classification and text, without exception objects."""

    code: str
    message: str


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeTokenUsage:
    """Reported token counts; missing values remain distinct from zero."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ContextUsage:
    """Current context occupancy, independent from cumulative billing."""

    tokens: int | None = None
    limit: int | None = None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnUsage:
    """An authoritative cumulative snapshot for one turn."""

    tokens: RuntimeTokenUsage = field(default_factory=RuntimeTokenUsage)
    source: Literal["requests", "turn", "unknown"] = "unknown"
    complete: bool = False
    context: ContextUsage = field(default_factory=ContextUsage)


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionMetrics:
    """Runtime-observed durations and actual model/tool step count."""

    duration_ms: float | None = None
    first_token_ms: float | None = None
    steps: int | None = None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnResult:
    """Settled final reply after hooks and runtime history handling."""

    status: TurnStatus = TurnStatus.COMPLETED
    content: tuple[OutputComponent, ...] = ()
    error: RuntimeErrorInfo | None = None
    reason: str = "completed"
    usage: TurnUsage = field(default_factory=TurnUsage)
    metrics: ExecutionMetrics = field(default_factory=ExecutionMetrics)

    @property
    def text(self) -> str:
        """Extract final plain text without reasoning or intermediate output."""
        return "".join(
            part.text for part in self.content if isinstance(part, PlainOutput)
        )


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnStarted:
    """Execution acquired its serial slot."""

    type: ClassVar[str] = "turn.started"


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnPrepared:
    """Presentation metadata without the model system prompt."""

    type: ClassVar[str] = "turn.prepared"
    user_prompt: str
    model: str
    provider_id: str | None = None
    persona_id: str | None = None
    tool_names: tuple[str, ...] = ()
    streaming: bool = False


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class TextDelta:
    """Append text to one content position in an assistant message."""

    type: ClassVar[str] = "text.delta"
    message_id: str
    part_index: int
    text: str


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ReasoningDelta:
    """Append readable reasoning to one message content position."""

    type: ClassVar[str] = "reasoning.delta"
    message_id: str
    part_index: int
    text: str


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class MessageCompleted:
    """An authoritative full message snapshot replacing earlier deltas."""

    type: ClassVar[str] = "message.completed"
    message_id: str
    content: tuple[OutputComponent, ...]
    status: TurnStatus = TurnStatus.COMPLETED


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class OutputReady:
    """A prepared user-facing output, independent of platform delivery success."""

    type: ClassVar[str] = "output.ready"
    target_umo: UMO
    source: Literal["tool", "reply"]
    content: tuple[OutputComponent, ...]


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class RequestStarted:
    """One provider method invocation, not a hidden HTTP attempt."""

    type: ClassVar[str] = "request.started"
    request_id: str
    provider_id: str | None = None
    model: str
    streaming: bool
    scope: Literal["provider_call"] = "provider_call"


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class RequestFinished:
    """The observed outcome and usage of one provider invocation."""

    type: ClassVar[str] = "request.finished"
    request_id: str
    status: TurnStatus
    usage: RuntimeTokenUsage | None = None
    error: RuntimeErrorInfo | None = None
    duration_ms: float


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStarted:
    """Actual executor entry after validation and plugin hooks."""

    type: ClassVar[str] = "tool.started"
    execution_id: str
    call_id: str
    name: str
    arguments: dict[str, JSONValue]


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolProgress:
    """Append or replace an executor output preview."""

    type: ClassVar[str] = "tool.progress"
    execution_id: str
    mode: Literal["append", "replace"]
    content: tuple[OutputComponent, ...]


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFinished:
    """Observed executor settlement, without claims about descendants."""

    type: ClassVar[str] = "tool.finished"
    execution_id: str
    call_id: str
    status: TurnStatus
    content: tuple[OutputComponent, ...] = ()
    structured_result: JSONValue = None
    error: RuntimeErrorInfo | None = None
    exit_code: int | None = None


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolRejected:
    """A tool call rejected before executor entry."""

    type: ClassVar[str] = "tool.rejected"
    call_id: str
    name: str
    arguments: dict[str, JSONValue]
    error: RuntimeErrorInfo
    content: tuple[OutputComponent, ...]


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class UsageUpdated:
    """Replace the turn usage and execution metric snapshots."""

    type: ClassVar[str] = "usage.updated"
    usage: TurnUsage
    metrics: ExecutionMetrics


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnFinished:
    """Terminal result after runtime hooks and history handling."""

    type: ClassVar[str] = "turn.finished"
    result: TurnResult


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeEvent:
    """One attributed fact; database sequence numbers belong to storage."""

    event_id: str
    session: AgentSessionRef
    turn_id: str | None
    created_at: datetime
    payload: RuntimeEventPayload

    @property
    def type(self) -> str:
        return self.payload.type


type RuntimeEventPayload = (
    TurnStarted
    | TurnPrepared
    | TextDelta
    | ReasoningDelta
    | MessageCompleted
    | OutputReady
    | RequestStarted
    | RequestFinished
    | ToolStarted
    | ToolProgress
    | ToolFinished
    | ToolRejected
    | UsageUpdated
    | TurnFinished
)


@register_protocol_dataclass
@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSessionStatus:
    ref: AgentSessionRef
    state: SessionState
    pending_turn_count: int
    active_turn_id: str | None

    @property
    def id(self) -> str:
        return self.ref.session_id


def chain_from_content(content: Sequence[OutputComponent]) -> MessageChain:
    """Convert deliverable Agent output to an SDK message chain."""
    parts = []
    for part in content:
        if isinstance(part, PlainOutput):
            parts.append(Plain(part.text))
        elif isinstance(part, ImageOutput) and part.source is not None:
            parts.append(Image(part.source))
        elif isinstance(part, AudioOutput) and part.source is not None:
            parts.append(Record(part.source))
        elif isinstance(part, VideoOutput) and part.source is not None:
            parts.append(Video(part.source))
        elif isinstance(part, FileOutput) and part.source is not None:
            parts.append(File(part.source, filename=part.name))
    return MessageChain(*parts)


class InputReceipt:
    """Await the Host's confirmation that steering was consumed and saved."""

    def __init__(self, run: AgentRun, receipt_id: str) -> None:
        self._run = run
        self._id = receipt_id

    def __await__(self) -> Generator[Any, None, bool]:
        return self._run._call("receipt", receipt_id=self._id).__await__()


class AgentRun:
    """A handle to one Host-owned turn, valid for this Runner connection."""

    def __init__(self, ctx: PluginContext, run_id: str) -> None:
        self._ctx = ctx
        self.id = run_id

    async def _call(self, operation: str, **payload: Any) -> Any:
        return await self._ctx._invoke_capability(
            "agent.run", operation, {"run_id": self.id, **payload}
        )

    async def result(self) -> TurnResult:
        return await self._call("result")

    def events(self) -> AsyncIterator[RuntimeEvent]:
        """Replay execution events, then follow the turn until it finishes."""
        return self._ctx._invoke_capability_stream(
            "agent.run", "events", {"run_id": self.id}
        )

    async def get_session(self) -> AgentSessionStatus:
        """Return the current state of the turn's runtime session."""
        return await self._call("session")

    session = get_session

    async def cancel(self) -> None:
        await self._call("cancel")

    async def follow_up(
        self,
        *,
        input_message: Sequence[InputComponent],
        send_to_user: bool | None = None,
    ) -> AgentRun:
        """Queue another turn in the same runtime and conversation."""
        run_id = await self._call(
            "follow_up", input_message=tuple(input_message), send_to_user=send_to_user
        )
        return AgentRun(self._ctx, run_id)

    async def steer(
        self, *, input_message: Sequence[InputComponent]
    ) -> InputReceipt | None:
        receipt_id = await self._call("steer", input_message=tuple(input_message))
        return InputReceipt(self, receipt_id) if receipt_id is not None else None


class AgentService:
    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def run_turn(
        self,
        *,
        input_message: Sequence[InputComponent],
        umo: UMO | None = None,
        send_to_user: bool = True,
        source_event: MessageEvent | None = None,
        session_id: str | None = None,
        metadata: dict[str, JSONValue] | None = None,
    ) -> AgentRun:
        """Submit a turn to a conversation using its configured Agent.

        An explicit source event must come from an active Host invocation
        for the target UMO. Otherwise, the current event is used when its
        UMO matches the target.
        """
        target = umo
        if target is None:
            target = (
                source_event.umo
                if source_event is not None
                else self._ctx._ambient_umo()
            )
        if target is None:
            raise InvalidRequest("agent.run requires a target UMO")
        if source_event is not None:
            if not source_event._source_token:
                raise InvalidRequest("source event must come from the Host")
            if source_event.umo != target:
                raise InvalidRequest("source event belongs to another UMO")
            source_id = source_event._source_token
        else:
            event = self._ctx._ambient_event()
            source_id = (
                event._source_token
                if event is not None and event.umo == target
                else None
            )
        run_id = await self._ctx._invoke_capability(
            "agent.run",
            "run_turn_from_event" if source_event is not None else "run_turn",
            {
                "umo": target,
                "input_message": tuple(input_message),
                "send_to_user": send_to_user,
                "session_id": session_id,
                "metadata": metadata,
                "source_event_id": source_id,
            },
        )
        return AgentRun(self._ctx, run_id)
