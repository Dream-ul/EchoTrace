"""SQLAlchemy models for the frozen EchoTrace storage contract.

The models intentionally use portable SQLAlchemy types so the same metadata can
be exercised against SQLite in unit tests and MySQL 8/InnoDB in deployment.
Identifiers remain strings because domain IDs carry a
short semantic prefix (``run_``, ``doc_`` etc.) while still fitting the frozen
36-character identifier budget.  Production deployments may replace the
generator with UUIDs without changing the relationships.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from echotrace.domain.models import beijing_now


class Base(DeclarativeBase):
    """Declarative base shared by migration and repositories."""


Id = String(36)
Short = String(128)
# URLs from public search providers regularly include signed/tracking query
# strings well beyond 255 characters. Keep opaque long values as Text; URL
# lookup/deduplication is performed through canonical_url_hash, not a prefix
# index on the raw value.
Long = String(1024)
BeijingDateTime = DateTime(timezone=True).with_variant(MYSQL_DATETIME(fsp=6), "mysql")


class SourceDocument(Base):
    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("provider", "source_identity_hash", "content_hash", name="uq_source_identity_content"),
        CheckConstraint("external_id IS NOT NULL OR canonical_url IS NOT NULL", name="ck_source_identity_present"),
        CheckConstraint("source_type IN ('rss','public_page','official_announcement','web_search','user_provided')", name="ck_source_type"),
        Index("ix_source_documents_content_hash", "content_hash"),
        Index("ix_source_documents_canonical_url_hash", "canonical_url_hash"),
        Index("ix_source_documents_published_at", "published_at"),
    )

    document_id: Mapped[str] = mapped_column(Id, primary_key=True)
    source_type: Mapped[str] = mapped_column(Short, nullable=False)
    provider: Mapped[str] = mapped_column(Short, nullable=False, default="")
    external_id: Mapped[str | None] = mapped_column(Short)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    canonical_url_hash: Mapped[str | None] = mapped_column(String(64))
    source_identity_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    supersedes_document_id: Mapped[str | None] = mapped_column(ForeignKey("source_documents.document_id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(Long, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(Short)
    author_name: Mapped[str | None] = mapped_column(Short)
    published_at: Mapped[datetime | None] = mapped_column(BeijingDateTime)
    fetched_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    raw_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class SourceCluster(Base):
    __tablename__ = "source_clusters"
    __table_args__ = (Index("ix_source_clusters_type_created", "cluster_type", "created_at"),)

    cluster_id: Mapped[str] = mapped_column(Id, primary_key=True)
    cluster_type: Mapped[str] = mapped_column(Short, nullable=False)
    canonical_document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.document_id", ondelete="RESTRICT"), nullable=False)
    method: Mapped[str] = mapped_column(Short, nullable=False)
    model_name: Mapped[str | None] = mapped_column(Short)
    model_version: Mapped[str | None] = mapped_column(Short)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class SourceClusterMember(Base):
    __tablename__ = "source_cluster_members"
    __table_args__ = (CheckConstraint("similarity_score IS NULL OR (similarity_score >= 0 AND similarity_score <= 1)", name="ck_cluster_similarity"),)

    cluster_id: Mapped[str] = mapped_column(ForeignKey("source_clusters.cluster_id", ondelete="RESTRICT"), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.document_id", ondelete="RESTRICT"), primary_key=True)
    member_role: Mapped[str] = mapped_column(Short, nullable=False, default="member")
    similarity_score: Mapped[float | None] = mapped_column(Float)


class AnalysisFeature(Base):
    __tablename__ = "analysis_features"
    __table_args__ = (
        UniqueConstraint("feature_type", "scope_hash", "model_name", "model_version", "prompt_version", "input_hash", name="uq_analysis_feature_input"),
        Index("ix_analysis_features_document", "document_id"),
    )

    feature_id: Mapped[str] = mapped_column(Id, primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.document_id", ondelete="RESTRICT"), nullable=False)
    feature_type: Mapped[str] = mapped_column(Short, nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    model_name: Mapped[str | None] = mapped_column(Short)
    model_version: Mapped[str | None] = mapped_column(Short)
    prompt_version: Mapped[str | None] = mapped_column(Short)
    input_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class TraceTemplateRow(Base):
    __tablename__ = "trace_templates"
    __table_args__ = (
        UniqueConstraint("template_id", "template_version", name="uq_trace_template_version"),
        Index("ix_trace_templates_type", "investigation_type"),
    )

    template_key: Mapped[str] = mapped_column(Short, primary_key=True)
    template_id: Mapped[str] = mapped_column(Short, nullable=False)
    template_version: Mapped[str] = mapped_column(Short, nullable=False)
    investigation_type: Mapped[str] = mapped_column(Short, nullable=False)
    display_name: Mapped[str] = mapped_column(Long, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Short, nullable=False, default="zh-CN")
    max_node_revisions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class TraceTemplateNodeRow(Base):
    __tablename__ = "trace_template_nodes"
    __table_args__ = (Index("ix_trace_template_nodes_order", "template_key", "node_order"),)

    template_key: Mapped[str] = mapped_column(ForeignKey("trace_templates.template_key", ondelete="RESTRICT"), primary_key=True)
    slot_id: Mapped[str] = mapped_column(Short, primary_key=True)
    node_order: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Long, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    aspects_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    claim_types_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    preferred_sources_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    research_questions_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    review_rubric_json: Mapped[dict[str, list[str]]] = mapped_column(JSON, default=dict, nullable=False)
    depends_on_slot_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    allow_multiple_instances: Mapped[bool] = mapped_column(nullable=False, default=False)
    media_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class TraceTemplateEdgeRow(Base):
    __tablename__ = "trace_template_edges"
    __table_args__ = (Index("ix_trace_template_edges_from", "template_key", "from_slot_id"),)

    template_key: Mapped[str] = mapped_column(ForeignKey("trace_templates.template_key", ondelete="RESTRICT"), primary_key=True)
    edge_order: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_slot_id: Mapped[str] = mapped_column(Short, nullable=False)
    to_slot_id: Mapped[str] = mapped_column(Short, nullable=False)
    relation: Mapped[str] = mapped_column(Short, nullable=False)
    label_goal: Mapped[str] = mapped_column(Text, nullable=False)
    requires_explicit_support: Mapped[bool] = mapped_column(nullable=False, default=False)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("stage IN ('emerging','spreading','contested','responded','cooling')", name="ck_event_stage"),
        CheckConstraint("risk_score >= 0 AND risk_score <= 1", name="ck_event_risk_score"),
    )

    event_id: Mapped[str] = mapped_column(Id, primary_key=True)
    title: Mapped[str] = mapped_column(Long, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Short, nullable=False, default="emerging")
    risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    entities_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, onupdate=beijing_now, nullable=False)


class InvestigationNode(Base):
    __tablename__ = "investigation_nodes"
    __table_args__ = (
        UniqueConstraint("run_id", "slot_id", "instance_no", name="uq_investigation_node_slot_instance"),
        CheckConstraint("status IN ('pending','researching','reviewing','revise','frozen','blocked','failed')", name="ck_investigation_node_status"),
        CheckConstraint("current_revision >= 0 AND current_revision <= 3", name="ck_investigation_node_revision"),
        Index("ix_investigation_nodes_run_status", "run_id", "status"),
    )

    node_id: Mapped[str] = mapped_column(Id, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.run_id", ondelete="RESTRICT"), nullable=False)
    template_id: Mapped[str] = mapped_column(Short, nullable=False)
    template_version: Mapped[str] = mapped_column(Short, nullable=False)
    slot_id: Mapped[str] = mapped_column(Short, nullable=False)
    node_order: Mapped[int] = mapped_column(Integer, nullable=False)
    instance_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Short, nullable=False, default="pending")
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frozen_revision: Mapped[int | None] = mapped_column(Integer)
    current_review_id: Mapped[str | None] = mapped_column(Short)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, onupdate=beijing_now, nullable=False)


class NodeRevision(Base):
    __tablename__ = "node_revisions"
    __table_args__ = (
        UniqueConstraint("node_id", "revision_no", name="uq_node_revision_no"),
        CheckConstraint("revision_no >= 0 AND revision_no <= 3", name="ck_node_revision_no"),
        Index("ix_node_revisions_node_created", "node_id", "created_at"),
    )

    revision_id: Mapped[str] = mapped_column(Id, primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("investigation_nodes.node_id", ondelete="RESTRICT"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False)
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class NodeClaim(Base):
    __tablename__ = "node_claims"
    __table_args__ = (Index("ix_node_claims_revision", "revision_id"),)

    claim_id: Mapped[str] = mapped_column(Id, primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("node_revisions.revision_id", ondelete="RESTRICT"), nullable=False)
    claim_type: Mapped[str] = mapped_column(Short, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    predicate: Mapped[str] = mapped_column(Text, nullable=False, default="")
    object_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    time_scope: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_document_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class NodeEvidence(Base):
    __tablename__ = "node_evidence"
    __table_args__ = (
        CheckConstraint("relation IN ('supports','contradicts','mentions')", name="ck_node_evidence_relation"),
        Index("ix_node_evidence_revision", "revision_id"),
        Index("ix_node_evidence_claim", "claim_id"),
    )

    evidence_id: Mapped[str] = mapped_column(Id, primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("node_revisions.revision_id", ondelete="RESTRICT"), nullable=False)
    claim_id: Mapped[str] = mapped_column(ForeignKey("node_claims.claim_id", ondelete="RESTRICT"), nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.document_id", ondelete="RESTRICT"), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    locator_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    relation: Mapped[str] = mapped_column(Short, nullable=False, default="supports")


class NodeMedia(Base):
    __tablename__ = "node_media"
    __table_args__ = (
        CheckConstraint("media_type IN ('image','video')", name="ck_node_media_type"),
        CheckConstraint("(source_url IS NOT NULL AND object_key IS NULL) OR (source_url IS NULL AND object_key IS NOT NULL)", name="ck_node_media_locator"),
        Index("ix_node_media_revision", "revision_id"),
    )

    media_id: Mapped[str] = mapped_column(Id, primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("node_revisions.revision_id", ondelete="RESTRICT"), nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.document_id", ondelete="RESTRICT"), nullable=False)
    media_type: Mapped[str] = mapped_column(Short, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    object_key: Mapped[str | None] = mapped_column(String(512))
    caption: Mapped[str] = mapped_column(Text, nullable=False)
    why_relevant: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(BeijingDateTime)
    captured_at: Mapped[datetime | None] = mapped_column(BeijingDateTime)


class NodeReview(Base):
    __tablename__ = "node_reviews"
    __table_args__ = (
        UniqueConstraint("node_id", "revision_no", name="uq_node_review_revision"),
        CheckConstraint("revision_no >= 1 AND revision_no <= 3", name="ck_node_review_no"),
        CheckConstraint("status IN ('revise','finalize')", name="ck_node_review_status"),
        Index("ix_node_reviews_node_created", "node_id", "created_at"),
    )

    review_id: Mapped[str] = mapped_column(Id, primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("investigation_nodes.node_id", ondelete="RESTRICT"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Short, nullable=False)
    covered_aspects_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    missing_aspects_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    redundant_claims_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    unclear_claims_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evidence_followups_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    rewrite_instructions_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    next_research_tasks_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    final_editor_note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class EventDocument(Base):
    __tablename__ = "event_documents"
    __table_args__ = (Index("ix_event_documents_document_event", "document_id", "event_id"),)

    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id", ondelete="RESTRICT"), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.document_id", ondelete="RESTRICT"), primary_key=True)
    relation: Mapped[str] = mapped_column(Short, nullable=False, default="mentions")
    added_by_run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.run_id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class InvestigationRun(Base):
    __tablename__ = "investigation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('created','running','paused','completed','failed','cancelled')", name="ck_run_status"),
        Index("ix_investigation_runs_event", "event_id"),
    )

    run_id: Mapped[str] = mapped_column(Id, primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id", ondelete="RESTRICT"), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    investigation_type: Mapped[str] = mapped_column(Short, nullable=False, default="auto")
    template_id: Mapped[str | None] = mapped_column(Short)
    template_version: Mapped[str | None] = mapped_column(Short)
    subject: Mapped[str | None] = mapped_column(Long)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    report_document_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    graph_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(Short, nullable=False, default="created")
    state_node: Mapped[str | None] = mapped_column(Short)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("task_plans.plan_id", ondelete="RESTRICT", use_alter=True))
    loop_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interrupt_reason: Mapped[str | None] = mapped_column(Text)
    resume_token_hash: Mapped[str | None] = mapped_column(String(128))
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, onupdate=beijing_now, nullable=False)


class TaskPlan(Base):
    __tablename__ = "task_plans"
    __table_args__ = (
        UniqueConstraint("run_id", "plan_version", name="uq_task_plan_version"),
        UniqueConstraint("run_id", "plan_hash", name="uq_task_plan_hash"),
        CheckConstraint("status IN ('proposed','active','superseded','rejected')", name="ck_plan_status"),
    )

    plan_id: Mapped[str] = mapped_column(Id, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.run_id", ondelete="RESTRICT"), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_plan_id: Mapped[str | None] = mapped_column(ForeignKey("task_plans.plan_id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(Short, nullable=False, default="proposed")
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    rationale_summary: Mapped[str | None] = mapped_column(Text)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    allowed_agents_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    allowed_capabilities_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    stop_conditions_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, onupdate=beijing_now, nullable=False)


class InvestigationTask(Base):
    __tablename__ = "investigation_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "idempotency_key", name="uq_task_idempotency"),
        CheckConstraint("status IN ('pending','running','succeeded','failed','skipped','cancelled')", name="ck_task_status"),
        Index("ix_investigation_tasks_run_status_priority", "run_id", "status", "priority"),
    )

    task_id: Mapped[str] = mapped_column(Id, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.run_id", ondelete="RESTRICT"), nullable=False)
    plan_id: Mapped[str] = mapped_column(ForeignKey("task_plans.plan_id", ondelete="RESTRICT"), nullable=False)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("investigation_nodes.node_id", ondelete="RESTRICT"))
    slot_id: Mapped[str | None] = mapped_column(Short)
    node_revision: Mapped[int | None] = mapped_column(Integer)
    review_id: Mapped[str | None] = mapped_column(ForeignKey("node_reviews.review_id", ondelete="RESTRICT"))
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("investigation_tasks.task_id", ondelete="RESTRICT"))
    agent: Mapped[str] = mapped_column(Short, nullable=False)
    stage: Mapped[str] = mapped_column(Short, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    capability: Mapped[str] = mapped_column(Short, nullable=False)
    status: Mapped[str] = mapped_column(Short, nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    current_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(Short)
    lease_until: Mapped[datetime | None] = mapped_column(BeijingDateTime)
    input_refs_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_refs_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Short)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, onupdate=beijing_now, nullable=False)


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dependency_no_self"),)

    task_id: Mapped[str] = mapped_column(ForeignKey("investigation_tasks.task_id", ondelete="RESTRICT"), primary_key=True)
    depends_on_task_id: Mapped[str] = mapped_column(ForeignKey("investigation_tasks.task_id", ondelete="RESTRICT"), primary_key=True)


class TaskAttempt(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt_no", name="uq_task_attempt_no"),)

    attempt_id: Mapped[str] = mapped_column(Id, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("investigation_tasks.task_id", ondelete="RESTRICT"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Short)
    status: Mapped[str] = mapped_column(Short, nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(BeijingDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(BeijingDateTime)
    error_code: Mapped[str | None] = mapped_column(Short)
    error_summary: Mapped[str | None] = mapped_column(Text)


class RunCheckpoint(Base):
    __tablename__ = "run_checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "state_version", name="uq_checkpoint_version"),)

    checkpoint_id: Mapped[str] = mapped_column(Id, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.run_id", ondelete="RESTRICT"), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state_node: Mapped[str | None] = mapped_column(Short)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_event_seq"),
        Index("ix_run_events_run_created", "run_id", "created_at"),
    )

    run_event_id: Mapped[str] = mapped_column(Id, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.run_id", ondelete="RESTRICT"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Short, nullable=False)
    actor_type: Mapped[str | None] = mapped_column(Short)
    actor_id: Mapped[str | None] = mapped_column(Short)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class AgentSkillDefinition(Base):
    __tablename__ = "agent_skill_definitions"
    __table_args__ = (UniqueConstraint("skill_name", "content_hash", name="uq_skill_definition_hash"),)

    skill_definition_id: Mapped[str] = mapped_column(Id, primary_key=True)
    skill_name: Mapped[str] = mapped_column(Short, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    package_path: Mapped[str] = mapped_column(Long, nullable=False)
    status: Mapped[str] = mapped_column(Short, nullable=False, default="active")
    loaded_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class AgentSkillActivation(Base):
    __tablename__ = "agent_skill_activations"
    __table_args__ = (UniqueConstraint("run_id", "task_id", "agent", "step_no", name="uq_skill_activation_step"),)

    activation_id: Mapped[str] = mapped_column(Id, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.run_id", ondelete="RESTRICT"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("investigation_tasks.task_id", ondelete="RESTRICT"), nullable=False)
    agent: Mapped[str] = mapped_column(Short, nullable=False)
    skill_definition_id: Mapped[str] = mapped_column(ForeignKey("agent_skill_definitions.skill_definition_id", ondelete="RESTRICT"), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_summary: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class ToolProviderDefinition(Base):
    __tablename__ = "tool_provider_definitions"
    __table_args__ = (UniqueConstraint("provider_name", "provider_version", "manifest_hash", name="uq_provider_manifest"),)

    provider_definition_id: Mapped[str] = mapped_column(Id, primary_key=True)
    provider_name: Mapped[str] = mapped_column(Short, nullable=False)
    provider_version: Mapped[str] = mapped_column(Short, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(Short, nullable=False, default="active")
    loaded_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", "provider_attempt_no", name="uq_tool_call_attempt"),
        CheckConstraint("status IN ('started','succeeded','fallback_succeeded','failed','skipped')", name="ck_tool_call_status"),
    )

    tool_call_id: Mapped[str] = mapped_column(Id, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.run_id", ondelete="RESTRICT"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("investigation_tasks.task_id", ondelete="RESTRICT"), nullable=False)
    agent: Mapped[str] = mapped_column(Short, nullable=False)
    capability: Mapped[str] = mapped_column(Short, nullable=False)
    provider_definition_id: Mapped[str] = mapped_column(ForeignKey("tool_provider_definitions.provider_definition_id", ondelete="RESTRICT"), nullable=False)
    attempt_group_id: Mapped[str] = mapped_column(Id, nullable=False)
    provider_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(Short, nullable=False, default="started")
    input_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    output_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    token_usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    cost_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Short)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(BeijingDateTime)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_outbox_dedup_key"),
        CheckConstraint("status IN ('pending','publishing','published','dead')", name="ck_outbox_status"),
        Index("ix_outbox_claim", "status", "available_at", "locked_until"),
    )

    outbox_id: Mapped[str] = mapped_column(Id, primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(Short, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Id, nullable=False)
    event_type: Mapped[str] = mapped_column(Short, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(Short, nullable=False, default="pending")
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(Short)
    locked_until: Mapped[datetime | None] = mapped_column(BeijingDateTime)
    published_message_id: Mapped[str | None] = mapped_column(Short)
    published_at: Mapped[datetime | None] = mapped_column(BeijingDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(BeijingDateTime, default=beijing_now, nullable=False)


# Fix production storage parameters without sacrificing SQLite tests.
for _table in Base.metadata.tables.values():
    _table.dialect_options["mysql"]["engine"] = "InnoDB"
    _table.dialect_options["mysql"]["charset"] = "utf8mb4"


__all__ = [name for name, value in globals().items() if isinstance(value, type) and issubclass(value, Base) and value is not Base] + ["Base"]
