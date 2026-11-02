"""Deterministic, role-specific projections for provider-facing context.

The workflow context contains durable records and implementation details.  A
provider request receives a bounded projection instead: task/state are always
present, while evidence, analysis, and transcript are explicit resource
directories with stable references.  This module never changes the durable
facts; it only removes fields that the current role does not need.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping

from .context_budget import estimate_tokens


PACK_VERSION = "context-pack.v1"
MAX_DOCUMENT_CARDS = 12
MAX_EVIDENCE_DESCRIPTORS = 24
MAX_ANALYSIS_DESCRIPTORS = 8
MAX_TRANSCRIPT_ITEMS = 8
MAX_SUMMARY_CHARS = 480
MAX_EXCERPT_CHARS = 400
MAX_STATE_CHARS = 18_000


def build_context_pack(role: str, context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the provider-facing pack for one role from workflow context."""

    source = dict(context or {})
    role_name = str(role or "agent").strip() or "agent"
    task = _task_projection(source)
    state = _state_projection(role_name, source)
    evidence, evidence_omissions = _evidence_projection(role_name, source)
    analysis, analysis_omissions = _analysis_projection(role_name, source)
    transcript, transcript_omissions = _transcript_projection(role_name, source)

    omissions = [*evidence_omissions, *analysis_omissions, *transcript_omissions]
    pack: dict[str, Any] = {
        "pack_version": PACK_VERSION,
        "run_id": str(source.get("run_id") or task.get("run_id") or ""),
        "role": role_name,
        "task": task,
        "state": state,
        "evidence": evidence,
        "analysis": analysis,
        "transcript": transcript,
        "omissions": omissions,
    }
    _attach_layer_stats(pack)
    return pack


def context_pack_hash(pack: Mapping[str, Any]) -> str:
    """Hash a pack without retaining its contents in a trace event."""

    payload = dict(pack)
    payload.pop("pack_hash", None)
    return sha256(_encoded(payload).encode("utf-8")).hexdigest()


