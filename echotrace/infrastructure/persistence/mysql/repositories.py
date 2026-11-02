"""Repositories and transactional unit of work for EchoTrace.

The adapter maps framework-independent domain objects to append-
or-update rows.  It deliberately exposes no SQLAlchemy session to callers.
All writes are intended to happen inside ``SqlAlchemyUnitOfWork``; exiting a
successful context commits once, while exceptions roll back the whole batch.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from echotrace.domain.models import (
    Document,
    Event as DomainEvent,
    EventStage,
    InvestigationRun as DomainRun,
    InvestigationStatus,
    BEIJING_TZ,
    beijing_now,
)
from echotrace.domain.trace_nodes import (
    ConclusionIR,
    EdgeSpec,
    NodeClaim,
    NodeDraft,
    NodeEvidence,
    NodeMedia,
    NodeMediaPolicy,
    NodeRevision as DomainNodeRevision,
    NodeReview,
    NodeSpec,
    TraceTemplate,
)

from .models import (
    AgentSkillActivation,
    AgentSkillDefinition,
    AnalysisFeature,
    Event,
    EventDocument,
    InvestigationTask,
    TaskAttempt,
    InvestigationRun,
    OutboxEvent,
    RunCheckpoint,
    RunEvent,
    SourceDocument,
    TaskPlan,
    TaskDependency,
    ToolCall,
    ToolProviderDefinition,
    TraceTemplateEdgeRow,
    TraceTemplateNodeRow,
    TraceTemplateRow,
    InvestigationNode,
    NodeClaim as NodeClaimRow,
    NodeEvidence as NodeEvidenceRow,
    NodeMedia as NodeMediaRow,
    NodeRevision as NodeRevisionRow,
    NodeReview as NodeReviewRow,
)


class ConcurrentUpdateError(RuntimeError):
    """Raised when optimistic state-version compare-and-swap fails."""


_CANONICAL_SOURCE_TYPES = {"rss", "public_page", "official_announcement", "web_search", "user_provided"}

# Checkpoints are recovery metadata, not a second source-of-truth archive.
# Full source bodies are kept in ``source_documents`` and full runtime events
# are kept in ``run_events``.  Keeping only a bounded tail here prevents a run
# with many search results or provider events from exceeding MySQL's packet
# limit while retaining enough information to inspect/resume the latest step.
_CHECKPOINT_SCHEMA_VERSION = 2
_CHECKPOINT_TRACE_TAIL = 64
_CHECKPOINT_STRING_LIMIT = 2048
_CHECKPOINT_LIST_LIMIT = 32
_CHECKPOINT_DICT_LIMIT = 64


def _canonical_source_type(source: str) -> str:
    """Map provider/source labels to the bounded persistence taxonomy.

    Search and fetch providers intentionally return human-useful labels such
    as ``www.whu.edu.cn`` or ``tavily``.  Those labels are preserved in
    ``provider``/``_source_label``; only the relational ``source_type`` needs a
    stable enum for MySQL's check constraint.  Unknown external labels are
    treated as public pages rather than making an otherwise valid investigation
    fail during a late checkpoint.
    """
    raw = str(source or "").strip()
    key = raw.lower()
    if key in _CANONICAL_SOURCE_TYPES:
        return key
    aliases = {
        "official": "official_announcement",
        "official_site": "official_announcement",
        "official_source": "official_announcement",
        "announcement": "official_announcement",
        "news": "public_page",
        "media": "public_page",
        "community": "user_provided",
        "independent-report": "public_page",
        "counter-report": "public_page",
        "researcher": "web_search",
        "search": "web_search",
        "tavily": "web_search",
        "bocha": "web_search",
        "user": "user_provided",
        "user-supplied": "user_provided",
        "user-provided": "user_provided",
        "user_upload": "user_provided",
        "user-upload": "user_provided",
        "user_feedback": "user_provided",
    }
    if key in aliases:
        return aliases[key]
    # A fetched hostname/URL, or an unclassified provider label, is still a
    # public page.  The original label remains available for UI/audit use.
    return "public_page"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _beijing(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(BEIJING_TZ)


def _checkpoint_value(value: Any, *, depth: int = 0) -> Any:
    """Bound diagnostic values embedded in a checkpoint.

    Checkpoints must remain useful for recovery diagnostics but cannot retain
    unbounded provider payloads.  The complete payload remains available via
    the event/audit tables and log files.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        if len(value) <= _CHECKPOINT_STRING_LIMIT:
            return value
        return value[:_CHECKPOINT_STRING_LIMIT] + f"...[truncated {len(value) - _CHECKPOINT_STRING_LIMIT} chars]"
    if depth >= 4:
        return str(value)
    if isinstance(value, dict):
        items = list(value.items())[:_CHECKPOINT_DICT_LIMIT]
        result = {str(key): _checkpoint_value(item, depth=depth + 1) for key, item in items}
        if len(value) > _CHECKPOINT_DICT_LIMIT:
            result["_truncated_keys"] = len(value) - _CHECKPOINT_DICT_LIMIT
        return result
    if isinstance(value, (list, tuple)):
        result = [_checkpoint_value(item, depth=depth + 1) for item in value[:_CHECKPOINT_LIST_LIMIT]]
        if len(value) > _CHECKPOINT_LIST_LIMIT:
            result.append(f"...[truncated {len(value) - _CHECKPOINT_LIST_LIMIT} items]")
        return result
    return value


def _checkpoint_document_ref(document: Document) -> dict[str, Any]:
    """Return provenance needed to locate a source, without its full body."""
    metadata = document.metadata if isinstance(document.metadata, dict) else {}
    return {
        "document_id": document.document_id,
        "content_hash": document.content_hash,
        "source": document.source,
        "title": document.title,
        "url": document.url,
        "author": document.author,
        "published_at": document.published_at.isoformat() if document.published_at else None,
        "fetched_at": document.fetched_at.isoformat() if document.fetched_at else None,
        "external_id": metadata.get("external_id"),
    }


def _checkpoint_state(run: DomainRun, *, state_version: int | None = None) -> dict[str, Any]:
    """Build a bounded, resumable checkpoint representation for ``run``."""
    trace = [_checkpoint_value(item) for item in run.trace[-_CHECKPOINT_TRACE_TAIL:]]
    event = {
        "event_id": run.event.event_id,
        "title": run.event.title,
        "objective": run.event.objective,
        "stage": run.event.stage.value,
        "aliases": list(run.event.aliases),
        "entities": list(run.event.entities),
        "risk_score": run.event.risk_score,
        "first_seen_at": run.event.first_seen_at.isoformat() if run.event.first_seen_at else None,
        "last_seen_at": run.event.last_seen_at.isoformat() if run.event.last_seen_at else None,
    }
    return {
        "checkpoint_schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "run_id": run.run_id,
        "state_version": run.state_version if state_version is None else state_version,
        "state_node": run.checkpoint or run.state_node,
        "status": run.status.value,
        "objective": run.objective,
        "investigation_type": run.investigation_type,
        "subject": run.subject,
        "scope": _checkpoint_value(run.scope),
        "event": event,
        "plan_id": run.plan_id,
        "loop_index": run.loop_index,
        "active_tasks": list(run.active_tasks),
        "interrupt_reason": run.interrupt_reason,
        "resume_token_hash": run.resume_token_hash,
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "graph_state": _checkpoint_value(run.graph_state),
        "documents": [_checkpoint_document_ref(document) for document in run.documents],
        "document_count": len(run.documents),
        "trace": trace,
        "trace_count": len(run.trace),
        "trace_seq": run.trace_seq,
    }


class InvestigationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, run: DomainRun, *, expected_state_version: int | None = None) -> DomainRun:
        """Persist a complete run snapshot with CAS and append-only events.

        A stale caller receives ``ConcurrentUpdateError`` rather than silently
        overwriting a newer run.
        """
        event = self.session.get(Event, run.event.event_id)
        now = _beijing(run.updated_at) or beijing_now()
        if event is None:
            event = Event(
                event_id=run.event.event_id,
                title=run.event.title,
                objective=run.event.objective,
                stage=run.event.stage.value,
                risk_score=run.event.risk_score,
                aliases_json=list(run.event.aliases),
                entities_json=list(run.event.entities),
                first_seen_at=_beijing(run.event.first_seen_at),
                last_seen_at=_beijing(run.event.last_seen_at),
                version=1,
                created_at=_beijing(run.event.first_seen_at),
                updated_at=now,
            )
            self.session.add(event)
        else:
            event.title = run.event.title
            event.objective = run.event.objective
            event.stage = run.event.stage.value
            event.risk_score = run.event.risk_score
            event.aliases_json = list(run.event.aliases)
            event.entities_json = list(run.event.entities)
            event.first_seen_at = _beijing(run.event.first_seen_at) or event.first_seen_at
            event.last_seen_at = _beijing(run.event.last_seen_at) or now
            event.version += 1
            event.updated_at = now

        row = self.session.get(InvestigationRun, run.run_id)
        current_version = row.state_version if row else 0
        if expected_state_version is not None and current_version != expected_state_version:
            raise ConcurrentUpdateError(f"run {run.run_id} state_version {current_version} != expected {expected_state_version}")
        next_version = current_version + 1
        state_node = run.checkpoint or run.state_node or (run.trace[-1].get("step") if run.trace else None)
        # The plan may be submitted in a separate transaction. Do not create a
        # dangling foreign key while the task service is still preparing it.
        persisted_plan_id = run.plan_id if run.plan_id and self.session.get(TaskPlan, run.plan_id) is not None else None
        if row is None:
            row = InvestigationRun(
                run_id=run.run_id,
                event_id=run.event.event_id,
                objective=run.objective,
                investigation_type=run.investigation_type,
                template_id=run.scope.get("template_id") if isinstance(run.scope, dict) else None,
                template_version=run.scope.get("template_version") if isinstance(run.scope, dict) else None,
                subject=run.subject,
                scope_json=run.scope,
                report_document_json=run.report_document or {},
                graph_state_json=run.graph_state or {},
                status=run.status.value,
                state_node=state_node,
                state_version=next_version,
                plan_id=persisted_plan_id,
                loop_index=run.loop_index,
                interrupt_reason=run.interrupt_reason or (run.error if run.status in {InvestigationStatus.PAUSED, InvestigationStatus.FAILED, InvestigationStatus.CANCELLED} else None),
                resume_token_hash=run.resume_token_hash,
                budget_json=getattr(run, "budget", {}) or {},
                created_at=_beijing(run.created_at),
                updated_at=now,
            )
            self.session.add(row)
        else:
            # Compare-and-swap at the database boundary.  Reading the version
            # and then assigning it in Python is racy when two workers resume
            # the same run concurrently.
            result = self.session.execute(
                update(InvestigationRun)
                .where(InvestigationRun.run_id == run.run_id, InvestigationRun.state_version == current_version)
                .values(
                    objective=run.objective,
                    investigation_type=run.investigation_type,
                    template_id=run.scope.get("template_id") if isinstance(run.scope, dict) else None,
                    template_version=run.scope.get("template_version") if isinstance(run.scope, dict) else None,
                    subject=run.subject,
                    scope_json=run.scope,
                    report_document_json=run.report_document or {},
                    graph_state_json=run.graph_state or {},
                    status=run.status.value,
                    state_node=state_node,
                    state_version=next_version,
                    plan_id=persisted_plan_id,
                    loop_index=run.loop_index,
                    resume_token_hash=run.resume_token_hash,
                    updated_at=now,
                    interrupt_reason=run.interrupt_reason or (run.error if run.status in {InvestigationStatus.PAUSED, InvestigationStatus.FAILED, InvestigationStatus.CANCELLED} else None),
                )
            )
            if result.rowcount != 1:
                raise ConcurrentUpdateError(f"run {run.run_id} changed while it was being saved")
            # Keep the identity-map instance coherent for any later repository
            # operation in this transaction.
            row.objective = run.objective
            row.investigation_type = run.investigation_type
            row.template_id = run.scope.get("template_id") if isinstance(run.scope, dict) else None
            row.template_version = run.scope.get("template_version") if isinstance(run.scope, dict) else None
            row.subject = run.subject
            row.scope_json = run.scope
            row.report_document_json = run.report_document or {}
            row.graph_state_json = run.graph_state or {}
            row.status = run.status.value
            row.state_node = state_node
            row.state_version = next_version
            row.plan_id = persisted_plan_id
            row.loop_index = run.loop_index
            row.resume_token_hash = run.resume_token_hash
            row.updated_at = now
            row.interrupt_reason = run.interrupt_reason or (run.error if run.status in {InvestigationStatus.PAUSED, InvestigationStatus.FAILED, InvestigationStatus.CANCELLED} else None)

        # Break the event -> run -> event_documents cycle explicitly.  MySQL
        # enforces the foreign key immediately; relying on ORM dependency
        # sorting here can schedule event_documents before its run parent.
        self.session.flush()

        for document in run.documents:
            self._save_document(document)
        # Source rows must exist before MySQL inserts event_documents; this is
        # another immediate-FK boundary that SQLite's default settings hide.
        self.session.flush()
        for document in run.documents:
            link = self.session.get(EventDocument, {"event_id": run.event.event_id, "document_id": document.document_id})
            if link is None:
                self.session.add(EventDocument(event_id=run.event.event_id, document_id=document.document_id, added_by_run_id=run.run_id))


        # Trace entries are immutable facts.  Retrying a save only inserts new seqs.
        existing_rows = self.session.scalars(
            select(RunEvent).where(RunEvent.run_id == run.run_id).order_by(RunEvent.seq)
        ).all()
        existing = {item.seq: item for item in existing_rows}
        # The live trace sink persists agent/provider events immediately with
        # the key ``trace:{run_id}:{in-memory seq}``; match that key before
        # comparing database sequence numbers to keep retries idempotent.
        existing_keys = {
            str((dict(item.payload_json or {}) or {}).get("_idempotency_key"))
            for item in existing_rows
        }
        existing_keys.discard("None")
        next_seq = max(existing, default=0)
        for trace in run.trace:
            seq = int(trace.get("seq", 0))
            idem = f"trace:{run.run_id}:{seq}"
            if idem in existing_keys:
                continue
            row_at_seq = existing.get(seq)
            if row_at_seq is not None:
                row_payload = dict(row_at_seq.payload_json or {})
                if (
                    row_at_seq.event_type == str(trace.get("step", "unknown"))
                    and row_payload.get("status") == trace.get("status")
                ):
                    continue
                # Application services may append task lifecycle events after
                # the Controller last persisted its in-memory trace.  Rebase
                # a colliding runtime event instead of silently dropping it.
                next_seq += 1
                seq = next_seq
                trace["seq"] = seq
            elif seq <= 0:
                next_seq += 1
                seq = next_seq
                trace["seq"] = seq
            else:
                next_seq = max(next_seq, seq)
            payload = {key: value for key, value in trace.items() if key not in {"seq", "step", "status", "at"}}
            if idem:
                payload["_idempotency_key"] = idem
                existing_keys.add(idem)
            event = RunEvent(
                run_event_id=f"re_{uuid4().hex[:12]}", run_id=run.run_id, seq=seq,
                event_type=str(trace.get("step", "unknown")), actor_type="runtime",
                payload_json={"status": trace.get("status"), **payload},
                created_at=_beijing(datetime.fromisoformat(trace["at"])) if trace.get("at") else now,
            )
            self.session.add(event)
            existing[seq] = event
        if state_node:
            self._append_checkpoint(run, next_version)
        # Keep the caller's domain snapshot aligned with the committed CAS
        # version for the next node transition.
        run.state_version = next_version
        run.state_node = state_node
        run.trace_seq = max((int(item.get("seq", 0)) for item in run.trace), default=run.trace_seq)
        return run

    def append_documents(self, run_id: str, documents: list[Document]) -> int:
        """Attach newly discovered immutable sources without replacing run state.

        Supplementary searches happen while node revisions are running.  They
        must be visible to evidence foreign keys, but they must not race the
        run snapshot CAS or rewrite graph state from a stale worker.
        """
        row = self.session.get(InvestigationRun, run_id)
        if row is None:
            raise ValueError(f"investigation not found: {run_id}")
        event = self.session.get(Event, row.event_id)
        if event is None:
            raise ValueError(f"event not found for investigation: {run_id}")
        saved = 0
        for document in documents:
            self._save_document(document)
            saved += 1
        self.session.flush()
        for document in documents:
            link = self.session.get(EventDocument, {"event_id": event.event_id, "document_id": document.document_id})
            if link is None:
                self.session.add(EventDocument(event_id=event.event_id, document_id=document.document_id, added_by_run_id=run_id))
        return saved

    def _save_document(self, document: Document) -> SourceDocument:
        external_id = document.metadata.get("external_id") if isinstance(document.metadata, dict) else None
        if not document.url and not external_id:
            raise ValueError(
                f"document {document.document_id} has no canonical URL or external_id; "
                "MySQL source_documents require a reproducible source identity"
            )
        row = self.session.get(SourceDocument, document.document_id)
        identity = document.url or f"external:{external_id}"
        source_type = _canonical_source_type(document.source)
        metadata = dict(document.metadata)
        if source_type != document.source:
            metadata.setdefault("_source_label", document.source)
        if row is None:
            row = SourceDocument(
                document_id=document.document_id,
                source_type=source_type,
                provider=document.source,
                external_id=external_id,
                canonical_url=document.url,
                canonical_url_hash=_hash(document.url) if document.url else None,
                source_identity_hash=_hash(identity),
                content_hash=document.content_hash,
                title=document.title,
                content=document.content,
                author_name=document.author,
                published_at=_beijing(document.published_at),
                fetched_at=_beijing(document.fetched_at) or beijing_now(),
                raw_metadata_json=metadata,
            )
            self.session.add(row)
        elif (
            row.content_hash != document.content_hash
            or row.canonical_url != document.url
            or row.external_id != external_id
        ):
            raise ValueError(
                f"document {document.document_id} is immutable; create a new version instead of mutating source content"
            )
        # ``provider`` and the user-facing source label are provenance fields,
        # not the immutable content identity.  A run restored from MySQL may
        # expose ``_source_label`` (for example ``www.whu.edu.cn``) while the
        # original row was written with the canonical provider category
        # ``web_search``.  Re-saving that run must not fail merely because the
        # read model preserved a richer label.
        return row

    def _append_checkpoint(self, run: DomainRun, state_version: int) -> str:
        state = _checkpoint_state(run, state_version=state_version)
        checkpoint_id = f"checkpoint_{uuid4().hex[:12]}"
        existing = self.session.scalar(select(RunCheckpoint).where(RunCheckpoint.run_id == run.run_id, RunCheckpoint.state_version == state_version))
        if existing:
            return existing.checkpoint_id
        self.session.add(RunCheckpoint(
            checkpoint_id=checkpoint_id, run_id=run.run_id, state_version=state_version,
            state_node=run.checkpoint or (run.trace[-1].get("step") if run.trace else None), state_json=state, state_hash=_hash(state),
        ))
        return checkpoint_id

    def get(self, run_id: str) -> DomainRun | None:
        row = self.session.get(InvestigationRun, run_id)
        if row is None:
            return None
        event_row = self.session.get(Event, row.event_id)
        if event_row is None:
            return None
        docs = []
        for document_row in self.session.scalars(select(SourceDocument).join(EventDocument, EventDocument.document_id == SourceDocument.document_id).where(EventDocument.event_id == row.event_id)).all():
            docs.append(Document(
                source=(document_row.raw_metadata_json or {}).get("_source_label", document_row.source_type), title=document_row.title, content=document_row.content,
                url=document_row.canonical_url, published_at=_beijing(document_row.published_at),
                fetched_at=_beijing(document_row.fetched_at) or beijing_now(),
                document_id=document_row.document_id, author=document_row.author_name,
                content_hash=document_row.content_hash, metadata=document_row.raw_metadata_json or {},
            ))
        traces = self.events(run_id)
        event = DomainEvent(title=event_row.title, objective=event_row.objective, event_id=event_row.event_id, stage=EventStage(event_row.stage), risk_score=event_row.risk_score, aliases=list(event_row.aliases_json or []), entities=list(event_row.entities_json or []), first_seen_at=_beijing(event_row.first_seen_at) or _beijing(event_row.created_at), last_seen_at=_beijing(event_row.last_seen_at) or _beijing(event_row.updated_at))
        status = InvestigationStatus(row.status) if row.status in {item.value for item in InvestigationStatus} else InvestigationStatus.CREATED
        return DomainRun(
            objective=row.objective,
            event=event,
            investigation_type=row.investigation_type or "auto",
            subject=row.subject,
            scope=dict(row.scope_json or {}),
            run_id=row.run_id,
            status=status,
            documents=docs,
            report_document=dict(row.report_document_json or {}) or None,
            graph_state=dict(row.graph_state_json or {}),
            checkpoint=row.state_node,
            state_node=row.state_node,
            state_version=row.state_version,
            plan_id=row.plan_id,
            loop_index=row.loop_index,
            interrupt_reason=row.interrupt_reason,
            resume_token_hash=row.resume_token_hash,
            error=row.interrupt_reason,
            created_at=_beijing(row.created_at) or beijing_now(),
            updated_at=_beijing(row.updated_at) or beijing_now(),
            trace=traces,
            trace_seq=max((item.get("seq", 0) for item in traces), default=0),
        )

    def list(self, *, limit: int | None = None) -> list[DomainRun]:
        query = select(InvestigationRun).order_by(InvestigationRun.updated_at.desc())
        if limit is not None:
            query = query.limit(limit)
        return [run for row in self.session.scalars(query).all() if (run := self.get(row.run_id)) is not None]

    def list_summaries(self, *, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        query = (
            select(
                InvestigationRun.run_id,
                InvestigationRun.objective,
                InvestigationRun.investigation_type,
                InvestigationRun.subject,
                InvestigationRun.status,
                InvestigationRun.state_node,
                InvestigationRun.created_at,
                InvestigationRun.updated_at,
                Event.event_id,
                Event.title.label("event_title"),
                Event.stage.label("event_stage"),
                Event.risk_score,
            )
            .join(Event, Event.event_id == InvestigationRun.event_id)
            .order_by(InvestigationRun.updated_at.desc(), InvestigationRun.run_id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [
            {
                "run_id": row.run_id,
                "objective": row.objective,
                "investigation_type": row.investigation_type or "auto",
                "subject": row.subject,
                "status": row.status,
                "checkpoint": row.state_node,
                "created_at": _beijing(row.created_at),
                "updated_at": _beijing(row.updated_at),
                "event": {
                    "event_id": row.event_id,
                    "title": row.event_title,
                    "stage": row.event_stage,
                    "risk_score": row.risk_score,
                },
            }
            for row in self.session.execute(query).all()
        ]

    def get_summary(self, run_id: str) -> dict[str, Any] | None:
        query = (
            select(
                InvestigationRun.run_id,
                InvestigationRun.objective,
                InvestigationRun.investigation_type,
                InvestigationRun.subject,
                InvestigationRun.status,
                InvestigationRun.state_node,
                InvestigationRun.created_at,
                InvestigationRun.updated_at,
                InvestigationRun.scope_json,
                InvestigationRun.interrupt_reason,
                InvestigationRun.template_id,
                InvestigationRun.template_version,
                Event.event_id,
                Event.title.label("event_title"),
                Event.stage.label("event_stage"),
                Event.risk_score,
            )
            .join(Event, Event.event_id == InvestigationRun.event_id)
            .where(InvestigationRun.run_id == run_id)
        )
        row = self.session.execute(query).first()
        if row is None:
            return None
        return {
            "run_id": row.run_id,
            "objective": row.objective,
            "investigation_type": row.investigation_type or "auto",
            "subject": row.subject,
            "status": row.status,
            "checkpoint": row.state_node,
            "created_at": _beijing(row.created_at),
            "updated_at": _beijing(row.updated_at),
            "scope": dict(row.scope_json or {}),
            "error": row.interrupt_reason,
            "interrupt_reason": row.interrupt_reason,
            "state_node": row.state_node,
            "template_id": row.template_id,
            "template_version": row.template_version,
            "event": {
                "event_id": row.event_id,
                "title": row.event_title,
                "stage": row.event_stage,
                "risk_score": row.risk_score,
            },
        }

    def graph_state(self, run_id: str) -> dict[str, Any] | None:
        value = self.session.scalar(select(InvestigationRun.graph_state_json).where(InvestigationRun.run_id == run_id))
        return dict(value or {}) if value is not None else None

    def report_document(self, run_id: str) -> dict[str, Any] | None:
        value = self.session.scalar(select(InvestigationRun.report_document_json).where(InvestigationRun.run_id == run_id))
        return dict(value or {}) if value is not None else None

    def events(self, run_id: str, *, after_seq: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        query = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after_seq).order_by(RunEvent.seq)
        if limit is not None:
            query = query.limit(limit)
        rows = self.session.scalars(query).all()
        result = []
        for row in rows:
            payload = dict(row.payload_json or {})
            payload.pop("_idempotency_key", None)
            # MySQL ``DATETIME`` columns are timezone-naive. Re-attach the
            # Asia/Shanghai offset at the API boundary so clients receive the
            # same wall-clock convention used by the database.
            result.append({
                "seq": row.seq,
                "step": row.event_type,
                "status": payload.pop("status", None),
                "at": (_beijing(row.created_at) or beijing_now()).isoformat(),
                **payload,
            })
        return result

    def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        actor_type: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        """Append one sequenced run event inside the caller's transaction."""
        # Always issue the locking SELECT. ``Session.get`` may return an
        # already-loaded identity-map row without issuing SQL, which would
        # skip the lock needed to serialize sequence allocation.
        run = self.session.execute(
            select(InvestigationRun)
            .where(InvestigationRun.run_id == run_id)
            .with_for_update()
        ).scalar_one_or_none()
        if run is None:
            raise KeyError(f"investigation not found: {run_id}")
        # This session factory intentionally disables autoflush. A caller may
        # have just saved runtime trace rows in this transaction, so make them
        # visible before computing MAX(seq), or a second append can reuse the
        # same sequence and violate uq_run_event_seq at commit.
        self.session.flush()
        if idempotency_key:
            existing_events = self.session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == event_type)
            ).all()
            for existing in existing_events:
                if (existing.payload_json or {}).get("_idempotency_key") == idempotency_key:
                    return existing.seq
        latest_seq = self.session.scalar(
            select(RunEvent.seq)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.seq.desc())
            .limit(1)
            .with_for_update()
        ) or 0
        seq = int(latest_seq) + 1
        event_payload = dict(payload or {})
        if idempotency_key:
            event_payload["_idempotency_key"] = idempotency_key
        self.session.add(RunEvent(
            run_event_id=f"re_{uuid4().hex[:12]}",
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            actor_type=actor_type,
            payload_json=event_payload,
        ))
        self.session.flush()
        return seq


class RunCheckpointRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(self, run: DomainRun, *, state: dict[str, Any] | None = None) -> str:
        # Even callers that provide a full domain snapshot must use the same
        # bounded representation; otherwise this alternate write path can
        # reintroduce oversized checkpoints.  Preserve unknown caller fields
        # as bounded diagnostics while keeping the canonical fields above.
        version = self.session.scalar(select(InvestigationRun.state_version).where(InvestigationRun.run_id == run.run_id)) or 0
        existing = self.session.scalar(select(RunCheckpoint).where(RunCheckpoint.run_id == run.run_id, RunCheckpoint.state_version == version))
        if existing:
            return existing.checkpoint_id
        bounded_state = _checkpoint_state(run, state_version=version)
        if state:
            provided_state = _checkpoint_value(state)
            if isinstance(provided_state, dict):
                bounded_state.update({key: value for key, value in provided_state.items() if key not in bounded_state})
        state = bounded_state
        checkpoint_id = f"checkpoint_{uuid4().hex[:12]}"
        self.session.add(RunCheckpoint(checkpoint_id=checkpoint_id, run_id=run.run_id, state_version=version, state_node=run.checkpoint, state_json=state, state_hash=_hash(state)))
        return checkpoint_id

    def latest(self, run_id: str) -> dict[str, Any] | None:
        row = self.session.scalars(select(RunCheckpoint).where(RunCheckpoint.run_id == run_id).order_by(RunCheckpoint.state_version.desc())).first()
        return row.state_json if row else None


class OutboxRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(self, *, aggregate_type: str, aggregate_id: str, event_type: str, payload: dict[str, Any], dedup_key: str, available_at: datetime | None = None) -> str:
        # Flush pending inserts so a second enqueue in the same transaction sees
        # the first dedup row before the database unique constraint is reached.
        self.session.flush()
        existing = self.session.scalar(select(OutboxEvent).where(OutboxEvent.dedup_key == dedup_key))
        if existing:
            return existing.outbox_id
        outbox_id = f"outbox_{uuid4().hex[:12]}"
        self.session.add(OutboxEvent(outbox_id=outbox_id, aggregate_type=aggregate_type, aggregate_id=aggregate_id, event_type=event_type, payload_json=payload, dedup_key=dedup_key, available_at=_beijing(available_at) or beijing_now()))
        return outbox_id

    def claim(self, *, owner: str, limit: int = 50, lease_seconds: int = 60, now: datetime | None = None) -> list[dict[str, Any]]:
        """Claim pending/expired rows in one transaction for a dispatcher.

        SQLite uses conditional row updates; MySQL claims rows with
        ``FOR UPDATE SKIP LOCKED``.
        """
        now = _beijing(now) or beijing_now()
        query = select(OutboxEvent).where(
            OutboxEvent.available_at <= now,
            or_(OutboxEvent.status == "pending", and_(OutboxEvent.status == "publishing", OutboxEvent.locked_until < now)),
        ).order_by(OutboxEvent.created_at).limit(limit)
        if self.session.bind is not None and self.session.bind.dialect.name == "mysql":
            query = query.with_for_update(skip_locked=True)
        rows = self.session.scalars(query).all()
        locked_until = now + timedelta(seconds=lease_seconds)
        result = []
        for row in rows:
            row.status = "publishing"; row.locked_by = owner; row.locked_until = locked_until; row.publish_attempts += 1
            result.append({"outbox_id": row.outbox_id, "aggregate_type": row.aggregate_type, "aggregate_id": row.aggregate_id, "event_type": row.event_type, "payload": row.payload_json, "dedup_key": row.dedup_key, "publish_attempts": row.publish_attempts})
        return result

    def mark_published(self, outbox_id: str, *, owner: str, message_id: str) -> bool:
        row = self.session.get(OutboxEvent, outbox_id)
        if row is None or row.locked_by != owner or row.status != "publishing":
            return False
        row.status = "published"; row.published_message_id = message_id; row.published_at = beijing_now(); row.locked_until = None; row.locked_by = None
        return True

    def mark_failed(self, outbox_id: str, *, owner: str, error: str, dead: bool = False) -> bool:
        row = self.session.get(OutboxEvent, outbox_id)
        if row is None or row.locked_by != owner or row.status != "publishing":
            return False
        row.status = "dead" if dead else "pending"; row.last_error = error; row.locked_until = None; row.locked_by = None
        return True

    def has_unpublished_task(self, task_id: str) -> bool:
        return self.session.scalar(select(OutboxEvent.outbox_id).where(
            OutboxEvent.aggregate_type == "investigation_task",
            OutboxEvent.aggregate_id == task_id,
            OutboxEvent.status.in_(("pending", "publishing")),
        )) is not None


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_plan(
        self, *, run_id: str, objective: str, allowed_agents: list[str],
        allowed_capabilities: list[str], budget: dict[str, Any], stop_conditions: list[str],
        parent_plan_id: str | None = None, activate: bool = True,
        plan_id: str | None = None, plan_version: int | None = None,
        definition: dict[str, Any] | None = None,
    ) -> str:
        run = self.session.get(InvestigationRun, run_id, with_for_update=True)
        if run is None:
            raise KeyError(f"investigation not found: {run_id}")
        payload = {
            "objective": objective, "allowed_agents": sorted(allowed_agents),
            "allowed_capabilities": sorted(allowed_capabilities), "budget": budget,
            "stop_conditions": stop_conditions, "parent_plan_id": parent_plan_id,
        }
        if definition:
            payload["definition"] = definition
        plan_hash = _hash(payload)
        if plan_id is not None:
            existing_by_id = self.session.get(TaskPlan, plan_id)
            if existing_by_id is not None:
                if existing_by_id.run_id != run_id:
                    raise ValueError("task plan id must belong to the same investigation run")
                return existing_by_id.plan_id
        existing = self.session.scalar(select(TaskPlan).where(TaskPlan.run_id == run_id, TaskPlan.plan_hash == plan_hash))
        if existing:
            return existing.plan_id
        latest_version = self.session.scalar(select(TaskPlan.plan_version).where(TaskPlan.run_id == run_id).order_by(TaskPlan.plan_version.desc())) or 0
        if parent_plan_id:
            parent = self.session.get(TaskPlan, parent_plan_id)
            if parent is None or parent.run_id != run_id:
                raise ValueError("parent plan must belong to the same investigation run")
        if activate:
            for active in self.session.scalars(select(TaskPlan).where(TaskPlan.run_id == run_id, TaskPlan.status == "active")).all():
                active.status = "superseded"
        plan_id = plan_id or f"plan_{uuid4().hex[:24]}"
        plan = TaskPlan(
            plan_id=plan_id, run_id=run_id, plan_version=plan_version or latest_version + 1,
            parent_plan_id=parent_plan_id, status="active" if activate else "proposed",
            objective=objective, allowed_agents_json=allowed_agents,
            allowed_capabilities_json=allowed_capabilities, budget_json=budget,
            stop_conditions_json=stop_conditions, plan_hash=plan_hash,
            definition_json=dict(definition or {}),
        )
        self.session.add(plan)
        self.session.flush()
        if activate:
            run.plan_id = plan_id
        return plan_id

    def get_node_plan(self, run_id: str) -> dict[str, Any] | None:
        """Return the newest persisted fixed-template plan for a run."""
        rows = self.session.scalars(
            select(TaskPlan).where(TaskPlan.run_id == run_id).order_by(TaskPlan.plan_version.desc())
        ).all()
        for row in rows:
            definition = dict(row.definition_json or {})
            if {"run_id", "template_id", "template_version", "tasks"}.issubset(definition):
                return definition
        return None

    def add_task(
        self, *, run_id: str, plan_id: str, agent: str, stage: str,
        question: str, capability: str, idempotency_key: str,
        dependencies: list[str] | None = None, priority: int = 0,
        budget: dict[str, Any] | None = None, task_id: str | None = None,
        node_id: str | None = None, slot_id: str | None = None,
        node_revision: int | None = None, review_id: str | None = None,
        input_refs: list[str] | None = None,
        input_data: dict[str, Any] | None = None,
    ) -> str:
        plan = self.session.get(TaskPlan, plan_id)
        if plan is None or plan.run_id != run_id:
            raise ValueError("task plan must belong to the same investigation run")
        existing = self.session.scalar(select(InvestigationTask).where(InvestigationTask.run_id == run_id, InvestigationTask.idempotency_key == idempotency_key))
        if existing:
            if (
                existing.plan_id != plan_id
                or existing.agent != agent
                or existing.stage != stage
                or existing.question != question
                or existing.capability != capability
                or existing.node_id != node_id
                or existing.slot_id != slot_id
                or existing.node_revision != node_revision
                or existing.review_id != review_id
                or existing.input_refs_json != list(input_refs or [])
                or existing.priority != priority
                or existing.budget_json != dict(budget or {})
                or existing.input_json != dict(input_data or {})
            ):
                raise ValueError("task idempotency key is already bound to a different task definition")
            return existing.task_id
        dependencies = list(dependencies or [])
        for dependency_id in dependencies:
            dependency = self.session.get(InvestigationTask, dependency_id)
            if dependency is None or dependency.run_id != run_id:
                raise ValueError("task dependency must belong to the same investigation run")
        task_id = task_id or f"task_{uuid4().hex[:24]}"
        self.session.add(InvestigationTask(
            task_id=task_id, run_id=run_id, plan_id=plan_id, agent=agent,
            node_id=node_id, slot_id=slot_id, node_revision=node_revision, review_id=review_id,
            stage=stage, question=question, capability=capability, status="pending",
            priority=priority, budget_json=budget or {}, idempotency_key=idempotency_key,
            state_version=0, input_refs_json=list(input_refs or []), output_refs_json=[],
            input_json=dict(input_data or {}),
        ))
        self.session.flush()
        for dependency_id in dependencies:
            self.session.add(TaskDependency(task_id=task_id, depends_on_task_id=dependency_id))
        return task_id

    def start_attempt(
        self,
        *,
        task_id: str,
        worker_id: str | None = None,
        expected_state_version: int | None = None,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        """Atomically claim a task and append its next execution attempt.

        The task row is the execution authority.  An attempt is an immutable
        history record after it reaches a terminal state; retries create a new
        ``attempt_no`` instead of overwriting a previous failure.
        """
        task = self.session.get(InvestigationTask, task_id, with_for_update=True)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        run = self.session.scalar(
            select(InvestigationRun)
            .where(InvestigationRun.run_id == task.run_id)
            .with_for_update()
        )
        if run is not None and run.status == "paused":
            error = ValueError("investigation is paused")
            error.not_ready = True
            error.defer_task = True
            error.error_code = "run_paused"
            raise error
        if expected_state_version is not None and task.state_version != expected_state_version:
            raise ConcurrentUpdateError(
                f"task {task_id} state_version {task.state_version} != expected {expected_state_version}"
            )
        if task.status in {"succeeded", "skipped", "cancelled"}:
            raise ValueError(f"cannot start attempt for terminal task: {task.status}")
        dependencies = self.session.scalars(
            select(TaskDependency).where(TaskDependency.task_id == task_id)
        ).all()
        if dependencies:
            dependency_ids = [item.depends_on_task_id for item in dependencies]
            dependency_rows = self.session.scalars(
                select(InvestigationTask).where(InvestigationTask.task_id.in_(dependency_ids))
            ).all()
            unfinished = sorted(
                row.task_id for row in dependency_rows
                if row.status not in {"succeeded", "skipped"}
            )
            if unfinished:
                error = ValueError(
                    f"task {task_id} dependencies are not complete: {', '.join(unfinished)}"
                )
                error.not_ready = True
                raise error
        # A live running task is owned by another worker; do not double-execute it.
        lease_until = _beijing(task.lease_until)
        if task.status == "running" and lease_until and lease_until > beijing_now():
            raise ValueError(f"task {task_id} is already running")

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = beijing_now()
        # Recover an abandoned lease explicitly before creating the retry.  A
        # stale attempt remains in the audit trail as failed rather than being
        # silently replaced by the new worker.
        if task.status == "running" and task.current_attempt_no:
            previous = self.session.scalar(select(TaskAttempt).where(
                TaskAttempt.task_id == task_id,
                TaskAttempt.attempt_no == task.current_attempt_no,
            ).with_for_update())
            if previous is not None and previous.status == "running":
                previous.status = "failed"
                previous.finished_at = now
                previous.heartbeat_at = now
                previous.error_code = "lease_expired"
                previous.error_summary = "task lease expired before completion"
        attempt_no = int(task.current_attempt_no or 0) + 1
        task.current_attempt_no = attempt_no
        task.status = "running"
        task.error_code = None
        task.error_summary = None
        task.state_version = int(task.state_version or 0) + 1
        task.lease_owner = worker_id
        task.lease_until = now + timedelta(seconds=lease_seconds)
        task.updated_at = now
        attempt_id = f"attempt_{uuid4().hex[:24]}"
        self.session.add(TaskAttempt(
            attempt_id=attempt_id,
            task_id=task_id,
            attempt_no=attempt_no,
            worker_id=worker_id,
            status="running",
            started_at=now,
            heartbeat_at=now,
        ))
        self.session.flush()
        return {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "run_id": task.run_id,
            "attempt_no": attempt_no,
            "state_version": task.state_version,
            "status": task.status,
            "worker_id": worker_id,
            "lease_until": task.lease_until.isoformat(),
        }

    def refresh_failed_task_budget(self, task_id: str, *, budget: dict[str, Any]) -> dict[str, Any]:
        """Give an explicitly recovered task a fresh deadline, preserving failure history."""

        task = self.session.get(InvestigationTask, task_id, with_for_update=True)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task.status != "failed":
            raise ValueError(f"cannot recover task that is not failed: {task.status}")
        if task.lease_owner is not None or task.lease_until is not None:
            raise ValueError("cannot recover a failed task with an active lease")
        task.budget_json = dict(budget)
        task.state_version = int(task.state_version or 0) + 1
        task.updated_at = beijing_now()
        return self.get_task(task_id) or {}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.session.get(InvestigationTask, task_id)
        if row is None:
            return None
        return {
            "task_id": row.task_id, "run_id": row.run_id, "plan_id": row.plan_id,
            "node_id": row.node_id, "slot_id": row.slot_id,
            "node_revision": row.node_revision, "review_id": row.review_id,
            "agent": row.agent, "stage": row.stage, "question": row.question,
            "capability": row.capability, "status": row.status,
            "priority": row.priority, "budget": dict(row.budget_json or {}),
            "current_attempt_no": row.current_attempt_no,
            "idempotency_key": row.idempotency_key,
            "state_version": row.state_version,
            "lease_owner": row.lease_owner,
            "lease_until": _beijing(row.lease_until).isoformat() if row.lease_until else None,
            "input_refs": list(row.input_refs_json or []),
            "input": dict(row.input_json or {}),
            "output_refs": list(row.output_refs_json or []),
        }

    def list_report_tasks(self, run_id: str) -> list[dict[str, Any]]:
        plans = self.session.scalars(
            select(TaskPlan).where(TaskPlan.run_id == run_id).order_by(TaskPlan.plan_version.desc())
        ).all()
        report_plan = next((item for item in plans if (item.budget_json or {}).get("phase") == "report"), None)
        if report_plan is None:
            return []
        rows = self.session.scalars(
            select(InvestigationTask)
            .where(
                InvestigationTask.run_id == run_id,
                InvestigationTask.plan_id == report_plan.plan_id,
                InvestigationTask.stage.in_(("report_plan", "report_section")),
            )
            .order_by(InvestigationTask.priority, InvestigationTask.created_at)
        ).all()
        return [
            {"task_id": row.task_id, "stage": row.stage, "slot_id": row.slot_id, "status": row.status}
            for row in rows
        ]

    def recover_expired(self, *, limit: int = 100, now: datetime | None = None) -> list[dict[str, Any]]:
        """Move expired running tasks back to pending without losing attempts."""
        now = _beijing(now) or beijing_now()
        query = select(InvestigationTask).where(
            InvestigationTask.status == "running",
            InvestigationTask.lease_until.is_not(None),
            InvestigationTask.lease_until < now,
        ).order_by(InvestigationTask.updated_at).limit(limit)
        if self.session.bind is not None and self.session.bind.dialect.name == "mysql":
            query = query.with_for_update(skip_locked=True)
        recovered: list[dict[str, Any]] = []
        for task in self.session.scalars(query).all():
            previous = self.session.scalar(select(TaskAttempt).where(
                TaskAttempt.task_id == task.task_id,
                TaskAttempt.attempt_no == task.current_attempt_no,
            ).with_for_update())
            if previous is not None and previous.status == "running":
                previous.status = "failed"
                previous.finished_at = now
                previous.heartbeat_at = now
                previous.error_code = "lease_expired"
                previous.error_summary = "task lease expired during worker failure"
            task.status = "pending"
            task.error_code = "lease_expired"
            task.error_summary = "task lease expired during worker failure"
            task.lease_owner = None
            task.lease_until = None
            task.state_version = int(task.state_version or 0) + 1
            task.updated_at = now
            recovered.append({
                "task_id": task.task_id, "run_id": task.run_id, "agent": task.agent,
                "capability": task.capability, "idempotency_key": task.idempotency_key,
                "task_attempt_no": int(task.current_attempt_no or 0),
                "state_version": int(task.state_version or 0),
            })
        return recovered

    def list_pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.session.scalars(select(InvestigationTask).where(
            InvestigationTask.status == "pending",
        ).order_by(InvestigationTask.priority, InvestigationTask.updated_at).limit(limit)).all()
        return [{
            "task_id": row.task_id, "run_id": row.run_id, "agent": row.agent,
            "capability": row.capability, "idempotency_key": row.idempotency_key,
            "task_attempt_no": int(row.current_attempt_no or 0),
            "state_version": int(row.state_version or 0),
        } for row in rows]

    def heartbeat_attempt(self, *, attempt_id: str, worker_id: str | None = None, lease_seconds: int = 60) -> bool:
        """Refresh the attempt heartbeat and its authoritative task lease."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        attempt = self.session.get(TaskAttempt, attempt_id, with_for_update=True)
        if attempt is None or attempt.status != "running":
            return False
        if worker_id is not None and attempt.worker_id not in {None, worker_id}:
            return False
        task = self.session.get(InvestigationTask, attempt.task_id, with_for_update=True)
        if task is None or task.status != "running":
            return False
        if worker_id is not None and task.lease_owner not in {None, worker_id}:
            return False
        now = beijing_now()
        attempt.heartbeat_at = now
        task.lease_until = now + timedelta(seconds=lease_seconds)
        task.updated_at = now
        return True

    def finish_attempt(
        self,
        *,
        attempt_id: str,
        status: str,
        error_code: str | None = None,
        error_summary: str | None = None,
        output_refs: list[str] | None = None,
        worker_id: str | None = None,
    ) -> bool:
        """Complete one running attempt and project status onto its task."""
        allowed = {"succeeded", "failed", "skipped", "cancelled"}
        if status not in allowed:
            raise ValueError(f"invalid terminal attempt status: {status}")
        attempt = self.session.get(TaskAttempt, attempt_id, with_for_update=True)
        if attempt is None or attempt.status != "running":
            return False
        task = self.session.get(InvestigationTask, attempt.task_id, with_for_update=True)
        if task is None:
            raise KeyError(f"task not found for attempt: {attempt_id}")
        if worker_id is not None and task.lease_owner not in {None, worker_id}:
            return False
        now = beijing_now()
        attempt.status = status
        attempt.finished_at = now
        attempt.heartbeat_at = now
        attempt.error_code = error_code
        attempt.error_summary = error_summary
        task.status = status
        task.error_code = error_code
        task.error_summary = error_summary
        task.output_refs_json = list(output_refs or [])
        task.lease_owner = None
        task.lease_until = None
        task.state_version = int(task.state_version or 0) + 1
        task.updated_at = now
        return True

    def defer_attempt(
        self,
        *,
        attempt_id: str,
        worker_id: str | None = None,
        error_code: str = "run_paused",
        error_summary: str | None = None,
    ) -> bool:
        """End the current lease while keeping the durable task resumable."""

        attempt = self.session.get(TaskAttempt, attempt_id, with_for_update=True)
        if attempt is None or attempt.status != "running":
            return False
        task = self.session.get(InvestigationTask, attempt.task_id, with_for_update=True)
        if task is None:
            raise KeyError(f"task not found for attempt: {attempt_id}")
        if worker_id is not None and task.lease_owner not in {None, worker_id}:
            return False
        now = beijing_now()
        summary = " ".join(str(error_summary or "investigation paused").split())[:500]
        attempt.status = "cancelled"
        attempt.finished_at = now
        attempt.heartbeat_at = now
        attempt.error_code = error_code
        attempt.error_summary = summary
        task.status = "pending"
        task.error_code = error_code
        task.error_summary = summary
        task.lease_owner = None
        task.lease_until = None
        task.state_version = int(task.state_version or 0) + 1
        task.updated_at = now
        return True

    def get_attempts(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(TaskAttempt).where(TaskAttempt.task_id == task_id).order_by(TaskAttempt.attempt_no)
        ).all()
        return [
            {
                "attempt_id": row.attempt_id,
                "task_id": row.task_id,
                "run_id": self.session.get(InvestigationTask, row.task_id).run_id,
                "attempt_no": row.attempt_no,
                "worker_id": row.worker_id,
                "status": row.status,
                "started_at": _beijing(row.started_at).isoformat() if row.started_at else None,
                "heartbeat_at": _beijing(row.heartbeat_at).isoformat() if row.heartbeat_at else None,
                "finished_at": _beijing(row.finished_at).isoformat() if row.finished_at else None,
                "error_code": row.error_code,
                "error_summary": row.error_summary,
            }
            for row in rows
        ]

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self.session.get(TaskAttempt, attempt_id)
        if row is None:
            return None
        task = self.session.get(InvestigationTask, row.task_id)
        return {
            "attempt_id": row.attempt_id,
            "task_id": row.task_id,
            "run_id": task.run_id if task is not None else None,
            "attempt_no": row.attempt_no,
            "worker_id": row.worker_id,
            "status": row.status,
            "started_at": _beijing(row.started_at).isoformat() if row.started_at else None,
            "heartbeat_at": _beijing(row.heartbeat_at).isoformat() if row.heartbeat_at else None,
            "finished_at": _beijing(row.finished_at).isoformat() if row.finished_at else None,
            "error_code": row.error_code,
            "error_summary": row.error_summary,
        }


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def register_skill(self, *, skill_name: str, description: str, content_hash: str, package_path: str) -> str:
        existing = self.session.scalar(select(AgentSkillDefinition).where(AgentSkillDefinition.skill_name == skill_name, AgentSkillDefinition.content_hash == content_hash))
        if existing:
            return existing.skill_definition_id
        definition_id = f"skilldef_{uuid4().hex[:20]}"
        self.session.add(AgentSkillDefinition(
            skill_definition_id=definition_id, skill_name=skill_name,
            description=description, content_hash=content_hash, package_path=package_path,
        ))
        return definition_id

    def activate_skill(self, *, run_id: str, task_id: str, agent: str, skill_definition_id: str, step_no: int, reason_summary: str) -> str:
        existing = self.session.scalar(select(AgentSkillActivation).where(
            AgentSkillActivation.run_id == run_id, AgentSkillActivation.task_id == task_id,
            AgentSkillActivation.agent == agent, AgentSkillActivation.step_no == step_no,
        ))
        if existing:
            return existing.activation_id
        activation_id = f"activation_{uuid4().hex[:20]}"
        self.session.add(AgentSkillActivation(
            activation_id=activation_id, run_id=run_id, task_id=task_id, agent=agent,
            skill_definition_id=skill_definition_id, step_no=step_no, reason_summary=reason_summary,
        ))
        return activation_id

    def register_provider(self, *, provider_name: str, provider_version: str, manifest_hash: str, capabilities: list[str]) -> str:
        same_version = self.session.scalars(select(ToolProviderDefinition).where(
            ToolProviderDefinition.provider_name == provider_name,
            ToolProviderDefinition.provider_version == provider_version,
        )).all()
        for definition in same_version:
            if definition.manifest_hash != manifest_hash:
                raise ValueError(f"provider {provider_name}@{provider_version} changed manifest hash without a version bump")
            return definition.provider_definition_id
        definition_id = f"providerdef_{uuid4().hex[:16]}"
        self.session.add(ToolProviderDefinition(
            provider_definition_id=definition_id, provider_name=provider_name,
            provider_version=provider_version, manifest_hash=manifest_hash,
            capabilities_json=capabilities,
        ))
        return definition_id

    def record_tool_call(
        self, *, tool_call_id: str, run_id: str, task_id: str, agent: str,
        capability: str, provider_definition_id: str, attempt_group_id: str,
        provider_attempt_no: int, idempotency_key: str, status: str,
        input_hash: str, output_summary: dict[str, Any] | None = None,
        duration_ms: int | None = None, token_usage: dict[str, Any] | None = None,
        cost: dict[str, Any] | None = None, error_code: str | None = None,
        error_summary: str | None = None,
    ) -> str:
        existing = self.session.scalar(select(ToolCall).where(
            ToolCall.task_id == task_id,
            ToolCall.idempotency_key == idempotency_key,
            ToolCall.provider_attempt_no == provider_attempt_no,
        ))
        if existing:
            return existing.tool_call_id
        existing = self.session.get(ToolCall, tool_call_id)
        if existing:
            return existing.tool_call_id
        self.session.add(ToolCall(
            tool_call_id=tool_call_id, run_id=run_id, task_id=task_id, agent=agent,
            capability=capability, provider_definition_id=provider_definition_id,
            attempt_group_id=attempt_group_id, provider_attempt_no=provider_attempt_no,
            idempotency_key=idempotency_key, status=status, input_hash=input_hash,
            output_summary_json=output_summary or {}, duration_ms=duration_ms,
            token_usage_json=token_usage or {}, cost_json=cost or {},
            error_code=error_code, error_summary=error_summary,
            finished_at=beijing_now() if status != "started" else None,
        ))
        return tool_call_id


class TraceNodeRepository:
    """Persist the node-driven investigation working set.

    Research drafts and reviews are append-only. A frozen revision's derived
    conclusion IR can be refreshed without changing its research content.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _template_key(template: TraceTemplate) -> str:
        return f"{template.template_id}@{template.template_version}"

    def register_template(self, template: TraceTemplate) -> str:
        key = self._template_key(template)
        existing = self.session.get(TraceTemplateRow, key)
        definition = template.to_dict()
        if existing is not None:
            if existing.definition_json != definition:
                raise ValueError(f"template key already contains a different definition: {key}")
            return key
        self.session.add(TraceTemplateRow(
            template_key=key,
            template_id=template.template_id,
            template_version=template.template_version,
            investigation_type=template.investigation_type,
            display_name=template.display_name,
            purpose=template.purpose,
            language=template.language,
            max_node_revisions=template.max_node_revisions,
            definition_json=definition,
        ))
        # The template children carry a physical foreign key.  SQLAlchemy
        # cannot infer this dependency from scalar FK fields alone, so make
        # the parent visible before inserting nodes and edges (required by
        # MySQL's immediate FK checks).
        self.session.flush()
        for node in template.node_specs:
            self.session.add(TraceTemplateNodeRow(
                template_key=key,
                slot_id=node.slot_id,
                node_order=node.order,
                title=node.title,
                goal=node.goal,
                aspects_json=list(node.aspects),
                claim_types_json=list(node.claim_types),
                preferred_sources_json=list(node.preferred_sources),
                research_questions_json=list(node.research_questions),
                review_rubric_json={axis: list(items) for axis, items in node.review_rubric.items()},
                depends_on_slot_ids_json=list(node.depends_on_slot_ids),
                allow_multiple_instances=node.allow_multiple_instances,
                media_policy_json=node.media_policy.to_dict(),
            ))
        for edge_order, edge in enumerate(template.edge_specs):
            self.session.add(TraceTemplateEdgeRow(
                template_key=key,
                edge_order=edge_order,
                from_slot_id=edge.from_slot_id,
                to_slot_id=edge.to_slot_id,
                relation=edge.relation.value,
                label_goal=edge.label_goal,
                requires_explicit_support=edge.requires_explicit_support,
            ))
        self.session.flush()
        return key

    def initialize_nodes(self, *, run_id: str, template: TraceTemplate) -> list[dict[str, Any]]:
        run = self.session.get(InvestigationRun, run_id)
        if run is None:
            raise KeyError(f"investigation not found: {run_id}")
        template_key = self.register_template(template)
        run.template_id = template.template_id
        run.template_version = template.template_version
        existing = {
            row.slot_id: row
            for row in self.session.scalars(select(InvestigationNode).where(InvestigationNode.run_id == run_id)).all()
        }
        result: list[dict[str, Any]] = []
        for spec in template.node_specs:
            row = existing.get(spec.slot_id)
            if row is None:
                row = InvestigationNode(
                    node_id=f"node_{uuid4().hex[:24]}",
                    run_id=run_id,
                    template_id=template.template_id,
                    template_version=template.template_version,
                    slot_id=spec.slot_id,
                    node_order=spec.order,
                    instance_no=1,
                    status="pending",
                    current_revision=0,
                )
                self.session.add(row)
                self.session.flush()
            elif row.template_id != template.template_id or row.template_version != template.template_version:
                raise ValueError(f"run already uses another template for slot: {spec.slot_id}")
            result.append(self._node_dict(row, template_key=template_key))
        return result

    @staticmethod
    def _node_dict(row: InvestigationNode, *, template_key: str | None = None) -> dict[str, Any]:
        return {
            "node_id": row.node_id,
            "run_id": row.run_id,
            "template_id": row.template_id,
            "template_version": row.template_version,
            "template_key": template_key or f"{row.template_id}@{row.template_version}",
            "slot_id": row.slot_id,
            "node_order": row.node_order,
            "instance_no": row.instance_no,
            "status": row.status,
            "current_revision": row.current_revision,
            "frozen_revision": row.frozen_revision,
            "current_review_id": row.current_review_id,
        }

    def list_nodes(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.session.scalars(select(InvestigationNode).where(InvestigationNode.run_id == run_id).order_by(InvestigationNode.node_order, InvestigationNode.instance_no)).all()
        return [self._node_dict(row) for row in rows]

    def begin_research(self, node_id: str, revision_no: int) -> bool:
        node = self.session.get(InvestigationNode, node_id, with_for_update=True)
        if node is None:
            raise KeyError(f"node not found: {node_id}")
        existing = self.session.scalar(select(NodeRevisionRow.revision_id).where(
            NodeRevisionRow.node_id == node_id,
            NodeRevisionRow.revision_no == revision_no,
        ))
        if existing is not None or node.status == "frozen":
            return False
        if node.status != "researching":
            node.status = "researching"
            node.updated_at = beijing_now()
            self.session.flush()
            return True
        return False

    def save_revision(self, revision: DomainNodeRevision) -> str:
        node = self.session.get(InvestigationNode, revision.node_id, with_for_update=True)
        if node is None or node.slot_id != revision.slot_id:
            raise ValueError("node revision does not reference a known node slot")
        if node.status == "frozen":
            raise ValueError("frozen node revisions are immutable")
        existing = self.session.scalar(select(NodeRevisionRow).where(
            NodeRevisionRow.node_id == revision.node_id,
            NodeRevisionRow.revision_no == revision.revision,
        ))
        if existing is not None:
            if existing.draft_json != revision.draft.to_dict():
                raise ValueError("node revision is immutable and already contains another draft")
            if revision.review is not None:
                saved_review = self.get_review(revision.node_id, revision.revision)
                if saved_review is None or saved_review["review"] != revision.review.to_dict():
                    raise ValueError("node revision is immutable and already contains another review")
            return existing.revision_id
        has_initial = self.session.scalar(select(NodeRevisionRow.revision_id).where(
            NodeRevisionRow.node_id == revision.node_id,
            NodeRevisionRow.revision_no == 0,
        )) is not None
        expected_revision = node.current_revision + 1 if has_initial else 0
        if revision.revision != expected_revision:
            raise ValueError("node revision must be the current next revision")
        revision_id = f"nrev_{uuid4().hex[:24]}"
        self.session.add(NodeRevisionRow(
            revision_id=revision_id,
            node_id=revision.node_id,
            revision_no=revision.revision,
            conclusion=revision.draft.conclusion,
            draft_json=revision.draft.to_dict(),
        ))
        self.session.flush()
        claim_ids = {claim.claim_id for claim in revision.draft.claims}
        evidence_ids = {evidence.evidence_id for evidence in revision.draft.evidence}
        for claim in revision.draft.claims:
            self.session.add(NodeClaimRow(
                claim_id=claim.claim_id,
                revision_id=revision_id,
                claim_type=claim.claim_type,
                text=claim.text,
                subject=claim.subject,
                predicate=claim.predicate,
                object_value=claim.object_value,
                time_scope=claim.time_scope,
                source_document_ids_json=list(claim.source_document_ids),
            ))
        for evidence in revision.draft.evidence:
            if evidence.claim_id not in claim_ids or evidence.evidence_id not in evidence_ids:
                raise ValueError("node evidence must belong to the revision claims")
            self.session.add(NodeEvidenceRow(
                evidence_id=evidence.evidence_id,
                revision_id=revision_id,
                claim_id=evidence.claim_id,
                document_id=evidence.document_id,
                excerpt=evidence.excerpt,
                locator_json=evidence.locator,
                relation=evidence.relation.value,
            ))
        for media in revision.draft.media:
            self.session.add(NodeMediaRow(
                media_id=media.media_id,
                revision_id=revision_id,
                document_id=media.document_id,
                media_type=media.media_type.value,
                source_url=media.source_url or None,
                object_key=media.object_key or None,
                caption=media.caption,
                why_relevant=media.why_relevant,
                published_at=_beijing(media.published_at),
                captured_at=_beijing(media.captured_at),
            ))
        review_id = None
        if revision.review is not None:
            review = revision.review
            review_id = self.save_review(review)
        node.current_revision = revision.revision
        node.current_review_id = review_id
        node.status = "reviewing" if revision.revision == 0 else ("revise" if revision.review and revision.review.status.value == "revise" else "reviewing")
        node.updated_at = beijing_now()
        self.session.flush()
        return revision_id

    def save_review(self, review: NodeReview) -> str:
        """Persist one immutable editorial review before its revision is written.

        Reviews are a recovery checkpoint: if the targeted research call fails,
        the controller can reload this record and retry the same revision
        without invoking the verifier again.
        """
        node = self.session.get(InvestigationNode, review.node_id, with_for_update=True)
        if node is None or node.slot_id != review.slot_id:
            raise ValueError("node review does not reference a known node slot")
        if node.status == "frozen":
            raise ValueError("frozen node reviews are immutable")
        existing = self.session.scalar(select(NodeReviewRow).where(
            NodeReviewRow.node_id == review.node_id,
            NodeReviewRow.revision_no == review.revision,
        ))
        payload = review.to_dict()
        payload.pop("slot_id", None)  # slot identity is owned by investigation_nodes
        if existing is not None:
            if self._review_dict(existing) != payload:
                raise ValueError("node review is immutable and already contains another review")
            return existing.review_id
        has_initial = self.session.scalar(select(NodeRevisionRow.revision_id).where(
            NodeRevisionRow.node_id == review.node_id,
            NodeRevisionRow.revision_no == 0,
        )) is not None
        if not has_initial or node.current_revision != review.revision - 1:
            raise ValueError("node review must target the next revision after the current draft")
        self.session.add(NodeReviewRow(
            review_id=review.review_id,
            node_id=review.node_id,
            revision_no=review.revision,
            status=review.status.value,
            covered_aspects_json=list(review.covered_aspects),
            missing_aspects_json=list(review.missing_aspects),
            redundant_claims_json=list(review.redundant_claims),
            unclear_claims_json=list(review.unclear_claims),
            evidence_followups_json=list(review.evidence_followups),
            rewrite_instructions_json=list(review.rewrite_instructions),
            next_research_tasks_json=list(review.next_research_tasks),
            final_editor_note=review.final_editor_note,
        ))
        self.session.flush()
        return review.review_id

    @staticmethod
    def _review_dict(row: NodeReviewRow) -> dict[str, Any]:
        return {
            "review_id": row.review_id,
            "node_id": row.node_id,
            "revision": row.revision_no,
            "status": row.status,
            "covered_aspects": list(row.covered_aspects_json or []),
            "missing_aspects": list(row.missing_aspects_json or []),
            "redundant_claims": list(row.redundant_claims_json or []),
            "unclear_claims": list(row.unclear_claims_json or []),
            "evidence_followups": list(row.evidence_followups_json or []),
            "rewrite_instructions": list(row.rewrite_instructions_json or []),
            "next_research_tasks": list(row.next_research_tasks_json or []),
            "final_editor_note": row.final_editor_note,
        }

    def get_review(self, node_id: str, revision_no: int | None = None) -> dict[str, Any] | None:
        query = select(NodeReviewRow).where(NodeReviewRow.node_id == node_id)
        if revision_no is not None:
            query = query.where(NodeReviewRow.revision_no == revision_no)
        else:
            query = query.order_by(NodeReviewRow.revision_no.desc())
        row = self.session.scalar(query)
        if row is None:
            return None
        value = self._review_dict(row)
        node = self.session.get(InvestigationNode, row.node_id)
        if node is not None:
            value["slot_id"] = node.slot_id
        return {"review": value}

    def freeze_node(self, node_id: str) -> dict[str, Any]:
        node = self.session.get(InvestigationNode, node_id, with_for_update=True)
        if node is None:
            raise KeyError(f"node not found: {node_id}")
        if node.status == "frozen":
            return self._node_dict(node)
        if node.current_revision != 3:
            raise ValueError("node can only freeze after the third revision")
        review = self.session.scalar(select(NodeReviewRow).where(
            NodeReviewRow.node_id == node_id,
            NodeReviewRow.revision_no == 3,
        ))
        if review is None or review.status != "finalize":
            raise ValueError("node requires a final third-round review before freeze")
        node.status = "frozen"
        node.frozen_revision = 3
        node.updated_at = beijing_now()
        self.session.flush()
        return self._node_dict(node)

    def update_frozen_conclusion_ir(self, node_id: str, revision_no: int, conclusion_ir: ConclusionIR) -> str:
        node = self.session.scalar(
            select(InvestigationNode)
            .where(InvestigationNode.node_id == node_id)
            .with_for_update()
        )
        if node is None:
            raise KeyError(f"node not found: {node_id}")
        if node.status != "frozen" or node.frozen_revision != revision_no:
            raise ValueError("only the currently frozen revision may refresh its derived conclusion IR")
        revision = self.session.scalar(
            select(NodeRevisionRow)
            .where(NodeRevisionRow.node_id == node_id, NodeRevisionRow.revision_no == revision_no)
            .with_for_update()
        )
        if revision is None:
            raise KeyError(f"node revision not found: {node_id}:{revision_no}")
        ir = conclusion_ir if isinstance(conclusion_ir, ConclusionIR) else ConclusionIR.from_dict(conclusion_ir)
        expected_hash = hashlib.sha256(revision.conclusion.encode("utf-8")).hexdigest()
        if ir.source_hash != expected_hash:
            raise ValueError("refreshed conclusion IR must reference the frozen conclusion")
        draft = dict(revision.draft_json or {})
        if str(draft.get("conclusion") or "") != revision.conclusion:
            raise ValueError("frozen draft conclusion does not match its revision")
        draft["conclusion_ir"] = ir.to_dict()
        revision.draft_json = draft
        self.session.flush()
        return revision.revision_id

    def get_revision(self, node_id: str, revision_no: int | None = None) -> dict[str, Any] | None:
        query = select(NodeRevisionRow).where(NodeRevisionRow.node_id == node_id)
        if revision_no is not None:
            query = query.where(NodeRevisionRow.revision_no == revision_no)
        else:
            query = query.order_by(NodeRevisionRow.revision_no.desc())
        row = self.session.scalar(query)
        if row is None:
            return None
        return {
            "revision_id": row.revision_id,
            "node_id": row.node_id,
            "revision": row.revision_no,
            "conclusion": row.conclusion,
            "draft": dict(row.draft_json or {}),
            "review": (self.get_review(row.node_id, row.revision_no) or {}).get("review"),
            "created_at": _beijing(row.created_at).isoformat() if row.created_at else None,
        }


class AnalysisFeatureRepository:
    """Append-only, idempotent storage for model/rule-derived features."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, feature: dict[str, Any], *, document_id: str | None = None) -> dict[str, Any]:
        document_id = str(document_id or feature.get("document_id") or "").strip()
        if not document_id:
            raise ValueError("analysis feature requires document_id")
        value = dict(feature.get("value") or {})
        feature_type = str(feature.get("feature_type") or "").strip()
        if not feature_type:
            raise ValueError("analysis feature requires feature_type")
        input_hash = str(feature.get("input_hash") or _hash(value))
        scope_hash = str(feature.get("scope_hash") or _hash({"document_id": document_id, "feature_type": feature_type}))
        model_name = feature.get("model_name")
        model_version = feature.get("model_version")
        prompt_version = feature.get("prompt_version")
        existing = self.session.scalar(select(AnalysisFeature).where(
            AnalysisFeature.feature_type == feature_type,
            AnalysisFeature.scope_hash == scope_hash,
            AnalysisFeature.model_name == model_name,
            AnalysisFeature.model_version == model_version,
            AnalysisFeature.prompt_version == prompt_version,
            AnalysisFeature.input_hash == input_hash,
        ))
        if existing is None:
            existing = AnalysisFeature(
                feature_id=f"feature_{uuid4().hex[:24]}",
                document_id=document_id,
                feature_type=feature_type,
                scope_hash=scope_hash,
                value_json=value,
                model_name=model_name,
                model_version=model_version,
                prompt_version=prompt_version,
                input_hash=input_hash,
            )
            self.session.add(existing)
            self.session.flush()
        return {
            "feature_id": existing.feature_id,
            "document_id": existing.document_id,
            "feature_type": existing.feature_type,
            "scope_hash": existing.scope_hash,
            "value": dict(existing.value_json or {}),
            "model_name": existing.model_name,
            "model_version": existing.model_version,
            "prompt_version": existing.prompt_version,
            "input_hash": existing.input_hash,
        }

    def list(self, document_id: str, *, feature_type: str | None = None) -> list[dict[str, Any]]:
        query = select(AnalysisFeature).where(AnalysisFeature.document_id == document_id).order_by(AnalysisFeature.created_at, AnalysisFeature.feature_id)
        if feature_type:
            query = query.where(AnalysisFeature.feature_type == feature_type)
        return [{
            "feature_id": row.feature_id, "document_id": row.document_id,
            "feature_type": row.feature_type, "scope_hash": row.scope_hash,
            "value": dict(row.value_json or {}), "model_name": row.model_name,
            "model_version": row.model_version, "prompt_version": row.prompt_version,
            "input_hash": row.input_hash,
        } for row in self.session.scalars(query).all()]

    def list_for_documents(self, document_ids: list[str]) -> list[dict[str, Any]]:
        ids = [str(item).strip() for item in document_ids if str(item).strip()]
        if not ids:
            return []
        query = select(AnalysisFeature).where(
            AnalysisFeature.document_id.in_(ids)
        ).order_by(AnalysisFeature.created_at, AnalysisFeature.feature_id)
        return [
            {
                "feature_id": row.feature_id,
                "document_id": row.document_id,
                "feature_type": row.feature_type,
                "scope_hash": row.scope_hash,
                "value": dict(row.value_json or {}),
                "model_name": row.model_name,
                "model_version": row.model_version,
                "prompt_version": row.prompt_version,
                "input_hash": row.input_hash,
            }
            for row in self.session.scalars(query).all()
        ]


class SqlAlchemyUnitOfWork:
    """Transaction boundary used by application services."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.session: Session | None = None

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        self.session = self.session_factory()
        self.runs = InvestigationRepository(self.session)
        self.checkpoints = RunCheckpointRepository(self.session)
        self.outbox = OutboxRepository(self.session)
        self.tasks = TaskRepository(self.session)
        self.trace_nodes = TraceNodeRepository(self.session)
        self.audit = AuditRepository(self.session)
        self.analysis_features = AnalysisFeatureRepository(self.session)
        return self

    def commit(self) -> None:
        if self.session is None:
            raise RuntimeError("unit of work is not active")
        self.session.commit()

    def rollback(self) -> None:
        if self.session is not None:
            self.session.rollback()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            if self.session is not None:
                self.session.close()
            self.session = None


__all__ = ["AnalysisFeatureRepository", "AuditRepository", "ConcurrentUpdateError", "InvestigationRepository", "OutboxRepository", "RunCheckpointRepository", "SqlAlchemyUnitOfWork", "TaskRepository", "TraceNodeRepository"]
