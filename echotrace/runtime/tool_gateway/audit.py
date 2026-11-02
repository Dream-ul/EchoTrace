"""Low-sensitivity Tool call audit records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCallAudit:
    call_id: str
    agent: str
    capability: str
    provider: str
    version: str
    status: str
    input_hash: str
    output_summary: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    token_usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    fallback_from: str | None = None
    previous_error_code: str | None = None
    attempt_group_id: str | None = None
    attempt: int = 1
    # Optional deployment identity supplied by persistence sinks.  Older
    # gateways may omit it; sinks then derive a stable fallback hash.
    manifest_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "agent": self.agent,
            "capability": self.capability,
            "provider": self.provider,
            "version": self.version,
            "status": self.status,
            "input_hash": self.input_hash,
            "output_summary": self.output_summary,
            "duration_ms": self.duration_ms,
            "token_usage": self.token_usage,
            "cost": self.cost,
            "error": self.error,
            "error_code": self.error_code,
            "fallback_from": self.fallback_from,
            "previous_error_code": self.previous_error_code,
            "attempt_group_id": self.attempt_group_id,
            "attempt": self.attempt,
            "manifest_hash": self.manifest_hash,
        }
