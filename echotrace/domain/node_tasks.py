"""Template-bound research plans for the node-driven investigation workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping

from .trace_nodes import MAX_NODE_REVISIONS, NodeReview, TraceContractError, TraceTemplate


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TraceContractError(f"{label} must be a non-empty string")
    return value.strip()


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TraceContractError(f"{label} must be an array of strings")
    result = tuple(_text(item, label) for item in value)
    if len(set(result)) != len(result):
        raise TraceContractError(f"{label} cannot contain duplicates")
    return result


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TraceContractError(f"{label} must be an object")
    return dict(value)


def node_task_id(run_id: str, template: TraceTemplate, node_id: str, slot_id: str, revision: int) -> str:
    identity = [run_id, template.template_id, template.template_version, node_id, slot_id, revision]
    digest = sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"ntask_{digest[:24]}"


@dataclass(frozen=True, slots=True)
class NodeResearchTask:
    task_id: str
    node_id: str
    slot_id: str
    revision: int
    question: str
    capability: str
    aspects: tuple[str, ...]
    preferred_sources: tuple[str, ...]
    depends_on_slot_ids: tuple[str, ...] = ()
    input_refs: tuple[str, ...] = ()
    priority: int = 0
    review_feedback: NodeReview | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "node_id", "slot_id", "question", "capability"):
            object.__setattr__(self, name, _text(getattr(self, name), f"node_task.{name}"))
        if type(self.revision) is not int or not 0 <= self.revision <= MAX_NODE_REVISIONS:
            raise TraceContractError("node_task.revision must be an integer from 0 through 3")
        if type(self.priority) is not int or self.priority < 0:
            raise TraceContractError("node_task.priority must be a non-negative integer")
        for name in ("aspects", "preferred_sources", "depends_on_slot_ids", "input_refs"):
            object.__setattr__(self, name, _strings(getattr(self, name), f"node_task.{name}"))
        if not self.aspects:
            raise TraceContractError("node_task.aspects cannot be empty")
        review = self.review_feedback
        if review is not None:
            review = NodeReview.from_dict(review.to_dict() if isinstance(review, NodeReview) else review)
            object.__setattr__(self, "review_feedback", review)
        if self.revision == 0:
            if review is not None:
                raise TraceContractError("initial node task cannot contain review feedback")
        elif review is None or (review.node_id, review.slot_id, review.revision) != (
            self.node_id, self.slot_id, self.revision,
        ):
            raise TraceContractError("revision task requires matching node and round review feedback")

    @property
    def idempotency_key(self) -> str:
        return self.task_id

    @property
    def stage(self) -> str:
        return "node_research" if self.revision == 0 else "node_revision"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "node_id": self.node_id, "slot_id": self.slot_id,
            "revision": self.revision, "question": self.question, "capability": self.capability,
            "aspects": list(self.aspects), "preferred_sources": list(self.preferred_sources),
            "depends_on_slot_ids": list(self.depends_on_slot_ids), "input_refs": list(self.input_refs),
            "priority": self.priority,
            "review_feedback": self.review_feedback.to_dict() if self.review_feedback else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeResearchTask":
        try:
            return cls(**_object(payload, "node_task"))
        except TypeError as exc:
            raise TraceContractError(f"invalid node task fields: {exc}") from exc


@dataclass(frozen=True, slots=True)
class NodeTaskPlan:
    run_id: str
    objective: str
    template_id: str
    template_version: str
    tasks: tuple[NodeResearchTask, ...]
    max_parallel_tasks: int = 3

    def __post_init__(self) -> None:
        for name in ("run_id", "objective", "template_id", "template_version"):
            object.__setattr__(self, name, _text(getattr(self, name), f"node_plan.{name}"))
        if type(self.max_parallel_tasks) is not int or self.max_parallel_tasks < 1:
            raise TraceContractError("node_plan.max_parallel_tasks must be a positive integer")
        if not isinstance(self.tasks, (list, tuple)) or not self.tasks:
            raise TraceContractError("node_plan.tasks must be a non-empty array")
        tasks = tuple(
            NodeResearchTask.from_dict(item.to_dict() if isinstance(item, NodeResearchTask) else item)
            for item in self.tasks
        )
        object.__setattr__(self, "tasks", tasks)
        for name in ("task_id", "node_id", "slot_id"):
            if len({getattr(item, name) for item in tasks}) != len(tasks):
                raise TraceContractError(f"node_plan tasks must have unique {name} values")

    @property
    def content_hash(self) -> str:
        return sha256(json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    @property
    def plan_id(self) -> str:
        return f"nplan_{self.content_hash[:24]}"

    def validate(
        self, *, template: TraceTemplate, node_ids: Mapping[str, str],
        available_capabilities: tuple[str, ...] | list[str],
        target_revisions: Mapping[str, int] | None = None,
    ) -> None:
        if (self.template_id, self.template_version) != (template.template_id, template.template_version):
            raise TraceContractError("node plan does not match the pinned template version")
        if not isinstance(available_capabilities, (list, tuple)) or not available_capabilities:
            raise TraceContractError("node plan requires an array of available capabilities")
        _strings(available_capabilities, "available_capabilities")
        specs = {spec.slot_id: spec for spec in template.node_specs}
        if set(node_ids) != set(specs) or len(set(node_ids.values())) != len(specs):
            raise TraceContractError("node identities must cover every template slot exactly once")
        targets = dict(target_revisions) if target_revisions is not None else {slot: 0 for slot in specs}
        if not targets or not set(targets).issubset(specs):
            raise TraceContractError("node plan targets reference unknown template slots")
        if any(type(revision) is not int or not 0 <= revision <= MAX_NODE_REVISIONS for revision in targets.values()):
            raise TraceContractError("node plan target revisions must be integers from 0 through 3")
        if {task.slot_id for task in self.tasks} != set(targets):
            raise TraceContractError("node plan must cover exactly the requested slots")
        dependencies = {slot: set(spec.depends_on_slot_ids) for slot, spec in specs.items()}
        for task in self.tasks:
            spec = specs[task.slot_id]
            if task.node_id != node_ids[task.slot_id] or task.revision != targets[task.slot_id]:
                raise TraceContractError("node task does not match its assigned node or revision")
            expected_id = node_task_id(self.run_id, template, task.node_id, task.slot_id, task.revision)
            if task.task_id != expected_id:
                raise TraceContractError("node task identity is not the stable run/slot/revision identity")
            if task.capability not in available_capabilities:
                raise TraceContractError(f"node task declares unavailable capability: {task.capability}")
            if task.aspects != spec.aspects or task.preferred_sources != spec.preferred_sources:
                raise TraceContractError("node task cannot redefine template aspects or preferred sources")
            deps = set(task.depends_on_slot_ids)
            if not set(spec.depends_on_slot_ids).issubset(deps):
                raise TraceContractError("node task cannot remove a template dependency")
            if task.slot_id in deps or not deps.issubset(specs):
                raise TraceContractError("node task dependencies reference self or unknown slots")
            dependencies[task.slot_id] = deps
        remaining = dependencies.copy()
        resolved: set[str] = set()
        while remaining:
            ready = [slot for slot, deps in remaining.items() if deps.issubset(resolved)]
            if not ready:
                raise TraceContractError("node plan dependencies contain a cycle")
            for slot in ready:
                resolved.add(slot)
                remaining.pop(slot)

    def ready_tasks(self, *, frozen_slot_ids: set[str], active_slot_ids: set[str] | None = None) -> tuple[NodeResearchTask, ...]:
        active = active_slot_ids or set()
        ready = [
            task for task in self.tasks
            if task.slot_id not in frozen_slot_ids | active
            and set(task.depends_on_slot_ids).issubset(frozen_slot_ids)
        ]
        ordered = sorted(ready, key=lambda task: (task.priority, task.slot_id))
        return tuple(ordered[:max(0, self.max_parallel_tasks - len(active))])

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "objective": self.objective,
            "template_id": self.template_id, "template_version": self.template_version,
            "tasks": [task.to_dict() for task in self.tasks], "max_parallel_tasks": self.max_parallel_tasks,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeTaskPlan":
        try:
            return cls(**_object(payload, "node_plan"))
        except TypeError as exc:
            raise TraceContractError(f"invalid node plan fields: {exc}") from exc


__all__ = ["NodeResearchTask", "NodeTaskPlan", "node_task_id"]
