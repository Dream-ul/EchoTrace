"""Controller for the fixed-template, node-driven Trace workflow.

The controller deliberately owns only workflow state.  Research and review
roles produce the node contracts; the repository is the recovery boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Protocol

from echotrace.agent.contracts import AgentResult
from echotrace.application.analysis_features import analyze_document
from echotrace.application.report_data import analysis_feature_datasets_for_node, research_data_needs
from echotrace.application.report_templates import load_report_template
from echotrace.domain.node_tasks import NodeResearchTask, NodeTaskPlan, node_task_id
from echotrace.domain.trace_nodes import (
    ConclusionIR,
    MAX_NODE_REVISIONS,
    NodeDraft,
    NodeRevision,
    NodeReview,
    TraceContractError,
    TraceTemplate,
    review_round_context,
)
from echotrace.domain.reporting import MetricObservation
from echotrace.agent.research_progress import default_research_policy

from .node_planner import NodeTaskPlanner


class NodeRepository(Protocol):
    def initialize_nodes(self, *, run_id: str, template: TraceTemplate) -> list[dict[str, Any]]: ...
    def list_nodes(self, run_id: str) -> list[dict[str, Any]]: ...
    def get_revision(self, node_id: str, revision_no: int | None = None) -> dict[str, Any] | None: ...
    def get_review(self, node_id: str, revision_no: int | None = None) -> dict[str, Any] | None: ...
    def save_revision(self, revision: NodeRevision) -> str: ...
    def save_review(self, review: NodeReview) -> str: ...
    def freeze_node(self, node_id: str) -> dict[str, Any]: ...
    def update_frozen_conclusion_ir(self, node_id: str, revision_no: int, conclusion_ir: ConclusionIR) -> str: ...


class NodeRoleRunner(Protocol):
    def __call__(self, role: str, objective: str, context: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class NodeControllerPolicy:
    max_role_attempts: int = 2


@dataclass(slots=True)
class NodeControllerResult:
    status: str
    plan: NodeTaskPlan
    nodes: tuple[dict[str, Any], ...]
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class NodeController:
    """Run node research and the fixed three-round editorial loop."""

    def __init__(
        self,
        *,
        repository: NodeRepository,
        planner: NodeTaskPlanner,
        researcher: NodeRoleRunner,
        verifier: NodeRoleRunner,
        formatter: NodeRoleRunner | None = None,
        available_capabilities: tuple[str, ...] | list[str],
        repository_factory: Callable[[], NodeRepository] | None = None,
        plan_loader: Callable[[str], NodeTaskPlan | None] | None = None,
        plan_saver: Callable[[NodeTaskPlan, TraceTemplate], Any] | None = None,
        analysis_feature_sink: Callable[..., Any] | None = None,
        analysis_feature_loader: Callable[[list[str]], list[dict[str, Any]]] | None = None,
        policy: NodeControllerPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.planner = planner
        self.researcher = researcher
        self.verifier = verifier
        self.formatter = formatter
        self.available_capabilities = tuple(available_capabilities)
        self.repository_factory = repository_factory
        self.plan_loader = plan_loader
        self.plan_saver = plan_saver
        self.analysis_feature_sink = analysis_feature_sink
        self.analysis_feature_loader = analysis_feature_loader
        self.policy = policy or NodeControllerPolicy()
        if self.policy.max_role_attempts < 1:
            raise ValueError("node controller policy limits must be positive")
        self._plans: dict[str, NodeTaskPlan] = {}

    def bind_run(self, run: Any) -> None:
        """Bind an active run to injected runners that expose audit sinks."""
        runners = [getattr(self.planner, "orchestrator", None), self.researcher, self.verifier, self.formatter]
        for runner in runners:
            binder = getattr(runner, "bind_run", None)
            if callable(binder):
                binder(run)

    def prepare(
        self,
        *,
        run_id: str,
        objective: str,
        template: TraceTemplate,
        input_refs: Mapping[str, tuple[str, ...] | list[str]] | None = None,
        execution_task_id: str | None = None,
    ) -> NodeControllerResult:
        """Initialize the fixed node plan without executing role calls."""
        events: list[dict[str, Any]] = []
        nodes = self.repository.list_nodes(run_id)
        if not nodes:
            nodes = self.repository.initialize_nodes(run_id=run_id, template=template)
        self._assert_node_set(nodes, template=template, run_id=run_id)
        node_ids = {row["slot_id"]: row["node_id"] for row in nodes}
        plan = self._plans.get(run_id)
        if plan is None:
            plan = self.plan_loader(run_id) if self.plan_loader is not None else None
            if plan is None:
                # Load current progress for resume
                current_drafts: dict[str, NodeDraft] = {}
                reviews: dict[str, NodeReview] = {}

                for node in nodes:
                    slot_id = node["slot_id"]
                    node_id = node["node_id"]
                    status = node.get("status")
                    current_rev = node.get("current_revision", 0)

                    # A node marked ``revise`` has a completed draft at its
                    # current revision and review feedback for the next one.
                    if status == "revise" and current_rev >= 0:
                        # Get the latest completed draft
                        draft_data = self.repository.get_revision(node_id, current_rev)
                        if draft_data and draft_data.get("draft"):
                            current_drafts[slot_id] = NodeDraft.from_dict(draft_data["draft"])

                        # Get the review for the next round (current_rev + 1)
                        review_data = self.repository.get_review(node_id, current_rev + 1)
                        if review_data and review_data.get("review"):
                            reviews[slot_id] = NodeReview.from_dict(review_data["review"])

                plan = self.planner.plan(
                    run_id=run_id,
                    objective=objective,
                    template=template,
                    node_ids=node_ids,
                    available_capabilities=self.available_capabilities,
                    input_refs=input_refs,
                    current_drafts=current_drafts,
                    reviews=reviews,
                    execution_task_id=execution_task_id,
                )
                if self.plan_saver is not None:
                    self.plan_saver(plan, template)
            else:
                plan.validate(
                    template=template,
                    node_ids=node_ids,
                    available_capabilities=self.available_capabilities,
                )
            self._plans[run_id] = plan
            events.append({"type": "node_plan.created", "plan_id": plan.plan_id})
        return NodeControllerResult("prepared", plan, tuple(nodes), events)

    def execute_research_task(
        self,
        *,
        plan: NodeTaskPlan,
        slot_id: str,
        revision: int,
        template: TraceTemplate,
        source_documents: list[dict[str, Any]],
        task_budget: Mapping[str, Any] | None = None,
    ) -> NodeDraft:
        """Execute and persist exactly one assigned Researcher revision."""
        repository = self._worker_repository()
        base_task = next((item for item in plan.tasks if item.slot_id == slot_id), None)
        if base_task is None:
            raise TraceContractError(f"node plan has no task for slot {slot_id}")
        existing = repository.get_revision(base_task.node_id, revision)
        if existing is not None:
            return NodeDraft.from_dict(existing["draft"])

        previous = repository.get_revision(base_task.node_id, revision - 1) if revision else None
        if revision and previous is None:
            raise TraceContractError(f"node {slot_id} is missing draft {revision - 1}")
        review = None
        if revision:
            review_row = repository.get_review(base_task.node_id, revision)
            if review_row is None:
                raise TraceContractError(f"node {slot_id} is missing review {revision}")
            review = NodeReview.from_dict(review_row["review"])
        task = NodeResearchTask(
            task_id=node_task_id(plan.run_id, template, base_task.node_id, slot_id, revision),
            node_id=base_task.node_id,
            slot_id=slot_id,
            revision=revision,
            question=base_task.question,
            capability=base_task.capability,
            aspects=base_task.aspects,
            preferred_sources=base_task.preferred_sources,
            depends_on_slot_ids=base_task.depends_on_slot_ids,
            input_refs=base_task.input_refs,
            priority=base_task.priority,
            review_feedback=review,
        )
        draft = self._research(
            task=task,
            template=template,
            current_draft=NodeDraft.from_dict(previous["draft"]) if previous else None,
            review=review,
            objective=plan.objective,
            run_id=plan.run_id,
            source_documents=source_documents,
            dependency_outputs=self._dependency_outputs(repository, task, plan),
            task_budget=task_budget,
        )
        repository.save_revision(NodeRevision(
            base_task.node_id,
            slot_id,
            revision,
            draft,
            review,
            revision - 1 if revision else None,
        ))
        return draft

    def reformat_frozen_revision(
        self,
        *,
        run_id: str,
        objective: str,
        template: TraceTemplate,
        slot_id: str,
        revision_no: int,
    ) -> ConclusionIR:
        """Refresh only the derived reader-facing IR of a frozen research revision."""

        repository = self._worker_repository()
        node = next((item for item in repository.list_nodes(run_id) if item.get("slot_id") == slot_id), None)
        if node is None or node.get("status") != "frozen" or node.get("frozen_revision") != revision_no:
            raise TraceContractError("only the currently frozen node revision can be reformatted")
        revision = repository.get_revision(str(node["node_id"]), revision_no)
        if revision is None or not isinstance(revision.get("draft"), Mapping):
            raise TraceContractError("frozen node revision has no persisted draft")
        if self.plan_loader is None:
            raise TraceContractError("reformatting a frozen revision requires its persisted node plan")
        plan = self.plan_loader(run_id)
        if plan is None:
            raise TraceContractError("frozen node revision has no persisted node plan")
        base_task = next((item for item in plan.tasks if item.slot_id == slot_id), None)
        if base_task is None:
            raise TraceContractError(f"node plan has no task for slot {slot_id}")
        review = None
        if revision_no:
            review_row = repository.get_review(str(node["node_id"]), revision_no)
            if review_row is None or not isinstance(review_row.get("review"), Mapping):
                raise TraceContractError("frozen node revision has no matching review")
            review = NodeReview.from_dict(review_row["review"])
        task = NodeResearchTask(
            task_id=node_task_id(run_id, template, str(node["node_id"]), slot_id, revision_no),
            node_id=str(node["node_id"]),
            slot_id=slot_id,
            revision=revision_no,
            question=base_task.question,
            capability=base_task.capability,
            aspects=base_task.aspects,
            preferred_sources=base_task.preferred_sources,
            depends_on_slot_ids=base_task.depends_on_slot_ids,
            input_refs=base_task.input_refs,
            priority=base_task.priority,
            review_feedback=review,
        )
        draft = NodeDraft.from_dict(revision["draft"])
        draft.conclusion_ir = None
        ir = self._format_conclusion(
            task=task,
            template=template,
            draft=draft,
            run_id=run_id,
            objective=objective,
        )
        repository.update_frozen_conclusion_ir(str(node["node_id"]), revision_no, ir)
        return ir

    def execute_review_task(
        self,
        *,
        plan: NodeTaskPlan,
        slot_id: str,
        review_round: int,
        execution_task_id: str,
        template: TraceTemplate,
        source_documents: list[dict[str, Any]],
        task_budget: Mapping[str, Any] | None = None,
    ) -> NodeReview:
        """Execute and persist exactly one assigned Verifier review."""
        repository = self._worker_repository()
        base_task = next((item for item in plan.tasks if item.slot_id == slot_id), None)
        if base_task is None:
            raise TraceContractError(f"node plan has no task for slot {slot_id}")
        existing = repository.get_review(base_task.node_id, review_round)
        if existing is not None:
            return NodeReview.from_dict(existing["review"])
        revision = repository.get_revision(base_task.node_id, review_round - 1)
        if revision is None:
            raise TraceContractError(f"node {slot_id} is missing draft {review_round - 1}")
        review = self._review(
            task=base_task,
            template=template,
            draft=NodeDraft.from_dict(revision["draft"]),
            revision=review_round,
            run_id=plan.run_id,
            objective=plan.objective,
            source_documents=source_documents,
            execution_task_id=execution_task_id,
            task_budget=task_budget,
        )
        repository.save_review(review)
        return review

    def freeze_node(self, node_id: str) -> dict[str, Any]:
        return self._worker_repository().freeze_node(node_id)

    @staticmethod
    def _assert_node_set(
        nodes: list[dict[str, Any]], *, template: TraceTemplate, run_id: str,
    ) -> None:
        """Stop instead of continuing with an in-memory plan after storage loss.

        A destructive maintenance command or an external database reset can
        remove the durable node rows while an Agent call is still running.
        Continuing would let the controller finish against stale in-memory
        state and write only a partial run snapshot.  The fixed Trace template
        gives us an exact identity set to verify at every wave boundary.
        """
        expected = {spec.slot_id for spec in template.node_specs}
        actual = {
            str(row.get("slot_id"))
            for row in nodes
            if isinstance(row, dict) and str(row.get("slot_id") or "").strip()
        }
        if actual != expected or len(nodes) != len(expected):
            raise TraceContractError(
                f"node storage integrity lost for run {run_id}: "
                f"expected {len(expected)} template nodes, found {len(nodes)}"
            )

    def _worker_repository(self) -> NodeRepository:
        return self.repository_factory() if self.repository_factory is not None else self.repository

    def _research(
        self,
        *,
        task: NodeResearchTask,
        template: TraceTemplate,
        current_draft: NodeDraft | None,
        review: NodeReview | None,
        objective: str,
        run_id: str,
        source_documents: list[dict[str, Any]],
        dependency_outputs: list[dict[str, Any]],
        task_budget: Mapping[str, Any] | None = None,
    ) -> NodeDraft:
        spec = template.node_spec(task.slot_id)
        report_template = load_report_template(template.investigation_type)
        report_data_needs = research_data_needs(report_template, task.slot_id)
        existing_features: list[dict[str, Any]] = []
        if self.analysis_feature_loader is not None:
            existing_features = self.analysis_feature_loader([
                str(item.get("document_id") or "")
                for item in source_documents if isinstance(item, Mapping)
            ])
        source_analysis_features = self._source_analysis_features(
            source_documents, target=spec.title, existing_features=existing_features,
        )
        if self.analysis_feature_sink is not None:
            for feature in source_analysis_features:
                self.analysis_feature_sink(
                    feature,
                    run_id=run_id,
                    document_id=str(feature.get("document_id") or ""),
                )
        revision_focus = review_round_context(spec, task.revision) if task.revision else {
            "round": 0,
            "axis": "initial_research",
            "label": "节点初稿",
            "objective": "依据节点目标建立完整的信息版图，并形成可复核的主张、证据和综合结论。",
            "rubric": [],
            "feedback_fields": [],
        }
        research_policy = default_research_policy(task.revision)
        context = {
            "run_id": run_id,
            "execution_task_id": task.task_id,
            "objective": objective,
            "trace_template": template.to_dict(),
            "node_spec": spec.to_dict(),
            "report_data_needs": report_data_needs,
            "task": task.to_dict(),
            "current_draft": current_draft.to_dict() if current_draft else None,
            "review_feedback": review.to_dict() if review else None,
            "review_focus": revision_focus,
            "input_refs": list(task.input_refs),
            # The live tool gateway appends documents discovered during this
            # task to the context list. Keep the same task-local list here so
            # post-call evidence validation sees those newly persisted IDs.
            "source_documents": source_documents if source_documents is not None else [],
            "source_analysis_features": source_analysis_features,
            "dependency_node_outputs": dependency_outputs,
            "research_policy": research_policy,
            "task_budget": dict(task_budget or {}),
        }
        validated_draft: NodeDraft | None = None

        def validate_research(raw: Any) -> None:
            nonlocal validated_draft
            payload = raw.answer if isinstance(raw, AgentResult) else raw
            value = payload.to_dict() if isinstance(payload, NodeDraft) else self._json_object(payload)
            value.update({"node_id": task.node_id, "slot_id": task.slot_id, "revision": task.revision})
            draft = self._normalize_draft(value, task)
            available_document_ids = {
                str(item.get("document_id"))
                for item in source_documents
                if isinstance(item, Mapping) and str(item.get("document_id") or "").strip()
            }
            unknown_document_ids = {
                evidence.document_id for evidence in draft.evidence
                if available_document_ids and evidence.document_id not in available_document_ids
            }
            if unknown_document_ids:
                raise TraceContractError(
                    "evidence references unavailable source documents: "
                    + ", ".join(sorted(unknown_document_ids)[:8])
                )
            validated_draft = draft

        context["_result_validator"] = validate_research
        raw = self._call("researcher", "依据节点目标和本轮反馈研究并填写指定节点。", context)
        if isinstance(raw, NodeDraft):
            value = raw.to_dict()
        else:
            value = self._json_object(raw)
        if validated_draft is None:
            raise TraceContractError("researcher output was not validated")
        draft = validated_draft
        if source_documents:
            referenced_documents = {item.document_id for item in draft.evidence}
            draft.analysis_features = [
                feature for feature in source_analysis_features
                if feature.get("document_id") in referenced_documents
            ]
            derived = analysis_feature_datasets_for_node(
                slot_id=task.slot_id,
                requirements=[item for item in report_data_needs if item.get("collection_mode") == "analysis_feature"],
                analysis_features=draft.analysis_features,
                evidence=[item.to_dict() for item in draft.evidence],
            )
            existing_ids = {item.dataset_id for item in draft.datasets}
            draft.datasets.extend(item for item in derived if item.dataset_id not in existing_ids)
        if self.formatter is not None:
            draft.conclusion_ir = self._format_conclusion(
                task=task,
                template=template,
                draft=draft,
                run_id=run_id,
                objective=objective,
                task_budget=task_budget,
            )
        return draft

    def _format_conclusion(
        self,
        *,
        task: NodeResearchTask,
        template: TraceTemplate,
        draft: NodeDraft,
        run_id: str,
        objective: str,
        task_budget: Mapping[str, Any] | None = None,
    ) -> ConclusionIR:
        """Turn the research conclusion into a bounded presentation IR."""

        context = {
            "run_id": run_id,
            "objective": objective,
            "node_task": task.to_dict(),
            "trace_template": template.to_dict(),
            "node_spec": template.node_spec(task.slot_id).to_dict(),
            "current_draft": draft.to_dict(),
            "conclusion": draft.conclusion,
            "claims": [claim.to_dict() for claim in draft.claims],
            "evidence": [evidence.to_dict() for evidence in draft.evidence],
            "media": [media.to_dict() for media in draft.media],
            "task_budget": dict(task_budget or {}),
        }

        def validate_formatter(raw: Any) -> None:
            payload = raw.answer if isinstance(raw, AgentResult) else raw
            value = self._json_object(payload)
            expected = {"node_id", "slot_id", "revision", "conclusion_ir"}
            if set(value) != expected:
                raise TraceContractError(
                    "conclusion_formatter output must contain exactly node_id, slot_id, revision and conclusion_ir"
                )
            identity = (value.get("node_id"), value.get("slot_id"), value.get("revision"))
            expected_identity = (task.node_id, task.slot_id, task.revision)
            if identity != expected_identity:
                raise TraceContractError("conclusion_formatter identity does not match the assigned node task")
            ir = ConclusionIR.from_dict(value["conclusion_ir"])
            validate_conclusion_ir_content(draft.conclusion, ir)

        raw = self._call(
            "conclusion_formatter",
            "将节点综合结论编辑为清晰、面向用户的展示用 conclusion_ir。",
            context,
            validate=validate_formatter,
        )
        value = self._json_object(raw)
        ir = ConclusionIR.from_dict(value["conclusion_ir"])
        # The application owns provenance for this derived presentation view.
        ir.source_hash = sha256(draft.conclusion.encode("utf-8")).hexdigest()
        return ir

    @staticmethod
    def _dependency_outputs(
        repository: NodeRepository,
        task: NodeResearchTask,
        plan: NodeTaskPlan,
    ) -> list[dict[str, Any]]:
        """Load frozen direct dependencies for the next node researcher.

        Dependencies are scheduling edges, but they also carry the preceding
        node's approved synthesis into the dependent node.  Passing only the
        direct dependencies keeps the prompt bounded and prevents unrelated
        nodes from becoming an implicit global context.
        """
        task_by_slot = {item.slot_id: item for item in plan.tasks}
        outputs: list[dict[str, Any]] = []
        for slot_id in task.depends_on_slot_ids:
            dependency = task_by_slot.get(slot_id)
            if dependency is None:
                continue
            row = repository.get_revision(dependency.node_id)
            if not row:
                continue
            draft = NodeDraft.from_dict(row["draft"])
            outputs.append({
                "node_id": dependency.node_id,
                "slot_id": dependency.slot_id,
                "revision": int(row.get("revision", draft.revision)),
                "draft": draft.to_dict(),
            })
        return outputs

    def _review(
        self,
        *,
        task: NodeResearchTask,
        template: TraceTemplate,
        draft: NodeDraft,
        revision: int,
        run_id: str,
        objective: str,
        source_documents: list[dict[str, Any]] | None = None,
        execution_task_id: str | None = None,
        task_budget: Mapping[str, Any] | None = None,
    ) -> NodeReview:
        context = {
            "run_id": run_id,
            "objective": objective,
            "execution_task_id": execution_task_id,
            "trace_template": template.to_dict(),
            "node_spec": template.node_spec(task.slot_id).to_dict(),
            "node_task": task.to_dict(),
            "current_draft": draft.to_dict(),
            "source_documents": list(source_documents or ()),
            "review_round": revision,
            "review_focus": review_round_context(template.node_spec(task.slot_id), revision),
            "task_budget": dict(task_budget or {}),
        }
        def validate_review(raw: Any) -> None:
            payload = raw.answer if isinstance(raw, AgentResult) else raw
            review_value = payload.to_dict() if isinstance(payload, NodeReview) else self._json_object(payload)
            expected_status = "finalize" if revision == MAX_NODE_REVISIONS else "revise"
            if review_value.get("status") != expected_status:
                raise TraceContractError(
                    f"verifier review status for assigned round {revision} must be {expected_status}"
                )

        raw = self._call(
            "verifier",
            "按本轮固定审稿方向审阅指定节点，并给出可执行的编辑反馈。",
            context,
            validate=validate_review,
        )
        value = raw.to_dict() if isinstance(raw, NodeReview) else self._json_object(raw)
        expected_status = "finalize" if revision == MAX_NODE_REVISIONS else "revise"
        if value.get("status") != expected_status:
            raise TraceContractError(
                f"verifier review status for assigned round {revision} must be {expected_status}"
            )
        # Identity belongs to the assigned task. Keep the model's editorial
        # fields, but prevent a stale or copied ID from blocking persistence.
        value["node_id"] = task.node_id
        value["slot_id"] = task.slot_id
        value["revision"] = revision
        value["review_id"] = self._stable_id("nreview", task.node_id, str(revision))
        return NodeReview.from_dict(value)

    def _call(
        self,
        role: str,
        objective: str,
        context: dict[str, Any],
        *,
        validate: Callable[[Any], None] | None = None,
    ) -> Any:
        if validate is None:
            candidate = context.pop("_result_validator", None)
            validate = candidate if callable(candidate) else None
        last: Exception | None = None
        retry_feedback = ""
        for attempt in range(self.policy.max_role_attempts):
            try:
                if role == "researcher":
                    attempt_objective = objective
                    if retry_feedback:
                        attempt_objective = (
                            f"{objective}\n\n上一轮输出未通过结构校验：{retry_feedback}。"
                            "请只修正指出的结构问题，保留已有内容和证据，不要从头重做或重复已完成的搜索。"
                        )
                    raw = self.researcher(role, attempt_objective, context)
                elif role == "verifier":
                    raw = self.verifier(role, objective, context)
                elif role == "conclusion_formatter" and self.formatter is not None:
                    raw = self.formatter(role, objective, context)
                else:
                    raise TraceContractError(f"unsupported node role: {role}")
                if isinstance(raw, AgentResult) and raw.status != "completed":
                    raise TraceContractError(f"{role} did not complete: {raw.status}")
                if validate is not None:
                    validate(raw)
                return raw.answer if isinstance(raw, AgentResult) else raw
            except Exception as exc:  # retry the same round; never advance revision
                last = exc
                if getattr(exc, "retryable", True) is False:
                    raise
                if attempt + 1 < self.policy.max_role_attempts and isinstance(
                    exc, (TraceContractError, ValueError, TypeError, KeyError),
                ):
                    retry_feedback = " ".join(str(exc).split())[:1200]
        raise last or RuntimeError(f"{role} failed")

    @staticmethod
    def _json_object(raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            try:
                # Node role answers are nested inside the native
                # agent_control JSON string.  Allow literal control
                # characters in prose fields while retaining all structural
                # and source-grounding validation below.
                raw = json.loads(raw, strict=False)
            except json.JSONDecodeError as exc:
                raise TraceContractError("node role output is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise TraceContractError("node role output must be an object")
        return dict(raw)

    @classmethod
    def _normalize_draft(cls, value: dict[str, Any], task: NodeResearchTask) -> NodeDraft:
        claims = [dict(item) for item in value.get("claims", [])]
        evidence = [dict(item) for item in value.get("evidence", [])]
        media = [dict(item) for item in value.get("media", [])]
        metrics = [dict(item) for item in value.get("metrics", [])]
        datasets = [dict(item) for item in value.get("datasets", [])]
        # Give missing model IDs deterministic placeholders before building
        # cross-record maps. Models occasionally omit an ID on one evidence
        # item while still referring to that item from a claim.
        for index, item in enumerate(claims):
            item.setdefault("claim_id", f"model-claim-{index}")
        for index, item in enumerate(evidence):
            item.setdefault("evidence_id", f"model-evidence-{index}")
        claim_new_ids = [
            cls._stable_id("nclaim", task.node_id, str(task.revision), str(index), str(item.get("claim_id") or ""))
            for index, item in enumerate(claims)
        ]
        evidence_new_ids = [
            cls._stable_id("nevidence", task.node_id, str(task.revision), str(index), str(item.get("evidence_id") or ""))
            for index, item in enumerate(evidence)
        ]
        claim_map: dict[str, str] = {}
        for index, item in enumerate(claims):
            claim_map.setdefault(str(item.get("claim_id")), claim_new_ids[index])
        evidence_map: dict[str, str] = {}
        for index, item in enumerate(evidence):
            evidence_map.setdefault(str(item.get("evidence_id")), evidence_new_ids[index])
        claim_refs_by_evidence: dict[str, list[str]] = {}
        for item in claims:
            claim_id = str(item.get("claim_id"))
            for evidence_id in item.get("evidence_ids", []):
                claim_refs_by_evidence.setdefault(str(evidence_id), []).append(claim_id)
        for index, item in enumerate(claims):
            old = str(item.get("claim_id"))
            item["claim_id"] = claim_new_ids[index]
            item["evidence_ids"] = [evidence_map.get(str(ref), str(ref)) for ref in item.get("evidence_ids", [])]
        for index, item in enumerate(evidence):
            old_evidence_id = str(item.get("evidence_id"))
            item["evidence_id"] = evidence_new_ids[index]
            old_claim_id = str(item.get("claim_id"))
            inferred_claim_ids = claim_refs_by_evidence.get(old_evidence_id, [])
            item["claim_id"] = claim_map.get(old_claim_id) or (
                claim_map.get(inferred_claim_ids[0]) if inferred_claim_ids else next(iter(claim_map.values()))
            )
        # Repair non-empty but stale evidence references emitted by a model.
        # Every claim must retain at least one real evidence row; otherwise the
        # domain contract cannot establish a source-document projection.
        evidence_by_id = {str(item["evidence_id"]): item for item in evidence}
        unused_evidence = list(evidence_by_id)
        for claim in claims:
            claim_id = str(claim["claim_id"])
            linked_ids = [str(ref) for ref in claim.get("evidence_ids", []) if str(ref) in evidence_by_id]
            if not linked_ids:
                linked_ids = [
                    evidence_id for evidence_id, item in evidence_by_id.items()
                    if str(item.get("claim_id")) == claim_id and evidence_id in unused_evidence
                ]
            if not linked_ids:
                # Do not attach an unrelated source merely to satisfy the
                # shape contract. A claim whose cited evidence was filtered
                # out (for example an internal search log) must be omitted;
                # the next bounded role attempt can then produce a grounded
                # claim against the supplied source set.
                claim["evidence_ids"] = []
                continue
            claim["evidence_ids"] = linked_ids
            for evidence_id in linked_ids:
                evidence_by_id[evidence_id]["claim_id"] = claim_id
                if evidence_id in unused_evidence:
                    unused_evidence.remove(evidence_id)
        claims = [item for item in claims if item.get("evidence_ids")]
        surviving_claim_ids = {str(item["claim_id"]) for item in claims}
        evidence = [item for item in evidence if str(item.get("claim_id")) in surviving_claim_ids]
        evidence_by_id = {str(item["evidence_id"]): item for item in evidence}
        for item in claims:
            # Source-document IDs are a projection of the evidence links. A
            # revision may replace evidence while the model carries forward
            # the previous projection; derive it again before the strict
            # NodeDraft contract is applied.
            item["source_document_ids"] = sorted(
                {
                    str(evidence_by_id[evidence_id]["document_id"])
                    for evidence_id in item["evidence_ids"]
                    if evidence_id in evidence_by_id
                }
            )
        for index, item in enumerate(media):
            item["media_id"] = cls._stable_id("nmedia", task.node_id, str(task.revision), str(index), str(item.get("media_id") or ""))
        # Evidence IDs are rewritten to stable application IDs above. Metrics
        # are optional model output, but their evidence links must follow the
        # same rewrite or a valid metric can be rejected as ungrounded. When a
        # model omits the temporary evidence key, use a uniquely matching
        # source document and locator; ambiguous or unmatched references stay
        # invalid and are handled by the normal bounded role retry.
        evidence_by_source: dict[str, list[dict[str, Any]]] = {}
        evidence_by_id = {str(item.get("evidence_id") or ""): item for item in evidence}
        for item in evidence:
            evidence_by_source.setdefault(str(item.get("document_id") or ""), []).append(item)
        for metric in metrics:
            reference = str(metric.get("evidence_id") or "")
            if reference in evidence_map:
                reference = evidence_map[reference]
                metric["evidence_id"] = reference
            matched_evidence = evidence_by_id.get(reference)
            if matched_evidence is not None:
                # The source document is a projection of the cited evidence;
                # revisions can replace the evidence while retaining a stale
                # source_document_id from the previous draft.
                metric["source_document_id"] = str(matched_evidence.get("document_id") or "")
                continue
            source_document_id = str(metric.get("source_document_id") or "")
            candidates = evidence_by_source.get(source_document_id, [])
            metric_locator = metric.get("locator")
            if isinstance(metric_locator, Mapping):
                metric_url = str(metric_locator.get("url") or "").strip()
                if metric_url:
                    url_matches = [
                        item for item in candidates
                        if isinstance(item.get("locator"), Mapping)
                        and str(item["locator"].get("url") or "").strip() == metric_url
                    ]
                    if len(url_matches) == 1:
                        candidates = url_matches
            if len(candidates) == 1:
                metric["evidence_id"] = str(candidates[0]["evidence_id"])
                metric["source_document_id"] = str(candidates[0].get("document_id") or "")
        for index, dataset in enumerate(datasets):
            if not isinstance(dataset.get("rows"), list):
                raise TraceContractError(
                    f"datasets[{index}].rows must be a list; omit the dataset when no source-backed rows are available"
                )
            if isinstance(dataset.get("rows"), list):
                dataset["rows"] = [dict(row) if isinstance(row, Mapping) else row for row in dataset["rows"]]
            # Models sometimes emit one limitation as a scalar even though
            # the domain contract intentionally uses a list. Preserve the
            # wording while normalizing this optional collection boundary.
            limitations = dataset.get("limitations")
            if isinstance(limitations, str):
                dataset["limitations"] = [limitations]
            elif limitations is None:
                dataset["limitations"] = []
            citations = dataset.get("citation_ids")
            if isinstance(citations, list):
                dataset["citation_ids"] = [evidence_map.get(str(item), str(item)) for item in citations]
            for row in dataset.get("rows", []) if isinstance(dataset.get("rows"), list) else ():
                if not isinstance(row, Mapping):
                    continue
                row_citations = row.get("citation_ids")
                if isinstance(row_citations, list):
                    row["citation_ids"] = [evidence_map.get(str(item), str(item)) for item in row_citations]
        # Metrics are optional report enrichments.  A model can produce a
        # useful, fully grounded NodeDraft while one optional metric is
        # malformed (for example ``direct_or_derived=derived`` without its
        # required formula).  Do not discard the whole node in that case:
        # retain only metrics that satisfy the domain contract and remove
        # their references from datasets.  Claims and evidence remain the
        # authoritative research result.
        valid_metrics: list[dict[str, Any]] = []
        valid_metric_ids: set[str] = set()
        for metric in metrics:
            try:
                normalized_metric = MetricObservation.from_dict(metric).to_dict()
            except (TraceContractError, TypeError, ValueError):
                continue
            valid_metrics.append(normalized_metric)
            valid_metric_ids.add(str(normalized_metric["metric_id"]))
        metrics = valid_metrics
        for dataset in datasets:
            if isinstance(dataset.get("input_metric_ids"), list):
                dataset["input_metric_ids"] = [
                    str(metric_id)
                    for metric_id in dataset["input_metric_ids"]
                    if str(metric_id) in valid_metric_ids
                ]
        # This structure is derived by the dedicated formatter after the
        # researcher draft is normalized and checked against its sources.
        value.pop("conclusion_ir", None)
        value.update({"node_id": task.node_id, "slot_id": task.slot_id, "revision": task.revision,
                      "claims": claims, "evidence": evidence, "media": media,
                      "metrics": metrics, "datasets": datasets})
        return NodeDraft.from_dict(value)

    @staticmethod
    def _source_analysis_features(
        source_documents: list[dict[str, Any]], *, target: str,
        existing_features: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Derive bounded descriptive features from the supplied source records.

        These features are calculated from source text before the model sees the
        node context. They describe the sample and never replace claim evidence.
        """
        features: list[dict[str, Any]] = list(existing_features or [])
        expected_types = {"time_mentions", "entity_mentions", "sentiment", "stance", "topic"}
        existing_types: dict[str, set[str]] = {}
        for item in features:
            if isinstance(item, Mapping):
                existing_types.setdefault(str(item.get("document_id") or ""), set()).add(
                    str(item.get("feature_type") or "")
                )
        for raw in source_documents:
            if not isinstance(raw, Mapping):
                continue
            document_id = str(raw.get("document_id") or "").strip()
            content = str(raw.get("content") or "").strip()
            if not document_id or not content:
                continue
            missing_types = expected_types - existing_types.get(document_id, set())
            if not missing_types:
                continue
            document = SimpleNamespace(
                document_id=document_id,
                content=content,
                content_hash=str(raw.get("content_hash") or ""),
                metadata=dict(raw.get("metadata") or {}) if isinstance(raw.get("metadata"), Mapping) else {},
                published_at=None,
                fetched_at=None,
            )
            features.extend(
                item for item in analyze_document(document, target=target)
                if str(item.get("feature_type") or "") in missing_types
            )
        return features

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
        return f"{prefix}_{digest}"

