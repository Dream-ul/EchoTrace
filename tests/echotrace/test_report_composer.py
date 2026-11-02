from __future__ import annotations

import json
from copy import deepcopy

import pytest

from app.investigation_composition import (
    _normalize_report_plan_payload,
    _validate_report_plan_answer,
    _validate_report_section_blocks,
)
from echotrace.application.report_composer import (
    assemble_report,
    build_report_material_pack,
    build_source_limits_fallback_section,
    compose_report,
    _visual_rows_usable,
    _normalize_section_for_assembly,
    _normalize_section_citations,
    _metric_family_compatible,
    _scope_node_material_ids,
    _materialize_channel_composition_charts,
    _deduplicate_equivalent_charts,
    _report_dataset_source_block_exists,
)
from echotrace.application.report_templates import load_report_template
from echotrace.application.node_trace import NodeTrace
from echotrace.domain.errors import TraceContractError
from echotrace.domain.reporting import ReportBlock


def _trace() -> NodeTrace:
    return NodeTrace(
        trace_id="trace-1",
        run_id="run-1",
        investigation_type="public_event",
        subject="测试事件",
        template_id="public_event.trace",
        template_version="trace-template.v1",
        nodes=(
            {
                "node_id": "node-1",
                "slot_id": "origin_early_signals",
                "node_order": 1,
                "status": "frozen",
                "frozen_revision": 3,
                "title": "起点与早期信号",
                "goal": "说明事件起点",
                "draft": {
                    "conclusion": "材料显示事件由一则公开说明引发。",
                    "revision": 3,
                    "claims": [{
                        "claim_id": "claim-1",
                        "text": "主体发布了公开说明。",
                        "source_document_ids": ["doc-1"],
                        "evidence_ids": ["evidence-1"],
                    }],
                    "evidence": [{
                        "evidence_id": "evidence-1",
                        "claim_id": "claim-1",
                        "document_id": "doc-1",
                        "excerpt": "公开说明原文片段",
                        "locator": {"url": "https://example.test/source"},
                    }],
                    "media": [],
                    "metrics": [],
                    "datasets": [],
                },
            },
        ),
        edges=(),
    )


def _plan_payload(trace: NodeTrace) -> dict:
    template = load_report_template("public_event")
    return {
        "report_plan": {
        "report_id": "report_run-1",
        "template_id": template.template_id,
        "title": "测试事件调查报告",
        "subtitle": "调查材料与可复核依据",
        "summary": "材料显示事件由一则公开说明引发。",
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "focus": chapter.purpose,
            }
            for chapter in template.chapters
        ],
        }
    }


def _section_payload(chapter, *, citation: str = "claim-1") -> dict:
    chapter_id = chapter.get("chapter_id") if isinstance(chapter, dict) else chapter.chapter_id
    subheadings = chapter.get("subheadings", []) if isinstance(chapter, dict) else chapter.subheadings
    blocks = []
    for heading in subheadings:
        level = heading.get("level") if isinstance(heading, dict) else heading.level
        title = heading.get("title") if isinstance(heading, dict) else heading.title
        blocks.extend([{
            "type": "heading",
            "payload": {"level": level, "text": title},
            "citations": [],
            "dataset_ids": [],
        }, {
            "type": "paragraph",
            "payload": {"text": "节点材料显示公开说明是最早可定位的信号。"},
            "citations": [citation],
            "dataset_ids": [],
        }])
    return {
        "chapter_id": chapter_id,
        "summary": "本章基于冻结节点整理。",
        "blocks": blocks,
        "metrics": [], "datasets": [], "visualizations": [], "media": [],
    }


def test_material_pack_contains_only_frozen_trace_material():
    pack = build_report_material_pack(_trace())
    assert pack["source_document_ids"] == ["doc-1"]
    assert pack["nodes"][0]["claims"][0]["claim_id"] == "claim-1"
    assert "content" not in pack["nodes"][0]


def test_node_local_material_ids_are_scoped_when_combining_trace_nodes():
    trace = _trace()
    first = deepcopy(trace.nodes[0])
    first["draft"]["metrics"] = [{
        "metric_id": "metric-1", "name": "指标一", "value": 1, "unit": "项",
        "time_scope": "调查期间", "source_document_id": "doc-1", "evidence_id": "evidence-1",
        "locator": {"url": "https://example.test/source"}, "direct_or_derived": "direct",
        "formula": "", "geography": "", "subject": "", "population": "",
        "denominator": "", "limitation": "", "confidence": None,
    }]
    first["draft"]["datasets"] = [{
        "dataset_id": "event_timeline", "purpose": "事件时间线", "columns": ["event", "citation_ids"],
        "rows": [{"event": "首次通报", "citation_ids": ["claim-1"]}], "grain": "单条事件",
        "sample_scope": "冻结节点材料", "time_scope": "调查期间", "method": "按来源整理",
        "input_metric_ids": ["metric-1"], "citation_ids": ["claim-1"], "limitations": [],
    }]
    second = deepcopy(first)
    second["node_id"] = "node-2"
    second["slot_id"] = "event_scope"
    second["node_order"] = 2
    trace = NodeTrace(
        trace_id=trace.trace_id, run_id=trace.run_id, investigation_type=trace.investigation_type,
        subject=trace.subject, template_id=trace.template_id, template_version=trace.template_version,
        nodes=(first, second), edges=trace.edges,
    )

    scoped = _scope_node_material_ids(trace)
    first_draft = scoped.nodes[0]["draft"]
    second_draft = scoped.nodes[1]["draft"]
    assert first_draft["metrics"][0]["metric_id"] != second_draft["metrics"][0]["metric_id"]
    assert first_draft["datasets"][0]["dataset_id"] != second_draft["datasets"][0]["dataset_id"]
    assert first_draft["datasets"][0]["input_metric_ids"] == [first_draft["metrics"][0]["metric_id"]]
    assert second_draft["datasets"][0]["input_metric_ids"] == [second_draft["metrics"][0]["metric_id"]]


