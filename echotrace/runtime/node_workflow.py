"""Run-level adapter for the node-driven Trace controller."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from echotrace.domain.models import InvestigationRun, InvestigationStatus
from echotrace.domain.trace_nodes import TraceContractError, TraceTemplate, get_trace_template, list_trace_templates
from echotrace.application.node_review import review_node_trace
from echotrace.application.node_trace import assemble_frozen_trace
from echotrace.application.report_templates import load_report_template

from .node_controller import NodeController, NodeControllerResult


class RunStore(Protocol):
    def get(self, run_id: str) -> InvestigationRun | None: ...
    def save(self, run: InvestigationRun, **kwargs: Any) -> InvestigationRun: ...


class NodeStore(Protocol):
    def list_nodes(self, run_id: str) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class NodeWorkflowResult:
    run: InvestigationRun
    nodes: NodeControllerResult


class NodeInvestigationController:
    """Bridge the Run API to the fixed-template node Controller.

    Source collection and run status are handled here; node revisions and
    reviews remain owned by :class:`NodeController` and its repository.
    """

    def __init__(
        self,
        *,
        controller: NodeController,
        run_store: RunStore,
        node_store: NodeStore,
        source_collector: Callable[[InvestigationRun], list[Any]] | None = None,
        node_task_submitter: Callable[[Any, Any], Any] | None = None,
        report_task_submitter: Callable[[InvestigationRun, Any, Any], Any] | None = None,
    ) -> None:
        self.controller = controller
        self.run_store = run_store
        self.node_store = node_store
        self.source_collector = source_collector
        self.node_task_submitter = node_task_submitter
        self.report_task_submitter = report_task_submitter

    def bind_run(self, run: InvestigationRun) -> None:
        self.controller.bind_run(run)
        binder = getattr(getattr(self, "report_runner", None), "bind_run", None)
        if callable(binder):
            binder(run)

    def run(self, run: InvestigationRun, *, execution_task_id: str | None = None) -> NodeWorkflowResult:
        self.bind_run(run)
        if run.status == InvestigationStatus.CANCELLED:
            raise RuntimeError("investigation is cancelled and cannot be resumed")
        self._assert_running(run.run_id)
        investigation_type = str(run.investigation_type or "auto").strip().lower()
        # Persist the initial source set before automatic classification so the
        # orchestrator sees the same durable evidence that later node agents use.
        if not run.documents and self.source_collector is not None:
            self._assert_running(run.run_id)
            incoming = list(self.source_collector(run))
            self._assert_running(run.run_id)
            run.documents.extend(incoming)
            if not incoming:
                raise RuntimeError("source discovery returned no documents")
            self.run_store.save(run)
        if investigation_type == "auto":
            run.add_trace("classification", "started", method="ai")
            self.run_store.save(run, expected_state_version=run.state_version)
            try:
                classification = self.controller.planner.classify(
                    run_id=run.run_id,
                    objective=run.objective,
                    event_title=run.event.title,
                    subject=run.subject,
                    source_documents=[
                        {
                            "document_id": document.document_id,
                            "source": document.source,
                            "title": document.title,
                            "content": document.content,
                            "url": document.url,
                            "content_hash": document.content_hash,
                            "metadata": dict(document.metadata or {}),
                        }
                        for document in run.documents
                    ],
                    candidates=list_trace_templates(),
                    execution_task_id=execution_task_id,
                )
            except Exception as exc:
                latest = self.run_store.get(run.run_id)
                if latest is not None and latest.status != InvestigationStatus.CANCELLED:
                    outcome = "deferred" if getattr(exc, "defer_task", False) else "failed"
                    latest.add_trace(
                        "classification", outcome, method="ai",
                        error=" ".join(str(exc).split())[:500],
                    )
                    self.run_store.save(latest, expected_state_version=latest.state_version)
                # Classification is part of the orchestrator start command.
                # Provider timeouts/refusals may be marked non-retryable at
                # the model boundary, but the durable start command should
                # receive another delivery unless the run was paused.
                if getattr(exc, "defer_task", False):
                    raise
                retry_error = TraceContractError(f"automatic classification failed: {exc}")
                retry_error.retryable = True
                retry_error.error_code = getattr(exc, "error_code", "classification_failed")
                raise retry_error from exc
            self._assert_running(run.run_id)
            investigation_type = str(classification.get("investigation_type") or "").strip()
            template = get_trace_template(investigation_type)
            run.investigation_type = investigation_type
            run.scope = dict(run.scope or {})
            run.scope["type_classification"] = {
                "method": "ai",
                "rationale": str(classification.get("rationale") or ""),
            }
            run.add_trace(
                "classification", "completed", method="ai",
                investigation_type=investigation_type,
                rationale=str(classification.get("rationale") or ""),
            )
            self.run_store.save(run, expected_state_version=run.state_version)
            self._assert_running(run.run_id)
        else:
            template = get_trace_template(investigation_type)
        if not run.documents:
            raise RuntimeError("distributed investigation requires persisted source documents")

        run.investigation_type = investigation_type
        run.scope = dict(run.scope or {})
        run.scope.update({"template_id": template.template_id, "template_version": template.template_version})
        run.status = InvestigationStatus.RUNNING
        run.error = None
        run.interrupt_reason = None
        self.run_store.save(run, expected_state_version=run.state_version)
        input_refs = {spec.slot_id: [item.document_id for item in run.documents] for spec in template.node_specs}
        if self.node_task_submitter is None:
            raise RuntimeError("distributed node workflow requires the node task submitter")
        result = self.controller.prepare(
            run_id=run.run_id,
            objective=run.objective,
            template=template,
            input_refs=input_refs,
            execution_task_id=execution_task_id,
        )
        self._assert_running(run.run_id)
        submission = self.node_task_submitter(result.plan, template)
        result.status = "queued"
        # NodeController persists revisions and reviews through its repository.
        # Re-read the run before writing run-level state so this final save does
        # not replace those newer graph-state fields with the intake snapshot.
        latest = self.run_store.get(run.run_id)
        if latest is None:
            # The durable run may have been removed by an external database
            # reset while an Agent call was still in flight.  Never resurrect
            # it from the stale in-memory snapshot: that would create a run
            # with missing nodes, revisions, and source records.
            raise RuntimeError(
                f"investigation storage integrity lost for run {run.run_id}: "
                "durable run row disappeared while the workflow was running"
            )
        if latest.status != InvestigationStatus.RUNNING:
            raise TraceContractError("investigation paused while its node plan was being prepared")
        if len(result.nodes) != len(template.node_specs):
            raise RuntimeError(
                f"investigation storage integrity lost for run {run.run_id}: "
                f"expected {len(template.node_specs)} nodes, found {len(result.nodes)}"
            )
        latest.graph_state = dict(latest.graph_state or {})
        latest.graph_state["node_controller_status"] = "queued"
        latest.graph_state["node_plan_id"] = result.plan.plan_id
        latest.graph_state["node_states"] = list(result.nodes)
        latest.checkpoint = "node_tasks_pending"
        latest.status = InvestigationStatus.RUNNING
        latest.add_trace(
            "node_tasks", "queued", plan_id=result.plan.plan_id,
            task_count=len(submission.task_ids),
        )
        def event_signature(event: dict[str, Any]) -> str:
            return json.dumps(
                {key: value for key, value in event.items() if key not in {"seq", "at"}},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )

        persisted_events = {event_signature(event) for event in latest.trace}
        for event in run.trace:
            signature = event_signature(event)
            if signature in persisted_events:
                continue
            latest.add_trace(
                str(event.get("step") or "node_agent"),
                str(event.get("status") or "completed"),
                **{key: value for key, value in event.items() if key not in {"seq", "step", "status", "at"}},
            )
            persisted_events.add(signature)
        for event in result.events:
            event_type = str(event.get("type") or "node_controller")
            latest.add_trace(event_type, "completed" if result.status == "completed" else "checkpoint", **{k: v for k, v in event.items() if k != "type"})
        self.run_store.save(latest, expected_state_version=latest.state_version)
        return NodeWorkflowResult(latest, result)

    def _assert_running(self, run_id: str) -> None:
        latest = self.run_store.get(run_id)
        if latest is None:
            raise RuntimeError(f"investigation not found: {run_id}")
        if latest.status == InvestigationStatus.PAUSED:
            from echotrace.application.investigation_service import TaskDeferredError

            raise TaskDeferredError("investigation is paused")
        if latest.status == InvestigationStatus.CANCELLED:
            raise RuntimeError("investigation is cancelled")

    def complete_frozen_nodes(self, run_id: str) -> bool:
        """Persist the frozen Trace and enqueue its report once all slots freeze."""
        latest = self.run_store.get(run_id)
        if latest is None:
            raise RuntimeError(f"investigation not found: {run_id}")
        self._assert_running(run_id)
        if latest.report_document:
            return True
        investigation_type = str(latest.investigation_type or "public_event").strip()
        template = get_trace_template(investigation_type)
        nodes = self.node_store.list_nodes(run_id)
        if len(nodes) != len(template.node_specs) or any(node.get("status") != "frozen" for node in nodes):
            return False
        node_trace = assemble_frozen_trace(latest, template, self.node_store)
        latest.graph_state = dict(latest.graph_state or {})
        latest.graph_state["node_trace"] = node_trace.to_dict()
        latest.graph_state["cross_node_review"] = review_node_trace(node_trace, template).to_dict()
        latest.graph_state["node_controller_status"] = "completed"
        latest.graph_state["node_states"] = nodes
        if self.report_task_submitter is None:
            raise RuntimeError("distributed node workflow requires the report task submitter")
        report_template = load_report_template(investigation_type)
        submission = self.report_task_submitter(latest, node_trace, report_template)
        latest.checkpoint = "report_queued"
        latest.status = InvestigationStatus.RUNNING
        latest.add_trace(
            "report", "queued", report_id=f"report_{run_id}",
            plan_id=getattr(submission, "plan_id", None),
            chapter_count=len(report_template.chapters),
        )
        self.run_store.save(latest)
        return True


__all__ = ["NodeInvestigationController", "NodeWorkflowResult"]
