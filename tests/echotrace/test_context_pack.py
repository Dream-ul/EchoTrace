from __future__ import annotations

import json

from echotrace.agent.context import AgentContextAssembler
from echotrace.agent.context_pack import build_context_pack, compact_context_pack, context_pack_hash


def _context() -> dict:
    return {
        "run_id": "run-1",
        "objective": "调查公共事件",
        "task": {"node_id": "node-1", "slot_id": "response", "revision": 1, "question": "谁做了什么"},
        "report_data_needs": [{
            "dataset_id": "response_timeline", "purpose": "按时间整理处置记录",
            "columns": ["time", "actor", "action", "status", "citation_ids"],
            "collection_mode": "research", "collection_guidance": "保留实际行动和逐行来源",
            "minimum_samples": 2, "sample_unit": "处置记录", "visuals": [],
        }],
        "node_spec": {"title": "响应与处置", "goal": "还原主体行动和结果", "aspects": ["主体", "行动"]},
        "current_draft": {
            "node_id": "node-1", "slot_id": "response", "revision": 0,
            "claims": [{"claim_id": "claim-1", "text": "机构采取了行动", "evidence_ids": ["evidence-1"]}],
            "evidence": [{"evidence_id": "evidence-1", "claim_id": "claim-1", "document_id": "doc-1", "excerpt": "公告原文片段", "locator": {"url": "https://example.test/a"}}],
            "conclusion": "已有初步处置结论",
        },
        "review_feedback": {"missing_aspects": ["结果"], "next_research_tasks": ["检索后续公告"]},
        "source_documents": [{
            "document_id": "doc-1", "source": "official", "title": "官方公告", "url": "https://example.test/a",
            "content": "原始正文" * 500, "content_hash": "sha256:doc-1",
        }],
        "source_analysis_features": [{"feature_id": "feature-1", "feature_type": "sentiment", "value": "neutral", "document_id": "doc-1"}],
        "dependency_node_outputs": [{"node_id": "node-0", "slot_id": "origin", "revision": 3, "draft": {"conclusion": "起点结论", "claims": [{"claim_id": "claim-0"}]}}],
        "transcript": [{"step": index, "action": "search_web", "summary": "已完成搜索"} for index in range(20)],
    }


def test_role_pack_is_bounded_and_self_describing():
    pack = build_context_pack("researcher", _context())

    assert pack["pack_version"] == "context-pack.v1"
    assert pack["pack_hash"] == context_pack_hash(pack)
    assert pack["task"]["node_spec"]["title"] == "响应与处置"
    assert pack["task"]["report_data_needs"][0]["dataset_id"] == "response_timeline"
    assert pack["evidence"][0]["resource_id"] == "document:doc-1"
    assert pack["evidence"][0]["when_to_use"]
    assert pack["evidence"][0]["access"]["capability"] == "get_source_document"
    assert len(pack["transcript"]) == 8
    assert any(item["kind"] == "transcript" for item in pack["omissions"])
    assert pack["budget"]["layers"]["evidence"]["estimated_tokens"] > 0


def test_role_projection_hides_source_material_from_orchestrator_and_reporter():
    context = _context()
    orchestrator = build_context_pack("orchestrator", {**context, "node_targets": [{"slot_id": "response", "node_spec": context["node_spec"], "current_draft": context["current_draft"], "review_feedback": context["review_feedback"]}]})
    reporter = build_context_pack("report_composer", {**context, "node_trace": {"nodes": []}})

    assert orchestrator["evidence"] == []
    assert orchestrator["analysis"] == []
    assert "claims" not in json.dumps(orchestrator["state"], ensure_ascii=False)
    assert reporter["evidence"] == []
    assert reporter["analysis"] == []


def test_formatter_pack_contains_complete_conclusion_without_source_fulltext_or_search_context():
    context = _context()
    conclusion = "结论内容。" * 3_000
    context["current_draft"]["conclusion"] = conclusion
    context["conclusion"] = conclusion
    pack = build_context_pack("conclusion_formatter", context)

    assert pack["state"]["conclusion"] == conclusion
    assert "原始正文" not in json.dumps(pack["state"], ensure_ascii=False)
    assert pack["evidence"] == []
    assert pack["analysis"] == []
    assert pack["transcript"] == []


