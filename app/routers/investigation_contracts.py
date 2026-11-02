"""Public response contracts for the investigation read API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventSummary(ApiModel):
    event_id: str
    title: str
    stage: str
    risk_score: float


class InvestigationSummary(ApiModel):
    run_id: str
    objective: str
    investigation_type: str
    subject: str | None
    status: str
    checkpoint: str | None
    created_at: datetime
    updated_at: datetime
    event: EventSummary


class InvestigationDetail(InvestigationSummary):
    scope: dict[str, Any]
    error: str | None
    interrupt_reason: str | None
    state_node: str | None
    template_id: str | None
    template_version: str | None


class InvestigationListResponse(ApiModel):
    success: bool
    runs: list[InvestigationSummary]
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


class InvestigationResponse(ApiModel):
    success: bool
    run: InvestigationSummary | InvestigationDetail


class EventPageResponse(ApiModel):
    success: bool
    events: list[dict[str, Any]]
    after_seq: int
    limit: int
    has_more: bool
    next_after_seq: int | None


class NodeRevisionRead(ApiModel):
    revision: int
    conclusion: str
    draft: dict[str, Any]
    review: dict[str, Any] | None
    created_at: datetime | None


class NodeRead(ApiModel):
    node_id: str
    run_id: str
    template_id: str
    template_version: str
    slot_id: str
    node_order: int
    instance_no: int
    status: str
    current_revision: int
    frozen_revision: int | None
    current_review_id: str | None
    title: str
    goal: str
    aspects: list[str]
    revision_history: list[NodeRevisionRead]


class NodeListResponse(ApiModel):
    success: bool
    run_id: str
    nodes: list[NodeRead]


class FrozenTraceRead(ApiModel):
    trace_id: str
    run_id: str
    investigation_type: str
    subject: str
    template_id: str
    template_version: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class TraceResponse(ApiModel):
    success: bool
    trace: FrozenTraceRead
    review: dict[str, Any] | None


class ReportResponse(ApiModel):
    success: bool
    report: dict[str, Any]


class ReportSectionProgress(ApiModel):
    chapter_id: str
    title: str
    status: str


class ReportProgressResponse(ApiModel):
    success: bool
    exists: bool
    status: str
    planning_status: str | None
    completed_sections: int
    total_sections: int
    sections: list[ReportSectionProgress]


class WorkbenchEvent(ApiModel):
    event_id: str
    title: str
    objective: str
    stage: str
    aliases: list[str]
    entities: list[str]
    risk_score: float
    first_seen_at: datetime
    last_seen_at: datetime


class WorkbenchCounts(ApiModel):
    documents: int
    claims: int
    evidence: int
    nodes: int
    frozen_nodes: int


class WorkbenchDocument(ApiModel):
    document_id: str
    source: str
    title: str
    url: str | None


class WorkbenchRead(ApiModel):
    run_id: str
    objective: str
    subject: str | None
    investigation_type: str
    status: str
    checkpoint: str | None
    event: WorkbenchEvent
    error: str | None
    documents: list[WorkbenchDocument]
    counts: WorkbenchCounts
    workflow_stages: list[dict[str, Any]]
    lifecycle_events: list[dict[str, Any]]
    audit_events: list[dict[str, Any]]
    timeline_entries: list[dict[str, Any]]
    recent_events: list[dict[str, Any]]
    source_clusters: list[dict[str, Any]]
    skill_audit: list[dict[str, Any]]
    tool_audit: list[dict[str, Any]]
    report: dict[str, Any] | None


class WorkbenchResponse(ApiModel):
    success: bool
    workbench: WorkbenchRead


__all__ = [
    "EventPageResponse",
    "InvestigationDetail",
    "InvestigationListResponse",
    "InvestigationResponse",
    "InvestigationSummary",
    "NodeListResponse",
    "NodeRead",
    "NodeRevisionRead",
    "ReportResponse",
    "ReportProgressResponse",
    "TraceResponse",
    "WorkbenchResponse",
    "WorkbenchRead",
]
