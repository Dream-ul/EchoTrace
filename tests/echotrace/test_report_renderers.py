import re

from echotrace.application.report_renderers import (
    render_report_html,
    render_report_json,
    render_report_markdown,
    render_report_pdf,
)
from echotrace.domain.reporting import AnalysisDataset, ReportBlock, ReportChapter, ReportDocument, VisualizationSpec


def _report() -> ReportDocument:
    return ReportDocument(
        report_id="report-1", run_id="run-1", trace_id="trace-1",
        investigation_type="public_event", template_id="public_event.report",
        template_version="1.0", template_content_hash="hash-1",
        title="测试事件调查报告", subtitle="基于冻结脉络",
        summary="当前材料支持一项有边界的结论。",
        sources=[{"document_id": "doc-1", "title": "调查通报", "url": "https://example.test/source", "publisher": "机构官网"}],
        citation_index={"claim-1": ["doc-1"], "doc-1": ["doc-1"]},
        chapters=[ReportChapter(
            "summary", "执行摘要", 1, "说明结果", "摘要",
            [ReportBlock("paragraph", {"text": "节点结论"}, ["claim-1"]),
             ReportBlock("table", {"title": "数据", "rows": [{"阶段": "初期", "数量": 2}]}, ["dataset-1"], ["dataset-1"])],
        )],
        source_index=["doc-1"],
        datasets=[AnalysisDataset(
            dataset_id="dataset-1", purpose="阶段数据", columns=["阶段", "数量"],
            rows=[{"阶段": "初期", "数量": 2}], grain="阶段", sample_scope="材料",
            time_scope="调查期间", method="直接整理", citation_ids=["doc-1"],
        )],
    )


def test_all_renderers_consume_the_same_report_document():
    report = _report()
    assert '"template_content_hash": "hash-1"' in render_report_json(report)
    markdown = render_report_markdown(report)
    assert "# 测试事件调查报告" in markdown and "## 1 执行摘要" in markdown and "| 阶段 | 数量 |" in markdown
    html = render_report_html(report)
    assert "lang=\"zh-CN\"" in html and "节点结论" in html
    assert "1 执行摘要" in html
    assert "claim-1" not in html and "doc-1" not in html
    assert "来源目录" in html and "调查通报" in html and "https://example.test/source" in html
    pdf_bytes = render_report_pdf(report)
    assert pdf_bytes.startswith(b"%PDF")


def test_report_pdf_paginates_long_content_without_dropping_the_end():
    import fitz

    report = _report()
    final_sentence = "报告末尾保留的结论。"
    report.chapters[0].blocks = [ReportBlock(
        "paragraph", {"text": ("调查材料支持这一阶段判断。" * 700) + final_sentence}
    )]

    pdf = fitz.open(stream=render_report_pdf(report), filetype="pdf")
    text = "\n".join(page.get_text() for page in pdf)

    assert len(pdf) > 1
    assert final_sentence in text


def test_renderers_number_template_headings_hierarchically_and_hide_duplicate_chapter_headings():
    report = _report()
    report.chapters[0].blocks = [
        ReportBlock("heading", {"level": 3, "text": "一、起点及范围"}),
        ReportBlock("paragraph", {"title": "材料观察", "text": "一段有来源的事实描述。"}, ["claim-1"]),
        ReportBlock("heading", {"level": 4, "text": "1.1.1 补充说明"}),
        ReportBlock("heading", {"level": 4, "text": "进一步核对"}),
        ReportBlock("heading", {"level": 3, "text": "执行摘要"}),
        ReportBlock("heading", {"level": 3, "text": "二、当前边界"}),
    ]

    markdown = render_report_markdown(report)
    html = render_report_html(report)

    for rendered in (markdown, html):
        assert "1.1 起点及范围" in rendered
        assert "1.1.1 补充说明" in rendered
        assert "1.1.1 1.1.1 补充说明" not in rendered
        assert "1.1.2 进一步核对" in rendered
        assert "1.2 当前边界" in rendered
        assert "一、起点及范围" not in rendered
        assert "1.3 执行摘要" not in rendered
    assert "**材料观察**" in markdown
    assert '<p class="block-title"><strong>材料观察</strong></p>' in html