def test_section_template_dataset_alias_resolves_to_materialized_dataset():
    section = {
        "chapter": {
            "chapter_id": "origin_scope",
            "blocks": [{
                "type": "timeline", "payload": {"title": "事件进展"},
                "citations": ["claim-1"], "dataset_ids": ["event_timeline"],
            }],
        },
        "datasets": [],
    }

    normalized = _normalize_section_for_assembly(
        section, available_dataset_ids={"report_template_event_timeline"},
    )
    assert normalized["chapter"]["blocks"][0]["dataset_ids"] == ["report_template_event_timeline"]

    prose = _normalize_section_for_assembly({
        "chapter": {"chapter_id": "spread_turning", "blocks": [{
            "type": "paragraph", "payload": {"text": "材料暂未形成可比较的传播序列。"},
            "citations": [], "dataset_ids": ["spread_trend"],
        }]},
    }, available_dataset_ids=set())
    assert prose["chapter"]["blocks"][0]["dataset_ids"] == []


def test_compose_report_parses_and_validates_json_agent_result():
    trace = _trace()
    template = load_report_template("public_event")

    def runner(role, objective, context):
        assert role == "report_composer"
        assert context["report_template"]["content_hash"] == template.content_hash
        if context["phase"] == "plan":
            return json.dumps(_plan_payload(trace), ensure_ascii=False)
        assert [item["chapter_id"] for item in context["chapter"]["report_context"]["chapter_outline"]] == [
            chapter.chapter_id for chapter in template.chapters
        ]
        report_context = context["chapter"]["report_context"]
        current_order = context["chapter"]["current_outline"]["order"]
        assert report_context["previous_chapter"] == (
            report_context["chapter_outline"][current_order - 2] if current_order > 1 else None
        )
        assert report_context["next_chapter"] == (
            report_context["chapter_outline"][current_order] if current_order < len(template.chapters) else None
        )
        return json.dumps(_section_payload(context["chapter"]), ensure_ascii=False)

    report = compose_report(trace=trace, template=template, report_id="report_run-1", runner=runner)
    assert report.report_id == "report_run-1"
    assert [chapter.chapter_id for chapter in report.chapters] == [chapter.chapter_id for chapter in template.chapters]


def test_compose_report_uses_application_report_id_when_plan_contains_another_id():
    trace = _trace()
    template = load_report_template("public_event")
    plan = _plan_payload(trace)
    plan["report_plan"]["report_id"] = "model-generated-id"

    report = compose_report(
        trace=trace,
        template=template,
        report_id="report_run-1",
        runner=lambda _role, _objective, context: plan if context["phase"] == "plan" else _section_payload(context["chapter"]),
    )

    assert report.report_id == "report_run-1"


def test_report_plan_accepts_bare_plan_with_full_chapter_metadata():
    template = load_report_template("public_event")
    payload = {
        "report_id": "model-generated-id",
        "title": "娴嬭瘯璋冩煡鎶ュ憡",
        "summary": "鏍规嵁宸插喕缁撴潗鏂欐暣鐞嗐€?",
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "focus": chapter.purpose,
                "node_slots": list(chapter.node_slots),
                "recommended_blocks": list(chapter.recommended_blocks),
            }
            for chapter in template.chapters
        ],
    }

    normalized = _normalize_report_plan_payload(payload)
    _validate_report_plan_answer(
        normalized,
        {"report_template": {"chapters": [
            {"chapter_id": chapter.chapter_id} for chapter in template.chapters
        ]}},
    )

    assert normalized["report_plan"] is payload


def test_report_plan_normalizer_keeps_documented_envelope_unchanged():
    payload = {"report_plan": {"title": "title", "summary": "summary", "chapters": []}}

    assert _normalize_report_plan_payload(payload) is payload


