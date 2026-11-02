"""Stable, framework-independent domain contracts for EchoTrace."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import uuid4
from threading import RLock
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """Return the application wall-clock used for persisted timestamps."""

    return datetime.now(BEIJING_TZ)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class EventStage(StrEnum):
    EMERGING = "emerging"
    SPREADING = "spreading"
    CONTESTED = "contested"
    RESPONDED = "responded"
    COOLING = "cooling"


class InvestigationStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Document:
    source: str
    title: str
    content: str
    url: str | None = None
    published_at: datetime | None = None
    fetched_at: datetime = field(default_factory=beijing_now)
    document_id: str = field(default_factory=lambda: new_id("doc"))
    author: str | None = None
    content_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("document content cannot be empty")
        if not self.content_hash:
            self.content_hash = sha256(self.content.strip().encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Event:
    title: str
    objective: str
    event_id: str = field(default_factory=lambda: new_id("event"))
    stage: EventStage = EventStage.EMERGING
    aliases: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    risk_score: float = 0.0
    first_seen_at: datetime = field(default_factory=beijing_now)
    last_seen_at: datetime = field(default_factory=beijing_now)


@dataclass(slots=True)
class InvestigationRun:
    objective: str
    event: Event
    investigation_type: str = "auto"
    subject: str | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: new_id("run"))
    status: InvestigationStatus = InvestigationStatus.CREATED
    documents: list[Document] = field(default_factory=list)
    report_document: dict[str, Any] | None = None
    # Durable node workflow state. It contains node plan, revisions, reviews,
    # and the frozen trace; prompts and provider credentials never enter it.
    graph_state: dict[str, Any] = field(default_factory=dict)
    checkpoint: str | None = None
    # Runtime checkpoint state used to resume the node controller.
    state_node: str | None = None
    state_version: int = 0
    plan_id: str | None = None
    loop_index: int = 0
    active_tasks: list[str] = field(default_factory=list)
    interrupt_reason: str | None = None
    resume_token_hash: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=beijing_now)
    updated_at: datetime = field(default_factory=beijing_now)
    trace: list[dict[str, Any]] = field(default_factory=list)
    trace_seq: int = 0
    _trace_lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def touch(self) -> None:
        self.updated_at = beijing_now()

    def add_trace(self, step: str, status: str, **data: Any) -> dict[str, Any]:
        with self._trace_lock:
            self.trace_seq += 1
            event = {"seq": self.trace_seq, "step": step, "status": status, "at": beijing_now().isoformat(), **data}
            self.trace.append(event)
            self.touch()
            return event

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InvestigationRun":
        payload = dict(payload)
        event_data = dict(payload.pop("event"))
        event_data["stage"] = EventStage(event_data.get("stage", EventStage.EMERGING))
        event = Event(**_restore_datetime(event_data, {"first_seen_at", "last_seen_at"}))
        documents = [Document(**_restore_datetime(item, {"published_at", "fetched_at"})) for item in payload.pop("documents", [])]
        payload["status"] = InvestigationStatus(payload.get("status", InvestigationStatus.CREATED))
        trace = payload.get("trace", [])
        payload["trace_seq"] = payload.get("trace_seq") or max((item.get("seq", 0) for item in trace), default=0)
        for key in ("created_at", "updated_at"):
            if isinstance(payload.get(key), str):
                payload[key] = datetime.fromisoformat(payload[key])
        return cls(event=event, documents=documents, **payload)


def _restore_datetime(value: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    result = dict(value)
    for key in keys:
        if isinstance(result.get(key), str):
            result[key] = datetime.fromisoformat(result[key])
    return result


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
            if not item.name.startswith("_")
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