def test_lists_keep_semantic_order_and_paragraphs_render_as_continuous_prose():
    report = _report()
    report.chapters[0].blocks = [
        ReportBlock("paragraph", {"text": "调查发现三个彼此独立的变化。"}),
        ReportBlock("bullet_list", {"items": ["平台讨论升温", "机构启动复核"]}),
        ReportBlock("ordered_list", {"items": ["发布通报", "启动调查"]}),
    ]

    html = render_report_html(report)
    markdown = render_report_markdown(report)

    assert '<div class="prose-block"><p>调查发现三个彼此独立的变化。</p></div>' in html
    assert '<ul class="report-list"><li>平台讨论升温</li><li>机构启动复核</li></ul>' in html
    assert '<ol class="report-list"><li>发布通报</li><li>启动调查</li></ol>' in html
    assert "- 平台讨论升温" in markdown and "- 机构启动复核" in markdown
    assert "1. 发布通报" in markdown and "2. 启动调查" in markdown
    assert '<div class="card"><p>调查发现三个彼此独立的变化。</p></div>' not in html


def test_array_rows_use_declared_columns_and_source_list_shows_title_without_role():
    report = _report()
    report.chapters[0].blocks = [
        ReportBlock("claim_table", {
            "title": "来源证据层级",
            "columns": ["来源类型", "核验状态"],
            "rows": [["法院通报", "已获取全文"], ["校方通报", "仅有转载"]],
        }),
        ReportBlock("source_list", {
            "items": [{
                "name": "法院二审通报",
                "role": "确认二审维持原判",
                "url": "https://example.test/court",
            }],
        }),
    ]

    rendered = render_report_html(report)
    markdown = render_report_markdown(report)

    assert "来源类型" in rendered and "法院通报" in rendered and "已获取全文" in rendered
    assert "法院二审通报" in rendered and "确认二审维持原判" not in rendered
    assert "法院通报" in markdown and "已获取全文" in markdown
    assert "法院二审通报" in markdown and "确认二审维持原判" not in markdown


def test_html_chart_renders_self_contained_visual_and_table_fallback():
    report = _report()
    report.visualizations = [VisualizationSpec(
        visualization_id="visual-1", type="chart", title="阶段数量", dataset_id="dataset-1",
        fallback="table", chart_kind="line", x_field="阶段", y_field="数量", citations=["doc-1"],
    )]
    report.chapters[0].blocks.append(ReportBlock(
        "chart", {"visualization_id": "visual-1"}, ["doc-1"], ["dataset-1"],
    ))
    html = render_report_html(report)
    assert "bar-chart" in html and "<svg" in html and "polyline" in html
    assert "查看数据表" in html and "初期" in html


def test_chart_axis_wraps_full_category_labels_and_keeps_narrow_viewport_scrollable():
    report = _report()
    labels = [f"阶段标签很长需要完整显示第{index}项" for index in range(12)]
    report.datasets[0].rows = [
        {"阶段": label, "数量": index + 1}
        for index, label in enumerate(labels)
    ]
    report.visualizations = [VisualizationSpec(
        visualization_id="visual-long-labels", type="chart", title="阶段数量", dataset_id="dataset-1",
        fallback="table", chart_kind="bar", x_field="阶段", y_field="数量", citations=["doc-1"],
    )]
    report.chapters[0].blocks = [ReportBlock(
        "chart", {"visualization_id": "visual-long-labels"}, ["doc-1"], ["dataset-1"],
    )]

    html = render_report_html(report)

    assert "overflow-x:auto" in html
    assert "min-width:640px" in html
    assert f"<title>{labels[0]}</title>" in html
    axis_label = re.search(r'<text class="axis-label"[^>]*>.*?</text>', html)
    assert axis_label
    visible_lines = re.findall(r"<tspan[^>]*>(.*?)</tspan>", axis_label.group(0))
    assert "".join(visible_lines) == labels[0]
    axis_positions = [float(value) for value in re.findall(r'<text class="axis-label"[^>]*\bx="([\d.]+)"', html)]
    assert axis_positions[0] >= 100 and axis_positions[-1] <= 680