def test_compose_report_drops_unknown_citations_and_keeps_valid_sources():
    trace = _trace()
    template = load_report_template("public_event")

    def runner(role, objective, context):
        if context["phase"] == "plan":
            return _plan_payload(trace)
        section = _section_payload(context["chapter"], citation="claim-1")
        for block in section["blocks"]:
            if block["type"] == "paragraph":
                block["citations"].append("claim-unknown")
        return section

    report = compose_report(
        trace=trace,
        template=template,
        report_id="report_run-1",
        runner=runner,
    )
    citations = {
        citation
        for chapter in report.chapters
        for block in chapter.blocks
        for citation in block.citations
    }
    assert "claim-unknown" not in citations
    assert "claim-1" in citations
    assert "公开说明" in " ".join(
        block.payload.get("text", "")
        for chapter in report.chapters
        for block in chapter.blocks
    )


def test_section_citation_normalization_reports_unknown_ids():
    section = {
        "chapter": {"chapter_id": "spread_turning", "blocks": [{
            "type": "paragraph",
            "payload": {"text": "正文保留"},
            "citations": ["claim-1", "evidence-unknown"],
            "dataset_ids": [],
        }]},
    }

    normalized = _normalize_section_citations(section, {"claim-1"})

    assert normalized["chapter"]["blocks"][0]["payload"]["text"] == "正文保留"
    assert normalized["chapter"]["blocks"][0]["citations"] == ["claim-1"]
    assert normalized["_citation_warnings"] == [{
        "chapter_id": "spread_turning",
        "unknown_ids": ["evidence-unknown"],
    }]


def test_compose_report_rejects_unplanned_section_headings():
    trace = _trace()
    template = load_report_template("public_event")

    def runner(role, objective, context):
        if context["phase"] == "plan":
            return _plan_payload(trace)
        section = _section_payload(context["chapter"])
        section["blocks"][0]["payload"]["text"] = "模型自创标题"
        return section

    with pytest.raises(TraceContractError, match="必须完整匹配 Markdown 模板"):
        compose_report(trace=trace, template=template, report_id="report_run-1", runner=runner)


def test_compose_report_requires_content_under_each_template_heading():
    trace = _trace()
    template = load_report_template("public_event")

    def runner(role, objective, context):
        if context["phase"] == "plan":
            return _plan_payload(trace)
        section = _section_payload(context["chapter"])
        section["blocks"] = [block for block in section["blocks"] if block["type"] == "heading"]
        return section

    with pytest.raises(TraceContractError, match="每个子标题后都必须有正文内容"):
        compose_report(trace=trace, template=template, report_id="report_run-1", runner=runner)


def test_compose_report_rejects_wrong_report_identity():
    trace = _trace()
    template = load_report_template("public_event")
    with pytest.raises(TraceContractError, match="报告规划必须包含中文"):
        compose_report(
            trace=trace,
            template=template,
            report_id="report_run-1",
            runner=lambda *_: {"report_plan": {"chapters": []}},
        )


def test_compose_report_uses_frozen_metric_when_model_repeats_edited_metric():
    trace = _trace()
    trace.nodes[0]["draft"]["metrics"] = [{
        "metric_id": "metric-1",
        "name": "样本数",
        "value": 3,
        "unit": "条",
        "time_scope": "调查期间",
        "source_document_id": "doc-1",
        "evidence_id": "evidence-1",
        "locator": {"url": "https://example.test/source"},
        "direct_or_derived": "direct",
        "formula": "",
        "geography": "",
        "subject": "",
        "population": "",
        "denominator": "",
        "limitation": "",
        "confidence": None,
    }]
    template = load_report_template("public_event")

    def runner(role, objective, context):
        if context["phase"] == "plan":
            return _plan_payload(trace)
        section = _section_payload(context["chapter"])
        section["metrics"] = [{"metric_id": "metric-1", "value": 999}]
        return section

    report = compose_report(trace=trace, template=template, report_id="report_run-1", runner=runner)

    assert report.metrics[0].metric_id == "metric-1"
    assert report.metrics[0].value == 3


def test_report_section_accepts_supported_blocks_beyond_template_recommendations():
    context = {
        "chapter": {
            "chapter_id": "origin_scope",
            "recommended_blocks": ["paragraph", "timeline", "evidence_card"],
        }
    }
    _validate_report_section_blocks(
        {"chapter_id": "origin_scope", "blocks": [{"type": "paragraph", "payload": {}}]}, context
    )
    _validate_report_section_blocks(
        {"chapter_id": "origin_scope", "blocks": [{"type": "ordered_list", "payload": {"items": ["第一步", "第二步"]}}]}, context
    )
    with pytest.raises(TraceContractError, match="allowed types"):
        _validate_report_section_blocks(
            {"chapter_id": "origin_scope", "blocks": [{"type": "unsupported"}]}, context
        )
    section = {
        "chapter_id": "origin_scope",
        "blocks": [{
            "type": "timeline",
            "payload": {"title": "事件顺序", "items": [
                {"time": "2026-08-01", "event": "投诉披露"},
                {"time": "2026-08-04", "event": "企业回应"},
            ]},
            "citations": ["claim-1"],
            "dataset_ids": [],
        }],
    }
    _validate_report_section_blocks(section, context)
    block = section["blocks"][0]
    assert block["type"] == "ordered_list"
    assert block["payload"]["items"] == ["2026-08-01：投诉披露", "2026-08-04：企业回应"]
    assert block["citations"] == ["claim-1"]


