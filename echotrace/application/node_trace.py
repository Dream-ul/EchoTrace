"""Project frozen Trace nodes into the user-facing node-driven Trace shape.

The assembler is intentionally a projection only.  It never classifies claims,
searches for sources, or invents nodes.  A Trace can be published only after
every slot in the selected template has reached the third frozen revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from echotrace.domain.models import InvestigationRun
from echotrace.domain.trace_nodes import (
    MAX_NODE_REVISIONS,
    FrozenTraceNode,
    NodeRevision,
    TraceContractError,
    TraceTemplate,
)


class FrozenNodeRepository(Protocol):
    def list_nodes(self, run_id: str) -> list[dict[str, Any]]: ...

    def get_revision(self, node_id: str, revision_no: int | None = None) -> dict[str, Any] | None: ...


def refresh_frozen_trace(
    trace: Mapping[str, Any],
    repository: FrozenNodeRepository,
) -> dict[str, Any]:
    """Refresh cached trace content from each node's persisted frozen revision."""

    records = repository.list_nodes(str(trace.get("run_id") or ""))
    by_node_id = {str(item.get("node_id") or ""): item for item in records}
    by_slot_id = {str(item.get("slot_id") or ""): item for item in records}
    nodes: list[dict[str, Any]] = []
    for cached in trace.get("nodes") or ():
        if not isinstance(cached, Mapping):
            raise TraceContractError("cached trace nodes must be objects")
        record = by_node_id.get(str(cached.get("node_id") or "")) or by_slot_id.get(
            str(cached.get("slot_id") or "")
        )
        if record is None or str(record.get("status")) != "frozen":
            raise TraceContractError(f"trace node {cached.get('slot_id')} has no persisted frozen revision")
        revision_no = record.get("frozen_revision")
        if not isinstance(revision_no, int):
            raise TraceContractError(f"trace node {cached.get('slot_id')} has no frozen revision number")
        revision = repository.get_revision(str(record["node_id"]), revision_no)
        draft = revision.get("draft") if isinstance(revision, Mapping) else None
        if not isinstance(draft, Mapping):
            raise TraceContractError(f"trace node {cached.get('slot_id')} has no persisted frozen draft")
        nodes.append({
            **dict(cached),
            "status": "frozen",
            "frozen_revision": revision_no,
            "draft": dict(draft),
        })
    if not nodes:
        raise TraceContractError("cached trace contains no nodes")
    return {**dict(trace), "nodes": nodes}


@dataclass(frozen=True, slots=True)
class NodeTrace:
    """Immutable, serializable projection of a completed node Trace."""

    trace_id: str
    run_id: str
    investigation_type: str
    subject: str
    template_id: str
    template_version: str
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "investigation_type": self.investigation_type,
            "subject": self.subject,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "nodes": [dict(node) for node in self.nodes],
            "edges": [dict(edge) for edge in self.edges],
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def build_node_trace_blocks(trace: NodeTrace) -> list[dict[str, Any]]:
    """Return the stable block projection consumed by the workbench.

    Blocks are a presentation projection of frozen nodes.  They deliberately
    retain the node's claims, evidence excerpts, conclusion, and media as the
    investigation's only factual projection.
    """

    return [
        {
            "block_id": f"{trace.trace_id}:{node.get('slot_id')}",
            "block_type": "node",
            "node_id": node.get("node_id"),
            "slot_id": node.get("slot_id"),
            "node_order": node.get("node_order"),
            "title": node.get("title"),
            "goal": node.get("goal"),
            "status": node.get("status"),
            "frozen_revision": node.get("frozen_revision"),
            "conclusion": dict(node.get("draft") or {}).get("conclusion", ""),
            "conclusion_ir": dict(node.get("draft") or {}).get("conclusion_ir"),
            "claims": list(dict(node.get("draft") or {}).get("claims") or []),
            "evidence": list(dict(node.get("draft") or {}).get("evidence") or []),
            "media": list(dict(node.get("draft") or {}).get("media") or []),
            "source_document_ids": list(node.get("source_document_ids") or []),
        }
        for node in trace.nodes
    ]


