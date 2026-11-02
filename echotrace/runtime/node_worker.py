"""Role-task handler for the durable node research/review state machine."""

from __future__ import annotations

from typing import Any

from echotrace.domain.trace_nodes import get_trace_template
from echotrace.infrastructure.coordination.redis.contracts import NodeTaskMessage
from echotrace.application.investigation_service import TaskDeferredError


class NodeRoleTaskHandler:
    """Run one assigned Researcher or Verifier round, then reconcile progress."""

    def __init__(self, *, controller: Any, run_store: Any, node_store: Any, task_service: Any, workflow: Any) -> None:
        self.controller = controller
        self.run_store = run_store
        self.node_store = node_store
        self.task_service = task_service
        self.workflow = workflow

    def __call__(self, message: NodeTaskMessage) -> dict[str, list[str]]:
        run = self.run_store.get(message.run_id)
        if run is None:
            raise RuntimeError(f"investigation not found: {message.run_id}")
        if str(run.status.value) == "paused":
            raise TaskDeferredError("investigation is paused")
        plan = self.task_service.get_node_plan(message.run_id)
        if plan is None:
            raise RuntimeError(f"node plan not found: {message.run_id}")
        template = get_trace_template(str(run.investigation_type or "public_event"))
        source_documents = [_document_payload(document) for document in run.documents]
        durable_task = self.task_service.get_task(message.task_id)
        task_budget = durable_task.get("budget") if isinstance(durable_task, dict) else None
        self.controller.bind_run(run)

        if message.agent == "researcher" and message.task_type.startswith("node.research."):
            self.node_store.begin_research(message.node_id, message.revision)
            self.controller.execute_research_task(
                plan=plan,
                slot_id=message.slot_id,
                revision=message.revision,
                template=template,
                source_documents=source_documents,
                task_budget=task_budget,
            )
            if message.revision == 3:
                self.controller.freeze_node(message.node_id)
        elif message.agent == "verifier" and message.task_type.startswith("node.review."):
            self.controller.execute_review_task(
                plan=plan,
                slot_id=message.slot_id,
                review_round=message.revision,
                execution_task_id=message.task_id,
                template=template,
                source_documents=source_documents,
                task_budget=task_budget,
            )
        else:
            raise ValueError(f"unsupported node role task: {message.agent}/{message.task_type}")

        get_run_status = getattr(self.task_service, "get_run_status", None)
        if callable(get_run_status) and get_run_status(message.run_id) == "paused":
            raise TaskDeferredError("investigation was paused while the node task was running")
        progress = self.task_service.ensure_node_tasks(plan=plan, template=template)
        if progress.all_nodes_frozen:
            self.workflow.complete_frozen_nodes(message.run_id)
        return {"output_refs": [f"node:{message.node_id}:revision:{message.revision}"]}


def _document_payload(document: Any) -> dict[str, Any]:
    return {
        "document_id": str(getattr(document, "document_id", "")),
        "source": str(getattr(document, "source", "")),
        "title": str(getattr(document, "title", "")),
        "content": str(getattr(document, "content", "")),
        "url": getattr(document, "url", None),
        "metadata": dict(getattr(document, "metadata", {}) or {}),
    }


__all__ = ["NodeRoleTaskHandler"]