def test_report_section_preserves_unbound_visual_data_as_claim_table():
    section = {
        "chapter_id": "response_results",
        "blocks": [{
            "type": "metric_grid",
            "payload": {"title": "处理数据", "metrics": [
                {"name": "投诉数", "value": 171},
                {"name": "平台反馈下架数", "value": 144},
            ]},
            "citations": ["claim-1"],
            "dataset_ids": [],
        }],
    }

    _validate_report_section_blocks(section, {"chapter": {"chapter_id": "response_results"}})

    block = section["blocks"][0]
    assert block["type"] == "claim_table"
    assert block["payload"]["rows"][0] == {"name": "投诉数", "value": 171}
    assert block["citations"] == ["claim-1"]


def test_report_section_drops_chart_without_dataset_or_reader_facing_content():
    section = {
        "chapter_id": "spread_turning",
        "blocks": [{"type": "chart", "payload": {"title": "传播趋势"}, "citations": [], "dataset_ids": []}],
    }

    _validate_report_section_blocks(section, {"chapter": {"chapter_id": "spread_turning"}})

    assert section["blocks"] == []


def test_report_section_normalizes_evidence_table_without_dataset_to_claim_table():
    section = {
        "chapter_id": "response_results",
        "blocks": [{
            "type": "table",
            "payload": {"title": "各方回应", "rows": [{"主体": "机构", "回应": "启动核查"}]},
            "citations": ["claim-1", "claim-1"],
            "dataset_ids": [],
        }],
    }

    _validate_report_section_blocks(section, {"chapter": {"chapter_id": "response_results"}})

    assert section["blocks"][0]["type"] == "claim_table"
    assert section["blocks"][0]["citations"] == ["claim-1"]
    assert section["blocks"][0]["payload"]["rows"][0]["回应"] == "启动核查"


@pytest.mark.parametrize("block", [None, "not-an-object", 7, []])
def test_report_section_rejects_non_object_blocks_with_index(block):
    context = {"chapter": {"chapter_id": "origin_scope", "recommended_blocks": ["paragraph"]}}

    with pytest.raises(TraceContractError, match=r"block\[1\] must be an object"):
        _validate_report_section_blocks(
            {"chapter_id": "origin_scope", "blocks": [{"type": "paragraph", "payload": {}}, block]},
            context,
        )


def test_sources_limits_fallback_is_text_only_and_trace_grounded():
    trace = _trace()
    template = load_report_template("public_event")
    chapter = next(item for item in template.chapters if item.chapter_id == "sources_limits")

    section = build_source_limits_fallback_section(trace=trace, chapter=chapter, order=5, reason="invalid block")

    assert {item["type"] for item in section["chapter"]["blocks"]} == {"heading", "source_list", "paragraph"}
    assert section["datasets"] == []
    assert section["visualizations"] == []
    source_block = next(item for item in section["chapter"]["blocks"] if item["type"] == "source_list")
    assert source_block["payload"]["source_ids"] == ["doc-1"]
    assert "结构校验未通过" not in " ".join(
        str(item.get("payload", {}).get("text") or "") for item in section["chapter"]["blocks"]
    )
    for block in section["chapter"]["blocks"]:
        ReportBlock.from_dict(block)


def test_assembly_repairs_unbound_visual_blocks_and_injects_trace_dataset():
    trace = _trace()
    trace.nodes[0]["draft"]["datasets"] = [{
        "dataset_id": "dataset-1",
        "purpose": "用于验收",
        "columns": ["label", "value"],
        "rows": [{"label": "样本", "value": 1}],
        "grain": "每项",
        "sample_scope": "测试材料",
        "time_scope": "调查期间",
        "method": "来源归纳",
        "input_metric_ids": [],
        "citation_ids": ["claim-1"],
        "limitations": [],
    }]
    template = load_report_template("public_event")
    plan = _plan_payload(trace)["report_plan"]
    plan["toc"] = [{"chapter_id": chapter.chapter_id, "title": chapter.title} for chapter in template.chapters]
    sections = []
    for order, chapter in enumerate(template.chapters, 1):
        payload = _section_payload(chapter)
        sections.append({
            "chapter": {
                "chapter_id": chapter.chapter_id,
                "title": chapter.title,
                "order": order,
                "purpose": chapter.purpose,
                "summary": payload["summary"],
                "blocks": payload["blocks"],
            },
            "metrics": [], "datasets": [], "visualizations": [], "media": [],
        })
    sections[0]["chapter"]["blocks"] = [{
        "type": "metric_grid",
        "payload": {"metrics": [{"name": "样本", "value": 1}]},
        "citations": ["claim-1"],
        "dataset_ids": [],
    }]
    sections[1]["chapter"]["blocks"] = [{
        "type": "table",
        "payload": {"rows": [{"label": "样本", "value": 1}]},
        "citations": ["claim-1"],
        "dataset_ids": ["dataset-1"],
    }]
    report = assemble_report(
        trace=trace, template=template, report_id="report_run-1", plan=plan, sections=sections,
    )
    assert report.chapters[0].blocks[0].type == "paragraph"
    assert [item.dataset_id for item in report.datasets] == ["dataset-1"]


