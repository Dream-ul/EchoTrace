"""Investigation API for the node-driven workflow."""

from __future__ import annotations

from threading import RLock
from typing import Any
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.investigation_composition import InvestigationRuntime, build_investigation_runtime
from echotrace.application.report_renderers import (
    render_report_html,
    render_report_markdown,
    render_report_pdf,
)
from echotrace.domain.models import Document, Event, InvestigationRun, InvestigationStatus
from echotrace.domain.trace_nodes import TraceContractError, get_trace_template
from echotrace.runtime.projections import InvestigationProjection
from echotrace.domain.reporting import ReportDocument
from echotrace.application.node_trace import refresh_frozen_trace

from .investigation_contracts import (
    EventPageResponse,
    InvestigationDetail,
    InvestigationListResponse,
    InvestigationResponse,
    NodeListResponse,
    ReportProgressResponse,
    ReportResponse,
    TraceResponse,
    WorkbenchResponse,
)


router = APIRouter(prefix="/api/investigations", tags=["investigations"])
_runtime: InvestigationRuntime | None = None
_runtime_lock = RLock()
_MAX_PAGE_SIZE = 100
_MAX_EVENT_PAGE_SIZE = 500


def get_investigation_runtime() -> InvestigationRuntime:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = build_investigation_runtime()
    return _runtime


def close_investigation_runtime() -> None:
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            _runtime.close()
            _runtime = None


def reload_investigation_runtime() -> None:
    global _runtime
    with _runtime_lock:
        previous = _runtime
        _runtime = None
        if previous is not None:
            previous.close()
        _runtime = build_investigation_runtime()


def _start_run(run_id: str, runtime: InvestigationRuntime, *, resume: bool = False) -> InvestigationRun:
    run = runtime.run_store.get(run_id)
    if run is None:
        raise KeyError(f"investigation not found: {run_id}")
    if run.status == InvestigationStatus.CANCELLED:
        raise ValueError("investigation is cancelled and cannot be resumed")
    if run.status != InvestigationStatus.RUNNING:
        run.status = InvestigationStatus.RUNNING
        run.error = None
        run.interrupt_reason = None
        run.add_trace("resume" if resume else "run", "queued")
        runtime.run_store.save(run)
        runtime.enqueue_run(run_id, idempotency_key=f"{run_id}:start:{run.state_version}")
    return run


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    url: str | None = None
    published_at: datetime | None = None
    author: str | None = None


class InvestigationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1)
    event_title: str | None = Field(default=None, min_length=1)
    investigation_type: str = Field(default="auto", min_length=1)
    subject: str | None = None
    scope: dict[str, object] = Field(default_factory=dict)
    documents: list[DocumentInput] = Field(default_factory=list)


def _documents(items: list[DocumentInput]) -> list[Document]:
    return [Document(**item.model_dump()) for item in items]


def _run_title(payload: InvestigationInput) -> str:
    return (payload.event_title or payload.subject or payload.objective).strip()[:256]


def _summary(run: InvestigationRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "objective": run.objective,
        "investigation_type": run.investigation_type,
        "subject": run.subject,
        "status": run.status.value,
        "checkpoint": run.checkpoint,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "event": {
            "event_id": run.event.event_id,
            "title": run.event.title,
            "stage": run.event.stage.value,
            "risk_score": run.event.risk_score,
        },
    }


def _detail(value: dict[str, Any]) -> dict[str, Any]:
    try:
        template = get_trace_template(value["investigation_type"])
    except TraceContractError:
        template = get_trace_template("public_event")
    return {
        **value,
        "template_id": value.get("template_id") or value.get("scope", {}).get("template_id") or template.template_id,
        "template_version": value.get("template_version") or value.get("scope", {}).get("template_version") or template.template_version,
    }


def _event_detail(run: InvestigationRun) -> dict[str, Any]:
    return {
        "event_id": run.event.event_id,
        "title": run.event.title,
        "objective": run.event.objective,
        "stage": run.event.stage.value,
        "aliases": list(run.event.aliases),
        "entities": list(run.event.entities),
        "risk_score": run.event.risk_score,
        "first_seen_at": run.event.first_seen_at,
        "last_seen_at": run.event.last_seen_at,
    }