def test_evidence_excerpt_is_rendered_and_empty_visuals_are_omitted():
    report = _report()
    report.chapters[0].blocks = [
        ReportBlock("evidence_card", {
            "title": "Excerpt source",
            "excerpt": "First-hand source text",
            "source_label": "Source A",
        }),
        ReportBlock("claim_table", {"title": "Empty table", "columns": ["claim"], "rows": []}),
    ]

    html = render_report_html(report)

    assert "First-hand source text" in html
    assert '<p class="quote">' in html
    assert "Empty table" not in html
    assert 'class="empty"' not in html


def test_evidence_card_with_quote_placeholder_is_omitted():
    report = _report()
    report.chapters[0].blocks = [
        ReportBlock("evidence_card", {"title": "Empty quote", "quote": '""'}),
        ReportBlock("evidence_card", {"title": "Quote with source", "quote": '“”', "source_label": "Court record"}),
    ]

    html = render_report_html(report)
    markdown = render_report_markdown(report)

    assert "Empty quote" not in html and 'class="quote">“”' not in html
    assert "Quote with source" in html and "Court record" in html
    assert "Empty quote" not in markdown and "Quote with source" in markdown


def test_timeline_uses_an_independent_connector_line():
    report = _report()
    report.chapters[0].blocks = [ReportBlock("timeline", {
        "title": "Timeline",
        "rows": [{"time": "2023-01-01", "event": "Event", "subject": "Actor"}],
    }, dataset_ids=["dataset-1"])]

    html = render_report_html(report)

    assert ".timeline:before{content:'';position:absolute" in html
    assert ".timeline-item:after{display:none}" in html
    assert '<div class="timeline-item">' in html


def test_timeline_rows_are_sorted_by_date_without_reordering_undated_rows():
    report = _report()
    report.chapters[0].blocks = [ReportBlock("timeline", {
        "title": "Timeline",
        "rows": [
            {"time": "2025-09-20", "event": "Later event"},
            {"time": "undated A", "event": "Undated first"},
            {"time": "2023年10月11日", "event": "Earlier event"},
            {"time": "undated B", "event": "Undated second"},
        ],
    }, dataset_ids=["dataset-1"])]

    html = render_report_html(report)

    assert html.index("Earlier event") < html.index("Later event")
    assert html.index("Undated first") < html.index("Undated second")


def test_timeline_does_not_truncate_after_forty_rows():
    report = _report()
    rows = [
        {"time": f"2023-10-{index + 1:02d}", "event": f"Event {index + 1}"}
        for index in range(45)
    ]
    report.chapters[0].blocks = [ReportBlock(
        "timeline", {"title": "Timeline", "rows": rows}, dataset_ids=["dataset-1"],
    )]

    html = render_report_html(report)

    assert html.count('<div class="timeline-item">') == 45
    assert "Event 45" in html


def test_empty_blocks_are_omitted_without_placeholder_copy():
    report = _report()
    report.datasets.append(AnalysisDataset(
        dataset_id="dataset-empty", purpose="空时间线", columns=["time", "event"],
        rows=[], grain="事件", sample_scope="材料", time_scope="调查期间",
        method="直接整理", citation_ids=["doc-1"],
    ))
    report.chapters[0].blocks = [
        ReportBlock("paragraph", {"title": "Empty paragraph", "text": "  "}),
        ReportBlock("callout", {"title": "Empty callout", "text": ""}),
        ReportBlock("claim_table", {"title": "Empty table", "rows": []}),
        ReportBlock("timeline", {"title": "Empty timeline", "rows": []}, dataset_ids=["dataset-empty"]),
    ]

    rendered = render_report_html(report)

    assert "Empty paragraph" not in rendered
    assert "Empty callout" not in rendered
    assert "Empty table" not in rendered
    assert "Empty timeline" not in rendered
    assert "暂无" not in rendered
    assert 'class="empty"' not in rendered