def test_assembly_ignores_model_metric_copies_with_unknown_ids():
    trace = _trace()
    trace.nodes[0]["draft"]["metrics"] = [{
        "metric_id": "metric-frozen-1", "name": "metric count", "value": 42, "unit": "items",
        "time_scope": "2026", "source_document_id": "doc-1", "evidence_id": "evidence-1",
        "locator": {"url": "https://example.test/source"}, "direct_or_derived": "direct",
        "formula": "", "geography": "", "subject": "", "population": "",
        "denominator": "", "limitation": "", "confidence": None,
    }]
    template = load_report_template("public_event")
    plan = _plan_payload(trace)["report_plan"]
    plan["toc"] = [{"chapter_id": chapter.chapter_id, "title": chapter.title} for chapter in template.chapters]
    sections = []
    for order, chapter in enumerate(template.chapters, 1):
        payload = _section_payload(chapter)
        if chapter.chapter_id == "response_outcome":
            payload["metrics"] = [{
                "metric_id": "metric_enterprise_monitoring_volume",
                "name": "metric count", "value": 42, "unit": "items",
            }]
        sections.append({
            "chapter": {
                "chapter_id": chapter.chapter_id,
                "title": chapter.title,
                "order": order,
                "purpose": chapter.purpose,
                "summary": payload["summary"],
                "blocks": payload["blocks"],
            },
            "metrics": payload["metrics"], "datasets": [], "visualizations": [], "media": [],
        })

    report = assemble_report(
        trace=trace, template=template, report_id="report_run-1", plan=plan, sections=sections,
    )

    assert [item.metric_id for item in report.metrics] == ["metric-frozen-1"]


def test_assembly_materializes_metric_cards_and_template_timeline_from_trace_data():
    trace = _trace()
    trace.nodes[0]["draft"]["metrics"] = [{
        "metric_id": "metric-1", "name": "复核材料", "value": 91, "unit": "份",
        "time_scope": "2026年", "source_document_id": "doc-1", "evidence_id": "evidence-1",
        "locator": {"url": "https://example.test/source"}, "direct_or_derived": "direct",
        "formula": "", "geography": "", "subject": "", "population": "",
        "denominator": "", "limitation": "", "confidence": None,
    }]
    trace.nodes[0]["draft"]["datasets"] = [{
        "dataset_id": "dataset-timeline", "purpose": "按时间整理事件节点、主体和可定位来源",
        "columns": ["time", "event", "subject", "citation_ids"],
        "rows": [
            {"time": "2026-01-01", "event": "发布通报", "subject": "机构", "citation_ids": ["claim-1"]},
            {"time": "2026-01-02", "event": "启动核查", "subject": "监管部门", "citation_ids": ["claim-1"]},
            {"time": "2026-01-03", "event": "公布结果", "subject": "调查组", "citation_ids": ["claim-1"]},
        ],
        "grain": "每条观察", "sample_scope": "冻结节点中的来源数据", "time_scope": "2026年",
        "method": "直接整理", "input_metric_ids": [], "citation_ids": ["claim-1"], "limitations": [],
    }]
    template = load_report_template("public_event")
    sections = []
    for order, chapter in enumerate(template.chapters, 1):
        sections.append({
            "chapter": {
                "chapter_id": chapter.chapter_id, "title": chapter.title, "order": order,
                "purpose": chapter.purpose, "summary": "基于冻结节点整理。", "blocks": [],
            },
            "metrics": [], "datasets": [], "visualizations": [], "media": [],
        })
    plan = _plan_payload(trace)["report_plan"]
    plan["toc"] = [{"chapter_id": chapter.chapter_id, "title": chapter.title} for chapter in template.chapters]
    report = assemble_report(
        trace=trace, template=template, report_id="report_run-1",
        plan=plan, sections=sections,
        source_catalog=[{"document_id": "doc-1", "title": "调查通报", "url": "https://example.test/source"}],
    )
    assert report.metrics[0].metric_id == "metric-1"
    assert any(block.type == "metric_grid" for block in report.chapters[0].blocks)
    assert any(block.type == "timeline" for block in report.chapters[1].blocks)
    assert any(visual.type == "timeline" for visual in report.visualizations)
    assert report.sources[0]["title"] == "调查通报"


