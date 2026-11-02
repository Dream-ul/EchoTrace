from __future__ import annotations

from dataclasses import asdict

from echotrace.application.node_trace import NodeTrace
from echotrace.application.report_composer import assemble_report
from echotrace.application.report_data import (
    analysis_feature_datasets_for_node,
    materialize_report_analysis_datasets,
    materialize_report_research_datasets,
    research_data_needs,
)
from echotrace.application.report_renderers import render_report_html
from echotrace.application.report_templates import load_report_template


def _features(count: int = 6) -> tuple[list[dict], list[dict]]:
    features = []
    evidence = []
    for index in range(count):
        document_id = f"doc-{index}"
        evidence_id = f"evidence-{index}"
        evidence.append({"evidence_id": evidence_id, "document_id": document_id})
        features.extend([
            {
                "feature_id": f"feature-sentiment-{index}",
                "document_id": document_id,
                "input_hash": f"hash-{index}",
                "factuality": "analysis_feature",
                "feature_type": "sentiment",
                "backend": "rules",
                "model_name": "echotrace-zh-rules",
                "model_version": "zh-rules.v1",
                "value": {"label_zh": "正向" if index % 2 == 0 else "负向"},
            },
            {
                "feature_id": f"feature-stance-{index}",
                "document_id": document_id,
                "input_hash": f"hash-{index}",
                "factuality": "analysis_feature",
                "feature_type": "stance",
                "backend": "rules",
                "model_name": "",
                "model_version": "",
                "value": {"label_zh": "支持" if index < 2 else "质疑"},
            },
            {
                "feature_id": f"feature-topic-{index}",
                "document_id": document_id,
                "input_hash": f"hash-{index}",
                "factuality": "analysis_feature",
                "feature_type": "topic",
                "backend": "rules",
                "model_name": "",
                "model_version": "",
                "value": {"topics": ["公共安全"] if index < 4 else ["公共舆情"]},
            },
        ])
    return features, evidence


def _feature_requirements(template, slot_id: str) -> list[dict]:
    return [
        asdict(item) for item in template.datasets
        if item.collection_mode == "analysis_feature" and slot_id in item.source_nodes
    ]


def test_orchestrator_research_requirements_are_typed_and_slot_scoped():
    template = load_report_template("public_event")
    needs = research_data_needs(template, "spread_amplification")

    assert [item["dataset_id"] for item in needs] == ["event_timeline", "spread_trend"]
    spread = next(item for item in needs if item["dataset_id"] == "spread_trend")
    assert spread["minimum_samples"] == 3
    assert "不同平台" in spread["collection_guidance"]
    assert "至少两个可比较时间点" in spread["collection_guidance"]
    assert spread["visuals"][0]["type"] == "line"


def test_analysis_features_become_cited_datasets_and_minimum_sample_charts():
    template = load_report_template("public_opinion")
    features, evidence = _features()
    source_datasets = analysis_feature_datasets_for_node(
        slot_id="core_positions",
        requirements=_feature_requirements(template, "core_positions"),
        analysis_features=features,
        evidence=evidence,
    )
    assert source_datasets
    assert all(dataset.source_feature_ids for dataset in source_datasets)
    assert all(dataset.citation_ids for dataset in source_datasets)
    assert all("不代表社交评论样本" in dataset.sample_scope for dataset in source_datasets)

    nodes = [{"slot_id": "core_positions", "draft": {
        "datasets": [dataset.to_dict() for dataset in source_datasets],
    }}]
    report_datasets = materialize_report_analysis_datasets(nodes=nodes, template=template)
    by_id = {item.dataset_id: item for item in report_datasets}
    sentiment = by_id["report_analysis_sentiment_distribution"]
    assert {row["label"]: row["count"] for row in sentiment.rows} == {"正向": 3, "负向": 3}
    assert sum(row["share"] for row in sentiment.rows) == 1
    assert set(sentiment.source_dataset_ids) == {item.dataset_id for item in source_datasets if item.purpose == "展示样本中的情感标签及其口径"}
    assert len(sentiment.source_feature_ids) == 6
    assert by_id["report_analysis_source_topic_distribution"].limitations[-1].endswith("合计可能超过 100%")


def test_analysis_feature_chart_is_omitted_below_template_sample_floor():
    template = load_report_template("public_opinion")
    features, evidence = _features(count=4)
    source_datasets = analysis_feature_datasets_for_node(
        slot_id="core_positions",
        requirements=_feature_requirements(template, "core_positions"),
        analysis_features=features,
        evidence=evidence,
    )
    nodes = [{"slot_id": "core_positions", "draft": {
        "datasets": [dataset.to_dict() for dataset in source_datasets],
    }}]

    report_datasets = materialize_report_analysis_datasets(nodes=nodes, template=template)

    assert "report_analysis_sentiment_distribution" not in {item.dataset_id for item in report_datasets}


