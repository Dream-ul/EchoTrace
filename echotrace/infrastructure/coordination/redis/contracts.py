"""v2 Redis key and node-task contracts.

Redis messages contain only references and control fields. Facts, evidence,
attempts and results remain authoritative in MySQL/object storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from echotrace.infrastructure.time import beijing_iso


_ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_TASK_RE = re.compile(
    r"^(?:node\.(?:research\.(?:initial|revision_[1-3])|review\.round_[1-3])|report\.(?:plan|section))$"
)


def _text(fields: Mapping[str | bytes, Any], key: str, default: str = "") -> str:
    value = fields.get(key, fields.get(key.encode("utf-8"), default))
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _role(value: str) -> str:
    role = str(value or "").strip()
    if not _ROLE_RE.fullmatch(role):
        raise ValueError(f"invalid Redis role: {value!r}")
    return role


@dataclass(frozen=True, slots=True)
class RedisKeys:
    """Stable v2 names for role streams and derived coordination state."""

    namespace: str = "echotrace:v2"

    def task_stream(self, role: str) -> str:
        return f"{self.namespace}:tasks:{_role(role)}"

    def task_group(self, role: str) -> str:
        return f"{self.namespace}:workers:{_role(role)}"

    def dead_letter_stream(self, role: str) -> str:
        return f"{self.namespace}:tasks:{_role(role)}:dlq"

    def permit(self, scope: str, identifier: str) -> str:
        return f"{self.namespace}:permit:{scope}:{identifier}"

    def global_permit(self) -> str:
        return self.permit("global", "workers")

    def run_permit(self, run_id: str) -> str:
        return self.permit("run", run_id)

    def role_permit(self, role: str) -> str:
        return self.permit("role", _role(role))

    def provider_permit(self, provider: str) -> str:
        return self.permit("provider", provider)

    def rate(self, provider: str, credential_hash: str, window: str, dimension: str = "requests") -> str:
        if dimension not in {"requests", "tokens"}:
            raise ValueError("rate dimension must be requests or tokens")
        # Keep one durable token bucket per provider credential.  The bucket
        # stores its own updated timestamp; embedding a fixed wall-clock
        # window here would reset capacity at minute boundaries.
        return f"{self.namespace}:rate:{dimension}:{provider}:{credential_hash}"

    def cache(self, capability: str, provider: str, provider_version: str, schema_version: str, request_hash: str) -> str:
        return f"{self.namespace}:cache:{capability}:{provider}:{provider_version}:{schema_version}:{request_hash}"

    def run_events(self, run_id: str) -> str:
        return f"{self.namespace}:run-events:{run_id}"

    def lease(self, resource: str, identifier: str) -> str:
        return f"{self.namespace}:lease:{resource}:{identifier}"


@dataclass(frozen=True, slots=True)
class NodeTaskMessage:
    """Small outbox-to-role-stream envelope for one node/report task."""

    outbox_id: str
    task_id: str
    run_id: str
    node_id: str
    slot_id: str
    task_type: str
    agent: str
    revision: int
    state_version: int
    idempotency_key: str
    context_pack_hash: str = ""
    enqueued_at: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "outbox_id", "task_id", "run_id", "node_id", "slot_id",
            "task_type", "agent", "idempotency_key", "context_pack_hash",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"node task requires {field_name}")
        _role(self.agent)
        if not _TASK_RE.fullmatch(self.task_type):
            raise ValueError(f"unsupported node task type: {self.task_type!r}")
        if self.revision < 0 or self.state_version < 0:
            raise ValueError("revision and state_version must be non-negative")
        if not self.enqueued_at:
            object.__setattr__(self, "enqueued_at", beijing_iso())

    def to_fields(self) -> dict[str, str]:
        return {
            "outbox_id": self.outbox_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "slot_id": self.slot_id,
            "task_type": self.task_type,
            "agent": self.agent,
            "revision": str(self.revision),
            "state_version": str(self.state_version),
            "idempotency_key": self.idempotency_key,
            "context_pack_hash": self.context_pack_hash,
            "enqueued_at": self.enqueued_at,
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str | bytes, Any]) -> "NodeTaskMessage":
        return cls(
            outbox_id=_text(fields, "outbox_id"),
            task_id=_text(fields, "task_id"),
            run_id=_text(fields, "run_id"),
            node_id=_text(fields, "node_id"),
            slot_id=_text(fields, "slot_id"),
            task_type=_text(fields, "task_type"),
            agent=_text(fields, "agent"),
            revision=int(_text(fields, "revision", "0") or 0),
            state_version=int(_text(fields, "state_version", "0") or 0),
            idempotency_key=_text(fields, "idempotency_key"),
            context_pack_hash=_text(fields, "context_pack_hash"),
            enqueued_at=_text(fields, "enqueued_at"),
        )


@dataclass(frozen=True, slots=True)
class InvestigationStartMessage:
    """Durable command that starts one run in the orchestrator role stream."""

    outbox_id: str
    run_id: str
    idempotency_key: str
    enqueued_at: str = ""

    def __post_init__(self) -> None:
        for field_name in ("outbox_id", "run_id", "idempotency_key"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"investigation start requires {field_name}")
        if not self.enqueued_at:
            object.__setattr__(self, "enqueued_at", beijing_iso())

    def to_fields(self) -> dict[str, str]:
        return {
            "outbox_id": self.outbox_id,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "enqueued_at": self.enqueued_at,
            "command": "investigation.start",
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str | bytes, Any]) -> "InvestigationStartMessage":
        return cls(
            outbox_id=_text(fields, "outbox_id"),
            run_id=_text(fields, "run_id"),
            idempotency_key=_text(fields, "idempotency_key"),
            enqueued_at=_text(fields, "enqueued_at"),
        )


__all__ = ["InvestigationStartMessage", "NodeTaskMessage", "RedisKeys"]
