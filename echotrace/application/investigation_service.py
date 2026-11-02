"""Transactional application use-cases for task execution.

This layer intentionally knows only persistence ports.  The controller decides
which task to submit; this service guarantees that submission,
attempt projection and outbox notification are committed together.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
from hashlib import sha256
from typing import Any, Callable

from echotrace.ports.persistence import UnitOfWork
from echotrace.domain.models import InvestigationStatus, beijing_now
from echotrace.domain.node_tasks import NodeResearchTask, NodeTaskPlan, node_task_id
from echotrace.domain.trace_nodes import NodeReview, TraceContractError, TraceTemplate


def _node_task_deadline_seconds() -> int:
    try:
        return max(60, int(os.getenv("ECHOTRACE_NODE_TASK_DEADLINE_SECONDS", "3600")))
    except ValueError:
        return 3600


def _default_node_task_budget(*, revision: int, agent: str) -> dict[str, Any]:
    """Create a durable deadline shared by role work and broker redeliveries."""

    deadline_seconds = _node_task_deadline_seconds()
    return {
        "agent": agent,
        "revision": revision,
        "deadline_seconds": deadline_seconds,
        "deadline_epoch": time.time() + deadline_seconds,
        "budget_semantics": "deadline bounds one role task; broker redeliveries share it and explicit failed-task recovery receives a fresh deadline",
    }


@dataclass(frozen=True, slots=True)
class PlanSubmission:
    plan_id: str
    task_ids: tuple[str, ...]
    outbox_ids: tuple[str, ...]
    all_nodes_frozen: bool = False


class TaskNotReadyError(RuntimeError):
    """A durable task is visible but one of its MySQL dependencies is pending."""

    not_ready = True


class TaskDeferredError(TaskNotReadyError):
    """A task belongs to a paused run and must remain pending until resume."""

    defer_task = True
    error_code = "run_paused"


class InvestigationTaskService:
    """Persist task lifecycle transitions without exposing a DB session."""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self.uow_factory = uow_factory

    def submit_node_plan(
        self, *, plan: NodeTaskPlan, template: TraceTemplate,
        available_capabilities: list[str] | tuple[str, ...],
        target_revisions: dict[str, int] | None = None,
    ) -> PlanSubmission:
        """Persist the fixed plan and schedule only currently executable rounds."""
        with self.uow_factory() as uow:
            run = uow.runs.get(plan.run_id)
            if run is None:
                raise TraceContractError(f"investigation not found: {plan.run_id}")
            if run.status == InvestigationStatus.PAUSED:
                raise TaskDeferredError("investigation is paused before node scheduling")
            nodes = uow.trace_nodes.list_nodes(plan.run_id)
            if any((node["template_id"], node["template_version"]) != (template.template_id, template.template_version) for node in nodes):
                raise TraceContractError("persisted nodes use a different template version")
            plan.validate(
                template=template, node_ids={node["slot_id"]: node["node_id"] for node in nodes},
                available_capabilities=available_capabilities, target_revisions=target_revisions,
            )
            plan_id = self._ensure_node_plan(uow, plan, template)
            uow.runs.append_event(
                run_id=plan.run_id, event_type="node_plan.created", actor_type="orchestrator",
                payload={"plan_id": plan_id}, idempotency_key=f"{plan_id}:created",
            )
            return self._ensure_node_tasks_in_uow(uow, plan, template, plan_id, nodes)

    def ensure_node_tasks(
        self, *, plan: NodeTaskPlan, template: TraceTemplate, retry_failed: bool = False,
    ) -> PlanSubmission:
        """Reconcile durable node state with its next role task after recovery."""
        with self.uow_factory() as uow:
            run = uow.runs.get(plan.run_id)
            if run is not None and run.status == InvestigationStatus.PAUSED:
                raise TaskDeferredError("investigation is paused")
            nodes = uow.trace_nodes.list_nodes(plan.run_id)
            if not nodes:
                raise TraceContractError("cannot schedule node tasks before template nodes are initialized")
            if any((node["template_id"], node["template_version"]) != (template.template_id, template.template_version) for node in nodes):
                raise TraceContractError("persisted nodes use a different template version")
            plan.validate(
                template=template,
                node_ids={node["slot_id"]: node["node_id"] for node in nodes},
                available_capabilities=sorted({task.capability for task in plan.tasks}),
            )
            plan_id = self._ensure_node_plan(uow, plan, template)
            return self._ensure_node_tasks_in_uow(
                uow, plan, template, plan_id, nodes, retry_failed=retry_failed,
            )

    @staticmethod
    def _ensure_node_plan(uow: UnitOfWork, plan: NodeTaskPlan, template: TraceTemplate) -> str:
        return uow.tasks.add_plan(
            run_id=plan.run_id,
            plan_id=plan.plan_id,
            objective=plan.objective,
            allowed_agents=["researcher", "verifier"],
            allowed_capabilities=sorted({task.capability for task in plan.tasks}),
            budget={
                "max_parallel_tasks": plan.max_parallel_tasks,
                "task_deadline_seconds": _node_task_deadline_seconds(),
                "stop_rule": "semantic_progress_then_synthesis; limits are failure guards",
            },
            stop_conditions=[
                "研究者确认固定方面和本轮审稿任务均有证据或编辑动作覆盖后主动 ready",
                "搜索次数、无新增来源次数和 revision 只作诊断或编辑提示，不作为强制停止条件",
                "任务 deadline、provider 限流和 Agent step limit 仅用于失败保护；失败必须保留原因并允许上层重试",
            ],
            definition=plan.to_dict(),
        )

    def _ensure_node_tasks_in_uow(
        self, uow: UnitOfWork, plan: NodeTaskPlan, template: TraceTemplate,
        plan_id: str, nodes: list[dict[str, Any]],
        *, retry_failed: bool = False,
    ) -> PlanSubmission:
        node_by_slot = {str(node["slot_id"]): node for node in nodes}
        frozen = {slot for slot, node in node_by_slot.items() if node.get("status") == "frozen"}
        task_ids: list[str] = []
        outbox_ids: list[str] = []
        for base in plan.tasks:
            node = node_by_slot[base.slot_id]
            if node.get("status") == "frozen":
                continue
            if not set(base.depends_on_slot_ids).issubset(frozen):
                continue

            node_id = str(node["node_id"])
            current_revision = int(node.get("current_revision") or 0)
            if current_revision >= 3:
                final_review = uow.trace_nodes.get_review(node_id, 3)
                if final_review and final_review.get("review", {}).get("status") == "finalize":
                    frozen_node = uow.trace_nodes.freeze_node(node_id)
                    node.update(frozen_node)
                    frozen.add(base.slot_id)
                else:
                    raise TraceContractError(f"node {base.slot_id} reached revision 3 without a final review")
                continue

            has_current_draft = uow.trace_nodes.get_revision(node_id, current_revision) is not None
            if not has_current_draft:
                revision = 0
                task_type = "node.research.initial"
                agent = "researcher"
                task_data: dict[str, Any] = self._research_task(plan.run_id, base, template, revision, None).to_dict()
                dependencies = self._parent_task_ids(plan, template, base)
            else:
                next_revision = current_revision + 1
                saved_review = uow.trace_nodes.get_review(node_id, next_revision)
                if saved_review is None:
                    revision = next_revision
                    task_type = f"node.review.round_{revision}"
                    agent = "verifier"
                    task_data = {
                        "task_type": task_type,
                        "stage": "node_review",
                        "node_id": node_id,
                        "slot_id": base.slot_id,
                        "revision": revision,
                        "question": base.question,
                        "capability": base.capability,
                    }
                    dependencies = [node_task_id(plan.run_id, template, node_id, base.slot_id, current_revision)]
                else:
                    revision = next_revision
                    task_type = f"node.research.revision_{revision}"
                    agent = "researcher"
                    review = NodeReview.from_dict(saved_review["review"])
                    task_data = self._research_task(plan.run_id, base, template, revision, review).to_dict()
                    dependencies = [self._review_task_id(plan.run_id, node_id, base.slot_id, revision)]

            task_id = self._task_id(plan.run_id, template, node_id, base.slot_id, task_type, revision)
            task_data = {**task_data, "task_id": task_id, "task_type": task_type}
            context_hash = sha256(json.dumps(task_data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            existing_definition = uow.tasks.get_task(task_id)
            retrying = bool(existing_definition and existing_definition.get("status") == "failed")
            if retrying and not retry_failed:
                continue
            if retrying:
                task_budget = _default_node_task_budget(revision=revision, agent=agent)
                uow.tasks.refresh_failed_task_budget(task_id, budget=task_budget)
                existing_definition = uow.tasks.get_task(task_id)
            else:
                task_budget = (
                    dict(existing_definition.get("budget") or {})
                    if isinstance(existing_definition, dict) and existing_definition.get("budget")
                    else _default_node_task_budget(revision=revision, agent=agent)
                )
            task_id = uow.tasks.add_task(
                run_id=plan.run_id,
                plan_id=plan_id,
                task_id=task_id,
                agent=agent,
                stage="node_review" if agent == "verifier" else ("node_research" if revision == 0 else "node_revision"),
                question=base.question,
                capability=base.capability,
                idempotency_key=f"node:{plan.run_id}:{base.slot_id}:{task_type}",
                dependencies=dependencies,
                priority=base.priority,
                node_id=node_id,
                slot_id=base.slot_id,
                node_revision=revision,
                review_id=(task_data.get("review_feedback") or {}).get("review_id"),
                input_refs=list(base.input_refs),
                input_data=task_data,
                budget=task_budget,
            )
            existing_task = uow.tasks.get_task(task_id)
            payload = {
                "task_id": task_id,
                "run_id": plan.run_id,
                "node_id": node_id,
                "slot_id": base.slot_id,
                "task_type": task_type,
                "agent": agent,
                "revision": revision,
                "state_version": int(existing_task.get("state_version") or 0) if existing_task else 0,
                "idempotency_key": f"node:{plan.run_id}:{base.slot_id}:{task_type}",
                "context_pack_hash": context_hash,
                "enqueued_at": beijing_now().isoformat(),
            }
            outbox_id = uow.outbox.enqueue(
                aggregate_type="investigation_task",
                aggregate_id=task_id,
                event_type="node.task.redrive" if retrying else task_type,
                payload=payload,
                dedup_key=(
                    f"{task_id}:retry:{int(existing_task.get('state_version') or 0)}"
                    if retrying else f"{task_id}:dispatch"
                ),
            )
            task_ids.append(task_id)
            outbox_ids.append(outbox_id)

        final_nodes = uow.trace_nodes.list_nodes(plan.run_id)
        all_frozen = bool(final_nodes) and all(node.get("status") == "frozen" for node in final_nodes)
        return PlanSubmission(plan_id, tuple(task_ids), tuple(outbox_ids), all_frozen)

    @staticmethod
    def _research_task(run_id: str, base: NodeResearchTask, template: TraceTemplate, revision: int, review: NodeReview | None) -> NodeResearchTask:
        task_id = node_task_id(run_id, template, base.node_id, base.slot_id, revision)
        return NodeResearchTask(
            task_id=task_id,
            node_id=base.node_id,
            slot_id=base.slot_id,
            revision=revision,
            question=base.question,
            capability=base.capability,
            aspects=base.aspects,
            preferred_sources=base.preferred_sources,
            depends_on_slot_ids=base.depends_on_slot_ids,
            input_refs=base.input_refs,
            priority=base.priority,
            review_feedback=review,
        )

    @staticmethod
    def _task_id(run_id: str, template: TraceTemplate, node_id: str, slot_id: str, task_type: str, revision: int) -> str:
        if task_type.startswith("node.research."):
            return node_task_id(run_id, template, node_id, slot_id, revision)
        identity = [run_id, node_id, slot_id, task_type, revision]
        return "vtask_" + sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _review_task_id(run_id: str, node_id: str, slot_id: str, revision: int) -> str:
        identity = [run_id, node_id, slot_id, f"node.review.round_{revision}", revision]
        return "vtask_" + sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _parent_task_ids(plan: NodeTaskPlan, template: TraceTemplate, task: NodeResearchTask) -> list[str]:
        node_ids = {item.slot_id: item.node_id for item in plan.tasks}
        return [
            node_task_id(plan.run_id, template, node_ids[slot_id], slot_id, 3)
            for slot_id in task.depends_on_slot_ids
        ]

    def enqueue_run_start(self, *, run_id: str, idempotency_key: str | None = None) -> str:
        """Commit the durable command consumed by the orchestrator worker."""
        key = idempotency_key or f"{run_id}:start"
        with self.uow_factory() as uow:
            outbox_id = uow.outbox.enqueue(
                aggregate_type="investigation_run",
                aggregate_id=run_id,
                event_type="investigation.start",
                payload={"run_id": run_id, "idempotency_key": key, "enqueued_at": beijing_now().isoformat()},
                dedup_key=key,
            )
            uow.runs.append_event(
                run_id=run_id, event_type="investigation.queued", actor_type="api",
                payload={"outbox_id": outbox_id}, idempotency_key=key,
            )
            return outbox_id

    def submit_report_tasks(
        self, *, run_id: str, report_id: str, template_id: str, template_version: str,
        chapter_ids: list[str] | tuple[str, ...], trace_id: str,
        retry_failed: bool = False,
    ) -> PlanSubmission:
        """Create the report.plan -> report.section task graph after Trace freeze."""
        chapter_ids = tuple(str(item).strip() for item in chapter_ids if str(item).strip())
        if not chapter_ids:
            raise TraceContractError("report task graph requires at least one chapter")
        identity = [run_id, report_id, template_id, template_version, trace_id, chapter_ids]
        plan_id = f"rplan_{sha256(json.dumps(identity, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]}"
        plan_task_id = f"rtask_{sha256((plan_id + ':plan').encode('utf-8')).hexdigest()[:24]}"
        with self.uow_factory() as uow:
            nodes = uow.trace_nodes.list_nodes(run_id)
            if not nodes or any(str(node.get("status") or "") != "frozen" for node in nodes):
                raise TraceContractError("report tasks require every trace node to be frozen")
            uow.tasks.add_plan(
                run_id=run_id,
                plan_id=plan_id,
                objective="按调查类型 Markdown 模板生成结构化中文报告",
                allowed_agents=["report_composer"],
                allowed_capabilities=["compose_report"],
                budget={"phase": "report", "template_id": template_id, "template_version": template_version},
                stop_conditions=[],
                activate=False,
                definition={"report_id": report_id, "trace_id": trace_id, "template_id": template_id, "template_version": template_version, "chapter_ids": list(chapter_ids)},
            )
            existing_plan_task = uow.tasks.get_task(plan_task_id)
            retrying_plan = retry_failed and existing_plan_task is not None and existing_plan_task.get("status") == "failed"
            if retrying_plan:
                uow.tasks.refresh_failed_task_budget(plan_task_id, budget={})
            task_ids = [uow.tasks.add_task(
                run_id=run_id, plan_id=plan_id, task_id=plan_task_id,
                agent="report_composer", stage="report_plan", question="规划报告目录和章节重点",
                capability="compose_report", idempotency_key=f"{report_id}:report.plan",
                # Report tasks are not Trace-node tasks.  Keep report_id in
                # the Redis envelope for routing, but leave the relational
                # node foreign key empty because report_id is not a node_id.
                priority=0, node_id=None, slot_id="report_plan", node_revision=0,
                input_refs=[trace_id], input_data={"task_type": "report.plan", "phase": "plan", "report_id": report_id, "trace_id": trace_id, "template_id": template_id, "template_version": template_version},
            )]
            section_task_ids: list[str] = []
            retry_envelopes: list[dict[str, Any]] = []
            if retrying_plan:
                refreshed = uow.tasks.get_task(plan_task_id) or {}
                retry_envelopes.append({
                    "task_id": plan_task_id, "run_id": run_id, "node_id": report_id,
                    "slot_id": "report_plan", "task_type": "report.plan", "agent": "report_composer",
                    "revision": 0, "state_version": int(refreshed.get("state_version") or 0),
                    "idempotency_key": f"{report_id}:report.plan:retry:{int(refreshed.get('state_version') or 0)}",
                    "context_pack_hash": sha256(f"{trace_id}:report.plan:retry".encode("utf-8")).hexdigest(),
                    "enqueued_at": beijing_now().isoformat(),
                })
            for order, chapter_id in enumerate(chapter_ids, 1):
                task_id = f"rtask_{sha256((plan_id + ':section:' + chapter_id).encode('utf-8')).hexdigest()[:24]}"
                existing = uow.tasks.get_task(task_id)
                retrying = retry_failed and existing is not None and existing.get("status") == "failed"
                if retrying:
                    uow.tasks.refresh_failed_task_budget(
                        task_id,
                        budget={},
                    )
                task_ids.append(uow.tasks.add_task(
                    run_id=run_id, plan_id=plan_id, task_id=task_id,
                    agent="report_composer", stage="report_section", question=f"生成报告章节：{chapter_id}",
                    capability="compose_report", idempotency_key=f"{report_id}:report.section:{chapter_id}",
                    dependencies=[plan_task_id], priority=order, node_id=None, slot_id=chapter_id, node_revision=0,
                    input_refs=[trace_id], input_data={"task_type": "report.section", "phase": "section", "report_id": report_id, "trace_id": trace_id, "template_id": template_id, "template_version": template_version, "chapter_id": chapter_id, "order": order},
                ))
                section_task_ids.append(task_id)
                if retrying:
                    refreshed = uow.tasks.get_task(task_id) or {}
                    retry_envelopes.append({
                        "task_id": task_id, "run_id": run_id, "node_id": report_id,
                        "slot_id": chapter_id, "task_type": "report.section", "agent": "report_composer",
                        "revision": 0, "state_version": int(refreshed.get("state_version") or 0),
                        "idempotency_key": f"{report_id}:report.section:{chapter_id}:retry:{int(refreshed.get('state_version') or 0)}",
                        "context_pack_hash": sha256(f"{trace_id}:report.section:{chapter_id}:retry".encode("utf-8")).hexdigest(),
                        "enqueued_at": beijing_now().isoformat(),
                    })
            envelopes = []
            for task_id, task_type, slot_id, revision in [
                (plan_task_id, "report.plan", "report_plan", 0),
                *[(task_id, "report.section", chapter_id, 0) for task_id, chapter_id in zip(section_task_ids, chapter_ids)],
            ]:
                envelopes.append({
                    "task_id": task_id, "run_id": run_id, "node_id": report_id, "slot_id": slot_id,
                    "task_type": task_type, "agent": "report_composer", "revision": revision,
                    "state_version": 0, "idempotency_key": f"{report_id}:{task_type}:{slot_id}",
                    "context_pack_hash": sha256(f"{trace_id}:{task_type}:{slot_id}".encode('utf-8')).hexdigest(),
                    "enqueued_at": beijing_now().isoformat(),
                })
            outbox_id = uow.outbox.enqueue(
                aggregate_type="report", aggregate_id=report_id, event_type="report.plan",
                payload={"run_id": run_id, "report_id": report_id, "trace_id": trace_id, "tasks": envelopes},
                dedup_key=f"{report_id}:report.tasks",
            )
            for envelope in retry_envelopes:
                uow.outbox.enqueue(
                    aggregate_type="investigation_task", aggregate_id=envelope["task_id"],
                    event_type="node.task.redrive", payload=envelope,
                    dedup_key=envelope["idempotency_key"],
                )
            uow.runs.append_event(
                run_id=run_id, event_type="report.tasks.created", actor_type="orchestrator",
                payload={"report_id": report_id, "plan_id": plan_id, "task_ids": task_ids},
                idempotency_key=f"{report_id}:report.tasks",
            )
            return PlanSubmission(plan_id, tuple(task_ids), (outbox_id,))

    def get_node_plan(self, run_id: str) -> NodeTaskPlan | None:
        """Load the latest persisted fixed-template plan for process recovery."""
        with self.uow_factory() as uow:
            payload = uow.tasks.get_node_plan(run_id)
            return NodeTaskPlan.from_dict(payload) if payload is not None else None

    def start_task(self, *, task_id: str, worker_id: str | None = None, expected_state_version: int | None = None, lease_seconds: int = 60) -> dict[str, Any]:
        with self.uow_factory() as uow:
            current = uow.tasks.get_task(task_id)
            if current is not None:
                run = uow.runs.get(str(current.get("run_id") or ""))
                if run is not None and run.status == InvestigationStatus.PAUSED:
                    raise TaskDeferredError("investigation is paused")
            if current and current.get("status") == "failed" and str(current.get("stage") or "").startswith("node_"):
                uow.tasks.refresh_failed_task_budget(
                    task_id,
                    budget=_default_node_task_budget(
                        revision=int(current.get("node_revision") or 0),
                        agent=str(current.get("agent") or "researcher"),
                    ),
                )
            attempt = uow.tasks.start_attempt(
                task_id=task_id,
                worker_id=worker_id,
                expected_state_version=expected_state_version,
                lease_seconds=lease_seconds,
            )
            uow.outbox.enqueue(
                aggregate_type="investigation_task",
                aggregate_id=task_id,
                event_type="task.started",
                payload=dict(attempt),
                dedup_key=f"{task_id}:attempt:{attempt['attempt_no']}:started",
            )
            # The task repository has already validated the task's run.  The
            # event append resolves that run through the concrete repository;
            # the worker identity is supplied by the controller.
            uow.runs.append_event(
                run_id=attempt["run_id"],
                event_type="task.started",
                actor_type=worker_id or "worker",
                payload={"task_id": task_id, "attempt_id": attempt["attempt_id"], "attempt_no": attempt["attempt_no"]},
                idempotency_key=f"{attempt['attempt_id']}:started",
            )
            return attempt

    def heartbeat_attempt(self, *, attempt_id: str, worker_id: str | None = None, lease_seconds: int = 60) -> bool:
        with self.uow_factory() as uow:
            return uow.tasks.heartbeat_attempt(
                attempt_id=attempt_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.uow_factory() as uow:
            return uow.tasks.get_task(task_id)

    def get_run_status(self, run_id: str) -> str | None:
        with self.uow_factory() as uow:
            run = uow.runs.get(run_id)
            return str(run.status.value) if run is not None else None

    def defer_task(self, *, attempt_id: str, worker_id: str | None = None, reason: str = "investigation paused") -> bool:
        with self.uow_factory() as uow:
            attempt = uow.tasks.get_attempt(attempt_id)
            deferred = uow.tasks.defer_attempt(
                attempt_id=attempt_id,
                worker_id=worker_id,
                error_code="run_paused",
                error_summary=" ".join(str(reason).split())[:500],
            )
            if deferred and attempt is not None:
                uow.runs.append_event(
                    run_id=str(attempt.get("run_id") or ""),
                    event_type="task.deferred",
                    actor_type="worker",
                    payload={"task_id": attempt.get("task_id"), "attempt_id": attempt_id, "reason": "run_paused"},
                    idempotency_key=f"{attempt_id}:deferred",
                )
            return deferred

    def pause_run(self, *, run_id: str, reason: str, actor: str = "user"):
        message = " ".join(str(reason).split())[:500] or "paused"
        with self.uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status in {InvestigationStatus.COMPLETED, InvestigationStatus.CANCELLED}:
                raise ValueError("investigation cannot be paused")
            if run.status != InvestigationStatus.PAUSED:
                run.status = InvestigationStatus.PAUSED
                run.error = message
                run.interrupt_reason = message
                run.add_trace("pause", "completed", reason=message, actor=actor)
                uow.runs.save(run)
            return run

    def get_report_tasks(self, run_id: str) -> list[dict[str, Any]]:
        with self.uow_factory() as uow:
            return uow.tasks.list_report_tasks(run_id)

    def finish_task(
        self,
        *,
        attempt_id: str,
        status: str,
        error_code: str | None = None,
        error_summary: str | None = None,
        output_refs: list[str] | None = None,
        worker_id: str | None = None,
    ) -> bool:
        with self.uow_factory() as uow:
            attempt = uow.tasks.get_attempt(attempt_id)
            if attempt is None:
                return False
            run = uow.runs.get(str(attempt.get("run_id") or ""))
            if status == "succeeded" and run is not None and run.status == InvestigationStatus.PAUSED:
                uow.tasks.defer_attempt(
                    attempt_id=attempt_id,
                    worker_id=worker_id,
                    error_code="run_paused",
                    error_summary="investigation paused before task completion",
                )
                return False
            finished = uow.tasks.finish_attempt(
                attempt_id=attempt_id,
                status=status,
                error_code=error_code,
                error_summary=error_summary,
                output_refs=output_refs,
                worker_id=worker_id,
            )
            if not finished:
                return False
            # The outbox event is keyed by attempt, so consumers can reconcile
            # the task from the attempt payload without a second write.
            uow.outbox.enqueue(
                aggregate_type="investigation_task",
                aggregate_id=attempt["task_id"],
                event_type=f"task.{status}",
                payload={"attempt_id": attempt_id, "task_id": attempt["task_id"], "run_id": attempt["run_id"], "status": status, "error_code": error_code, "output_refs": list(output_refs or [])},
                dedup_key=f"{attempt_id}:{status}",
            )
            uow.runs.append_event(
                run_id=attempt["run_id"],
                event_type=f"task.{status}",
                actor_type="worker",
                payload={"task_id": attempt["task_id"], "attempt_id": attempt_id, "status": status},
                idempotency_key=f"{attempt_id}:{status}",
            )
            return True

    def pause_run_for_task_failure(self, *, run_id: str, task_id: str, reason: str) -> None:
        """Leave a durable, visible run state when a task reaches the DLQ."""
        message = " ".join(str(reason).split())[:500] or "role task exhausted its delivery budget"
        with self.uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None or run.status != InvestigationStatus.RUNNING:
                return
            run.status = type(run.status).PAUSED
            run.error = message
            run.interrupt_reason = message
            run.add_trace("role_task", "paused", task_id=task_id, error=message)
            uow.runs.save(run)
            uow.runs.append_event(
                run_id=run_id,
                event_type="role_task.dead_lettered",
                actor_type="worker",
                payload={"task_id": task_id, "error": message},
                idempotency_key=f"{task_id}:dead_lettered",
            )

    def recover_expired_tasks(self, *, limit: int = 100) -> tuple[str, ...]:
        """Release expired attempts and enqueue node-task redrive events."""
        recovered_ids: list[str] = []
        with self.uow_factory() as uow:
            for task in uow.tasks.recover_expired(limit=limit):
                recovered_ids.append(task["task_id"])
                full_task = uow.tasks.get_task(task["task_id"]) or task
                envelope = self._task_envelope(full_task)
                uow.outbox.enqueue(
                    aggregate_type="investigation_task", aggregate_id=task["task_id"],
                    event_type="node.task.redrive", payload={**envelope, "redriven_at": beijing_now().isoformat()},
                    dedup_key=f"{task['task_id']}:redrive:{task['state_version']}",
                )
                uow.runs.append_event(
                    run_id=task["run_id"], event_type="node.task.redrive", actor_type="recovery",
                    payload={"task_id": task["task_id"], "state_version": task["state_version"]},
                    idempotency_key=f"{task['task_id']}:redrive:{task['state_version']}",
                )
        return tuple(recovered_ids)

    def redrive_pending_tasks(self, *, limit: int = 100) -> tuple[str, ...]:
        """Rebuild task messages when Redis Streams were lost or flushed."""
        redriven: list[str] = []
        with self.uow_factory() as uow:
            for task in uow.tasks.list_pending(limit=limit):
                run = uow.runs.get(str(task.get("run_id") or ""))
                if run is not None and run.status != InvestigationStatus.RUNNING:
                    continue
                if uow.outbox.has_unpublished_task(task["task_id"]):
                    continue
                full_task = uow.tasks.get_task(task["task_id"]) or task
                if full_task.get("status") != "pending":
                    continue
                key = f"{task['task_id']}:redrive:{task['state_version']}"
                envelope = self._task_envelope(full_task)
                uow.outbox.enqueue(
                    aggregate_type="investigation_task", aggregate_id=task["task_id"],
                    event_type="node.task.redrive", payload=envelope, dedup_key=key,
                )
                uow.runs.append_event(
                    run_id=task["run_id"], event_type="node.task.redrive", actor_type="recovery",
                    payload={"task_id": task["task_id"], "reason": "redis_stream_rebuild"},
                    idempotency_key=key,
                )
                redriven.append(task["task_id"])
        return tuple(redriven)

    @staticmethod
    def _task_envelope(task: dict[str, Any]) -> dict[str, Any]:
        input_data = dict(task.get("input") or {})
        revision = int(task.get("node_revision") if task.get("node_revision") is not None else input_data.get("revision") or 0)
        stage = str(task.get("stage") or input_data.get("stage") or "node_research")
        if stage in {"report_plan", "report_section"}:
            task_type = str(input_data.get("task_type") or ("report.plan" if stage == "report_plan" else "report.section"))
            agent = "report_composer"
            node_id = str(input_data.get("report_id") or "")
        elif stage == "node_review":
            task_type = f"node.review.round_{max(1, revision)}"
            agent = "verifier"
            node_id = task.get("node_id") or input_data.get("node_id")
        else:
            task_type = "node.research.initial" if revision == 0 else f"node.research.revision_{revision}"
            agent = str(task.get("agent") or "researcher")
            node_id = task.get("node_id") or input_data.get("node_id")
        context_hash = sha256(json.dumps(input_data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return {
            "task_id": task.get("task_id"), "run_id": task.get("run_id"),
            "node_id": node_id,
            "slot_id": task.get("slot_id") or input_data.get("slot_id"),
            "task_type": task_type, "agent": agent, "revision": revision,
            "state_version": int(task.get("state_version") or 0),
            "idempotency_key": task.get("idempotency_key") or input_data.get("task_id"),
            "context_pack_hash": context_hash, "enqueued_at": beijing_now().isoformat(),
        }


__all__ = ["InvestigationTaskService", "PlanSubmission", "TaskNotReadyError"]
