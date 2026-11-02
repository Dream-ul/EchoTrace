"""Short-lived UoW adapter implementing the runtime run-store port."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from echotrace.domain.models import Document, InvestigationRun
from .models import SourceDocument

from .repositories import SqlAlchemyUnitOfWork


class SqlAlchemyRunStore:
    """Expose repository operations without leaking a live Session.

    Each public call owns one transaction.  This is the safe adapter for the
    current synchronous runtime; application services can group run,
    task, audit and outbox writes in one explicitly injected Unit of Work.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save(self, run: InvestigationRun, *, expected_state_version: int | None = None) -> InvestigationRun:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.save(run, expected_state_version=expected_state_version)

    def get(self, run_id: str) -> InvestigationRun | None:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.get(run_id)

    def list(self, *, limit: int | None = None) -> list[InvestigationRun]:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.list(limit=limit)

    def list_summaries(self, *, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.list_summaries(limit=limit, offset=offset)

    def get_summary(self, run_id: str) -> dict[str, Any] | None:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.get_summary(run_id)

    def graph_state(self, run_id: str) -> dict[str, Any] | None:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.graph_state(run_id)

    def report_document(self, run_id: str) -> dict[str, Any] | None:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.report_document(run_id)

    def events(self, run_id: str, *, after_seq: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.events(run_id, after_seq=after_seq, limit=limit)

    def document_content_hash(self, document_id: str) -> str | None:
        """Return an existing immutable source hash for controller versioning."""
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            row = uow.session.get(SourceDocument, document_id)
            return str(row.content_hash) if row is not None else None

    def append_event(self, run_id: str, event: dict[str, Any]) -> int:
        """Append a live runtime event in its own short transaction."""
        payload = {
            key: value
            for key, value in event.items()
            if key not in {"seq", "step", "status", "at"}
        }
        payload["status"] = event.get("status")
        idempotency_key = f"trace:{run_id}:{event.get('seq')}"
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.append_event(
                run_id=run_id,
                event_type=str(event.get("step") or "unknown"),
                actor_type="runtime",
                payload=payload,
                idempotency_key=idempotency_key,
            )

    def append_documents(self, run_id: str, documents: list[Document]) -> int:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.runs.append_documents(run_id, documents)

    def save_analysis_feature(
        self,
        feature: dict[str, Any],
        *,
        run_id: str | None = None,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.analysis_features.save(feature, document_id=document_id)

    def list_analysis_features(self, document_id: str, *, feature_type: str | None = None) -> list[dict[str, Any]]:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.analysis_features.list(document_id, feature_type=feature_type)

    def list_analysis_features_many(self, document_ids: list[str]) -> list[dict[str, Any]]:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.analysis_features.list_for_documents(document_ids)



class SqlAlchemyTraceNodeStore:
    """Expose node checkpoints through one short transaction per operation."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def initialize_nodes(self, *, run_id: str, template: Any) -> list[dict[str, Any]]:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.trace_nodes.initialize_nodes(run_id=run_id, template=template)

    def list_nodes(self, run_id: str) -> list[dict[str, Any]]:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.trace_nodes.list_nodes(run_id)

    def begin_research(self, node_id: str, revision_no: int) -> bool:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.trace_nodes.begin_research(node_id, revision_no)

    def get_revision(self, node_id: str, revision_no: int | None = None) -> dict[str, Any] | None:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.trace_nodes.get_revision(node_id, revision_no)

    def get_review(self, node_id: str, revision_no: int | None = None) -> dict[str, Any] | None:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.trace_nodes.get_review(node_id, revision_no)

    def save_revision(self, revision: Any) -> str:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.trace_nodes.save_revision(revision)

    def save_review(self, review: Any) -> str:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.trace_nodes.save_review(review)

    def freeze_node(self, node_id: str) -> dict[str, Any]:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.trace_nodes.freeze_node(node_id)

    def update_frozen_conclusion_ir(self, node_id: str, revision_no: int, conclusion_ir: Any) -> str:
        with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            return uow.trace_nodes.update_frozen_conclusion_ir(node_id, revision_no, conclusion_ir)


__all__ = ["SqlAlchemyRunStore", "SqlAlchemyTraceNodeStore"]
