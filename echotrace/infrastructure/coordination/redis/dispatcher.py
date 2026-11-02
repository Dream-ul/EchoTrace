"""Transactional MySQL outbox to Redis Streams dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from echotrace.ports.persistence import UnitOfWork

from .client import RedisStreamBus
from .contracts import InvestigationStartMessage, NodeTaskMessage


@dataclass(frozen=True, slots=True)
class DispatchResult:
    claimed: int
    published: int
    failed: int
    dead: int


class OutboxDispatcher:
    """Publish committed outbox rows with at-least-once semantics.

    Claiming and marking are separate short MySQL transactions.  A crash after
    ``XADD`` and before ``mark_published`` may publish a duplicate; consumers
    must use the outbox/dedup key and task idempotency key to eliminate effects.
    """

    def __init__(self, uow_factory: Callable[[], UnitOfWork], bus: RedisStreamBus, *, max_publish_attempts: int = 5, lease_seconds: int = 60) -> None:
        self.uow_factory = uow_factory
        self.bus = bus
        self.max_publish_attempts = max(1, int(max_publish_attempts))
        self.lease_seconds = max(1, int(lease_seconds))

    def dispatch_once(self, *, owner: str, limit: int = 50) -> DispatchResult:
        with self.uow_factory() as uow:
            rows = uow.outbox.claim(owner=owner, limit=limit, lease_seconds=self.lease_seconds)
        published = failed = dead = 0
        for row in rows:
            message_id: str | None = None
            try:
                if _is_start_row(row):
                    message = self._start_message(row)
                    message_id = self.bus.publish_investigation_start(message.to_fields())
                elif _is_task_row(row):
                    task_rows = list(_task_rows(row))
                    if not task_rows:
                        raise ValueError("task outbox row contains no node task envelopes")
                    for task_row in task_rows:
                        message = self._message(task_row)
                        message_id = self.bus.publish_task(message.agent, message.to_fields())
                else:
                    # Non-task outbox rows are durable run notifications. They
                    # never share a worker stream with executable work.
                    payload = dict(row.get("payload") or {})
                    run_id = str(payload.get("run_id") or "")
                    message_id = self.bus.publish_run_event(run_id, {
                        "outbox_id": str(row["outbox_id"]),
                        "run_id": run_id,
                        "event_type": str(row.get("event_type") or ""),
                        "payload": payload,
                    }) if run_id else f"audit:{row['outbox_id']}"
                with self.uow_factory() as uow:
                    if uow.outbox.mark_published(row["outbox_id"], owner=owner, message_id=message_id):
                        published += 1
            except Exception as exc:
                is_dead = int(row.get("publish_attempts") or 0) >= self.max_publish_attempts
                with self.uow_factory() as uow:
                    if uow.outbox.mark_failed(row["outbox_id"], owner=owner, error=_safe_error(exc), dead=is_dead):
                        failed += 1
                        dead += int(is_dead)
        return DispatchResult(len(rows), published, failed, dead)

    @staticmethod
    def _message(row: dict[str, Any]) -> NodeTaskMessage:
        payload = dict(row.get("payload") or {})
        task_type = str(payload.get("task_type") or row.get("event_type") or "")
        return NodeTaskMessage(
            outbox_id=str(row["outbox_id"]),
            task_id=str(payload.get("task_id") or row["aggregate_id"]),
            run_id=str(payload.get("run_id") or ""),
            node_id=str(payload.get("node_id") or ""),
            slot_id=str(payload.get("slot_id") or ""),
            task_type=task_type,
            agent=str(payload.get("agent") or ""),
            revision=int(payload.get("revision") or 0),
            state_version=int(payload.get("state_version") or 0),
            idempotency_key=str(payload.get("idempotency_key") or row.get("dedup_key") or row["outbox_id"]),
            context_pack_hash=str(payload.get("context_pack_hash") or ""),
            enqueued_at=str(payload.get("enqueued_at") or ""),
        )

    @staticmethod
    def _start_message(row: dict[str, Any]) -> InvestigationStartMessage:
        payload = dict(row.get("payload") or {})
        return InvestigationStartMessage(
            outbox_id=str(row["outbox_id"]),
            run_id=str(payload.get("run_id") or row.get("aggregate_id") or ""),
            idempotency_key=str(payload.get("idempotency_key") or row.get("dedup_key") or row["outbox_id"]),
            enqueued_at=str(payload.get("enqueued_at") or ""),
        )


def _is_task_row(row: dict[str, Any]) -> bool:
    payload = dict(row.get("payload") or {})
    task_type = str(payload.get("task_type") or row.get("event_type") or "")
    return task_type.startswith("node.") or task_type.startswith("report.") or bool(payload.get("tasks"))


def _task_rows(row: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Expand one plan outbox row into small role-stream envelopes.

    Keeping the plan notification as one transactional outbox row preserves
    atomic plan submission while each Redis message still carries the strict
    v2 task envelope consumed by a role worker.
    """
    payload = dict(row.get("payload") or {})
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        return tuple({
            "outbox_id": f"{row['outbox_id']}:{index}",
            "aggregate_id": item.get("task_id") or row.get("aggregate_id"),
            "event_type": item.get("task_type") or "node.research.initial",
            "payload": {
                **item,
                "run_id": item.get("run_id") or payload.get("run_id"),
            },
            "dedup_key": item.get("idempotency_key") or f"{row.get('dedup_key')}:{index}",
        } for index, item in enumerate(tasks) if isinstance(item, dict))
    return (row,)


def _is_start_row(row: dict[str, Any]) -> bool:
    return str(row.get("event_type") or "") == "investigation.start"


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240] or exc.__class__.__name__


__all__ = ["DispatchResult", "OutboxDispatcher"]