def test_source_list_uses_collapsed_chapter_citation_style_for_html_and_markdown():
    report = _report()
    report.chapters[0].blocks = [ReportBlock("source_list", {
        "title": "Sources",
        "items": [
            {"name": "Court notice", "description": "The court upheld the first-instance ruling.", "role": "Confirms the ruling", "publisher": "Court", "published_at": "2025-09-20", "url": "https://example.test/court"},
            {"name": "University statement", "description": "The university published its review results.", "role": "Confirms its review", "publisher": "University", "url": "https://example.test/university"},
        ],
    })]

    html = render_report_html(report)
    markdown = render_report_markdown(report)

    assert '<details class="chapter-sources chapter-source-list"><summary>本节依据（2 个来源）</summary>' in html
    chapter_html = html.split('<details class="chapter-sources chapter-source-list">', 1)[1].split('</details>', 1)[0]
    assert chapter_html.count('<div class="source-item">') == 2
    assert '<span class="source-summary">The court upheld the first-instance ruling.</span>' in chapter_html
    assert "Confirms the ruling" not in chapter_html and "2025-09-20" not in chapter_html
    assert "Court notice: The court upheld the first-instance ruling." in markdown
    assert "Confirms the ruling" not in markdown


def test_metric_units_are_not_appended_twice_and_summary_is_the_single_kpi_view():
    report = _report()
    report.metrics = []
    from echotrace.domain.reporting import MetricObservation

    report.metrics = [MetricObservation(
        metric_id="metric-1", name="传播量", value="12万余次", unit="次", time_scope="调查期间",
        source_document_id="doc-1", evidence_id="evidence-1", locator={"url": "https://example.test/source"},
    )]
    report.chapters[0].blocks.append(ReportBlock(
        "metric_grid", {"title": "关键指标", "items": [{"name": "传播量", "value": "12万余次", "unit": "次"}]},
        ["claim-1"], ["dataset-1"],
    ))
    rendered = render_report_html(report)
    assert "12万余次次" not in rendered
    assert rendered.count("12万余次") == 1
    assert rendered.count('class="kpi"') == 1


def test_chapter_citations_are_collapsed_and_internal_notes_are_hidden():
    report = _report()
    report.chapters[0].blocks.extend([
        ReportBlock("paragraph", {"text": "为何本章不绘制传播趋势图：当前数据不具备可比较的数值轴。"}, ["claim-1"]),
        ReportBlock("paragraph", {"text": "另一段有依据的结论。"}, ["claim-1"]),
    ])
    rendered = render_report_html(report)
    assert "为何本章不绘制传播趋势图" not in rendered
    assert rendered.count("本节依据（1 条来源）") == 1
    assert rendered.count("调查通报") == 2  # collapsed chapter entry + directory entry
    markdown = render_report_markdown(report)
    assert "为何本章不绘制传播趋势图" not in markdown


def test_pie_and_doughnut_charts_are_self_contained_svg():
    report = _report()
    report.datasets.append(AnalysisDataset(
        dataset_id="dataset-pie", purpose="分类数据", columns=["类别", "数量"],
        rows=[{"类别": "甲", "数量": 3}, {"类别": "乙", "数量": 2}], grain="类别",
        sample_scope="材料", time_scope="调查期间", method="直接整理", citation_ids=["doc-1"],
    ))
    for kind in ("pie", "doughnut"):
        report.visualizations = [VisualizationSpec(
            visualization_id=f"visual-{kind}", type="chart", title="分类占比", dataset_id="dataset-pie",
            fallback="table", chart_kind=kind, x_field="类别", y_field="数量", citations=["doc-1"],
        )]
        report.chapters[0].blocks = [ReportBlock(
            "chart", {"visualization_id": f"visual-{kind}"}, ["doc-1"], ["dataset-pie"],
        )]
        rendered = render_report_html(report)
        assert "<svg" in rendered and "<path" in rendered
        assert 'M 380.0 150' in rendered
        if kind == "doughnut":
            assert 'fill="#fff"' in rendered