def compact_context_pack(pack: Mapping[str, Any], level: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a progressively smaller, still-addressable role pack."""

    if not 1 <= level <= 5:
        raise ValueError("context pack compaction level must be from 1 through 5")
    result = json.loads(_encoded(pack))
    before_hash = str(result.get("pack_hash") or context_pack_hash(result))
    before_counts = _resource_counts(result)
    task = result.get("task") if isinstance(result.get("task"), dict) else {}
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    analysis = result.get("analysis") if isinstance(result.get("analysis"), list) else []

    if level >= 1:
        task.pop("review_feedback", None)
        state.pop("uncompleted_items", None)
        state.pop("input_refs", None)
        for item in evidence:
            _shorten_resource(item, summary_chars=360, excerpt_chars=400)
        result["analysis"] = analysis[:16]
        _record_truncated(result, "analysis", len(analysis) - len(result["analysis"]))

    if level >= 2:
        evidence = result.get("evidence", [])
        for item in evidence:
            _shorten_resource(item, summary_chars=240, excerpt_chars=160)
        result["analysis"] = result.get("analysis", [])[:8]
        _record_truncated(result, "analysis", len(analysis) - len(result["analysis"]))

    if level >= 3:
        evidence = result.get("evidence", [])
        for item in evidence:
            card = item.get("document_card") if isinstance(item, dict) else None
            if isinstance(card, dict):
                card.pop("relevant_excerpts", None)
        docs, refs = _split_evidence(evidence)
        result["evidence"] = docs[:8] + refs[:16]
        _record_truncated(result, "documents", len(docs) - min(len(docs), 8))
        _record_truncated(result, "evidence", len(refs) - min(len(refs), 16))
        result["analysis"] = result.get("analysis", [])[:4]
        _record_truncated(result, "analysis", len(analysis) - len(result["analysis"]))

    if level >= 4:
        evidence = result.get("evidence", [])
        docs, refs = _split_evidence(evidence)
        result["evidence"] = docs[:6] + refs[:12]
        _record_truncated(result, "documents", len(docs) - min(len(docs), 6))
        _record_truncated(result, "evidence", len(refs) - min(len(refs), 12))
        result["analysis"] = []
        _record_truncated(result, "analysis", len(analysis))
        _compact_node_state(result, state, claim_limit=32, evidence_limit=16, dependency_limit=8)

    if level >= 5:
        evidence = result.get("evidence", [])
        docs, refs = _split_evidence(evidence)
        result["evidence"] = docs[:4] + refs[:8]
        _record_truncated(result, "documents", len(docs) - min(len(docs), 4))
        _record_truncated(result, "evidence", len(refs) - min(len(refs), 8))
        _compact_node_state(result, state, claim_limit=16, evidence_limit=8, dependency_limit=4)

    if result.get("role") == "report_composer":
        _compact_report_state(result, level)

    _attach_layer_stats(result)
    after_counts = _resource_counts(result)
    return result, {
        "level": level,
        "before_hash": before_hash,
        "after_hash": result["pack_hash"],
        "before_resources": before_counts,
        "after_resources": after_counts,
        "omission_count": len(result.get("omissions") or []),
    }


def _task_projection(context: Mapping[str, Any]) -> dict[str, Any]:
    task = context.get("task") or context.get("node_task") or {}
    task_source = dict(task) if isinstance(task, Mapping) else {}
    # The full review is already projected in state.review_feedback.
    task_source.pop("review_feedback", None)
    task_value = _bounded_mapping(task_source, max_chars=8_000)
    node_spec = context.get("node_spec")
    if isinstance(node_spec, Mapping):
        task_value["node_spec"] = _bounded_mapping(node_spec, max_chars=8_000)
    data_needs = context.get("report_data_needs")
    if isinstance(data_needs, list):
        task_value["report_data_needs"] = [
            _bounded_mapping(item, max_chars=2_500)
            for item in data_needs[:16]
            if isinstance(item, Mapping)
        ]
    template = context.get("trace_template")
    if isinstance(template, Mapping):
        task_value["template"] = {
            key: template.get(key)
            for key in ("template_id", "template_version", "investigation_type", "language")
            if template.get(key) is not None
        }
    for key in ("run_id", "execution_task_id", "objective", "investigation_type", "event_title", "subject", "review_round", "review_focus", "research_policy", "task_budget"):
        if key in context and key not in task_value:
            task_value[key] = _bounded_value(context[key], max_chars=4_000)
    if "objective" in task_value:
        task_value["objective"] = str(task_value["objective"] or "")[:2_000]
    return task_value


def _state_projection(role: str, context: Mapping[str, Any]) -> dict[str, Any]:
    if role == "orchestrator":
        if context.get("classification") is True:
            documents = []
            for item in context.get("source_documents", ())[:12] if isinstance(context.get("source_documents"), (list, tuple)) else ():
                if not isinstance(item, Mapping):
                    continue
                content = " ".join(str(item.get("content") or item.get("snippet") or "").split())
                documents.append({
                    "document_id": str(item.get("document_id") or "")[:120],
                    "title": str(item.get("title") or "")[:240],
                    "source": str(item.get("source") or "")[:80],
                    "url": str(item.get("url") or "")[:500],
                    "published_at": str(item.get("published_at") or "")[:80],
                    "summary": _summary(item, content),
                    "excerpt": content[:800],
                })
            candidate_values = context.get("investigation_type_candidates", ())
            candidates = candidate_values if isinstance(candidate_values, (list, tuple)) else ()
            return {
                "mode": "classification",
                "investigation_type_candidates": [
                    _bounded_mapping(item, max_chars=1_000)
                    for item in candidates
                    if isinstance(item, Mapping)
                ],
                "source_documents": documents,
            }
        targets = context.get("node_targets")
        return {
            "node_targets": [
                _node_target(item)
                for item in (targets if isinstance(targets, list) else [])[:64]
                if isinstance(item, Mapping)
            ],
            "available_capabilities": list(context.get("available_capabilities") or ())[:64],
            "budget": _bounded_mapping(context.get("budget") or {}, max_chars=2_000),
        }
    if role == "report_composer":
        phase = str(context.get("phase") or "plan")
        raw_template = context.get("report_template") or context.get("trace_template") or {}
        chapter = context.get("chapter") or {}
        chapter_id = str(chapter.get("chapter_id") or chapter.get("id") or "") if isinstance(chapter, Mapping) else ""
        source_material = context.get("chapter_material") or context.get("material_pack") or {}
        chapter_slots = _report_chapter_slots(raw_template, chapter_id, chapter)
        chapter_material = _select_report_nodes(source_material, chapter_slots) if phase == "section" else {}
        return {
            "phase": phase,
            "chapter": _bounded_mapping(chapter, max_chars=8_000),
            "chapter_material": _report_material_projection(chapter_material),
            "trace": _report_trace_outline(context.get("trace") or context.get("node_trace") or {}, chapter_slots if phase == "section" else None),
            "material_pack": _report_material_index(context.get("material_pack") or {}),
            "template": _report_template_projection(raw_template, chapter_id if phase == "section" else ""),
        }
    if role == "conclusion_formatter":
        draft = context.get("current_draft")
        draft = draft if isinstance(draft, Mapping) else {}
        return {
            "node_id": str(draft.get("node_id") or ""),
            "slot_id": str(draft.get("slot_id") or ""),
            "revision": draft.get("revision"),
            "conclusion": str(context.get("conclusion") or draft.get("conclusion") or ""),
            "claims": [
                {
                    key: str(claim.get(key) or "")[:1_000]
                    for key in ("claim_id", "claim_type", "text")
                    if key in claim
                }
                for claim in (context.get("claims") or draft.get("claims") or ())[:64]
                if isinstance(claim, Mapping)
            ],
            "evidence": [
                {
                    key: _bounded_value(evidence.get(key), max_chars=1_200)
                    for key in ("document_id", "excerpt", "locator", "relation")
                    if key in evidence
                }
                for evidence in (context.get("evidence") or draft.get("evidence") or ())[:24]
                if isinstance(evidence, Mapping)
            ],
            "media": [
                {
                    key: str(media.get(key) or "")[:600]
                    for key in ("media_type", "caption", "why_relevant")
                    if key in media
                }
                for media in (context.get("media") or draft.get("media") or ())[:24]
                if isinstance(media, Mapping)
            ],
        }
    state: dict[str, Any] = {
        "current_draft": _draft_state(context.get("current_draft")),
        "review_feedback": _review_projection(context.get("review_feedback")),
        "dependency_summaries": _dependency_summaries(context.get("dependency_node_outputs")),
    }
    return state


def _evidence_projection(role: str, context: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if role not in {"researcher", "verifier"}:
        return [], []
    documents = [item for item in context.get("source_documents", ()) if isinstance(item, Mapping)]
    draft = context.get("current_draft")
    draft_evidence = [item for item in (draft.get("evidence", ()) if isinstance(draft, Mapping) else ()) if isinstance(item, Mapping)]
    by_id = {str(item.get("document_id")): item for item in documents if str(item.get("document_id") or "").strip()}
    wanted_ids = {str(item.get("document_id")) for item in draft_evidence if str(item.get("document_id") or "").strip()}
    if role == "verifier" and wanted_ids:
        selected = [item for item in documents if str(item.get("document_id")) in wanted_ids]
    else:
        selected = [item for item in documents if str(item.get("document_id") or "") in wanted_ids]
        selected.extend(item for item in documents if str(item.get("document_id") or "") not in wanted_ids)

    resources: list[dict[str, Any]] = []
    for item in selected[:MAX_DOCUMENT_CARDS]:
        document_id = str(item.get("document_id") or "").strip()
        if not document_id:
            continue
        content = str(item.get("content") or item.get("snippet") or "").strip()
        summary = _summary(item, content)
        locator = {"url": str(item.get("url") or "").strip()}
        resource: dict[str, Any] = {
            "resource_id": f"document:{document_id}",
            "resource_kind": "evidence",
            "title": str(item.get("title") or document_id)[:240],
            "summary": summary,
            "why_relevant": "用于当前节点主张的来源核对和补充检索。",
            "when_to_use": ["需要确认主体、行动、结果、时间或限定条件", "现有证据不足以支撑当前主张"],
            "when_not_to_use": ["只需要来源数量或发布时间统计"],
            "citation": {
                "document_id": document_id,
                "locator": locator,
                "content_hash": str(item.get("content_hash") or ""),
            },
            "available_representations": ["card", "excerpt", "normalized_chunk"],
            "access": {"capability": "get_source_document", "argument_hint": {"document_id": document_id}},
            "limits": {"max_chars": MAX_EXCERPT_CHARS, "requires_permission": False},
            "omitted": False,
            "document_card": {
                "document_id": document_id,
                "source_type": str(item.get("source") or "web_search"),
                "url": str(item.get("url") or ""),
                "published_at": item.get("published_at"),
                "content_hash": str(item.get("content_hash") or ""),
                "summary": summary,
                "relevant_excerpts": [{"excerpt_id": f"excerpt:{document_id}:preview", "text": content[:MAX_EXCERPT_CHARS], "locator": locator}],
            },
        }
        resources.append(resource)

    for item in draft_evidence[:MAX_EVIDENCE_DESCRIPTORS]:
        evidence_id = str(item.get("evidence_id") or "").strip()
        document_id = str(item.get("document_id") or "").strip()
        if not evidence_id or not document_id:
            continue
        source = by_id.get(document_id, {})
        excerpt = str(item.get("excerpt") or "").strip()
        resources.append({
            "resource_id": f"evidence:{evidence_id}",
            "resource_kind": "evidence",
            "title": str(source.get("title") or document_id)[:240],
            "summary": excerpt[:MAX_SUMMARY_CHARS],
            "why_relevant": "当前草稿已经引用的可复核证据片段。",
            "when_to_use": ["复核主张与证据的支持关系", "需要补充上下文或限定条件"],
            "when_not_to_use": ["把片段原文直接当作新的主张"],
            "citation": {
                "document_id": document_id,
                "evidence_id": evidence_id,
                "locator": dict(item.get("locator") or {}),
                "content_hash": str(source.get("content_hash") or ""),
            },
            "available_representations": ["excerpt", "normalized_chunk"],
            "access": {"capability": "get_source_excerpt", "argument_hint": {"evidence_id": evidence_id}},
            "limits": {"max_chars": MAX_EXCERPT_CHARS, "requires_permission": False},
            "omitted": False,
        })
    omissions: list[dict[str, Any]] = []
    if len(documents) > MAX_DOCUMENT_CARDS:
        omissions.append({"kind": "documents", "count": len(documents) - MAX_DOCUMENT_CARDS, "reason": "outside_role_scope_or_pack_limit", "reacquire": "list_source_resources"})
    if len(draft_evidence) > MAX_EVIDENCE_DESCRIPTORS:
        omissions.append({"kind": "evidence", "count": len(draft_evidence) - MAX_EVIDENCE_DESCRIPTORS, "reason": "pack_limit", "reacquire": "list_source_resources"})
    return resources, omissions


def _analysis_projection(role: str, context: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if role not in {"researcher", "verifier"}:
        return [], []
    features = [item for item in context.get("source_analysis_features", ()) if isinstance(item, Mapping)]
    resources = []
    for item in features[:MAX_ANALYSIS_DESCRIPTORS]:
        feature_id = str(item.get("feature_id") or item.get("feature_type") or "analysis").strip()
        resources.append({
            "resource_id": f"analysis:{feature_id}",
            "resource_kind": "analysis",
            "summary": _summary(item, ""),
            "why_relevant": "作为情感、立场、主题或传播信号辅助理解当前节点。",
            "when_to_use": ["需要解释样本分布、时间窗口或异常值"],
            "when_not_to_use": ["用分析信号替代事实证据或直接证明事件发生"],
            "citation": {"document_id": str(item.get("document_id") or ""), "content_hash": str(item.get("input_hash") or "")},
            "available_representations": ["aggregate", "samples"],
            "access": {"capability": "get_analysis_context", "argument_hint": {"feature_type": str(item.get("feature_type") or "")}},
            "limits": {"max_chars": 2_000, "requires_permission": False},
            "omitted": False,
        })
    omissions = []
    if len(features) > MAX_ANALYSIS_DESCRIPTORS:
        omissions.append({"kind": "analysis", "count": len(features) - MAX_ANALYSIS_DESCRIPTORS, "reason": "pack_limit", "reacquire": "get_analysis_context"})
    return resources, omissions


def _transcript_projection(role: str, context: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if role == "conclusion_formatter":
        return [], []
    values = [item for item in context.get("transcript", ()) if isinstance(item, Mapping)]
    selected = [_bounded_mapping(item, max_chars=2_000) for item in values[-MAX_TRANSCRIPT_ITEMS:]]
    omissions = []
    if len(values) > MAX_TRANSCRIPT_ITEMS:
        omissions.append({"kind": "transcript", "count": len(values) - MAX_TRANSCRIPT_ITEMS, "reason": "recent_items_only", "reacquire": "get_transcript_context"})
    return selected, omissions


def _draft_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result = {key: value.get(key) for key in ("node_id", "slot_id", "revision", "conclusion", "media", "metrics", "datasets") if key in value}
    result["claims"] = [
        {key: claim.get(key) for key in ("claim_id", "claim_type", "text", "subject", "predicate", "object_value", "time_scope", "source_document_ids", "evidence_ids") if key in claim}
        for claim in value.get("claims", ())[:64] if isinstance(claim, Mapping)
    ]
    result["evidence_refs"] = [
        {key: evidence.get(key) for key in ("evidence_id", "claim_id", "document_id", "locator", "relation") if key in evidence}
        for evidence in value.get("evidence", ())[:MAX_EVIDENCE_DESCRIPTORS] if isinstance(evidence, Mapping)
    ]
    return result


def _review_projection(value: Any) -> dict[str, Any] | None:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {
        key: value.get(key)
        for key in ("review_id", "node_id", "slot_id", "revision", "status")
        if key in value
    }
    fields = (
        "covered_aspects", "missing_aspects", "redundant_claims", "unclear_claims",
        "evidence_followups", "rewrite_instructions", "next_research_tasks",
    )
    for key in fields:
        items = value.get(key)
        if isinstance(items, list):
            result[key] = [str(item)[:700] for item in items[:12]]
    if value.get("final_editor_note") is not None:
        result["final_editor_note"] = str(value["final_editor_note"])[:2_000]
    return result


def _dependency_summaries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output = []
    for item in value[:16]:
        if not isinstance(item, Mapping):
            continue
        draft = item.get("draft") if isinstance(item.get("draft"), Mapping) else {}
        output.append({
            "node_id": item.get("node_id"), "slot_id": item.get("slot_id"), "revision": item.get("revision"),
            "conclusion": str(draft.get("conclusion") or item.get("conclusion") or "")[:MAX_SUMMARY_CHARS],
            "claim_refs": [str(claim.get("claim_id")) for claim in draft.get("claims", ())[:24] if isinstance(claim, Mapping) and claim.get("claim_id")],
            "evidence_refs": [str(evidence.get("evidence_id")) for evidence in draft.get("evidence", ())[:24] if isinstance(evidence, Mapping) and evidence.get("evidence_id")],
        })
    return output


def _node_target(value: Mapping[str, Any]) -> dict[str, Any]:
    target = {
        key: _bounded_value(value.get(key), max_chars=4_000)
        for key in ("node_id", "slot_id", "revision", "node_spec", "input_refs")
        if key in value
    }
    if isinstance(value.get("report_data_needs"), list):
        target["report_data_needs"] = [
            _bounded_mapping(item, max_chars=2_500)
            for item in value["report_data_needs"][:16]
            if isinstance(item, Mapping)
        ]
    draft = value.get("current_draft")
    review = value.get("review_feedback")
    if isinstance(draft, Mapping):
        target["current_state"] = {
            "revision": draft.get("revision"),
            "claim_count": len(draft.get("claims", ())) if isinstance(draft.get("claims"), list) else 0,
            "evidence_count": len(draft.get("evidence", ())) if isinstance(draft.get("evidence"), list) else 0,
            "conclusion_summary": str(draft.get("conclusion") or "")[:MAX_SUMMARY_CHARS],
        }
    if isinstance(review, Mapping):
        target["review_state"] = {
            "revision": review.get("revision"),
            "status": review.get("status"),
            "missing_aspects": list(review.get("missing_aspects") or ())[:12],
            "next_research_tasks": list(review.get("next_research_tasks") or ())[:12],
        }
    return target


def _uncompleted_items(context: Mapping[str, Any]) -> list[str]:
    review = context.get("review_feedback")
    if not isinstance(review, Mapping):
        return []
    values: list[str] = []
    for key in ("missing_aspects", "evidence_followups", "rewrite_instructions", "next_research_tasks"):
        for item in review.get(key, ()) if isinstance(review.get(key), list) else ():
            text = str(item).strip()
            if text:
                values.append(text[:500])
    return values[:32]


def _summary(item: Mapping[str, Any], content: str) -> str:
    summary = str(item.get("summary") or item.get("snippet") or "").strip()
    if summary:
        return summary[:MAX_SUMMARY_CHARS]
    return " ".join(content.split())[:MAX_SUMMARY_CHARS]


def _template_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: value.get(key) for key in ("template_id", "template_version", "investigation_type", "language") if value.get(key) is not None}


def _report_trace_projection(value: Any) -> dict[str, Any]:
    """Bound report input per node while preserving its reference graph."""

    if not isinstance(value, Mapping):
        return {}
    nodes: list[dict[str, Any]] = []
    for node in value.get("nodes", ())[:64] if isinstance(value.get("nodes"), list) else ():
        if not isinstance(node, Mapping):
            continue
        draft = node.get("draft") if isinstance(node.get("draft"), Mapping) else {}
        nodes.append({
            key: node.get(key)
            for key in ("node_id", "slot_id", "node_order", "title", "goal")
            if key in node
        } | {"draft": _report_draft_projection(draft)})
    return {
        key: value.get(key)
        for key in ("trace_id", "run_id", "investigation_type", "subject", "template_id", "template_version")
        if key in value
    } | {"nodes": nodes, "edges": list(value.get("edges") or ())[:128]}


def _report_material_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        return {}
    nodes = []
    for node in value.get("nodes", ())[:64] if isinstance(value.get("nodes"), list) else ():
        if not isinstance(node, Mapping):
            continue
        nodes.append({
            key: node.get(key)
            for key in ("node_id", "slot_id", "node_order", "title", "goal", "conclusion")
            if key in node
        } | {
            "claims": [_report_claim_projection(item) for item in node.get("claims", ())[:64] if isinstance(item, Mapping)],
            "evidence": [_report_evidence_projection(item) for item in node.get("evidence", ())[:96] if isinstance(item, Mapping)],
            "media": [_report_media_projection(item) for item in node.get("media", ())[:32] if isinstance(item, Mapping)],
            "metrics": [_report_metric_projection(item) for item in node.get("metrics", ())[:64] if isinstance(item, Mapping)],
            "datasets": [_report_dataset_projection(item) for item in node.get("datasets", ())[:32] if isinstance(item, Mapping)],
        })
    return {
        key: value.get(key)
        for key in ("trace_id", "run_id", "investigation_type", "subject", "source_document_ids")
        if key in value
    } | {
        "nodes": nodes,
        "edges": list(value.get("edges") or ())[:128],
        "template_datasets": [
            _report_dataset_projection(item)
            for item in value.get("template_datasets", ())[:32]
            if isinstance(item, Mapping)
        ],
    }


def _report_trace_outline(value: Any, slots: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    nodes: list[dict[str, Any]] = []
    for node in value.get("nodes", ()) if isinstance(value.get("nodes"), list) else ():
        if not isinstance(node, Mapping):
            continue
        slot_id = str(node.get("slot_id") or "")
        if slots is not None and slot_id not in slots:
            continue
        draft = node.get("draft") if isinstance(node.get("draft"), Mapping) else node
        nodes.append({
            key: node.get(key)
            for key in ("node_id", "slot_id", "node_order", "title", "goal")
            if key in node
        } | {
            "conclusion": str(draft.get("conclusion") or node.get("conclusion") or "")[:1_200],
            "claim_count": len(draft.get("claims", ())) if isinstance(draft.get("claims"), list) else 0,
            "evidence_count": len(draft.get("evidence", ())) if isinstance(draft.get("evidence"), list) else 0,
            "media_count": len(draft.get("media", ())) if isinstance(draft.get("media"), list) else 0,
            "metric_count": len(draft.get("metrics", ())) if isinstance(draft.get("metrics"), list) else 0,
            "dataset_count": len(draft.get("datasets", ())) if isinstance(draft.get("datasets"), list) else 0,
        })
    edges = [
        dict(edge) for edge in value.get("edges", ()) if isinstance(edge, Mapping)
        and (slots is None or (str(edge.get("from_slot_id") or "") in slots and str(edge.get("to_slot_id") or "") in slots))
    ]
    return {
        key: value.get(key)
        for key in ("trace_id", "run_id", "investigation_type", "subject", "template_id", "template_version")
        if key in value
    } | {"nodes": nodes, "edges": edges[:128]}


def _report_material_index(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    nodes = []
    for node in value.get("nodes", ()) if isinstance(value.get("nodes"), list) else ():
        if not isinstance(node, Mapping):
            continue
        nodes.append({
            "node_id": node.get("node_id"),
            "slot_id": node.get("slot_id"),
            "claim_count": len(node.get("claims", ())) if isinstance(node.get("claims"), list) else 0,
            "evidence_count": len(node.get("evidence", ())) if isinstance(node.get("evidence"), list) else 0,
            "media_count": len(node.get("media", ())) if isinstance(node.get("media"), list) else 0,
            "metric_count": len(node.get("metrics", ())) if isinstance(node.get("metrics"), list) else 0,
            "dataset_count": len(node.get("datasets", ())) if isinstance(node.get("datasets"), list) else 0,
        })
    return {
        key: value.get(key)
        for key in ("trace_id", "run_id", "investigation_type", "subject")
        if key in value
    } | {
        "nodes": nodes[:64],
        "source_document_ids": list(value.get("source_document_ids") or ())[:256],
        "template_dataset_count": len(value.get("template_datasets", ())) if isinstance(value.get("template_datasets"), list) else 0,
    }


def _report_dataset_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: value.get(key)
        for key in (
            "dataset_id", "purpose", "columns", "grain", "sample_scope", "time_scope",
            "method", "input_metric_ids", "citation_ids", "source_dataset_ids",
            "source_feature_ids", "limitations",
        )
        if key in value
    }
    rows = value.get("rows") if isinstance(value.get("rows"), list) else []
    result["rows"] = rows[:160]
    if len(rows) > 160:
        result["row_count"] = len(rows)
        result["omitted_rows"] = len(rows) - 160
    return result


def _report_claim_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: value.get(key)
        for key in (
            "claim_id", "claim_type", "text", "subject", "predicate", "object_value",
            "time_scope", "source_document_ids", "evidence_ids",
        )
        if key in value
    }
    if isinstance(result.get("text"), str):
        result["text"] = result["text"][:2_000]
    return result


def _report_evidence_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: value.get(key)
        for key in ("evidence_id", "claim_id", "document_id", "locator", "relation")
        if key in value
    }
    result["excerpt"] = str(value.get("excerpt") or "")[:1_200]
    return result


def _report_media_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "media_id", "media_type", "document_id", "caption", "why_relevant",
            "source_url", "object_key", "published_at", "captured_at",
        )
        if key in value
    }


def _report_metric_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "metric_id", "name", "value", "unit", "time_scope", "source_document_id",
            "evidence_id", "locator", "direct_or_derived", "formula", "geography", "subject",
            "population", "denominator", "limitation", "confidence",
        )
        if key in value
    }


def _report_chapter_slots(template: Any, chapter_id: str, chapter: Any) -> set[str]:
    if isinstance(chapter, Mapping) and isinstance(chapter.get("node_slots"), (list, tuple)):
        return {str(item) for item in chapter["node_slots"]}
    if not isinstance(template, Mapping):
        return set()
    for item in template.get("chapters", ()) if isinstance(template.get("chapters"), list) else ():
        if not isinstance(item, Mapping):
            continue
        if str(item.get("chapter_id") or item.get("id") or "") == chapter_id:
            return {str(slot) for slot in item.get("node_slots", ())}
    return set()


def _select_report_nodes(value: Any, slots: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    nodes = [
        dict(item) for item in value.get("nodes", ()) if isinstance(item, Mapping)
        and str(item.get("slot_id") or "") in slots
    ]
    selected_slots = {str(item.get("slot_id") or "") for item in nodes}
    edges = [
        dict(item) for item in value.get("edges", ()) if isinstance(item, Mapping)
        and str(item.get("from_slot_id") or "") in selected_slots
        and str(item.get("to_slot_id") or "") in selected_slots
    ]
    return {
        key: value.get(key)
        for key in ("trace_id", "run_id", "investigation_type", "subject", "source_document_ids")
        if key in value
    } | {"nodes": nodes, "edges": edges[:128]}


def _report_template_projection(value: Any, chapter_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    chapters = [item for item in value.get("chapters", ()) if isinstance(item, Mapping)]
    if chapter_id:
        chapters = [item for item in chapters if str(item.get("chapter_id") or item.get("id") or "") == chapter_id]
    selected_slots = {
        str(slot) for item in chapters for slot in (item.get("node_slots") or ())
    }
    datasets = [item for item in value.get("datasets", ()) if isinstance(item, Mapping)]
    if chapter_id:
        datasets = [item for item in datasets if not item.get("source_nodes") or set(map(str, item.get("source_nodes") or ())) & selected_slots]
    dataset_ids = {str(item.get("dataset_id") or item.get("id") or "") for item in datasets}
    visuals = [item for item in value.get("visuals", ()) if isinstance(item, Mapping)]
    if chapter_id:
        visuals = [item for item in visuals if str(item.get("dataset_id") or item.get("dataset") or "") in dataset_ids]
    result = {
        key: value.get(key)
        for key in (
            "template_id", "version", "investigation_type", "trace_template_id",
            "trace_template_version", "language", "content_hash",
        )
        if key in value
    }
    result["chapters"] = [_bounded_mapping(item, max_chars=3_000) for item in chapters]
    result["datasets"] = [_bounded_mapping(item, max_chars=2_000) for item in datasets]
    result["visuals"] = [_bounded_mapping(item, max_chars=2_000) for item in visuals]
    if value.get("instructions") is not None:
        result["instructions"] = str(value["instructions"])[:12_000]
    return result


def _compact_report_state(pack: dict[str, Any], level: int) -> None:
    state = pack.get("state") if isinstance(pack.get("state"), dict) else {}
    material = state.get("chapter_material") if isinstance(state.get("chapter_material"), dict) else {}
    nodes = material.get("nodes") if isinstance(material.get("nodes"), list) else []
    if not nodes:
        return
    claim_chars = {1: 1_600, 2: 1_100, 3: 800, 4: 550, 5: 400}[level]
    excerpt_chars = {1: 900, 2: 600, 3: 360, 4: 220, 5: 120}[level]
    conclusion_chars = {1: 6_000, 2: 4_500, 3: 3_500, 4: 2_600, 5: 2_000}[level]
    shortened = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("conclusion"), str) and len(node["conclusion"]) > conclusion_chars:
            node["conclusion"] = node["conclusion"][:conclusion_chars]
            shortened += 1
        for claim in node.get("claims", ()) if isinstance(node.get("claims"), list) else ():
            if isinstance(claim, dict) and isinstance(claim.get("text"), str) and len(claim["text"]) > claim_chars:
                claim["text"] = claim["text"][:claim_chars]
                shortened += 1
        for evidence in node.get("evidence", ()) if isinstance(node.get("evidence"), list) else ():
            if isinstance(evidence, dict) and isinstance(evidence.get("excerpt"), str) and len(evidence["excerpt"]) > excerpt_chars:
                evidence["excerpt"] = evidence["excerpt"][:excerpt_chars]
                shortened += 1
        for dataset in node.get("datasets", ()) if isinstance(node.get("datasets"), list) else ():
            if isinstance(dataset, dict) and isinstance(dataset.get("rows"), list) and len(dataset["rows"]) > 160:
                dataset["row_count"] = len(dataset["rows"])
                dataset["omitted_rows"] = len(dataset["rows"]) - 160
                dataset["rows"] = dataset["rows"][:160]
                shortened += 1
    if shortened:
        _record_truncated(pack, "report_material_text", shortened)


def _report_draft_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "node_id": value.get("node_id"),
        "slot_id": value.get("slot_id"),
        "revision": value.get("revision"),
        "conclusion": str(value.get("conclusion") or "")[:4_000],
        "claims": [
            {
                key: item.get(key)
                for key in ("claim_id", "claim_type", "text", "subject", "predicate", "object_value", "time_scope", "source_document_ids", "evidence_ids")
                if key in item
            }
            for item in value.get("claims", ())[:64]
            if isinstance(item, Mapping)
        ],
        "evidence": [
            {
                **{key: item.get(key) for key in ("evidence_id", "claim_id", "document_id", "locator", "relation") if key in item},
                "excerpt": str(item.get("excerpt") or "")[:1_200],
            }
            for item in value.get("evidence", ())[:96]
            if isinstance(item, Mapping)
        ],
        "media": list(value.get("media") or ())[:32],
        "metrics": list(value.get("metrics") or ())[:64],
        "datasets": list(value.get("datasets") or ())[:32],
    }


def _bounded_mapping(value: Any, *, max_chars: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    encoded = _encoded(value)
    if len(encoded) <= max_chars:
        return dict(value)
    return {"status": "bounded", "summary": encoded[:max_chars], "omitted": True}


def _bounded_value(value: Any, *, max_chars: int) -> Any:
    if isinstance(value, Mapping):
        return _bounded_mapping(value, max_chars=max_chars)
    if isinstance(value, list):
        return [_bounded_value(item, max_chars=max_chars) for item in value[:64]]
    if isinstance(value, str):
        return value[:max_chars]
    return value


def _attach_layer_stats(pack: dict[str, Any]) -> None:
    layers = ("task", "state", "evidence", "analysis", "transcript")
    layer_stats: dict[str, dict[str, Any]] = {}
    for name in layers:
        encoded = _encoded(pack.get(name))
        layer_stats[name] = {
            "chars": len(encoded),
            "estimated_tokens": estimate_tokens(encoded),
            "hash": sha256(encoded.encode("utf-8")).hexdigest(),
        }
    pack["budget"] = {"layers": layer_stats, "estimated_input_tokens": sum(item["estimated_tokens"] for item in layer_stats.values())}
    pack["pack_hash"] = context_pack_hash(pack)


def _shorten_resource(item: Any, *, summary_chars: int, excerpt_chars: int) -> None:
    if not isinstance(item, dict):
        return
    if isinstance(item.get("summary"), str):
        item["summary"] = item["summary"][:summary_chars]
    card = item.get("document_card")
    if isinstance(card, dict):
        if isinstance(card.get("summary"), str):
            card["summary"] = card["summary"][:summary_chars]
        excerpts = card.get("relevant_excerpts")
        if isinstance(excerpts, list):
            for excerpt in excerpts:
                if isinstance(excerpt, dict) and isinstance(excerpt.get("text"), str):
                    excerpt["text"] = excerpt["text"][:excerpt_chars]


def _split_evidence(items: list[Any]) -> tuple[list[Any], list[Any]]:
    docs = [item for item in items if isinstance(item, dict) and str(item.get("resource_id", "")).startswith("document:")]
    refs = [item for item in items if isinstance(item, dict) and str(item.get("resource_id", "")).startswith("evidence:")]
    return docs, refs


def _resource_counts(pack: Mapping[str, Any]) -> dict[str, int]:
    evidence = pack.get("evidence") if isinstance(pack.get("evidence"), list) else []
    docs, refs = _split_evidence(evidence)
    analysis = pack.get("analysis") if isinstance(pack.get("analysis"), list) else []
    state = pack.get("state") if isinstance(pack.get("state"), dict) else {}
    draft = state.get("current_draft") if isinstance(state.get("current_draft"), dict) else {}
    return {
        "documents": len(docs),
        "evidence": len(refs),
        "analysis": len(analysis),
        "claims": len(draft.get("claims", ())) if isinstance(draft.get("claims"), list) else 0,
        "draft_evidence_refs": len(draft.get("evidence_refs", ())) if isinstance(draft.get("evidence_refs"), list) else 0,
    }


def _record_truncated(pack: dict[str, Any], kind: str, count: int) -> None:
    if count <= 0:
        return
    omissions = pack.setdefault("omissions", [])
    for item in omissions:
        if isinstance(item, dict) and item.get("kind") == kind and item.get("reason") == "context_budget":
            item["count"] = int(item.get("count", 0)) + count
            return
    omissions.append({
        "kind": kind,
        "count": count,
        "reason": "context_budget",
        "reacquire": "list_source_resources",
    })


def _compact_node_state(
    pack: dict[str, Any], state: dict[str, Any], *, claim_limit: int, evidence_limit: int, dependency_limit: int,
) -> None:
    draft = state.get("current_draft")
    if isinstance(draft, dict):
        if isinstance(draft.get("claims"), list):
            _record_truncated(pack, "claims", len(draft["claims"]) - min(len(draft["claims"]), claim_limit))
            draft["claims"] = draft["claims"][:claim_limit]
            for claim in draft["claims"]:
                if isinstance(claim, dict) and isinstance(claim.get("text"), str):
                    claim["text"] = claim["text"][:900]
        if isinstance(draft.get("evidence_refs"), list):
            _record_truncated(pack, "evidence_refs", len(draft["evidence_refs"]) - min(len(draft["evidence_refs"]), evidence_limit))
            draft["evidence_refs"] = draft["evidence_refs"][:evidence_limit]
        if isinstance(draft.get("conclusion"), str):
            draft["conclusion"] = draft["conclusion"][:4_000]
    dependencies = state.get("dependency_summaries")
    if isinstance(dependencies, list):
        _record_truncated(pack, "dependencies", len(dependencies) - min(len(dependencies), dependency_limit))
        state["dependency_summaries"] = dependencies[:dependency_limit]


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


__all__ = ["PACK_VERSION", "build_context_pack", "compact_context_pack", "context_pack_hash"]