def test_assembly_materializes_a_chart_only_from_comparable_metrics():
    trace = _trace()
    trace.nodes[0]["draft"]["metrics"] = [
        {
            "metric_id": "metric-click-1", "name": "处分通报页面点击量", "value": "12万余次", "unit": "次",
            "time_scope": "2025年7月31日16时前", "source_document_id": "doc-1", "evidence_id": "evidence-1",
            "locator": {"url": "https://example.test/source"}, "direct_or_derived": "direct", "formula": "",
            "geography": "", "subject": "", "population": "", "denominator": "", "limitation": "", "confidence": None,
        },
        {
            "metric_id": "metric-click-2", "name": "同一处分通报页面点击量（后续时点）", "value": "超17万", "unit": "次",
            "time_scope": "2025年7月31日晚间", "source_document_id": "doc-1", "evidence_id": "evidence-1",
            "locator": {"url": "https://example.test/source"}, "direct_or_derived": "direct", "formula": "",
            "geography": "", "subject": "", "population": "", "denominator": "", "limitation": "", "confidence": None,
        },
    ]
    template = load_report_template("public_event")
    sections = []
    for order, chapter in enumerate(template.chapters, 1):
        sections.append({
            "chapter": {
                "chapter_id": chapter.chapter_id, "title": chapter.title, "order": order,
                "purpose": chapter.purpose, "summary": "基于冻结节点整理。", "blocks": [],
            },
            "metrics": [], "datasets": [], "visualizations": [], "media": [],
        })
    plan = _plan_payload(trace)["report_plan"]
    plan["toc"] = [{"chapter_id": chapter.chapter_id, "title": chapter.title} for chapter in template.chapters]
    report = assemble_report(
        trace=trace, template=template, report_id="report_run-1", plan=plan, sections=sections,
        source_catalog=[{"document_id": "doc-1", "title": "调查通报", "url": "https://example.test/source"}],
    )
    assert any(item.visualization_id == "report_metric-series" for item in report.visualizations)
    series = next(item for item in report.datasets if item.dataset_id == "report_metric_series")
    assert series.rows[0]["数值"] == 120000
    assert series.rows[1]["数值"] == 170000
    assert any(block.type == "chart" for chapter in report.chapters for block in chapter.blocks)


def test_channel_share_table_gets_cited_pie_chart_with_complete_matching_source_data():
    from echotrace.domain.reporting import AnalysisDataset, ReportChapter, ReportDocument

    chapter = ReportChapter(
        "spread_turning", "传播变化", 1, "说明传播变化", "样本渠道分布", [ReportBlock(
            "table",
            {
                "title": "监测样本内的渠道分布",
                "columns": ["平台", "信息量（条）", "占监测样本比例"],
                "rows": [
                    ["短视频平台", "22,636", "36.0%"],
                    ["客户端", "21,381", "34.0%"],
                    ["微博", "12,322", "19.6%"],
                    ["微信", "4,071", "6.5%"],
                    ["其他渠道", "2,449", "3.9%"],
                ],
            },
            ["claim-1"], ["dataset-1"],
        )],
    )
    report = ReportDocument(
        report_id="report-1", run_id="run-1", trace_id="trace-1",
        investigation_type="public_event", template_id="public_event.report",
        template_version="1.0", template_content_hash="hash-1",
        title="报告", subtitle="", summary="摘要", chapters=[chapter],
        datasets=[AnalysisDataset(
            dataset_id="dataset-1", purpose="渠道信息", columns=["平台", "数量", "占比"],
            rows=[
                {"平台": "短视频平台", "数量": "22,636", "占比": "36.0%"},
                {"平台": "客户端", "数量": "21,381", "占比": "34.0%"},
                {"平台": "微博", "数量": "12,322", "占比": "19.6%"},
                {"平台": "微信", "数量": "4,071", "占比": "6.5%"},
                {"平台": "其他渠道", "数量": "2,449", "占比": "3.9%"},
            ], grain="平台", sample_scope="监测样本", time_scope="调查期间",
            method="直接整理", citation_ids=["claim-1"],
        )],
    )

    _materialize_channel_composition_charts(report)

    visual = next(item for item in report.visualizations if item.chart_kind == "pie")
    dataset = next(item for item in report.datasets if item.dataset_id == visual.dataset_id)
    assert dataset.source_dataset_ids == ["dataset-1"]
    assert dataset.rows[-1]["channel"] == "其他渠道"
    assert dataset.rows[-1]["share"] == "3.9%"
    assert len(dataset.rows) == 5
    assert chapter.blocks[1].type == "chart"
    assert _report_dataset_source_block_exists(report, dataset)


def test_channel_share_table_parses_labeled_columns_and_mapping_rows():
    from echotrace.domain.reporting import AnalysisDataset, ReportChapter, ReportDocument

    chapter = ReportChapter(
        "spread_turning", "传播变化", 1, "说明传播变化", "样本渠道分布", [ReportBlock(
            "table",
            {
                "title": "监测样本内的渠道分布",
                "columns": [
                    {"key": "platform", "label": "平台"},
                    {"key": "information_count", "label": "信息量（条）"},
                    {"key": "share", "label": "占监测样本比例"},
                ],
                "rows": [
                    {"platform": "短视频", "information_count": "60", "share": "60.0%"},
                    {"platform": "社交平台", "information_count": "40", "share": "40.0%"},
                ],
            },
            ["claim-1"], ["dataset-1"],
        )],
    )
    report = ReportDocument(
        report_id="report-1", run_id="run-1", trace_id="trace-1",
        investigation_type="public_event", template_id="public_event.report",
        template_version="1.0", template_content_hash="hash-1",
        title="报告", subtitle="", summary="摘要", chapters=[chapter],
        datasets=[AnalysisDataset(
            dataset_id="dataset-1", purpose="渠道信息", columns=["platform", "count", "share"],
            rows=[
                {"platform": "短视频", "count": "60", "share": "60.0%"},
                {"platform": "社交平台", "count": "40", "share": "40.0%"},
            ],
            grain="渠道占比", sample_scope="监测样本", time_scope="调查期间",
            method="直接整理", citation_ids=["claim-1"],
        )],
    )

    _materialize_channel_composition_charts(report)

    visual = next(item for item in report.visualizations if item.chart_kind == "pie")
    dataset = next(item for item in report.datasets if item.dataset_id == visual.dataset_id)
    assert [(row["channel"], row["share"]) for row in dataset.rows] == [
        ("短视频", "60.0%"), ("社交平台", "40.0%"),
    ]


