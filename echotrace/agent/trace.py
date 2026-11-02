"""Trace adapters connecting the Agent kernel to runtime persistence ports."""

from __future__ import annotations

from typing import Any, Callable, Protocol


class RunTracePort(Protocol):
    """Small subset implemented by InvestigationRun and future stores."""

    def add_trace(self, step: str, status: str, **data: Any) -> Any:
        ...


class InvestigationRunTraceSink:
    """Persist low-sensitivity Agent turn events in an existing run trace.

    The sink deliberately stores hashes and contract metadata, not prompt
    contents. It is an adapter, so the persistence layer can replace the
    underlying port without coupling AgentRuntime to SQLAlchemy.
    """

    def __init__(self, run: RunTracePort, *, persist: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.run = run
        self.persist = persist

    def write(self, event: dict[str, Any]) -> None:
        if getattr(getattr(self.run, "status", None), "value", getattr(self.run, "status", None)) in {"failed", "cancelled", "completed"}:
            return
        payload = {
            key: value
            for key, value in event.items()
            if key not in {"step", "status", "summary"} and value is not None
        }
        # ``step`` belongs to the Agent loop while the outer run trace reserves
        # that field for graph nodes such as ``dispatch_research``.
        if event.get("step") is not None:
            payload["agent_step"] = event["step"]
        trace_event = self.run.add_trace(
            "agent_turn",
            str(event.get("status", "observed")),
            summary=event.get("summary", ""),
            **payload,
        )
        if self.persist is not None:
            self.persist(dict(trace_event))

    def add_trace(self, step: str, status: str, **data: Any) -> dict[str, Any]:
        """Implement the ToolRouter trace port with immediate persistence."""
        if getattr(getattr(self.run, "status", None), "value", getattr(self.run, "status", None)) in {"failed", "cancelled", "completed"}:
            return {"step": step, "status": status, **data}
        event = self.run.add_trace(step, status, **data)
        if self.persist is not None:
            self.persist(dict(event))
        return event


__all__ = ["InvestigationRunTraceSink", "RunTracePort"]