def test_research_datasets_from_independent_nodes_join_only_as_template_allows():
    template = load_report_template("public_event")
    purpose = next(item.purpose for item in template.datasets if item.dataset_id == "event_timeline")
    columns = ["time", "event", "subject", "citation_ids"]
    nodes = [
        {"slot_id": "origin_early_signals", "draft": {"datasets": [{
            "dataset_id": "timeline-origin", "purpose": purpose, "columns": columns,
            "rows": [{"time": "2026-01-01", "event": "首发", "subject": "机构", "citation_ids": ["evidence-1"]}],
            "grain": "事件记录", "sample_scope": "已引用来源中的事件记录", "time_scope": "首发日期",
            "method": "按来源时间和事件整理", "input_metric_ids": [], "citation_ids": ["evidence-1"],
            "limitations": [],
        }]}},
        {"slot_id": "spread_amplification", "draft": {"datasets": [{
            "dataset_id": "timeline-spread", "purpose": purpose, "columns": columns,
            "rows": [
                {"time": "2026-01-02", "event": "开始转载", "subject": "媒体", "citation_ids": ["evidence-2"]},
                {"time": "2026-01-03", "event": "话题升温", "subject": "平台", "citation_ids": ["evidence-3"]},
            ],
            "grain": "传播事件", "sample_scope": "已引用来源中的传播记录", "time_scope": "传播期间",
            "method": "按来源时间和事件整理", "input_metric_ids": [], "citation_ids": ["evidence-2", "evidence-3"],
            "limitations": [],
        }]}},
    ]

    projected = materialize_report_research_datasets(nodes=nodes, template=template)

    timeline = next(item for item in projected if item.dataset_id == "report_template_event_timeline")
    assert len(timeline.rows) == 3
    assert timeline.source_dataset_ids == ["timeline-origin", "timeline-spread"]
    assert timeline.citation_ids == ["evidence-1", "evidence-2", "evidence-3"]


def test_comment_sample_dataset_aggregates_only_unique_cited_comments():
    template = load_report_template("public_opinion")
    requirement = next(item for item in template.datasets if item.dataset_id == "comment_sentiment_samples")
    rows = [
        {"sample_id": f"comment-{index}", "label": "负向" if index < 6 else "正向", "count": 1,
         "citation_ids": [f"evidence-{index}"]}
        for index in range(10)
    ]
    nodes = [{"slot_id": "core_positions", "draft": {"datasets": [{
        "dataset_id": "comments-core", "purpose": requirement.purpose,
        "columns": list(requirement.columns), "rows": rows,
        "grain": "每条去重评论", "sample_scope": "公开页面A，2026年1月", "time_scope": "2026年1月",
        "method": "按可见评论逐条编码", "input_metric_ids": [],
        "citation_ids": [f"evidence-{index}" for index in range(10)], "limitations": [],
    }]}}]

    result = materialize_report_research_datasets(nodes=nodes, template=template)

    comments = next(item for item in result if item.dataset_id == "report_template_comment_sentiment_samples")
    assert comments.columns == ["label", "count", "share", "citation_ids"]
    assert {row["label"]: row["count"] for row in comments.rows} == {"负向": 6, "正向": 4}
    assert sum(row["share"] for row in comments.rows) == 1
    assert comments.sample_scope.startswith("10 条去重且可定位的用户评论")


def test_report_assembly_renders_only_supported_feature_charts():
    template = load_report_template("public_opinion")
    features, evidence = _features()
    source_datasets = analysis_feature_datasets_for_node(
        slot_id="core_positions",
        requirements=_feature_requirements(template, "core_positions"),
        analysis_features=features,
        evidence=evidence,
    )
    claims = [{
        "claim_id": "claim-1", "text": "采集来源文本呈现不同表达信号。",
        "source_document_ids": [f"doc-{index}" for index in range(6)],
        "evidence_ids": [f"evidence-{index}" for index in range(6)],
    }]
    trace = NodeTrace(
        trace_id="trace-opinion", run_id="run-opinion", investigation_type="public_opinion",
        subject="公共议题", template_id="public_opinion.trace", template_version="trace-template.v1",
        nodes=({
            "node_id": "node-opinion", "slot_id": "core_positions", "node_order": 1,
            "status": "frozen", "frozen_revision": 3, "title": "核心观点",
            "goal": "说明样本中的主题、情感和立场",
            "draft": {
                "revision": 3, "conclusion": "来源文本分析仅描述本次采集样本。",
                "claims": claims,
                "evidence": [
                    {"evidence_id": f"evidence-{index}", "claim_id": "claim-1",
                     "document_id": f"doc-{index}", "excerpt": f"可定位来源片段 {index}",
                     "locator": {"url": f"https://example.test/{index}"}}
                    for index in range(6)
                ],
                "media": [], "metrics": [],
                "datasets": [dataset.to_dict() for dataset in source_datasets],
                "analysis_features": features,
            },
        },),
        edges=(),
    )
    sections = []
    for order, chapter in enumerate(template.chapters, 1):
        sections.append({
            "chapter": {
                "chapter_id": chapter.chapter_id, "title": chapter.title, "order": order,
                "purpose": chapter.purpose, "summary": "按冻结材料整理。", "blocks": [],
            },
            "metrics": [], "datasets": [], "visualizations": [], "media": [],
        })
    plan = {
        "title": "公共议题调查报告", "subtitle": "公开来源文本分析",
        "summary": "报告范围限于本次实际采集的公开来源文本。",
        "toc": [{"chapter_id": chapter.chapter_id, "title": chapter.title} for chapter in template.chapters],
    }

    report = assemble_report(
        trace=trace, template=template, report_id="report-opinion", plan=plan, sections=sections,
    )

    chart_ids = {item.visualization_id for item in report.visualizations}
    assert {"report_sentiment-distribution", "report_source-topic-distribution", "report_source-stance-distribution"}.issubset(chart_ids)
    assert any(block.type == "chart" for chapter in report.chapters for block in chapter.blocks)
    assert all(set(dataset.source_feature_ids).issubset({item["feature_id"] for item in features}) for dataset in report.datasets if dataset.source_feature_ids)

    rendered = render_report_html(report)
    assert rendered.count("<svg") >= 3
    assert "公开来源文本情感信号" in rendered
    assert "查看数据表" in rendered
