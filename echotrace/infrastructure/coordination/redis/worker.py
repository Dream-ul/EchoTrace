"""At-least-once Redis Streams worker with MySQL task authority."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any, Callable

from .client import RedisStreamBus
from .contracts import InvestigationStartMessage, NodeTaskMessage


logger = logging.getLogger("echotrace.redis.worker")


@dataclass(frozen=True, slots=True)
class WorkerResult:
    received: int = 0
    succeeded: int = 0
    retried: int = 0
    dead_lettered: int = 0
    failed: int = 0


class StreamTaskWorker:
    """Consume task envelopes and ACK only after domain persistence succeeds.

    ``handler`` performs the role-specific work.  It may return output
    references or ``{"output_refs": [...]}``; the worker itself never writes
    evidence directly and delegates task lifecycle transactions to the
    application service.
    """

    def __init__(self, bus: RedisStreamBus, task_service: Any, handler: Callable[[NodeTaskMessage], Any], *, role: str, consumer: str, max_deliveries: int = 3, lease_seconds: int = 60) -> None:
        self.bus = bus
        self.task_service = task_service
        self.handler = handler
        self.role = role
        self.consumer = consumer
        self.max_deliveries = max(1, int(max_deliveries))
        self.lease_seconds = max(1, int(lease_seconds))
        self.bus.ensure_group(role)

    def process_once(self, *, count: int = 1, block_ms: int = 0) -> WorkerResult:
        rows = self.bus.read(role=self.role, consumer=self.consumer, count=count, block_ms=block_ms)
        return self._process(rows)

    def reclaim_once(self, *, min_idle_ms: int = 60_000, count: int = 100) -> WorkerResult:
        rows = self.bus.reclaim(role=self.role, consumer=self.consumer, min_idle_ms=min_idle_ms, count=count)
        return self._process(rows)

    def _process(self, rows: list[tuple[str, dict[str, str], int]]) -> WorkerResult:
        result = WorkerResult(received=len(rows))
        for message_id, fields, deliveries in rows:
            # Reset per-delivery state; a failed previous message must not
            # accidentally reuse its attempt when this message is malformed.
            attempt: dict[str, Any] | None = None
            message: NodeTaskMessage | None = None
            try:
                message = NodeTaskMessage.from_fields(fields)
                if message.agent != self.role:
                    raise ValueError(f"task role mismatch: expected {self.role}, got {message.agent}")
                get_task = getattr(self.task_service, "get_task", None)
                if callable(get_task):
                    current = get_task(message.task_id)
                    if isinstance(current, dict) and current.get("status") in {"succeeded", "skipped", "cancelled"}:
                        # Duplicate delivery after a committed domain result is
                        # harmless and should not create a spurious retry.
                        self.bus.ack(self.role, message_id)
                        continue
                    if isinstance(current, dict) and current.get("status") == "running":
                        # Keep the entry pending. The current attempt may fail
                        # after a long run, and its message must remain available
                        # for a durable retry once MySQL records that failure.
                        continue
                try:
                    attempt = self.task_service.start_task(task_id=message.task_id, worker_id=self.consumer, lease_seconds=self.lease_seconds)
                except Exception:
                    # Another consumer may have claimed the task after the
                    # initial status read. Recheck MySQL before treating this
                    # duplicate delivery as a failed attempt or dead-lettering it.
                    if callable(get_task):
                        try:
                            current = get_task(message.task_id)
                        except Exception:
                            logger.exception("failed to recheck task after concurrent start: task_id=%s", message.task_id)
                        else:
                            status = current.get("status") if isinstance(current, dict) else None
                            if status == "running":
                                logger.info(
                                    "task start lost a concurrent claim; keeping message pending: task_id=%s consumer=%s",
                                    message.task_id,
                                    self.consumer,
                                )
                                continue
                            if status in {"succeeded", "skipped", "cancelled"}:
                                self.bus.ack(self.role, message_id)
                                continue
                    raise
                heartbeat_stop = Event()
                heartbeat_lost = Event()

                def maintain_lease() -> None:
                    heartbeat = getattr(self.task_service, "heartbeat_attempt", None)
                    if not callable(heartbeat):
                        return
                    interval = max(1.0, self.lease_seconds / 3)
                    while not heartbeat_stop.wait(interval):
                        try:
                            if not heartbeat(
                                attempt_id=attempt["attempt_id"],
                                worker_id=self.consumer,
                                lease_seconds=self.lease_seconds,
                            ):
                                heartbeat_lost.set()
                                return
                        except Exception:
                            continue

                heartbeat_thread = Thread(target=maintain_lease, name=f"lease-{message.task_id}", daemon=True)
                heartbeat_thread.start()
                try:
                    output = self.handler(message)
                finally:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=2)
                if heartbeat_lost.is_set():
                    raise RuntimeError("task lease ownership was lost while the role handler was running")
                get_run_status = getattr(self.task_service, "get_run_status", None)
                if callable(get_run_status) and get_run_status(message.run_id) == "paused":
                    defer = getattr(self.task_service, "defer_task", None)
                    if callable(defer):
                        defer(
                            attempt_id=attempt["attempt_id"], worker_id=self.consumer,
                            reason="investigation paused before task completion",
                        )
                    continue
                refs = _output_refs(output)
                committed = self.task_service.finish_task(
                    attempt_id=attempt["attempt_id"], status="succeeded",
                    output_refs=refs, worker_id=self.consumer,
                )
                if committed is False:
                    current = get_task(message.task_id) if callable(get_task) else None
                    if (
                        isinstance(current, dict)
                        and current.get("status") == "pending"
                        and callable(get_run_status)
                        and get_run_status(message.run_id) == "paused"
                    ):
                        continue
                    raise RuntimeError("task result was not committed; message remains pending")
                self.bus.ack(self.role, message_id)
                result = _add(result, succeeded=1)
            except Exception as exc:
                if getattr(exc, "defer_task", False):
                    defer = getattr(self.task_service, "defer_task", None)
                    if isinstance(attempt, dict) and callable(defer):
                        try:
                            defer(
                                attempt_id=attempt["attempt_id"], worker_id=self.consumer,
                                reason=_safe_error(exc),
                            )
                        except Exception:
                            logger.exception("failed to defer paused task: task_id=%s", message.task_id if message else "unknown")
                    continue
                if getattr(exc, "not_ready", False):
                    # The message remains pending.  The dependency task will
                    # complete in MySQL and a later reclaim pass will retry it.
                    continue
                # If an attempt was started, complete it as failed so the next
                # delivery creates a new immutable TaskAttempt row.
                if isinstance(attempt, dict):
                    try:
                        self.task_service.finish_task(
                            attempt_id=attempt["attempt_id"], status="failed",
                            error_code=str(getattr(exc, "error_code", "worker_error")),
                            error_summary=_safe_error(exc),
                            worker_id=self.consumer,
                        )
                    except Exception:
                        pass
                attempt_no = int(attempt.get("attempt_no") or 0) if isinstance(attempt, dict) else 0
                retries_exhausted = (
                    attempt_no >= self.max_deliveries
                    if attempt_no > 0
                    else deliveries >= self.max_deliveries
                )
                if getattr(exc, "retryable", True) is False or retries_exhausted:
                    self.bus.dead_letter(role=self.role, message_id=message_id, fields=fields, reason=_safe_error(exc), deliveries=deliveries)
                    self.bus.ack(self.role, message_id)
                    pause = getattr(self.task_service, "pause_run_for_task_failure", None)
                    if callable(pause) and message is not None:
                        try:
                            pause(run_id=message.run_id, task_id=message.task_id, reason=_safe_error(exc))
                        except Exception:
                            logger.exception(
                                "failed to pause run after task dead-letter: run_id=%s task_id=%s",
                                message.run_id,
                                message.task_id,
                            )
                    result = _add(result, dead_lettered=1, failed=1)
                else:
                    result = _add(result, retried=1, failed=1)
        return result


class ReportComposerWorker(StreamTaskWorker):
    """Role worker for the two-phase report.plan/report.section graph."""

    def __init__(
        self,
        bus: RedisStreamBus,
        task_service: Any,
        *,
        plan_handler: Callable[[NodeTaskMessage], Any],
        section_handler: Callable[[NodeTaskMessage], Any],
        consumer: str,
        max_deliveries: int = 3,
        lease_seconds: int = 60,
    ) -> None:
        self._plan_handler = plan_handler
        self._section_handler = section_handler
        super().__init__(bus, task_service, self._handle, role="report_composer", consumer=consumer, max_deliveries=max_deliveries, lease_seconds=lease_seconds)

    def _handle(self, message: NodeTaskMessage) -> Any:
        if message.task_type == "report.plan":
            return self._plan_handler(message)
        if message.task_type == "report.section":
            return self._section_handler(message)
        raise ValueError(f"unsupported report task type: {message.task_type}")


class OrchestratorRunWorker:
    """Consume durable run-start commands outside the API process."""

    def __init__(
        self,
        bus: RedisStreamBus,
        handler: Callable[[str, str], Any],
        *,
        consumer: str,
        max_deliveries: int = 3,
        on_terminal_failure: Callable[[str, Exception], Any] | None = None,
    ) -> None:
        self.bus = bus
        self.handler = handler
        self.consumer = consumer
        self.max_deliveries = max(1, int(max_deliveries))
        self.on_terminal_failure = on_terminal_failure
        self.bus.ensure_group("orchestrator")

    def process_once(self, *, count: int = 1, block_ms: int = 0) -> WorkerResult:
        rows = self.bus.read(role="orchestrator", consumer=self.consumer, count=count, block_ms=block_ms)
        return self._process(rows)

    def reclaim_once(self, *, min_idle_ms: int = 60_000, count: int = 100) -> WorkerResult:
        rows = self.bus.reclaim(role="orchestrator", consumer=self.consumer, min_idle_ms=min_idle_ms, count=count)
        return self._process(rows)

    def _process(self, rows: list[tuple[str, dict[str, str], int]]) -> WorkerResult:
        result = WorkerResult(received=len(rows))
        for message_id, fields, deliveries in rows:
            message: InvestigationStartMessage | None = None
            try:
                message = InvestigationStartMessage.from_fields(fields)
                self.handler(message.run_id, message.outbox_id)
                self.bus.ack("orchestrator", message_id)
                result = _add(result, succeeded=1)
            except Exception as exc:
                if getattr(exc, "defer_task", False):
                    # Resume publishes a new start command after the run state
                    # changes, so this paused in-flight command is complete.
                    self.bus.ack("orchestrator", message_id)
                    continue
                if getattr(exc, "not_ready", False):
                    continue
                if getattr(exc, "retryable", True) is False or deliveries >= self.max_deliveries:
                    self.bus.dead_letter(
                        role="orchestrator", message_id=message_id, fields=fields,
                        reason=_safe_error(exc), deliveries=deliveries,
                    )
                    self.bus.ack("orchestrator", message_id)
                    if message is not None and self.on_terminal_failure is not None:
                        try:
                            self.on_terminal_failure(message.run_id, exc)
                        except Exception:
                            logger.exception(
                                "failed to record terminal orchestrator failure: run_id=%s",
                                message.run_id,
                            )
                    result = _add(result, dead_lettered=1, failed=1)
                else:
                    result = _add(result, retried=1, failed=1)
        return result


def _output_refs(output: Any) -> list[str]:
    if isinstance(output, dict):
        values = output.get("output_refs") or output.get("refs") or []
    elif isinstance(output, (list, tuple, set)):
        values = output
    else:
        values = []
    return [str(value) for value in values]


def _add(result: WorkerResult, **changes: int) -> WorkerResult:
    values = {field: getattr(result, field) for field in result.__dataclass_fields__}
    for key, value in changes.items():
        values[key] += value
    return WorkerResult(**values)


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240] or exc.__class__.__name__


__all__ = ["OrchestratorRunWorker", "ReportComposerWorker", "StreamTaskWorker", "WorkerResult"]