def _report_nodes(count=3):
    nodes = []
    for index in range(count):
        nodes.append({
            "node_id": f"node-{index}",
            "slot_id": f"slot-{index}",
            "node_order": index + 1,
            "title": f"Node {index}",
            "goal": "Explain the event",
            "draft": {
                "conclusion": "conclusion " * 500,
                "claims": [{
                    "claim_id": f"claim-{index}", "text": "claim text " * 300,
                    "evidence_ids": [f"evidence-{index}"], "source_document_ids": [f"doc-{index}"],
                    "unneeded_field": "excluded",
                }],
                "evidence": [{
                    "evidence_id": f"evidence-{index}", "claim_id": f"claim-{index}",
                    "document_id": f"doc-{index}", "excerpt": "evidence excerpt " * 200,
                    "locator": {"url": f"https://example.test/{index}"}, "relation": "supports",
                }],
                "media": [], "metrics": [], "datasets": [],
            },
        })
    return nodes


def _report_context(*, phase="plan", chapter=None, slots=()):
    nodes = _report_nodes()
    material_nodes = []
    for node in nodes:
        draft = node["draft"]
        material_nodes.append({
            key: node[key] for key in ("node_id", "slot_id", "node_order", "title", "goal")
        } | {
            "conclusion": draft["conclusion"], "claims": draft["claims"],
            "evidence": draft["evidence"], "media": draft["media"],
            "metrics": draft["metrics"], "datasets": draft["datasets"],
        })
    template_chapters = [
        {"chapter_id": "chapter-a", "node_slots": ["slot-0"], "title": "Chapter A"},
        {"chapter_id": "chapter-b", "node_slots": ["slot-1", "slot-2"], "title": "Chapter B"},
    ]
    if chapter is not None:
        template_chapters = [{"chapter_id": chapter["chapter_id"], "node_slots": list(slots), "title": "Selected chapter"}]
    return {
        "run_id": "run-1",
        "trace": {"trace_id": "trace-1", "run_id": "run-1", "nodes": nodes, "edges": []},
        "material_pack": {"run_id": "run-1", "nodes": material_nodes, "source_document_ids": [f"doc-{index}" for index in range(3)]},
        "report_template": {
            "template_id": "public_event.report", "version": "1.0",
            "chapters": template_chapters, "datasets": [], "visuals": [],
        },
        "phase": phase,
        **({"chapter": chapter} if chapter is not None else {}),
        **({"chapter_material": {"nodes": material_nodes, "source_document_ids": [f"doc-{index}" for index in range(3)]}} if phase == "section" else {}),
    }


def test_report_plan_receives_trace_outline_and_material_index_only():
    pack = build_context_pack("report_composer", _report_context())

    state = pack["state"]
    assert len(state["trace"]["nodes"]) == 3
    assert state["trace"]["nodes"][0]["claim_count"] == 1
    assert state["trace"]["nodes"][0]["conclusion"]
    assert "draft" not in state["trace"]["nodes"][0]
    assert state["chapter_material"] == {}
    assert state["material_pack"]["nodes"][0]["claim_count"] == 1
    assert "claims" not in state["material_pack"]["nodes"][0]
    assert len(state["template"]["chapters"]) == 2