def test_chart_data_table_omits_repeated_row_citations_and_labels_percentage_axis():
    report = _report()
    report.sources.append({
        "document_id": "doc-2", "title": "监测报告", "url": "https://example.test/monitor",
        "publisher": "监测机构",
    })
    report.source_index.append("doc-2")
    report.citation_index["evidence-2"] = ["doc-2"]
    report.datasets.append(AnalysisDataset(
        dataset_id="dataset-channel", purpose="监测样本渠道分布",
        columns=["channel", "count", "share", "citation_ids"],
        rows=[
            {"channel": "微博", "count": "120", "share": "60.0%", "citation_ids": ["evidence-2"]},
            {"channel": "微信", "count": "80", "share": "40.0%", "citation_ids": ["evidence-2"]},
        ], grain="每个渠道占样本比例", sample_scope="200条监测样本",
        time_scope="调查期间", method="按样本数量统计", citation_ids=["evidence-2"],
    ))
    report.visualizations.append(VisualizationSpec(
        visualization_id="visual-channel", type="chart", title="样本渠道构成",
        dataset_id="dataset-channel", fallback="table", chart_kind="pie",
        x_field="channel", y_field="share", citations=["evidence-2"],
    ))
    report.chapters[0].blocks = [ReportBlock(
        "chart", {"title": "样本渠道構成", "visualization_id": "visual-channel"},
        ["evidence-2"], ["dataset-channel"],
    )]

    html = render_report_html(report)

    assert "每个渠道占样本比例" in html
    assert "监测报告" in html
    assert "来源材料" not in html
    assert "citation_ids" not in html
    assert "evidence-2" not in html
    assert "60.0%" in html
    markdown = render_report_markdown(report)
    assert "纵轴指标：占比" in markdown
    assert "监测报告" in markdown
    assert "来源材料" not in markdown
    assert "citation_ids" not in markdown
    assert "evidence-2" not in markdown


def test_unknown_english_table_headers_are_not_shown_to_readers():
    report = _report()
    report.chapters[0].blocks = [ReportBlock(
        "table", {"columns": ["platform", "citations"], "rows": [["微博", ["claim-1"]]]},
        ["claim-1"], ["dataset-1"],
    )]

    for rendered in (render_report_html(report), render_report_markdown(report)):
        assert "platform" not in rendered
        assert "citations" not in rendered
        assert "平台" in rendered
        assert "来源材料" not in rendered
        assert "调查通报" in rendered


def test_labeled_internal_table_columns_render_the_chinese_labels():
    report = _report()
    report.chapters[0].blocks = [ReportBlock(
        "table",
        {
            "columns": [
                {"key": "platform", "label": "平台"},
                {"key": "citations", "label": "来源材料"},
            ],
            "rows": [{"platform": "微博", "citations": ["claim-1"]}],
        },
        ["claim-1"], ["dataset-1"],
    )]

    html = render_report_html(report)
    markdown = render_report_markdown(report)

    for rendered in (html, markdown):
        assert "platform" not in rendered
        assert "citations" not in rendered
        assert "平台" in rendered
        assert "来源材料" not in rendered
        assert "调查通报" in rendered


