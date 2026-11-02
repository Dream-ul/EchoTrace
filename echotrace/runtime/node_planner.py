"""Constrain Orchestrator proposals to a fixed Trace template and node targets."""

from __future__ import annotations

import json
from typing import Any, Mapping

from echotrace.agent.contracts import AgentResult
from echotrace.application.report_data import research_data_needs
from echotrace.application.report_templates import load_report_template
from echotrace.domain.node_tasks import NodeResearchTask, NodeTaskPlan, node_task_id
from echotrace.domain.trace_nodes import NodeDraft, NodeReview, TraceContractError, TraceTemplate

from .contracts import RoleRunner


class NodeTaskPlanner:
    def __init__(self, orchestrator: RoleRunner, *, max_parallel_tasks: int = 3) -> None:
        if type(max_parallel_tasks) is not int or max_parallel_tasks < 1:
            raise ValueError("max_parallel_tasks must be a positive integer")
        self.orchestrator = orchestrator
        self.max_parallel_tasks = max_parallel_tasks

    def plan(
        self, *, run_id: str, objective: str, template: TraceTemplate,
        node_ids: Mapping[str, str], available_capabilities: tuple[str, ...] | list[str],
        current_drafts: Mapping[str, NodeDraft] | None = None,
        reviews: Mapping[str, NodeReview] | None = None,
        input_refs: Mapping[str, tuple[str, ...] | list[str]] | None = None,
        execution_task_id: str | None = None,
    ) -> NodeTaskPlan:
        drafts = dict(current_drafts or {})
        feedback = dict(reviews or {})
        refs = dict(input_refs or {})
        slots = {spec.slot_id for spec in template.node_specs}
        if set(node_ids) != slots or len(set(node_ids.values())) != len(slots):
            raise TraceContractError("node identities must cover every template slot exactly once")
        if any(not isinstance(value, str) or not value.strip() for value in node_ids.values()):
            raise TraceContractError("node identities must be non-empty strings")
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(objective, str) or not objective.strip():
            raise TraceContractError("node planning requires run_id and objective")
        if not isinstance(available_capabilities, (list, tuple)) or not available_capabilities or any(not isinstance(cap, str) or not cap.strip() for cap in available_capabilities):
            raise TraceContractError("node planning requires available capabilities")
        if not (set(drafts) | set(feedback) | set(refs)).issubset(slots):
            raise TraceContractError("node planning context references unknown template slots")
        if set(drafts) != set(feedback):
            raise TraceContractError("targeted revision planning requires a draft for every review")
        for slot, values in refs.items():
            if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise TraceContractError(f"input_refs for {slot} must be an array of non-empty strings")
        drafts = {slot: NodeDraft.from_dict(draft.to_dict()) for slot, draft in drafts.items()}
        feedback = {slot: NodeReview.from_dict(review.to_dict()) for slot, review in feedback.items()}
        targets = {slot: 0 for slot in slots} if not feedback else {slot: review.revision for slot, review in feedback.items()}
        report_template = load_report_template(template.investigation_type)
        for slot, review in feedback.items():
            draft = drafts[slot]
            if (review.node_id, review.slot_id) != (node_ids[slot], slot):
                raise TraceContractError("review feedback belongs to a different node")
            if (draft.node_id, draft.slot_id, draft.revision) != (node_ids[slot], slot, review.revision - 1):
                raise TraceContractError("targeted revision must follow its current draft exactly once")
        node_targets = []
        for spec in sorted(template.node_specs, key=lambda item: item.order):
            if spec.slot_id not in targets:
                continue
            node_targets.append({
                "node_id": node_ids[spec.slot_id], "slot_id": spec.slot_id,
                "revision": targets[spec.slot_id], "node_spec": spec.to_dict(),
                "current_draft": drafts[spec.slot_id].to_dict() if spec.slot_id in drafts else None,
                "review_feedback": feedback[spec.slot_id].to_dict() if spec.slot_id in feedback else None,
                "input_refs": list(refs.get(spec.slot_id, ())),
                "report_data_needs": research_data_needs(report_template, spec.slot_id),
            })
        context = {
            "run_id": run_id, "execution_task_id": execution_task_id,
            "objective": objective, "investigation_type": template.investigation_type,
            "trace_template": template.to_dict(), "node_targets": node_targets,
            "available_capabilities": sorted(set(available_capabilities)),
            "budget": {"max_parallel_tasks": self.max_parallel_tasks},
        }
        raw = self.orchestrator("orchestrator", "Plan research for exactly the assigned Trace node slots.", context)
        if isinstance(raw, AgentResult):
            if raw.status != "completed":
                raise TraceContractError(f"node orchestrator did not complete: {raw.status}")
            raw = raw.answer
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TraceContractError("node orchestrator output is not valid JSON") from exc
        if not isinstance(raw, dict) or set(raw) != {"template_id", "template_version", "tasks"}:
            raise TraceContractError("node orchestrator must return template_id, template_version and tasks only")
        if (raw["template_id"], raw["template_version"]) != (template.template_id, template.template_version):
            raise TraceContractError("node orchestrator cannot change the pinned template")
        if not isinstance(raw["tasks"], list):
            raise TraceContractError("node orchestrator tasks must be an array")
        # Validate that orchestrator returned exactly one task per target slot
        proposed_slots = [item.get("slot_id") for item in raw["tasks"] if isinstance(item, dict)]
        if len(proposed_slots) != len(set(proposed_slots)):
            raise TraceContractError("node orchestrator returned duplicate slot_id entries")
        if set(proposed_slots) != set(targets.keys()):
            raise TraceContractError("node orchestrator tasks must cover exactly the assigned target slots")
        tasks = []
        for item in raw["tasks"]:
            required = {"slot_id", "question", "capability", "priority"}
            if not isinstance(item, dict) or not required.issubset(item) or set(item) - required - {"depends_on_slot_ids"}:
                raise TraceContractError("node task proposal has missing or unsupported fields")
            slot = item["slot_id"]
            if not isinstance(slot, str) or slot not in targets:
                raise TraceContractError("node task proposal references an unassigned slot")
            spec = template.node_spec(slot)
            deps = item.get("depends_on_slot_ids", [])
            if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
                raise TraceContractError("proposed dependencies must be an array of slot IDs")
            tasks.append(NodeResearchTask(
                task_id=node_task_id(run_id, template, node_ids[slot], slot, targets[slot]),
                node_id=node_ids[slot], slot_id=slot, revision=targets[slot],
                question=item["question"], capability=item["capability"], priority=item["priority"],
                aspects=spec.aspects, preferred_sources=spec.preferred_sources,
                depends_on_slot_ids=tuple(dict.fromkeys((*spec.depends_on_slot_ids, *deps))),
                input_refs=tuple(refs.get(slot, ())), review_feedback=feedback.get(slot),
            ))
        plan = NodeTaskPlan(
            run_id=run_id, objective=objective, template_id=template.template_id,
            template_version=template.template_version,
            tasks=tuple(sorted(tasks, key=lambda task: template.node_spec(task.slot_id).order)),
            max_parallel_tasks=self.max_parallel_tasks,
        )
        plan.validate(template=template, node_ids=node_ids, available_capabilities=available_capabilities, target_revisions=targets)
        return plan

    def classify(
        self,
        *,
        run_id: str,
        objective: str,
        event_title: str,
        subject: str | None,
        source_documents: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        candidates: tuple[TraceTemplate, ...] | list[TraceTemplate],
        execution_task_id: str | None = None,
    ) -> dict[str, str]:
        """Ask the existing orchestrator to choose a registered template.

        Classification deliberately uses the same role runner as node planning;
        production runners therefore apply the same model, contract retry, and
        audit behavior to both requests.
        """
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(objective, str) or not objective.strip():
            raise TraceContractError("investigation classification requires run_id and objective")
        if not isinstance(event_title, str) or not event_title.strip():
            raise TraceContractError("investigation classification requires event_title")
        candidate_list = tuple(candidates)
        if not candidate_list:
            raise TraceContractError("no investigation templates are registered")
        allowed = {item.investigation_type for item in candidate_list}
        if len(allowed) != len(candidate_list):
            raise TraceContractError("investigation template types must be unique")
        documents = [dict(item) for item in source_documents if isinstance(item, Mapping)]
        context = {
            "classification": True,
            "run_id": run_id,
            "execution_task_id": execution_task_id,
            "objective": objective,
            "event_title": event_title,
            "subject": subject,
            "source_documents": documents[:12],
            "investigation_type_candidates": [
                {
                    "investigation_type": item.investigation_type,
                    "display_name": item.display_name,
                    "purpose": item.purpose,
                }
                for item in candidate_list
            ],
        }
        raw = self.orchestrator(
            "orchestrator",
            "根据调查目标和来源材料选择一个最匹配的调查类别。",
            context,
        )
        if isinstance(raw, AgentResult):
            if raw.status != "completed":
                raise TraceContractError(f"classification orchestrator did not complete: {raw.status}")
            raw = raw.answer
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TraceContractError("classification orchestrator output is not valid JSON") from exc
        if not isinstance(raw, dict) or set(raw) != {"investigation_type", "rationale"}:
            raise TraceContractError("classification output must contain investigation_type and rationale only")
        selected_value = raw.get("investigation_type")
        selected = selected_value.strip() if isinstance(selected_value, str) else ""
        if selected not in allowed:
            raise TraceContractError(f"classification selected unsupported type: {selected or '(empty)'}")
        rationale_value = raw.get("rationale")
        if not isinstance(rationale_value, str):
            raise TraceContractError("classification rationale must be a non-empty string")
        rationale = " ".join(rationale_value.split())
        if not rationale:
            raise TraceContractError("classification rationale must be a non-empty string")
        if len(rationale) > 500:
            raise TraceContractError("classification rationale is too long")
        return {"investigation_type": selected, "rationale": rationale}


__all__ = ["NodeTaskPlanner"]