def test_report_section_receives_only_its_template_mapped_node_material():
    chapter = {
        "chapter_id": "chapter-b", "node_slots": ["slot-1", "slot-2"],
        "report_context": {
            "summary": "按材料顺序说明事件变化。",
            "chapter_outline": [{"order": 1, "title": "起点"}, {"order": 2, "title": "变化"}],
            "previous_chapter": {"order": 1, "title": "起点"},
            "next_chapter": None,
        },
    }
    pack = build_context_pack(
        "report_composer", _report_context(phase="section", chapter=chapter, slots=chapter["node_slots"])
    )

    state = pack["state"]
    assert [node["slot_id"] for node in state["chapter_material"]["nodes"]] == ["slot-1", "slot-2"]
    assert [node["slot_id"] for node in state["trace"]["nodes"]] == ["slot-1", "slot-2"]
    assert len(state["material_pack"]["nodes"]) == 3
    assert [item["chapter_id"] for item in state["template"]["chapters"]] == ["chapter-b"]
    assert state["chapter"]["report_context"]["summary"] == "按材料顺序说明事件变化。"
    assert state["chapter"]["report_context"]["previous_chapter"]["title"] == "起点"
    material_node = state["chapter_material"]["nodes"][0]
    assert material_node["claims"][0]["claim_id"] == "claim-1"
    assert material_node["evidence"][0]["evidence_id"] == "evidence-1"
    assert "unneeded_field" not in material_node["claims"][0]

    compacted, _ = compact_context_pack(pack, 4)
    compact_node = compacted["state"]["chapter_material"]["nodes"][0]
    assert compact_node["claims"][0]["claim_id"] == "claim-1"
    assert compact_node["evidence"][0]["evidence_id"] == "evidence-1"
    assert len(compact_node["claims"][0]["text"]) <= 550
    assert len(compact_node["evidence"][0]["excerpt"]) <= 220
    assert compacted["pack_hash"] == context_pack_hash(compacted)


def test_report_section_with_no_mapped_slots_receives_no_node_material():
    chapter = {"chapter_id": "chapter-empty", "node_slots": []}
    pack = build_context_pack(
        "report_composer", _report_context(phase="section", chapter=chapter, slots=[])
    )

    assert pack["state"]["chapter_material"]["nodes"] == []
    assert pack["state"]["trace"]["nodes"] == []


def test_assembler_places_pack_in_dedicated_field_without_objective_embedding():
    pack = build_context_pack("researcher", _context())
    messages = AgentContextAssembler().build(
        role="researcher", objective="研究指定节点", skill_index=(), active_skill=None,
        skill_instructions=None, observations=[], context_pack=pack,
    )
    payload = json.loads(messages[1]["content"])

    assert payload["objective"] == "研究指定节点"
    assert payload["context_pack"]["pack_hash"] == pack["pack_hash"]


def test_context_pack_compaction_keeps_a_valid_referenceable_node_snapshot():
    context = _context()
    context["source_documents"] = [
        {
            "document_id": f"doc-{index}",
            "source": "official",
            "title": f"公告 {index}",
            "url": f"https://example.test/{index}",
            "content": "来源原文片段" * 400,
            "content_hash": f"sha256:{index}",
        }
        for index in range(12)
    ]
    context["source_analysis_features"] = [
        {"feature_id": f"feature-{index}", "feature_type": "sentiment", "summary": "样本分析" * 80}
        for index in range(16)
    ]
    context["current_draft"]["evidence"] = [
        {
            "evidence_id": f"evidence-{index}",
            "claim_id": "claim-1",
            "document_id": f"doc-{index % 12}",
            "excerpt": "可复核原文片段" * 100,
            "locator": {"url": f"https://example.test/{index % 12}"},
        }
        for index in range(24)
    ]
    pack = build_context_pack("researcher", context)

    compacted, metadata = compact_context_pack(pack, 4)

    assert compacted["pack_hash"] == context_pack_hash(compacted)
    assert compacted["task"]["node_spec"]["title"] == pack["task"]["node_spec"]["title"]
    assert compacted["state"]["current_draft"]["claims"][0]["claim_id"] == "claim-1"
    assert compacted["state"]["current_draft"]["evidence_refs"][0]["evidence_id"] == "evidence-0"
    assert len(compacted["evidence"]) <= 18
    assert compacted["analysis"] == []
    assert metadata["after_resources"]["documents"] <= 6
    assert metadata["after_resources"]["evidence"] <= 12
    assert any(item["reason"] == "context_budget" for item in compacted["omissions"])
    assert all(item.get("access", {}).get("capability") for item in compacted["evidence"])
    payload = {"objective": context["objective"]}
    assert "结构化流程上下文" not in payload["objective"]