def _frozen_node(
    *,
    repository: FrozenNodeRepository,
    record: Mapping[str, Any],
    title: str,
    goal: str,
) -> FrozenTraceNode:
    node_id = str(record.get("node_id") or "").strip()
    slot_id = str(record.get("slot_id") or "").strip()
    if not node_id or not slot_id:
        raise TraceContractError("frozen node record requires node_id and slot_id")
    revisions: list[NodeRevision] = []
    for revision_no in range(MAX_NODE_REVISIONS + 1):
        payload = repository.get_revision(node_id, revision_no)
        if payload is None:
            raise TraceContractError(f"frozen node {slot_id} is missing revision {revision_no}")
        revision_payload = {
            key: payload[key]
            for key in ("node_id", "slot_id", "revision", "draft", "review", "parent_revision")
            if key in payload
        }
        revision_payload.setdefault("slot_id", slot_id)
        revision_payload.setdefault("parent_revision", revision_no - 1 if revision_no else None)
        revisions.append(NodeRevision.from_dict(revision_payload))
    frozen = FrozenTraceNode(
        node_id=node_id,
        slot_id=slot_id,
        draft=revisions[MAX_NODE_REVISIONS].draft,
        revisions=revisions,
    )
    draft = frozen.draft.to_dict()
    source_document_ids = sorted({
        document_id
        for claim in frozen.draft.claims
        for document_id in claim.source_document_ids
    } | {evidence.document_id for evidence in frozen.draft.evidence})
    return {
        "node_id": frozen.node_id,
        "slot_id": frozen.slot_id,
        "node_order": int(record.get("node_order") or 0),
        "title": title,
        "goal": goal,
        "status": "frozen",
        "frozen_revision": MAX_NODE_REVISIONS,
        "draft": draft,
        "source_document_ids": source_document_ids,
    }


def assemble_frozen_trace(
    run: InvestigationRun,
    template: TraceTemplate,
    repository: FrozenNodeRepository,
) -> NodeTrace:
    """Assemble a Trace strictly from all frozen slots in ``template``."""

    records = repository.list_nodes(run.run_id)
    by_slot = {str(record.get("slot_id")): record for record in records}
    expected_slots = {spec.slot_id for spec in template.node_specs}
    unexpected_slots = sorted(set(by_slot) - expected_slots)
    if unexpected_slots:
        raise TraceContractError(f"node repository contains slots outside template: {', '.join(unexpected_slots)}")

    nodes: list[dict[str, Any]] = []
    node_ids: dict[str, str] = {}
    for spec in template.node_specs:
        record = by_slot.get(spec.slot_id)
        if record is None:
            raise TraceContractError(f"template slot {spec.slot_id} has no persisted node")
        if str(record.get("status")) != "frozen" or int(record.get("frozen_revision") or 0) != MAX_NODE_REVISIONS:
            raise TraceContractError(f"template slot {spec.slot_id} is not frozen")
        node = _frozen_node(repository=repository, record=record, title=spec.title, goal=spec.goal)
        nodes.append(node)
        node_ids[spec.slot_id] = node["node_id"]

    edges = tuple({
        "edge_id": f"edge_{run.run_id}_{edge.from_slot_id}_{edge.to_slot_id}",
        "from_node_id": node_ids[edge.from_slot_id],
        "to_node_id": node_ids[edge.to_slot_id],
        "from_slot_id": edge.from_slot_id,
        "to_slot_id": edge.to_slot_id,
        "relation": edge.relation.value,
        "label_goal": edge.label_goal,
        "requires_explicit_support": edge.requires_explicit_support,
    } for edge in template.edge_specs)
    return NodeTrace(
        trace_id=f"node_trace_{run.run_id}_{template.template_id}_{template.template_version}",
        run_id=run.run_id,
        investigation_type=template.investigation_type,
        subject=str(run.subject or run.event.title),
        template_id=template.template_id,
        template_version=template.template_version,
        nodes=tuple(nodes),
        edges=edges,
    )


assemble_node_trace = assemble_frozen_trace


__all__ = [
    "NodeTrace", "assemble_frozen_trace", "assemble_node_trace", "build_node_trace_blocks",
    "refresh_frozen_trace",
]
