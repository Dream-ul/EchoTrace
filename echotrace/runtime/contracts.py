"""Runtime contracts owned by the node-driven workflow."""

from __future__ import annotations

from typing import Any, Protocol

from echotrace.agent.contracts import AgentResult
from echotrace.agent.context_pack import build_context_pack


class RoleRunner(Protocol):
    """Call one configured Agent role with structured workflow context."""

    def __call__(self, role: str, objective: str, context: dict[str, Any]) -> Any: ...


class AgentRuntimeRunner:
    """Adapt the Agent loop to the node planner role contract."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def __call__(self, role: str, objective: str, context: dict[str, Any]) -> AgentResult:
        pack = build_context_pack(role, context)
        return self.runtime.run(role, objective, context_pack=pack)


__all__ = ["AgentRuntimeRunner", "RoleRunner"]