def test_placeholder_citations_in_table_resolve_to_real_source_titles():
    report = _report()
    report.sources.append({
        "document_id": "doc-2", "title": "监测数据复盘报告", "url": "https://example.test/report",
        "publisher": "监测机构",
    })
    report.source_index.append("doc-2")
    report.citation_index["evidence-2"] = ["doc-2"]
    report.chapters[0].blocks = [ReportBlock(
        "table",
        {
            "columns": [
                {"key": "count", "label": "信息量（条）"},
                {"key": "citations", "label": "citations"},
                {"key": "time_scope", "label": "time_scope"},
            ],
            "rows": [{
                "count": "120",
                "citations": "相关材料、相关材料",
                "time_scope": "调查期间",
            }],
        },
        ["evidence-2"], ["dataset-1"],
    )]

    for rendered in (render_report_html(report), render_report_markdown(report)):
        assert "信息量（条）" in rendered
        assert "来源材料" not in rendered
        assert "时间范围" in rendered
        assert "citations" not in rendered
        assert "time_scope" not in rendered
        assert "相关材料、相关材料" not in rendered
        assert "监测数据复盘报告" in rendered


def test_repeated_source_material_column_does_not_push_data_columns_out_of_view():
    report = _report()
    repeated_sources = "；".join(["同一来源材料内容" * 20] * 4)
    report.chapters[0].blocks = [ReportBlock(
        "table",
        {
            "columns": ["日期", "平台", "信息量（条）", "来源材料"],
            "rows": [
                ["2026-08-01", "微博", "120", repeated_sources],
                ["2026-08-02", "微信", "80", repeated_sources],
            ],
        },
        [], ["dataset-1"],
    )]

    rendered = render_report_html(report)

    assert "<th>日期</th>" in rendered
    assert "<th>平台</th>" in rendered
    assert "<th>信息量（条）</th>" in rendered
    assert "<th>来源材料</th>" not in rendered
    assert "同一来源材料内容" not in rendered
    assert "table-layout:fixed" in rendered
    assert "overflow-wrap:anywhere" in rendered


def test_scatter_and_radar_charts_use_shape_specific_svg_renderers():
    report = _report()
    report.datasets.extend([
        AnalysisDataset(
            dataset_id="dataset-scatter", purpose="数值样本", columns=["横轴", "纵轴"],
            rows=[{"横轴": 1, "纵轴": 4}, {"横轴": 2, "纵轴": 3}], grain="样本",
            sample_scope="材料", time_scope="调查期间", method="直接整理", citation_ids=["doc-1"],
        ),
        AnalysisDataset(
            dataset_id="dataset-radar", purpose="同一对象维度", columns=["维度", "得分"],
            rows=[{"维度": "速度", "得分": 3}, {"维度": "覆盖", "得分": 4}, {"维度": "稳定", "得分": 2}], grain="对象维度",
            sample_scope="材料", time_scope="调查期间", method="直接整理", citation_ids=["doc-1"],
        ),
    ])
    for kind, dataset_id, x_field, y_field, marker in (
        ("scatter", "dataset-scatter", "横轴", "纵轴", 'class="point"'),
        ("radar", "dataset-radar", "维度", "得分", 'class="area"'),
    ):
        visual_id = f"visual-{kind}"
        report.visualizations = [VisualizationSpec(
            visualization_id=visual_id, type="chart", title=kind, dataset_id=dataset_id,
            fallback="table", chart_kind=kind, x_field=x_field, y_field=y_field, citations=["doc-1"],
        )]
        report.chapters[0].blocks = [ReportBlock(
            "chart", {"visualization_id": visual_id}, ["doc-1"], [dataset_id],
        )]
        rendered = render_report_html(report)
        assert "<svg" in rendered and marker in rendered


def test_dashboard_hides_repeated_metric_observations():
    report = _report()
    from echotrace.domain.reporting import MetricObservation

    report.metrics = [
        MetricObservation(
            metric_id="metric-1", name="武汉大学官网处分通报页面点击量", value="12万余次", unit="次",
            time_scope="截至2025-07-31", source_document_id="doc-1", evidence_id="evidence-1",
            locator={"url": "https://example.test/source"},
        ),
        MetricObservation(
            metric_id="metric-2", name="官网处分通报点击量（口径一）", value="12万余次", unit="次",
            time_scope="截至2025-07-31", source_document_id="doc-1", evidence_id="evidence-1",
            locator={"url": "https://example.test/source"},
        ),
    ]
    rendered = render_report_html(report)
    assert rendered.count("12万余次") == 1


