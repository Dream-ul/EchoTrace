"""Read-only review of a completed node-driven Trace.

This module deliberately reviews the assembled Trace instead of reopening a
node.  Node-level research and the three verifier rounds have already frozen
the node contents by the time this service runs.  Findings are editorial
warnings for the caller; they are not a publication gate and never mutate the
Trace or its persisted revisions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping

from echotrace.domain.trace_nodes import TraceContractError
from echotrace.domain.trace_nodes import TraceTemplate

from .node_trace import NodeTrace


@dataclass(frozen=True, slots=True)
class CrossNodeFinding:
    """One actionable warning about the assembled Trace narrative."""

    finding_id: str
    kind: str
    severity: str
    slot_ids: tuple[str, ...]
    message: str
    recommendation: str

    def __post_init__(self) -> None:
        if not str(self.finding_id).strip():
            raise TraceContractError("cross-node finding_id cannot be empty")
        if not str(self.kind).strip():
            raise TraceContractError("cross-node finding kind cannot be empty")
        if self.severity not in {"info", "warning"}:
            raise TraceContractError("cross-node finding severity must be info or warning")
        if not self.slot_ids:
            raise TraceContractError("cross-node finding requires at least one slot_id")
        if any(not str(slot_id).strip() for slot_id in self.slot_ids):
            raise TraceContractError("cross-node finding slot_ids cannot be empty")
        if not str(self.message).strip() or not str(self.recommendation).strip():
            raise TraceContractError("cross-node finding requires message and recommendation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "kind": self.kind,
            "severity": self.severity,
            "slot_ids": list(self.slot_ids),
            "message": self.message,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True, slots=True)
class CrossNodeReview:
    """Serializable, read-only result of reviewing one assembled Trace."""

    review_id: str
    run_id: str
    template_id: str
    template_version: str
    status: str
    findings: tuple[CrossNodeFinding, ...]

    def __post_init__(self) -> None:
        if self.status not in {"clean", "review"}:
            raise TraceContractError("cross-node review status must be clean or review")
        if not str(self.review_id).strip() or not str(self.run_id).strip():
            raise TraceContractError("cross-node review identity cannot be empty")
        findings = tuple(
            item if isinstance(item, CrossNodeFinding) else CrossNodeFinding(**item)
            for item in self.findings
        )
        object.__setattr__(self, "findings", findings)
        expected_status = "review" if findings else "clean"
        if self.status != expected_status:
            raise TraceContractError("cross-node review status does not match findings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "run_id": self.run_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "status": self.status,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).casefold()


def _node_draft(node: Mapping[str, Any]) -> Mapping[str, Any]:
    draft = node.get("draft")
    return draft if isinstance(draft, Mapping) else {}


def _source_ids(node: Mapping[str, Any]) -> set[str]:
    values = node.get("source_document_ids")
    if isinstance(values, (list, tuple, set)):
        return {_text(value) for value in values if _text(value)}
    draft = _node_draft(node)
    result = {
        _text(value)
        for claim in draft.get("claims", [])
        if isinstance(claim, Mapping)
        for value in claim.get("source_document_ids", [])
        if _text(value)
    }
    result.update(
        _text(evidence.get("document_id"))
        for evidence in draft.get("evidence", [])
        if isinstance(evidence, Mapping) and _text(evidence.get("document_id"))
    )
    return result


def _finding(
    *,
    index: int,
    kind: str,
    slot_ids: Iterable[str],
    message: str,
    recommendation: str,
    severity: str = "warning",
) -> CrossNodeFinding:
    slots = tuple(dict.fromkeys(_text(slot_id) for slot_id in slot_ids if _text(slot_id)))
    return CrossNodeFinding(
        finding_id=f"cross_node_{index:03d}_{kind}",
        kind=kind,
        severity=severity,
        slot_ids=slots or ("trace",),
        message=message,
        recommendation=recommendation,
    )


def review_node_trace(trace: NodeTrace, template: TraceTemplate) -> CrossNodeReview:
    """Review ``trace`` against ``template`` without changing either object.

    The checks are intentionally conservative.  They flag structural breaks,
    exact repeated content, explicit structured contradictions, and missing
    source support for a required transition.  They do not infer causality
    from prose or decide whether a node is substantively persuasive.
    """

    findings: list[CrossNodeFinding] = []

    def add(kind: str, slots: Iterable[str], message: str, recommendation: str) -> None:
        findings.append(_finding(index=len(findings) + 1, kind=kind, slot_ids=slots, message=message, recommendation=recommendation))

    expected_slots = [spec.slot_id for spec in template.node_specs]
    actual_slots = [_text(node.get("slot_id")) for node in trace.nodes]
    if actual_slots != expected_slots:
        add(
            "order",
            tuple(dict.fromkeys(actual_slots + expected_slots)),
            "节点顺序或模板槽位与调查模板不一致。",
            "按模板顺序重新检查节点投影；如需内容变更，请创建新的调查 revision。",
        )

    expected_set = set(expected_slots)
    actual_by_slot: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for node in trace.nodes:
        actual_by_slot[_text(node.get("slot_id"))].append(node)
    missing = [slot for slot in expected_slots if not actual_by_slot.get(slot)]
    unexpected = [slot for slot in actual_by_slot if slot not in expected_set]
    duplicate_slots = [slot for slot, nodes in actual_by_slot.items() if slot and len(nodes) > 1]
    if missing or unexpected or duplicate_slots:
        slots = tuple(missing + unexpected + duplicate_slots)
        add(
            "order",
            slots,
            "Trace 中存在缺失、额外或重复的模板节点。",
            "重新组装冻结节点投影，并确认模板 revision 与运行记录一致。",
        )

    expected_edges = {
        (edge.from_slot_id, edge.to_slot_id, edge.relation.value)
        for edge in template.edge_specs
    }
    actual_edges = {
        (_text(edge.get("from_slot_id")), _text(edge.get("to_slot_id")), _text(edge.get("relation")))
        for edge in trace.edges
        if isinstance(edge, Mapping)
    }
    missing_edges = sorted(expected_edges - actual_edges)
    extra_edges = sorted(actual_edges - expected_edges)
    if missing_edges or extra_edges:
        edge_slots = [slot for edge in missing_edges + extra_edges for slot in edge[:2]]
        add(
            "edge",
            edge_slots,
            "Trace 的实际边关系与模板 EdgeSpec 不一致。",
            "依据模板重新生成边；不要通过编辑冻结节点来修复结构问题。",
        )
    known_slots = set(actual_by_slot)
    dangling = [
        (_text(edge.get("from_slot_id")), _text(edge.get("to_slot_id")))
        for edge in trace.edges
        if isinstance(edge, Mapping)
        and (_text(edge.get("from_slot_id")) not in known_slots or _text(edge.get("to_slot_id")) not in known_slots)
    ]
    if dangling:
        add(
            "edge",
            [slot for edge in dangling for slot in edge],
            "Trace 边引用了不存在的节点槽位。",
            "删除悬空边并从当前冻结节点重新组装 Trace。",
        )

    # Exact repeated claim/conclusion text is a safe duplicate signal.  A
    # shared person, organisation, or document alone is deliberately ignored.
    repeated: dict[str, list[str]] = defaultdict(list)
    for slot_id, nodes in actual_by_slot.items():
        for node in nodes:
            draft = _node_draft(node)
            texts = [_text(claim.get("text")) for claim in draft.get("claims", []) if isinstance(claim, Mapping)]
            texts.append(_text(draft.get("conclusion")))
            for value in {_normalise_text(text) for text in texts if _normalise_text(text)}:
                repeated[value].append(slot_id)
    for text, slots in sorted(repeated.items()):
        unique_slots = tuple(dict.fromkeys(slots))
        if len(unique_slots) > 1:
            add(
                "duplicate",
                unique_slots,
                "多个节点包含完全重复的主张或结论内容。",
                "确认重复内容是否承担不同节点目标；若没有，应在新的调查 revision 中合并或改写。",
            )

    # Structured subject/predicate/object disagreements are the only prose
    # independent contradiction check.  Free-form language is left to editors.
    structured: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for slot_id, nodes in actual_by_slot.items():
        for node in nodes:
            for claim in _node_draft(node).get("claims", []):
                if not isinstance(claim, Mapping):
                    continue
                subject = _text(claim.get("subject"))
                predicate = _text(claim.get("predicate"))
                value = _text(claim.get("object_value"))
                if subject and predicate and value:
                    structured[(subject.casefold(), predicate.casefold())].append((slot_id, value.casefold()))
    for (subject, predicate), entries in sorted(structured.items()):
        values = {value for _, value in entries}
        if len(values) > 1:
            slots = tuple(dict.fromkeys(slot for slot, _ in entries))
            add(
                "contradiction",
                slots,
                f"结构化主张对同一对象 {subject}/{predicate} 给出了相互不同的值。",
                "核对来源和时间范围，在新的调查 revision 中明确冲突或保留一个有依据的表述。",
            )

    node_map = {slot: nodes[0] for slot, nodes in actual_by_slot.items() if nodes}
    for edge in template.edge_specs:
        if not edge.requires_explicit_support:
            continue
        target = node_map.get(edge.to_slot_id)
        if target is None or not _source_ids(target):
            add(
                "unsupported_transition",
                (edge.from_slot_id, edge.to_slot_id),
                "模板要求该节点转换有明确来源支持，但后续节点没有可定位的来源材料。",
                "补充后续节点的主张依据和证据片段，并通过新的调查 revision 重新冻结。",
            )

    return CrossNodeReview(
        review_id=f"cross_node_review_{trace.run_id}_{trace.template_id}_{trace.template_version}",
        run_id=trace.run_id,
        template_id=trace.template_id,
        template_version=trace.template_version,
        status="review" if findings else "clean",
        findings=tuple(findings),
    )


__all__ = ["CrossNodeFinding", "CrossNodeReview", "review_node_trace"]