def test_claim_table_channel_share_uses_matching_total_for_unlisted_pie_slice():
    from echotrace.domain.reporting import MetricObservation, ReportChapter, ReportDocument

    chapter = ReportChapter(
        "spread_turning", "传播变化", 1, "说明传播变化", "样本渠道分布", [ReportBlock(
            "claim_table",
            {
                "title": "渠道分布（第三方监测样本）",
                "caption": "本表来自2026年7月21日至8月6日14时的单一监测系统复盘。",
                "columns": ["渠道", "信息量（条）", "占监测总量比例"],
                "rows": [
                    ["视频", "22,636", "36.0%"],
                    ["客户端", "21,381", "34.0%"],
                    ["微博", "12,322", "19.6%"],
                    ["微信", "4,071", "6.5%"],
                ],
            },
            ["claim-1"], [],
        )],
    )
    metric = MetricObservation(
        metric_id="metric-total", name="第三方监测累计相关网络信息量",
        value=62841, unit="条", time_scope="2026-07-21至2026-08-06 14:00",
        source_document_id="doc-1", evidence_id="evidence-1",
        locator={"url": "https://example.test/source"},
    )
    report = ReportDocument(
        report_id="report-1", run_id="run-1", trace_id="trace-1",
        investigation_type="public_event", template_id="public_event.report",
        template_version="1.0", template_content_hash="hash-1",
        title="报告", subtitle="", summary="摘要", chapters=[chapter],
        metrics=[metric],
    )
    trace = _trace()
    node = deepcopy(trace.nodes[0])
    node["draft"]["metrics"] = [metric.to_dict()]
    trace = NodeTrace(
        trace_id=trace.trace_id, run_id=trace.run_id,
        investigation_type=trace.investigation_type, subject=trace.subject,
        template_id=trace.template_id, template_version=trace.template_version,
        nodes=(node,), edges=trace.edges,
    )

    incomplete_denominator = deepcopy(report)
    incomplete_denominator.metrics[0].value = 60000
    wrong_period = deepcopy(report)
    wrong_period.metrics[0].time_scope = "2025-07-21至2025-08-06 14:00"

    _materialize_channel_composition_charts(report)
    _materialize_channel_composition_charts(incomplete_denominator)
    _materialize_channel_composition_charts(wrong_period)

    visual = next(item for item in report.visualizations if item.chart_kind == "pie")
    dataset = next(item for item in report.datasets if item.dataset_id == visual.dataset_id)
    assert dataset.input_metric_ids == ["metric-total"]
    assert [(row["channel"], row["count"], row["share"]) for row in dataset.rows] == [
        ("视频", 22636, "36.0%"),
        ("客户端", 21381, "34.0%"),
        ("微博", 12322, "19.6%"),
        ("微信", 4071, "6.5%"),
        ("其它", 2431, "3.9%"),
    ]
    assert dataset.rows[0]["citation_ids"] == ["claim-1"]
    assert dataset.rows[-1]["citation_ids"] == ["claim-1", "evidence-1"]
    assert dataset.citation_ids == ["claim-1", "evidence-1"]
    assert chapter.blocks[1].type == "chart"
    assert _report_dataset_source_block_exists(report, dataset)
    from echotrace.application.report_composer import _validate_report_references

    _validate_report_references(report, trace)
    assert not incomplete_denominator.visualizations
    assert len(incomplete_denominator.chapters[0].blocks) == 1
    assert not wrong_period.visualizations
    assert len(wrong_period.chapters[0].blocks) == 1
    from echotrace.application.report_renderers import render_report_html

    rendered = render_report_html(report)
    assert "<path" in rendered
    assert "其它" in rendered
    assert "未列部分" not in rendered
    assert "其它部分按同口径监测总量减去表中已列渠道数量计算" in rendered