__all__ = ["NodeController", "NodeControllerPolicy", "NodeControllerResult", "validate_conclusion_ir_content"]


def validate_conclusion_ir_content(conclusion: str, ir: ConclusionIR) -> None:
    """Check reader-facing text for implementation artifacts, not verbatim overlap."""

    if not str(conclusion or "").strip():
        raise TraceContractError("conclusion_formatter requires a non-empty node conclusion")
    displayed: list[str] = []
    for block in ir.blocks:
        block_type = str(block.get("type") or "")
        if block_type in {"heading", "paragraph"}:
            displayed.append(str(block.get("text") or ""))
        elif block_type == "list":
            displayed.extend(str(item) for item in block.get("items", ()))
        elif block_type == "table":
            if block.get("title"):
                displayed.append(str(block["title"]))
            displayed.extend(str(item) for item in block.get("columns", ()))
            displayed.extend(str(cell) for row in block.get("rows", ()) for cell in row)

    content = "\n".join(displayed)
    if not content.strip():
        raise TraceContractError("conclusion_formatter produced no reader-facing text")
    internal_reference = re.compile(
        r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b|"
        r"\b(?:discussion_trend|report_data_needs|limitations)\b|"
        r"\b(?:n?claim|nevidence|evidence|document|doc|node|run|task|metric|dataset|citation|media|feature)_[A-Za-z0-9-]+\b",
        re.IGNORECASE,
    )
    match = internal_reference.search(content)
    if match:
        raise TraceContractError(
            f"conclusion_formatter output contains an internal identifier or field name: {match.group(0)}"
        )
