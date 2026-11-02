import pytest

from echotrace.domain import (
    AnalysisDataset,
    MetricObservation,
    NodeClaim,
    NodeDraft,
    NodeEvidence,
    ReportBlock,
    ReportChapter,
    ReportDocument,
    TraceContractError,
    VisualizationSpec,
)


def _metric() -> MetricObservation:
    return MetricObservation(
        metric_id="metric-1",
        name="受影响样本",
        value=12,
        unit="个",
        time_scope="2026-09-01/2026-09-02",
        source_document_id="doc-1",
        evidence_id="evidence-1",
        locator={"quote": "共12个样本"},
    )


def _dataset() -> AnalysisDataset:
    return AnalysisDataset(
        dataset_id="dataset-1",
        purpose="按阶段展示样本数量",
        columns=["阶段", "数量"],
        rows=[{"阶段": "初期", "数量": 12}],
        grain="事件阶段",
        sample_scope="已定位的12个样本",
        time_scope="2026-09-01/2026-09-02",
        method="按来源中的直接数量汇总",
        input_metric_ids=["metric-1"],
        citation_ids=["evidence-1"],
    )


def test_metric_and_dataset_round_trip_preserve_lineage():
    metric = _metric()
    dataset = _dataset()
    assert MetricObservation.from_dict(metric.to_dict()).to_dict() == metric.to_dict()
    assert AnalysisDataset.from_dict(dataset.to_dict()).to_dict() == dataset.to_dict()


def test_analysis_dataset_rejects_report_template_metadata_fields():
    payload = _dataset().to_dict()
    payload["merge_strategy"] = "event_rows"

    with pytest.raises(TraceContractError, match="unsupported fields: merge_strategy"):
        AnalysisDataset.from_dict(payload)


def test_derived_metric_requires_formula_and_evidence():
    with pytest.raises(TraceContractError, match="derived metric requires formula"):
        MetricObservation(
            metric_id="metric-derived",
            name="比例",
            value=0.5,
            unit="比例",
            time_scope="期间",
            source_document_id="doc-1",
            evidence_id="e-1",
            locator={"quote": "原始数据"},
            direct_or_derived="derived",
        )


def test_visualization_and_report_require_existing_datasets():
    dataset = _dataset()
    visual = VisualizationSpec(
        visualization_id="visual-1",
        type="chart",
        title="样本趋势",
        dataset_id=dataset.dataset_id,
        fallback="table",
        chart_kind="line",
        x_field="阶段",
        y_field="数量",
        citations=["evidence-1"],
    )
    report = ReportDocument(
        report_id="report-1",
        run_id="run-1",
        trace_id="trace-1",
        investigation_type="public_event",
        template_id="public_event.report",
        template_version="1.0",
        template_content_hash="hash-1",
        title="事件调查报告",
        summary="基于冻结节点生成。",
        chapters=[
            ReportChapter(
                chapter_id="chapter-1",
                title="摘要",
                order=1,
                purpose="说明核心结果",
                summary="摘要",
                blocks=[
                    ReportBlock(
                        type="chart",
                        payload={"visualization_id": visual.visualization_id},
                        citations=["evidence-1"],
                        dataset_ids=[dataset.dataset_id],
                    )
                ],
            )
        ],
        metrics=[_metric()],
        datasets=[dataset],
        visualizations=[visual],
    )
    assert ReportDocument.from_dict(report.to_dict()).to_dict() == report.to_dict()
    with pytest.raises(TraceContractError, match="unknown dataset"):
        ReportDocument(
            report_id="bad-report",
            run_id="run-1",
            trace_id="trace-1",
            investigation_type="public_event",
            template_id="public_event.report",
            template_version="1.0",
            template_content_hash="hash-1",
            title="坏报告",
            summary="摘要",
            chapters=[
                ReportChapter("chapter-1", "摘要", 1, "说明", "摘要", [])
            ],
            metrics=[_metric()],
            datasets=[dataset],
            visualizations=[VisualizationSpec("bad", "chart", "坏图", "missing", "table", chart_kind="line")],
        )


def test_visualization_without_fallback_defaults_to_table():
    visual = VisualizationSpec.from_dict({
        "visualization_id": "visual-1",
        "type": "chart",
        "title": "样本趋势",
        "dataset_id": "dataset-1",
        "chart_kind": "line",
        "x_field": "阶段",
        "y_field": "数量",
    })

    assert visual.fallback == "table"


def test_chart_kind_returned_as_visualization_type_is_normalized():
    visual = VisualizationSpec.from_dict({
        "visualization_id": "visual-pie",
        "type": "pie",
        "title": "样本构成",
        "dataset_id": "dataset-1",
        "x_field": "类别",
        "y_field": "占比",
    })

    assert visual.type == "chart"
    assert visual.chart_kind == "pie"
    assert visual.fallback == "table"


def test_node_draft_persists_metrics_datasets_and_analysis_features():
    claim = NodeClaim(
        "claim-1",
        "impact",
        "材料显示有12个受影响样本。",
        source_document_ids=["doc-1"],
        evidence_ids=["evidence-1"],
    )
    evidence = NodeEvidence("evidence-1", "claim-1", "doc-1", "共12个样本", {"quote": "共12个样本"})
    draft = NodeDraft(
        node_id="node-1",
        slot_id="event_scope",
        revision=0,
        claims=[claim],
        evidence=[evidence],
        conclusion="现有材料显示有12个受影响样本。",
        media=[],
        metrics=[_metric()],
        datasets=[_dataset()],
        analysis_features=[
            {
                "feature_type": "sentiment",
                "value": {"label_zh": "负向", "confidence": 0.8},
                "document_id": "doc-1",
                "factuality": "analysis_feature",
            }
        ],
    )
    assert NodeDraft.from_dict(draft.to_dict()).to_dict() == draft.to_dict()