def test_equivalent_template_and_model_charts_are_deduplicated():
    from echotrace.domain.reporting import AnalysisDataset, ReportChapter, ReportDocument, VisualizationSpec

    rows = [
        {"time": "2026-08-03", "count": 10000},
        {"time": "2026-08-04", "count": 16706},
    ]
    datasets = [AnalysisDataset(
        dataset_id=dataset_id, purpose="传播观测变化", columns=["time", "count"], rows=rows,
        grain="单日相关信息量", sample_scope="同一监测系统", time_scope="调查期间",
        method="来源整理", citation_ids=["claim-1"],
    ) for dataset_id in ("template-trend", "agent-trend")]
    datasets[1].time_scope = "另一种时间范围描述"
    visuals = [VisualizationSpec(
        visualization_id=visual_id, type="chart", title=title, dataset_id=dataset_id,
        fallback="table", chart_kind=kind, x_field="time", y_field="count",
        citations=["claim-1"],
    ) for visual_id, title, dataset_id, kind in (
        ("template", "传播观测变化", "template-trend", "line"),
        ("agent", "全网信息量逐日变化", "agent-trend", "bar"),
    )]
    chapter = ReportChapter("spread_turning", "传播变化", 1, "传播", "变化", [
        ReportBlock("chart", {"title": "传播观测变化", "visualization_id": "template"}, ["claim-1"], ["template-trend"]),
        ReportBlock("chart", {"title": "全网信息量逐日变化", "visualization_id": "agent"}, ["claim-1"], ["agent-trend"]),
    ])
    report = ReportDocument(
        report_id="report-1", run_id="run-1", trace_id="trace-1",
        investigation_type="public_event", template_id="public_event.report",
        template_version="1.0", template_content_hash="hash-1",
        title="报告", subtitle="", summary="摘要", chapters=[chapter],
        datasets=datasets, visualizations=visuals,
    )

    _deduplicate_equivalent_charts(report)

    assert len([block for block in chapter.blocks if block.type == "chart"]) == 1


def test_raw_sample_distribution_chart_is_removed_when_aggregate_chart_exists():
    from echotrace.domain.reporting import AnalysisDataset, ReportChapter, ReportDocument, VisualizationSpec

    raw = AnalysisDataset(
        dataset_id="comment-samples",
        purpose="逐条评论样本",
        columns=["sample_id", "label", "count"],
        rows=[
            {"sample_id": "sample-1", "label": "positive", "count": 1},
            {"sample_id": "sample-2", "label": "positive", "count": 1},
            {"sample_id": "sample-3", "label": "negative", "count": 1},
        ],
        grain="逐条评论",
        sample_scope="评论样本",
        time_scope="调查期间",
        method="来源材料整理",
    )
    aggregate = AnalysisDataset(
        dataset_id="sentiment-summary",
        purpose="情感分布",
        columns=["label", "count"],
        rows=[
            {"label": "positive", "count": 2},
            {"label": "negative", "count": 1},
        ],
        grain="情感标签",
        sample_scope="评论样本",
        time_scope="调查期间",
        method="逐条样本计数",
    )
    detail = ReportBlock("table", {"title": "评论明细"}, dataset_ids=[raw.dataset_id])
    raw_chart = ReportBlock(
        "chart", {"title": "可定位评论样本情感分布", "visualization_id": "raw-visual"},
        dataset_ids=[raw.dataset_id],
    )
    aggregate_chart = ReportBlock(
        "chart", {"title": "情感分布", "visualization_id": "aggregate-visual"},
        dataset_ids=[aggregate.dataset_id],
    )
    chapter = ReportChapter("chapter-1", "样本分析", 1, "分析", "分析样本", [detail, raw_chart, aggregate_chart])
    report = ReportDocument(
        report_id="report-1", run_id="run-1", trace_id="trace-1",
        investigation_type="public_event", template_id="template", template_version="1",
        title="报告", summary="摘要", chapters=[chapter], template_content_hash="hash-1",
        datasets=[raw, aggregate],
        visualizations=[
            VisualizationSpec(
                visualization_id="raw-visual", type="chart", title="可定位评论样本情感分布",
                dataset_id=raw.dataset_id, fallback="table", chart_kind="bar",
                x_field="sample_id", y_field="count",
            ),
            VisualizationSpec(
                visualization_id="aggregate-visual", type="chart", title="情感分布",
                dataset_id=aggregate.dataset_id, fallback="table", chart_kind="pie",
                x_field="label", y_field="count",
            ),
        ],
    )

    _deduplicate_equivalent_charts(report)

    assert [block.type for block in chapter.blocks] == ["table", "chart"]
    assert chapter.blocks[0].dataset_ids == [raw.dataset_id]
    assert chapter.blocks[1].dataset_ids == [aggregate.dataset_id]


def test_metric_family_does_not_merge_similar_but_different_measurements():
    assert _metric_family_compatible(
        "处置通报页面点击量",
        "同一处置通报页面点击量（后续时间点）",
    )
    assert not _metric_family_compatible(
        "官方自报｜监测相关网络信息量",
        "第三方监测｜累计相关网络信息量",
    )


@pytest.mark.parametrize(
    "field, rows",
    [
        ("unit", [
            {"category": "批次一", "value": 10, "unit": "件"},
            {"category": "批次二", "value": 20, "unit": "%"},
        ]),
        ("denominator", [
            {"category": "地区一", "value": 10, "denominator": "已抽检产品"},
            {"category": "地区二", "value": 20, "denominator": "全部销售产品"},
        ]),
    ],
)
def test_template_chart_rejects_mixed_measurement_basis(field, rows):
    assert field in rows[0]
    assert not _visual_rows_usable("bar", rows, "category", "value")
