"""Structured contracts at the model/runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, Sequence


class AgentAction(StrEnum):
    ACTIVATE_SKILL = "activate_skill"
    READ_SKILL_REFERENCE = "read_skill_reference"
    CALL_TOOL = "call_tool"
    FINAL = "final"
    RESEARCH_CHECKPOINT = "research_checkpoint"
    REQUEST_HUMAN = "request_human"


class DecisionMode(StrEnum):
    JSON = "json"
    NATIVE = "native"


class AgentRuntimeError(RuntimeError):
    """A bounded runtime rejection returned to the Agent as an observation."""

    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ToolSchemaNotLoadedError(AgentRuntimeError):
    def __init__(self, capability: str) -> None:
        super().__init__(
            f"load deferred tool with search_tools(capabilities=[name]) before calling it: {capability}",
            "tool_schema_not_loaded",
        )


@dataclass(frozen=True, slots=True)
class AgentDecision:
    action: AgentAction
    summary: str
    skill: str | None = None
    reference: str | None = None
    capability: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None

    def validate(self) -> None:
        if not isinstance(self.action, AgentAction):
            raise ValueError("action must be an AgentAction")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("summary must be a non-empty string")
        if not isinstance(self.arguments, dict):
            raise ValueError("arguments must be an object")
        required = {
            AgentAction.ACTIVATE_SKILL: "skill",
            AgentAction.READ_SKILL_REFERENCE: "reference",
            AgentAction.CALL_TOOL: "capability",
            AgentAction.FINAL: "answer",
            AgentAction.RESEARCH_CHECKPOINT: "answer",
            AgentAction.REQUEST_HUMAN: "answer",
        }[self.action]
        for name in ("skill", "reference", "capability", "answer"):
            value = getattr(self, name)
            if name == required:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{self.action.value} requires non-empty {name}")
            elif value is not None:
                raise ValueError(f"{self.action.value} does not accept {name}")
        if self.action != AgentAction.CALL_TOOL and self.arguments:
            raise ValueError(f"{self.action.value} does not accept arguments")


@dataclass(frozen=True, slots=True)
class AgentDecisionBatch:
    """One native model turn containing independent business Tool calls.

    Individual actions remain ``AgentDecision`` values.  The batch is only a
    transport envelope for a provider-native parallel Tool call response; it
    deliberately cannot contain control actions that change Agent state.
    """

    decisions: tuple[AgentDecision, ...]

    def validate(self) -> None:
        if len(self.decisions) < 2:
            raise ValueError("parallel AgentDecision batch requires at least two decisions")
        for decision in self.decisions:
            decision.validate()
            if decision.action != AgentAction.CALL_TOOL:
                raise ValueError("parallel AgentDecision batch can contain only call_tool actions")


@dataclass(frozen=True, slots=True)
class AgentResult:
    status: str
    answer: str
    steps: int
    active_skill: str | None
    trace: tuple[dict[str, Any], ...]
    observations: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class AgentTraceEvent:
    """Auditable, low-sensitivity record for one Agent loop turn."""

    step: int
    action: str
    status: str
    role: str
    model: str
    prompt_version: str
    prompt_hash: str
    skill: str | None = None
    skill_version: str | None = None
    capability: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action": self.action,
            "status": self.status,
            "role": self.role,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "prompt_hash": self.prompt_hash,
            "skill": self.skill,
            "skill_version": self.skill_version,
            "capability": self.capability,
            "error_code": self.error_code,
        }


class AgentTraceSink(Protocol):
    """Optional persistence adapter for low-sensitivity Agent trace events."""

    def write(self, event: dict[str, Any]) -> Any:
        ...


class ModelClient(Protocol):
    """Adapter implemented by the configured model client or a test adapter."""

    @property
    def model_name(self) -> str:
        ...

    @property
    def decision_mode(self) -> DecisionMode:
        """Explicit transport mode; never inferred from an endpoint name."""
        ...

    def start_session(self) -> None:
        ...

    def decide(self, messages: Sequence[dict[str, Any]], *, tools: Sequence[dict[str, Any]] = ()) -> AgentDecision | AgentDecisionBatch:
        ...

    def record_result(self, result: dict[str, Any]) -> None:
        """Complete the pending native call, including errors and terminal actions."""
        ...


class ToolGateway(Protocol):
    """The only interface through which an Agent may execute a Tool."""

    def ensure_discovered(self) -> None:
        ...

    def tool_context(
        self,
        agent: str | None = None,
        capabilities: Sequence[str] | None = None,
        *,
        loaded_capabilities: Sequence[str] = (),
    ) -> dict[str, Any]:
        ...

    def native_tools(
        self,
        agent: str | None = None,
        capabilities: Sequence[str] | None = None,
        *,
        loaded_capabilities: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        ...

    def call(self, agent: str, capability: str, payload: dict[str, Any], *, run: Any | None = None) -> dict[str, Any]:
        ...