def test_dashboard_deduplicates_numeric_and_spacing_variants():
    report = _report()
    from echotrace.domain.reporting import MetricObservation

    report.metrics = [
        MetricObservation(
            metric_id="metric-1", name="同一处分通报页面点击量（后续时点）", value="超17万", unit="次",
            time_scope="2025-07-31 晚间报道时点", source_document_id="doc-1", evidence_id="evidence-1",
            locator={"url": "https://example.test/source"},
        ),
        MetricObservation(
            metric_id="metric-2", name="官网处分通报点击量（口径二）", value="超17万", unit="次",
            time_scope="2025-07-31晚间报道时点", source_document_id="doc-1", evidence_id="evidence-1",
            locator={"url": "https://example.test/source"},
        ),
    ]
    rendered = render_report_html(report)
    assert rendered.count("超17万次") == 1


def test_reader_facing_text_replaces_material_pack_identifiers():
    report = _report()
    report.chapters[0].blocks = [ReportBlock(
        "paragraph",
        {"text": "本结论来自 NodeTrace（trace_id：trace_123，run_id：run_123），完整清单以 document:page_abc 为准。"},
        ["claim-1"],
    )]
    rendered = render_report_html(report)
    assert "NodeTrace" not in rendered
    assert "trace_id" not in rendered
    assert "run_id" not in rendered
    assert "document:" not in rendered
    assert "调查脉络" in rendered
    assert "来源材料" in rendered


def test_renderers_do_not_reinterpret_serialized_objects_as_report_blocks():
    report = _report()
    report.chapters[0].blocks.extend([
        ReportBlock("paragraph", {"text": "time: 2026-01-01; event: 发布通报; subject: 机构; citation_ids: ['evidence_1234567890']"}),
        ReportBlock("paragraph", {"text": "name: 复核材料; unit: 份; value: 91; metric_id: metric_123; time_scope: 2026年"}),
        ReportBlock("paragraph", {"text": "{'text': '起点窗口的时序', 'level': 3}"}),
    ])
    rendered = render_report_html(report)
    markdown = render_report_markdown(report)
    assert '<div class="timeline-item"' not in rendered
    assert '<div class="kpi-grid">' not in rendered
    assert "2026-01-01" not in rendered
    assert "91份" not in rendered
    assert "起点窗口的时序" not in rendered
    assert "evidence_1234567890" not in rendered and "metric_123" not in rendered
    assert "citation_ids" not in rendered and "metric_id" not in rendered
    assert "2026-01-01" not in markdown and "91份" not in markdown
    assert "起点窗口的时序" not in markdown


def test_reader_facing_renderers_hide_internal_ids_and_identifier_columns():
    report = _report()
    report.chapters[0].blocks.extend([
        ReportBlock("paragraph", {"text": "来源来自 claim_abc123 和 evidence_def456；node_id=node_012345。"}),
        ReportBlock("table", {"rows": [{
            "actor": "调查机构", "action": "发布说明", "node_id": "node_012345",
            "claim_id": "claim_abc123", "source_document_id": "doc-1",
        }]}, dataset_ids=["dataset-1"]),
        ReportBlock("source_list", {"source_ids": ["claim-1"]}, ["claim-1"]),
    ])

    html = render_report_html(report)
    markdown = render_report_markdown(report)
    for rendered in (html, markdown):
        assert "claim_abc123" not in rendered
        assert "evidence_def456" not in rendered
        assert "node_012345" not in rendered
        assert "node_id" not in rendered
        assert "claim_id" not in rendered
        assert "source_document_id" not in rendered
    assert "调查机构" in html and "调查机构" in markdown
    assert "调查通报" in html and "调查通报" in markdown
