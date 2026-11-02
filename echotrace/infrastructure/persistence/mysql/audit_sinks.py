"""Adapters that connect the Agent/Tool audit callbacks to MySQL.

The Agent kernel remains database-agnostic: it emits low-sensitivity metadata
through callbacks, while this module owns the transaction and the mapping to
the current audit tables.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from echotrace.runtime.tool_gateway.audit import ToolCallAudit

from .repositories import SqlAlchemyUnitOfWork


def _fallback_manifest_hash(audit: ToolCallAudit) -> str:
    payload = {"provider": audit.provider, "version": audit.version, "capability": audit.capability}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class SqlAlchemySkillActivationSink:
    """Persist each successful Skill activation as a deployment snapshot + event."""

    def __init__(self, session_factory: sessionmaker[Session], *, run_id: str, task_id: str, agent: str) -> None:
        self.session_factory = session_factory
        self.run_id = run_id
        self.task_id = task_id
        self.agent = agent

    def __call__(self, event: dict[str, Any]) -> str | None:
        if event.get("type") != "skill_activated":
            return None
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            definition_id = uow.audit.register_skill(
                skill_name=str(event["name"]),
                description=str(event.get("description", "")) or f"Activated Skill {event['name']}",
                content_hash=str(event["content_hash"]),
                package_path=str(event.get("package_path", "")),
            )
            return uow.audit.activate_skill(
                run_id=self.run_id,
                task_id=self.task_id,
                agent=str(event.get("agent", self.agent)),
                skill_definition_id=definition_id,
                step_no=int(event.get("step_no", 1)),
                reason_summary=str(event.get("reason_summary", "")) or "selected by Agent",
            )


class SqlAlchemyToolCallSink:
    """Persist ToolRouter's immutable audit records in ``tool_calls``."""

    def __init__(self, session_factory: sessionmaker[Session], *, run_id: str, task_id: str, agent: str) -> None:
        self.session_factory = session_factory
        self.run_id = run_id
        self.task_id = task_id
        self.agent = agent

    def __call__(self, audit: ToolCallAudit) -> str:
        payload = audit.to_dict() if hasattr(audit, "to_dict") else dict(audit)
        # ToolCall's status enum is intentionally narrower than preflight
        # audit statuses.  A rejected/denied/budgeted call is still a failed
        # execution record, never silently dropped.
        status = str(payload.get("status", "failed"))
        if status not in {"started", "succeeded", "fallback_succeeded", "failed", "skipped"}:
            status = "failed"
        provider_id_hash = str(payload.get("manifest_hash") or _fallback_manifest_hash(audit))
        attempt_group_id = str(payload.get("attempt_group_id") or f"attempt:{audit.call_id}")
        attempt_no = int(payload.get("attempt", 1) or 1)
        idempotency_key = f"{self.run_id}:{self.task_id}:{attempt_group_id}:{attempt_no}"
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            provider_id = uow.audit.register_provider(
                provider_name=str(payload.get("provider", audit.provider)),
                provider_version=str(payload.get("version", audit.version)),
                manifest_hash=provider_id_hash,
                capabilities=[str(payload.get("capability", audit.capability))],
            )
            return uow.audit.record_tool_call(
                tool_call_id=str(payload.get("call_id", audit.call_id)),
                run_id=self.run_id,
                task_id=self.task_id,
                agent=str(payload.get("agent", self.agent)),
                capability=str(payload.get("capability", audit.capability)),
                provider_definition_id=provider_id,
                attempt_group_id=attempt_group_id,
                provider_attempt_no=attempt_no,
                idempotency_key=idempotency_key,
                status=status,
                input_hash=str(payload.get("input_hash", "")),
                output_summary=payload.get("output_summary") or {},
                duration_ms=payload.get("duration_ms"),
                token_usage=payload.get("token_usage") or {},
                cost=payload.get("cost") or {},
                error_code=payload.get("error_code"),
                error_summary=payload.get("error"),
            )


__all__ = ["SqlAlchemySkillActivationSink", "SqlAlchemyToolCallSink"]