def _get_run_or_404(run_id: str, runtime: InvestigationRuntime) -> InvestigationRun:
    run = runtime.run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="investigation not found")
    return run


def _get_summary_or_404(run_id: str, runtime: InvestigationRuntime) -> dict[str, Any]:
    value = runtime.run_store.get_summary(run_id)
    if value is None:
        raise HTTPException(status_code=404, detail="investigation not found")
    return value


@router.post("", response_model=InvestigationResponse, status_code=201)
def create_investigation(payload: InvestigationInput, runtime: InvestigationRuntime = Depends(get_investigation_runtime)):
    documents = _documents(payload.documents)
    run = InvestigationRun(
        objective=payload.objective.strip(),
        event=Event(title=_run_title(payload), objective=payload.objective.strip()),
        investigation_type=payload.investigation_type.strip(),
        subject=(payload.subject or "").strip() or None,
        scope=dict(payload.scope),
        documents=documents,
    )
    run.add_trace("intake", "completed", document_count=len(documents))
    runtime.run_store.save(run)
    return {"success": True, "run": _summary(run)}


@router.post("/{run_id}/run", response_model=InvestigationResponse)
def run_investigation(run_id: str, runtime: InvestigationRuntime = Depends(get_investigation_runtime)):
    try:
        return {"success": True, "run": _summary(_start_run(run_id, runtime))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{run_id}/resume", response_model=InvestigationResponse)
def resume_investigation(run_id: str, runtime: InvestigationRuntime = Depends(get_investigation_runtime)):
    try:
        return {"success": True, "run": _summary(_start_run(run_id, runtime, resume=True))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{run_id}/pause", response_model=InvestigationResponse)
def pause_investigation(
    run_id: str,
    reason: str = Query(default="paused by user", min_length=1, max_length=500),
    runtime: InvestigationRuntime = Depends(get_investigation_runtime),
):
    pause = getattr(getattr(runtime, "task_service", None), "pause_run", None)
    if callable(pause):
        try:
            run = pause(run_id=run_id, reason=reason, actor="user")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="investigation not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    else:
        run = _get_run_or_404(run_id, runtime)
        if run.status in {InvestigationStatus.COMPLETED, InvestigationStatus.CANCELLED}:
            raise HTTPException(status_code=409, detail="investigation cannot be paused")
        run.status = InvestigationStatus.PAUSED
        run.error = reason
        run.interrupt_reason = reason
        run.add_trace("pause", "completed", reason=reason)
        runtime.run_store.save(run)
    return {"success": True, "run": _summary(run)}


@router.post("/{run_id}/cancel", response_model=InvestigationResponse)
def cancel_investigation(
    run_id: str,
    reason: str = Query(default="cancelled by user", min_length=1, max_length=500),
    runtime: InvestigationRuntime = Depends(get_investigation_runtime),
):
    run = _get_run_or_404(run_id, runtime)
    if run.status in {InvestigationStatus.COMPLETED, InvestigationStatus.CANCELLED}:
        raise HTTPException(status_code=409, detail="investigation cannot be cancelled")
    run.status = InvestigationStatus.CANCELLED
    run.error = reason
    run.interrupt_reason = reason
    run.add_trace("cancel", "completed", reason=reason)
    runtime.run_store.save(run)
    return {"success": True, "run": _summary(run)}


@router.get("", response_model=InvestigationListResponse)
def list_investigations(
    limit: int = Query(default=30, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    runtime: InvestigationRuntime = Depends(get_investigation_runtime),
):
    rows = runtime.run_store.list_summaries(limit=limit + 1, offset=offset)
    has_more = len(rows) > limit
    return {
        "success": True,
        "runs": rows[:limit],
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


@router.get("/{run_id}", response_model=InvestigationResponse)
def get_investigation(run_id: str, runtime: InvestigationRuntime = Depends(get_investigation_runtime)):
    return {"success": True, "run": _detail(_get_summary_or_404(run_id, runtime))}


@router.get("/{run_id}/events", response_model=EventPageResponse)
def get_investigation_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=_MAX_EVENT_PAGE_SIZE),
    runtime: InvestigationRuntime = Depends(get_investigation_runtime),
):
    _get_summary_or_404(run_id, runtime)
    rows = runtime.run_store.events(run_id, after_seq=after_seq, limit=limit + 1)
    has_more = len(rows) > limit
    events = rows[:limit]
    return {
        "success": True,
        "events": events,
        "after_seq": after_seq,
        "limit": limit,
        "has_more": has_more,
        "next_after_seq": int(events[-1]["seq"]) if has_more and events else None,
    }


@router.get("/{run_id}/nodes", response_model=NodeListResponse)
def get_investigation_nodes(run_id: str, runtime: InvestigationRuntime = Depends(get_investigation_runtime)):
    run = _get_summary_or_404(run_id, runtime)
    repository = runtime.controller.node_store
    try:
        template = get_trace_template(run["investigation_type"])
    except TraceContractError:
        template = get_trace_template("public_event")
    specs = {item.slot_id: item for item in template.node_specs}
    nodes: list[dict[str, Any]] = []
    for record in repository.list_nodes(run_id):
        slot_id = str(record["slot_id"])
        spec = specs.get(slot_id)
        revision_no = int(record.get("current_revision") or 0)
        history = []
        for index in range(revision_no + 1):
            revision = repository.get_revision(record["node_id"], index)
            if revision is not None:
                history.append({
                    key: revision.get(key)
                    for key in ("revision", "conclusion", "draft", "review", "created_at")
                })
        nodes.append({
            **{
                key: record.get(key)
                for key in (
                    "node_id", "run_id", "template_id", "template_version", "slot_id",
                    "node_order", "instance_no", "status", "current_revision",
                    "frozen_revision", "current_review_id",
                )
            },
            "title": spec.title if spec else slot_id,
            "goal": spec.goal if spec else "",
            "aspects": list(spec.aspects) if spec else [],
            "revision_history": history,
        })
    return {"success": True, "run_id": run_id, "nodes": nodes}


@router.get("/{run_id}/trace", response_model=TraceResponse)
def get_investigation_trace(run_id: str, runtime: InvestigationRuntime = Depends(get_investigation_runtime)):
    _get_summary_or_404(run_id, runtime)
    graph_state = runtime.run_store.graph_state(run_id) or {}
    node_trace = graph_state.get("node_trace")
    if node_trace is None:
        raise HTTPException(status_code=409, detail="investigation has no completed frozen node Trace")
    try:
        node_trace = refresh_frozen_trace(node_trace, runtime.controller.node_store)
    except TraceContractError as exc:
        raise HTTPException(status_code=409, detail=f"investigation frozen Trace is incomplete: {exc}") from exc
    return {
        "success": True,
        "trace": node_trace,
        "review": graph_state.get("cross_node_review"),
    }


@router.get(
    "/{run_id}/report",
    response_model=ReportResponse,
    responses={200: {"content": {
        "text/markdown": {"schema": {"type": "string"}},
        "text/html": {"schema": {"type": "string"}},
        "application/pdf": {"schema": {"type": "string", "format": "binary"}},
    }}},
)
def get_investigation_report(
    run_id: str,
    format: str = Query(default="json", pattern="^(json|markdown|html|pdf)$"),
    runtime: InvestigationRuntime = Depends(get_investigation_runtime),
):
    _get_summary_or_404(run_id, runtime)
    report = runtime.run_store.report_document(run_id)
    if not isinstance(report, dict) or not report:
        raise HTTPException(status_code=409, detail="investigation has no generated report")
    try:
        document = ReportDocument.from_dict(report)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"持久化报告不符合 ReportDocument 契约：{exc}") from exc
    if format == "json":
        return {"success": True, "report": document.to_dict()}
    if format == "markdown":
        return Response(render_report_markdown(document), media_type="text/markdown; charset=utf-8")
    if format == "html":
        return Response(render_report_html(document), media_type="text/html; charset=utf-8")
    if format == "pdf":
        return Response(render_report_pdf(document), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={document.report_id}.pdf"})
    raise HTTPException(status_code=400, detail="format 只能是 json、markdown、html 或 pdf")


@router.get("/{run_id}/report/progress", response_model=ReportProgressResponse)
def get_report_progress(run_id: str, runtime: InvestigationRuntime = Depends(get_investigation_runtime)):
    run = _get_summary_or_404(run_id, runtime)
    task_service = getattr(runtime, "task_service", None)
    tasks = task_service.get_report_tasks(run_id) if task_service is not None else []
    plan_task = next((item for item in tasks if item.get("stage") == "report_plan"), None)
    section_tasks = {str(item.get("slot_id") or ""): item for item in tasks if item.get("stage") == "report_section"}
    report = runtime.run_store.report_document(run_id) or {}
    try:
        from echotrace.application.report_templates import load_report_template

        template = load_report_template(str(run.get("investigation_type") or "public_event"))
        chapter_specs = [(item.chapter_id, item.title) for item in template.chapters]
    except Exception:
        chapter_specs = [(str(item.get("chapter_id") or ""), str(item.get("title") or "章节")) for item in report.get("chapters", [])]
    sections = []
    for chapter_id, title in chapter_specs:
        task = section_tasks.get(chapter_id)
        raw_status = str((task or {}).get("status") or "pending")
        status = {
            "succeeded": "completed", "skipped": "completed", "running": "generating",
            "failed": "failed", "cancelled": "cancelled", "pending": "queued",
        }.get(raw_status, "queued")
        sections.append({"chapter_id": chapter_id, "title": title, "status": status})
    completed_sections = sum(item["status"] == "completed" for item in sections)
    planning_status = str(plan_task.get("status")) if plan_task else None
    if report:
        status = "completed"
        sections = [{**item, "status": "completed"} for item in sections]
        completed_sections = len(sections) or len(report.get("chapters", []))
    elif not tasks:
        status = "not_started"
    elif planning_status == "failed":
        status = "failed"
    elif any(str(item.get("status") or "") == "failed" for item in tasks):
        status = "failed"
    elif planning_status != "succeeded":
        status = "planning"
    else:
        status = "writing"
    return {
        "success": True,
        "exists": bool(tasks) or bool(report),
        "status": status,
        "planning_status": planning_status,
        "completed_sections": completed_sections,
        "total_sections": len(sections),
        "sections": sections,
    }


@router.get("/{run_id}/workbench", response_model=WorkbenchResponse)
def get_investigation_workbench(run_id: str, runtime: InvestigationRuntime = Depends(get_investigation_runtime)):
    run = _get_run_or_404(run_id, runtime)
    node_rows = runtime.controller.node_store.list_nodes(run_id)
    drafts = [
        revision["draft"]
        for node in node_rows
        if (revision := runtime.controller.node_store.get_revision(node["node_id"], node.get("current_revision"))) is not None
    ]
    return {
        "success": True,
        "workbench": {
            "run_id": run.run_id,
            "objective": run.objective,
            "subject": run.subject,
            "investigation_type": run.investigation_type,
            "status": run.status.value,
            "checkpoint": run.checkpoint,
            "event": _event_detail(run),
            "error": run.error,
            "documents": [
                {"document_id": item.document_id, "source": item.source, "title": item.title, "url": item.url}
                for item in run.documents
            ],
            "counts": {
                "documents": len(run.documents),
                "claims": sum(len(item.get("claims") or []) for item in drafts),
                "evidence": sum(len(item.get("evidence") or []) for item in drafts),
                "nodes": len(node_rows),
                "frozen_nodes": sum(1 for item in node_rows if item.get("status") == "frozen"),
            },
            "workflow_stages": InvestigationProjection.workflow_stages(run),
            "lifecycle_events": InvestigationProjection.lifecycle_events(run),
            "audit_events": InvestigationProjection.audit_events(run)[-40:],
            "timeline_entries": InvestigationProjection.workbench_timeline(run),
            "recent_events": InvestigationProjection.timeline(run)[-10:],
            "source_clusters": InvestigationProjection.source_clusters(run),
            "skill_audit": InvestigationProjection.skill_audit(run),
            "tool_audit": InvestigationProjection.tool_audit(run),
            "report": run.report_document,
        },
    }


@router.post("/{run_id}/nodes/{slot_id}/reformat")
def reformat_frozen_node(run_id: str, slot_id: str, runtime: InvestigationRuntime = Depends(get_investigation_runtime)):
    try:
        return {"success": True, **runtime.reformat_frozen_node(run_id, slot_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TraceContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
