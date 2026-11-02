"""Render the persisted ReportDocument without reinterpreting investigation facts."""

from __future__ import annotations

import html
import json
import re
import math
import textwrap
import unicodedata
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any, Mapping
from urllib.parse import urlsplit

from echotrace.domain.reporting import ReportDocument


_HEADING_NUMBER_PREFIX = re.compile(
    r"^\s*(?:\d+(?:\.\d+)+\.?\s+|(?:第\s*)?\d+\s*[章节篇、．.)：:]\s*|[一二三四五六七八九十]+[、．.]\s*)"
)


def _strip_heading_number(value: str) -> str:
    return _HEADING_NUMBER_PREFIX.sub("", value, count=1).strip()


def _chapter_heading_map(chapter: Any) -> dict[int, tuple[int, str, str] | None]:
    counters = [0, 0]
    headings: dict[int, tuple[int, str, str] | None] = {}
    chapter_title = _strip_heading_number(_safe_text(chapter.title))
    for block in chapter.blocks:
        if block.type != "heading":
            continue
        payload = block.payload
        text = _strip_heading_number(_safe_text(payload.get("text") or payload.get("title") or ""))
        if not text or text == chapter_title:
            headings[id(block)] = None
            continue
        try:
            requested_level = int(payload.get("level") or 3)
        except (TypeError, ValueError):
            requested_level = 3
        depth = 2 if requested_level >= 4 and counters[0] else 1
        counters[depth - 1] += 1
        if depth == 1:
            counters[1] = 0
        number_parts = [str(chapter.order), str(counters[0])]
        if depth == 2:
            number_parts.append(str(counters[1]))
        number = ".".join(number_parts)
        headings[id(block)] = (depth + 2, number, text)
    return headings


def render_report_json(report: ReportDocument) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


def render_report_markdown(report: ReportDocument) -> str:
    lines = [f"# {_safe_text(report.title)}"]
    if report.subtitle:
        lines.append(f"\n> {_safe_text(report.subtitle)}")
    lines.append(f"\n{_safe_text(report.summary)}")
    lines.append("\n## 目录")
    chapter_orders = {chapter.chapter_id: chapter.order for chapter in report.chapters}
    lines.extend(
        f"- [{chapter_orders.get(item['chapter_id'], index)} {_safe_text(item['title'])}](#{item['chapter_id']})"
        for index, item in enumerate(report.toc, 1)
    )
    if report.metrics:
        lines.append("\n## 关键指标")
        lines.append("\n| 指标 | 数值 | 时间范围 |")
        lines.append("| --- | --- | --- |")
        lines.extend(
            f"| {_markdown_value(metric.name)} | {_markdown_value(_metric_display_value(metric.value, metric.unit))} | {_markdown_value(metric.time_scope)} |"
            for metric in _unique_metrics(report.metrics)[:12]
        )
    for chapter in report.chapters:
        lines.extend([
            f"\n## {chapter.order} {_strip_heading_number(_safe_text(chapter.title))}",
            f"\n{_safe_text(chapter.summary)}",
        ])
        heading_map = _chapter_heading_map(chapter)
        for block in chapter.blocks:
            if block.type == "metric_grid" and report.metrics:
                continue
            _markdown_block(
                lines, block.type, block.payload, block.citations, block.dataset_ids, report,
                heading=heading_map.get(id(block)),
            )
        citations = _chapter_source_ids(chapter.blocks, report)
        if citations:
            _markdown_citation_details(lines, citations, report)
    if report.sources:
        lines.append("\n## 附录：来源目录")
        for index, source in enumerate(report.sources, 1):
            label = _safe_text(source.get("title") or "来源材料")
            url = _safe_http_url(source.get("url"))
            lines.append(f"- 来源 {index}：{label}" + (f"（{url}）" if url else ""))
    return "\n".join(lines).strip() + "\n"


def _markdown_block(
    lines: list[str],
    block_type: str,
    payload: dict[str, Any],
    citations: list[str],
    dataset_ids: list[str],
    report: ReportDocument,
    *,
    heading: tuple[int, str, str] | None = None,
) -> None:
    title = _safe_text(payload.get("title") or "").strip()
    raw_text = str(payload.get("text") or "")
    if block_type == "heading":
        if heading is None:
            return
        level, number, text = heading
        lines.append(f"\n{'#' * level} {number} {text}")
        return
    if block_type in {"paragraph", "callout"} and (
        not raw_text.strip() or _is_internal_explanation(raw_text) or _looks_like_serialized_object(raw_text)
    ):
        return
    if block_type in {"bullet_list", "ordered_list"} and not payload.get("items"):
        return
    if block_type in {"claim_table", "table", "timeline", "metric_grid"} and not (
        payload.get("rows") or payload.get("items") or payload.get("metrics")
    ):
        return
    if title:
        lines.append(f"\n**{_strip_heading_number(title)}**")
    if block_type in {"paragraph", "callout"} and payload.get("text"):
        if _is_internal_explanation(raw_text) or _looks_like_serialized_object(raw_text):
            return
        lines.append(f"\n{_safe_text(raw_text)}")
    elif block_type == "chart":
        visual = next(
            (item for item in report.visualizations if item.visualization_id == str(payload.get("visualization_id") or "")),
            None,
        )
        dataset_id = visual.dataset_id if visual else (dataset_ids[0] if dataset_ids else "")
        dataset = next((item for item in report.datasets if item.dataset_id == dataset_id), None)
        if dataset:
            y_field = visual.y_field if visual else ""
            if y_field:
                lines.append(f"\n{_chart_description_text(dataset, y_field)}")
            rows = _table_rows(dataset.rows, dataset.columns, report=report, citations=citations)
            if rows:
                columns = [column for column in rows[0] if not _is_internal_column(column)]
                if columns:
                    lines.append("\n| " + " | ".join(_display_key(column) for column in columns) + " |")
                    lines.append("| " + " | ".join("---" for _ in columns) + " |")
                    lines.extend(
                        "| " + " | ".join(_markdown_value(row.get(column, "")) for column in columns) + " |"
                        for row in rows
                    )
    elif block_type in {"bullet_list", "ordered_list"}:
        marker = "- " if block_type == "bullet_list" else None
        for index, item in enumerate(payload.get("items") or (), 1):
            lines.append(f"{marker or f'{index}. '}{_list_item_text(item)}")
    elif block_type == "evidence_card":
        quote = _substantive_text(payload.get("quote") or payload.get("text") or payload.get("excerpt") or "")
        detail = _substantive_text(payload.get("detail") or "")
        source_label = _substantive_text(payload.get("source_label") or "")
        if not quote and not detail and not source_label:
            if title and lines and lines[-1] == f"\n**{_strip_heading_number(title)}**":
                lines.pop()
            return
        if quote:
            lines.append(f"\n> {quote}")
        if detail:
            lines.append(f"\n{detail}")
        if source_label:
            lines.append(f"\n來源說明：{source_label}")
    elif block_type in {"claim_table", "table", "timeline", "metric_grid"}:
        raw_rows = payload.get("rows") or payload.get("items") or ()
        rows = _table_rows(raw_rows, payload.get("columns") or (), report=report, citations=citations)
        if rows:
            columns = [column for column in rows[0] if not _is_internal_column(column)]
            if columns:
                lines.append("\n| " + " | ".join(_display_key(column) for column in columns) + " |")
                lines.append("| " + " | ".join("---" for _ in columns) + " |")
                lines.extend("| " + " | ".join(_markdown_value(row.get(column, "")) for column in columns) + " |" for row in rows)
        elif raw_rows:
            lines.extend(f"- {_safe_text(item)}" for item in raw_rows)
    elif block_type in {"image", "video"}:
        media_id = str(payload.get("media_id") or "")
        url = str(payload.get("source_url") or payload.get("url") or "")
        lines.append(("\n图片" if block_type == "image" else "\n视频") + (f"（{url}）" if url else ""))
    elif block_type == "source_list":
        entries = _source_list_entries(
            payload.get("items") or payload.get("source_ids") or (), citations, report,
        )
        lines.extend(f"- {label}" + (f": {summary}" if summary else "") for label, summary, _url in entries)
    else:
        summary = _payload_summary(payload)
        if summary:
            lines.append(f"\n{summary}")
    # Sources are presented once at chapter level to keep the prose readable.


def _markdown_citation_details(lines: list[str], citations: list[str], report: ReportDocument) -> None:
    sources = _citation_sources(citations, report)
    if not sources:
        return
    lines.append(f"\n**本节依据（{len(sources)} 条来源）**")
    for index, source in sources:
        title = _markdown_value(source.get("title") or "来源材料")
        url = _safe_http_url(source.get("url"))
        lines.append(f"- 来源 {index}：{title}" + (f"（{url}）" if url else ""))


def render_report_html(report: ReportDocument) -> str:
    source_map = {str(item.get("document_id")): item for item in report.sources}
    parts = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        f"<title>{html.escape(_safe_text(report.title))}</title>",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "<style>"
        ".prose-block{width:100%;max-width:100%;margin:14px 0 18px}.report-callout{width:100%;margin:16px 0;padding:14px 18px;border-left:4px solid var(--accent);background:#fff8f4}.report-list{width:100%;margin:10px 0 20px;padding-left:1.65rem}.report-list li{padding-left:3px;margin:5px 0}.report-visual{width:100%;max-width:100%;min-width:0;margin:16px 0;overflow:visible}.chart-shell{width:100%;overflow-x:auto;overflow-y:hidden}.chart-shell svg{display:block;width:100%;min-width:640px;height:auto;min-height:0}.chapter-source-list{margin:14px 0 20px}"
        ":root{--ink:#172033;--muted:#667085;--line:#e4e7ec;--brand:#176b87;--soft:#f5f8fa;--accent:#e27856}"
        "*{box-sizing:border-box}body{font-family:Inter,system-ui,'Microsoft YaHei',sans-serif;width:100%;max-width:1180px;margin:0 auto;padding:0 28px 70px;color:var(--ink);line-height:1.72;background:#fff;overflow-x:hidden}h1,h2,h3,p,li,span{overflow-wrap:anywhere;word-break:break-word}"
        ".hero{margin:0 -28px 34px;padding:64px 28px 54px;background:#173f45;color:#fff;border-bottom:5px solid var(--accent)}.hero h1{font-size:42px;line-height:1.18;margin:0 0 12px;max-width:820px}.hero .subtitle{font-size:18px;opacity:.88}.hero .summary{max-width:860px;color:#fff;opacity:.95;margin-top:24px}"
        "h2{font-size:28px;border-top:1px solid var(--line);padding-top:28px;margin-top:48px}h3{font-size:19px;margin:22px 0 8px}.block-title{font-size:15px;margin:0 0 8px;color:var(--ink)}.summary{font-size:18px;color:#445268}.muted{color:var(--muted);font-size:13px}.card{border:1px solid var(--line);border-radius:8px;padding:20px;margin:16px 0;background:#fff;box-shadow:0 3px 14px rgba(23,32,51,.04)}.card p:last-child{margin-bottom:0}"
        ".toc{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;padding:0;list-style:none}.toc a{display:block;padding:12px 14px;background:var(--soft);border-radius:8px;color:var(--brand);text-decoration:none}.dashboard{margin:22px 0 34px}.dashboard h2{margin-top:0;border:0;padding:0}.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.kpi{display:block;border:1px solid #d8e7eb;border-top:4px solid var(--accent);border-radius:8px;padding:17px 16px;background:#fbfdfe;min-height:96px}.kpi b{display:block;font-size:27px;line-height:1.2;color:#173f45}.kpi span{display:block;color:#445268;font-size:14px;margin-top:6px}.dataset-card{background:#fbfdfe}.dataset-title{display:flex;justify-content:space-between;gap:16px;align-items:baseline}.dataset-title small{color:var(--muted);font-weight:400}"
        ".report-table{border-collapse:separate;border-spacing:0;table-layout:fixed;width:100%;max-width:100%;margin:12px 0;overflow:hidden;border:1px solid var(--line);border-radius:8px}.report-table th,.report-table td{border-bottom:1px solid var(--line);padding:10px 12px;text-align:left;vertical-align:top;overflow-wrap:anywhere;word-break:break-word}.report-table th{background:var(--soft);font-weight:650;color:#344054}.report-table tr:last-child td{border-bottom:0}.chart-shell{width:100%;overflow:hidden;border:1px solid #e6eef0;border-radius:8px;background:#fcfefe;padding:10px}.chart-shell svg{display:block;width:100%;height:auto;min-height:220px}.chart-shell .axis{stroke:#b8c9cd;stroke-width:1}.chart-shell .grid{stroke:#e5eef0;stroke-width:1}.chart-shell .series{stroke:#176b87;stroke-width:3;fill:none;stroke-linecap:round;stroke-linejoin:round}.chart-shell .area{fill:#176b87;fill-opacity:.14}.chart-shell .point{fill:#e27856;stroke:#fff;stroke-width:2}.chart-shell .bar-mark{fill:#3d8790}.chart-shell .bar-mark:hover{fill:#e27856}.chart-shell text{font-family:Inter,system-ui,'Microsoft YaHei',sans-serif;fill:#667085;font-size:11px}.chart-shell .value-label{fill:#344054;font-size:10px}.chart-legend{display:flex;flex-wrap:wrap;gap:8px 14px;margin:8px 4px 0;color:var(--muted);font-size:12px}.chart-legend i{display:inline-block;width:9px;height:9px;border-radius:2px;background:#3d8790;margin-right:4px}.bar-chart{display:block;min-height:0;padding:0;border:0}.bar{display:flex;align-items:flex-start;justify-content:center;min-height:4px;background:#3d8790;border-radius:5px 5px 0 0;padding-top:3px}.bar em{font-size:10px;font-style:normal;color:#fff;overflow:hidden}.bar-label{margin-top:6px;text-align:center;font-size:12px;color:var(--muted);overflow-wrap:anywhere}.kpi-details{margin-top:7px;color:var(--muted);font-size:12px}.kpi-details summary{cursor:pointer;color:var(--brand)}.kpi-details p{margin:5px 0 0}.timeline{position:relative;margin:18px 0 4px;padding-left:24px;border-left:3px solid #cfe3e8}.timeline-item{position:relative;padding:0 0 18px 16px}.timeline-item:before{content:'';position:absolute;left:-32px;top:7px;width:11px;height:11px;border-radius:50%;background:var(--accent);border:3px solid #fff;box-shadow:0 0 0 1px #e3b7a9}.timeline-time{font-size:13px;color:var(--brand);font-weight:700}.timeline-event{margin-top:2px}.callout{border-left:4px solid var(--accent);background:#fff8f4}.evidence{border-left:4px solid #8db9c4;background:#f7fbfc}.quote{font-size:16px;margin:4px 0}.source-link{color:var(--brand);text-decoration:none}.source-list{display:grid;gap:10px}.source-item{padding:12px 14px;background:var(--soft);border-radius:8px}.chapter-sources{margin:18px 0}.chapter-sources summary,.source-directory summary{cursor:pointer;color:var(--brand);font-weight:650}.media{max-width:100%;max-height:480px;border-radius:8px}.footnote{color:var(--muted);font-size:12px;margin-top:14px}.empty{color:var(--muted);font-style:italic}"
        ".chart-shell{overflow-x:auto;overflow-y:hidden}.chart-shell svg{min-width:640px;min-height:0}.report-visual{width:100%;max-width:100%;min-width:0;overflow:visible}.prose-block{width:100%;max-width:100%}"
        ".timeline{position:relative;margin:18px 0 4px;padding:0 0 0 30px;overflow:visible;border:0}.timeline:before{content:'';position:absolute;left:6px;top:8px;bottom:8px;width:2px;background:#cfe3e8}.timeline-item{position:relative;padding:0 0 22px}.timeline-item:before{z-index:1;left:-30px;top:6px;width:13px;height:13px}.timeline-item:after{display:none}.timeline-item:last-child{padding-bottom:0}.source-item span{display:block;margin-top:5px;color:#445268}.source-meta{display:block;margin-top:4px;color:var(--muted);font-size:12px}"
        "@media(max-width:680px){body{padding:0 16px 48px}.hero{margin:0 -16px 28px;padding:42px 16px}.hero h1{font-size:32px;word-break:break-all}.hero .summary{font-size:16px}.hero .subtitle{font-size:16px}h2{font-size:24px}.report-table{font-size:13px;display:block;overflow-x:auto;white-space:normal}.kpi b{font-size:23px}.kpi b.kpi-long{font-size:16px;line-height:1.45}}"
        "@media print{@page{size:A4;margin:0}body{width:auto;max-width:none;margin:0;padding:0 42px 48px;overflow:visible}.hero{margin:0 -42px 28px;padding:42px 42px 34px}h2,h3{break-after:avoid;page-break-after:avoid}.card,.report-callout,.report-visual,.source-directory,.chapter-sources{break-inside:avoid;page-break-inside:avoid}.chart-shell{overflow:visible}.chart-shell svg{width:100%;min-width:0;height:auto}.report-table{overflow:visible}a{color:inherit;text-decoration:none}}"
        "</style></head><body>",
        f"<header class=\"hero\"><h1>{html.escape(_safe_text(report.title))}</h1>",
    ]
    if report.subtitle:
        parts.append(f"<p class=\"subtitle\">{html.escape(_safe_text(report.subtitle))}</p>")
    parts.append(f"<p class=\"summary\">{html.escape(_safe_text(report.summary))}</p></header>")
    parts.append("<h2>目录</h2><ol class=\"toc\">")
    chapter_orders = {chapter.chapter_id: chapter.order for chapter in report.chapters}
    parts.extend(
        f"<li><a href=\"#{html.escape(item['chapter_id'])}\">"
        f"{chapter_orders.get(item['chapter_id'], index)} {html.escape(_strip_heading_number(_safe_text(item['title'])))}</a></li>"
        for index, item in enumerate(report.toc, 1)
    )
    parts.append("</ol>")
    parts.append(_render_dashboard(report, source_map))
    datasets = {dataset.dataset_id: dataset for dataset in report.datasets}
    visualizations = {visual.visualization_id: visual for visual in report.visualizations}
    for chapter in report.chapters:
        parts.append(
            f"<section id=\"{html.escape(chapter.chapter_id)}\"><h2>"
            f"{chapter.order} {html.escape(_strip_heading_number(_safe_text(chapter.title)))}</h2>"
            f"<p>{html.escape(_safe_text(chapter.summary))}</p>"
        )
        heading_map = _chapter_heading_map(chapter)
        for block in chapter.blocks:
            if block.type == "metric_grid" and report.metrics:
                continue
            parts.append(_html_block(
                block.type, block.payload, block.citations, block.dataset_ids,
                datasets, visualizations, report, source_map,
                heading=heading_map.get(id(block)),
            ))
        if not any(block.type == "source_list" for block in chapter.blocks):
            chapter_sources = _chapter_source_ids(chapter.blocks, report)
            if chapter_sources:
                parts.append(_render_chapter_citations(chapter_sources, report, source_map))
        parts.append("</section>")
    if report.sources:
        parts.append(_render_sources(report.sources))
    parts.append("</body></html>")
    return "".join(parts)


def _html_block(block_type: str, payload: dict[str, Any], citations: list[str], dataset_ids: list[str], datasets: dict[str, Any], visualizations: dict[str, Any], report: ReportDocument, source_map: dict[str, dict[str, Any]], *, heading: tuple[int, str, str] | None = None) -> str:
    block_title = _strip_heading_number(_safe_text(payload.get("title") or ""))
    title = f"<p class=\"block-title\"><strong>{html.escape(block_title)}</strong></p>" if block_title else ""
    if block_type == "heading":
        if heading is None:
            return ""
        level, number, text = heading
        anchor = number.replace(".", "-")
        return f"<h{level} id=\"section-{anchor}\">{number} {html.escape(text)}</h{level}>"
    if block_type == "evidence_card":
        quote = _substantive_text(payload.get("quote") or payload.get("text") or payload.get("excerpt") or "")
        detail = _substantive_text(payload.get("detail") or "")
        source_label = _substantive_text(payload.get("source_label") or "")
        if not quote and not detail and not source_label:
            return ""
        quote_html = f'<p class="quote">“{html.escape(quote)}”</p>' if quote else ""
        return f"<div class=\"card evidence\">{title}{quote_html}{_paragraph_html(detail) if detail else ''}{f'<p class=\"muted\">{html.escape(source_label)}</p>' if source_label else ''}</div>"
    if block_type == "source_list":
        entries = _source_list_entries(
            payload.get("items") or payload.get("source_ids") or (), citations, report,
        )
        if not entries:
            return ""
        items = []
        for label, summary, url in entries:
            label_html = html.escape(label)
            if url:
                label_html = f"<a class=\"source-link\" href=\"{html.escape(url, quote=True)}\" target=\"_blank\" rel=\"noopener\">{label_html}</a>"
            summary_html = f"<span class=\"source-summary\">{html.escape(summary)}</span>" if summary else ""
            items.append(f"<div class=\"source-item\"><strong>{label_html}</strong>{summary_html}</div>")
        return f"<details class=\"chapter-sources chapter-source-list\"><summary>本节依据（{len(entries)} 个来源）</summary><div class=\"source-list\">{''.join(items)}</div></details>"
    if block_type in {"paragraph", "callout"}:
        text = str(payload.get("text") or "")
        if not text.strip() or _is_internal_explanation(text) or _looks_like_serialized_object(text):
            return ""
        if block_type == "callout":
            return f"<aside class=\"report-callout\">{title}{_paragraph_html(text)}</aside>"
        return f"<div class=\"prose-block\">{title}{_paragraph_html(text)}</div>"
    if block_type in {"bullet_list", "ordered_list"}:
        tag = "ul" if block_type == "bullet_list" else "ol"
        items = "".join(f"<li>{html.escape(_list_item_text(item))}</li>" for item in payload.get("items") or ())
        return f"{title}<{tag} class=\"report-list\">{items}</{tag}>"
    if block_type == "metric_grid":
        values = payload.get("items") or payload.get("metrics") or ()
        cards = "".join(_metric_card(item) for item in values if isinstance(item, Mapping))
        return f"<div class=\"card\">{title}{cards}</div>"
    if block_type == "chart":
        visual = visualizations.get(str(payload.get("visualization_id") or ""))
        dataset = datasets.get(visual.dataset_id) if visual else (datasets.get(dataset_ids[0]) if dataset_ids else None)
        rows = list(dataset.rows) if dataset else []
        x_field = visual.x_field if visual else ""
        y_field = visual.y_field if visual else ""
        if rows and (not x_field or not y_field):
            x_field, y_field = _infer_chart_fields(rows)
        chart_kind = visual.chart_kind if visual else "bar"
        if rows and x_field and y_field:
            chart = _render_svg_chart(rows, chart_kind=chart_kind, x_field=x_field, y_field=y_field, title=_safe_text(payload.get("title") or (visual.title if visual else "数据图表")))
            if chart:
                description = _chart_description(dataset, y_field) if dataset else ""
                table_rows = _table_rows(rows, dataset.columns if dataset else (), report=report, citations=citations)
                return f"<figure class=\"report-visual\">{title}{description}{chart}<details><summary>查看数据表</summary>{_html_table(table_rows, report=report, citations=citations)}</details></figure>"
        if not rows:
            return ""
        table_rows = _table_rows(rows, dataset.columns if dataset else (), report=report, citations=citations)
        return f"<figure class=\"report-visual\">{title}{_html_table(table_rows, report=report, citations=citations)}</figure>"
    if block_type in {"table", "claim_table", "timeline"}:
        raw_rows = payload.get("rows") or payload.get("items") or ()
        rows = _table_rows(raw_rows, payload.get("columns") or (), report=report, citations=citations)
        if not rows and dataset_ids:
            dataset = datasets.get(dataset_ids[0])
            rows = _table_rows(dataset.rows, dataset.columns, report=report, citations=citations) if dataset else []
        if block_type == "timeline" and rows:
            return f"<div class=\"report-visual\">{title}{_timeline(rows)}</div>"
        if not rows:
            # Empty source sets should not create placeholder copy in the report.
            return ""
        return f"<div class=\"report-visual\">{title}{_html_table(rows, report=report, citations=citations)}</div>"
    if block_type in {"image", "video"}:
        media_id = str(payload.get("media_id") or "")
        media_item = next((item for item in report.media if str(item.get("media_id") or "") == media_id), {})
        url = _safe_http_url(payload.get("source_url") or payload.get("url") or media_item.get("source_url"))
        if not url:
            kind = "图片" if block_type == "image" else "视频"
            return f"<div class=\"report-visual\">{title}<p>{kind}已关联，但当前没有可直接访问的链接。</p></div>"
        tag = "img" if block_type == "image" else "video"
        extra = " controls" if tag == "video" else ""
        return f"<div class=\"report-visual\">{title}<{tag} class=\"media\" src=\"{html.escape(url, quote=True)}\"{extra}></{tag}></div>"
    summary = _payload_summary(payload)
    if not summary:
        return ""
    return f"<div class=\"card\">{title}<p>{html.escape(summary)}</p></div>"


def _html_table(rows: list[dict[str, Any]], *, report: ReportDocument | None = None, citations: list[str] | None = None) -> str:
    if not rows:
        return ""
    columns = [column for column in rows[0] if not _is_internal_column(column) and not _is_source_column(column)]
    if not columns:
        return ""
    head = "".join(f"<th>{html.escape(_display_key(column))}</th>" for column in columns)
    body = "".join("<tr>" + "".join(f"<td>{_display_value(row.get(column, ''), report=report, citations=citations)}</td>" for column in columns) + "</tr>" for row in rows)
    return f"<table class=\"report-table\"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _table_rows(rows: Any, columns: Any, *, report: ReportDocument | None = None, citations: list[str] | None = None) -> list[dict[str, Any]]:
    if not isinstance(rows, (list, tuple)):
        return []
    if all(isinstance(row, Mapping) for row in rows):
        if isinstance(columns, (list, tuple)) and columns:
            descriptors = [
                (str(column.get("key") or column.get("label") or ""), _column_key(column))
                if isinstance(column, Mapping)
                else (str(column), _column_key(column))
                for column in columns
            ]
            result = []
            for row in rows:
                normalized: dict[str, Any] = {}
                for key, label in descriptors:
                    if _is_source_column(key) or _is_source_column(label):
                        continue
                    normalized[label] = row.get(key, row.get(label, ""))
                result.append(normalized)
        else:
            result = [dict(row) for row in rows]
    else:
        if not isinstance(columns, (list, tuple)) or not columns:
            return []
        names = [_column_key(column) for column in columns]
        if any(not name for name in names):
            return []
        result = [dict(zip(names, row)) for row in rows if isinstance(row, (list, tuple)) and len(row) == len(names)]
    for row in result:
        for key in list(row):
            if _is_source_column(key):
                row.pop(key, None)
    return result


def _is_source_column(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value).strip().lower())
    return normalized in {
        "citation", "citations", "citationid", "citationids",
        "source", "sources", "sourceid", "sourceids", "sourcetitle", "sourcetitles",
        "sourcematerial", "sourcematerials", "sourcedocumentid", "sourcedocumentids",
        "来源", "来源材料", "来源标题", "来源文献", "来源文件", "引用", "引用材料", "引用来源",
    }


_INTERNAL_ID_RE = re.compile(
    r"\b(?:n?node|n?claim|n?evidence|n?metric|dataset|trace|run|report|page|bocha|tavily|doc|document|source|media|visualization|investigation|task|attempt|record|analysis)[_-][A-Za-z0-9-]+\b",
    re.IGNORECASE,
)
_INTERNAL_RESOURCE_RE = re.compile(
    r"\b(?:document|source_document|resource)\s*[:=：]\s*[^\s，,；;）)]*",
    re.IGNORECASE,
)
_INTERNAL_FIELD_RE = re.compile(
    r"\b(?:trace|run|node|claim|evidence|metric|dataset|citation|source_document|document|media|visualization|report|task|attempt|page|investigation|record|analysis)_(?:id|ids)\b(?:\s*[:=：]\s*[^,，;；\]\s]+)?",
    re.IGNORECASE,
)
def _safe_text(value: Any) -> str:
    """Keep implementation identifiers out of user-facing report prose."""

    if isinstance(value, Mapping):
        text = next((str(value[key]) for key in ("text", "quote", "detail", "summary", "title", "label") if value.get(key)), "")
    elif isinstance(value, (list, tuple)):
        text = "、".join(filter(None, (_safe_text(item) for item in value)))
    else:
        text = str(value if value is not None else "")
    # The model may repeat the internal material-pack vocabulary in otherwise
    # reader-facing prose. Keep the conclusion, but remove implementation
    # identifiers and replace the technical reference with a normal label.
    text = re.sub(r"\bNodeTrace\b", "调查脉络", text, flags=re.IGNORECASE)
    text = _INTERNAL_RESOURCE_RE.sub("来源材料", text)
    text = _INTERNAL_FIELD_RE.sub("", text)
    text = _INTERNAL_ID_RE.sub("相关材料", text)
    text = re.sub(r"\b(?:document|source_document|resource)\s*[:=：]\s*相关材料", "来源材料", text, flags=re.IGNORECASE)
    text = re.sub(r"（\s*[，,、；;]\s*）", "", text)
    text = re.sub(r"\(\s*[,、;]\s*\)", "", text)
    return text


def _is_internal_column(value: Any) -> bool:
    column = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return column in {"id", "ids"} or column.endswith(("_id", "_ids"))


def _safe_http_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    return url if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""


_QUOTE_ONLY_RE = re.compile(r"^[\s\"'“”‘’「」『』`]+$")


def _substantive_text(value: Any) -> str:
    text = _safe_text(value).strip()
    return "" if not text or _QUOTE_ONLY_RE.fullmatch(text) else text


def _paragraph_html(value: Any) -> str:
    raw = str(value or "")
    if _is_internal_explanation(raw):
        return ""
    if _looks_like_serialized_object(raw):
        return ""
    text = _safe_text(raw)
    if not text:
        return ""
    return "".join(f"<p>{html.escape(part)}</p>" for part in text.split("\n") if part.strip())


def _is_internal_explanation(value: Any) -> bool:
    text = str(value or "")
    if not text.strip():
        return True
    if any(marker in text for marker in (
        "为何本章不绘制", "为什么本章不绘制", "当前数据不具备可比较的数值轴",
        "本章实际使用的数据集", "本报告使用的数据集", "本章使用的数据集",
    )):
        return True
    if any(marker in text for marker in ("metric_id", "dataset_id", "citation_ids", "source_document_id", "node_id", "claim_id", "evidence_id")):
        return True
    return False


def _looks_like_serialized_object(value: str) -> bool:
    text = value.strip()
    return len(text) >= 2 and text.startswith("{") and text.endswith("}")


def _payload_summary(payload: Mapping[str, Any]) -> str:
    text = payload.get("text") or payload.get("summary") or payload.get("detail")
    if text:
        return _safe_text(text)
    title = payload.get("title") or "该内容"
    return f"{_safe_text(title)}已整理到报告材料中。"


def _display_key(value: Any) -> str:
    labels = {
        "time": "时间", "time_scope": "时间范围", "date": "日期", "event": "事件/观察",
        "subject": "主体", "actor": "主体", "action": "行动", "observable_action": "可观察行动",
        "action_type": "行动类型", "status": "状态", "source_class": "来源类型",
        "trigger": "触发内容", "trigger_type": "触发类型", "observable_node": "观察节点",
        "wave": "传播阶段", "platform": "平台", "count": "数量", "stage": "阶段",
        "amount": "金额", "unit": "单位", "value": "数值", "name": "指标",
        "label": "类别", "share": "占比", "sample_scope": "样本范围", "display_value": "展示数值",
        "citations": "来源材料", "citation": "来源材料", "citation_ids": "来源材料",
        "source_id": "来源材料", "source_ids": "来源材料", "role": "角色", "basis": "依据",
        "certainty": "确定程度", "confidence": "可信程度", "matter": "事项", "statement": "说明",
        "limitation": "限制", "limitations": "限制", "denominator": "统计分母", "population": "统计对象",
        "publisher": "发布方", "description": "说明", "summary": "概要", "title": "标题",
        "count": "数量", "channel": "渠道", "category": "类别",
        "information_count": "信息量（条）", "info_count": "信息量（条）",
        "platform_name": "平台", "type": "类型", "matter": "事项",
        "percentage": "占比", "percent": "占比", "ratio": "比例",
        "value_count": "数量", "source": "来源", "source_title": "来源标题",
        "time_precision": "时间精度", "time_basis": "时间依据", "event_time": "事件时间",
        "event": "事件/观察", "evidence": "证据", "argument": "论点",
        "factuality": "事实属性", "directness": "直接程度", "importance": "重要性",
    }
    raw = _column_key(value)
    translated = labels.get(raw) or labels.get(raw.lower())
    if translated:
        return translated
    if re.search(r"[A-Za-z]", raw):
        chinese_label = re.sub(r"[A-Za-z][A-Za-z0-9_]*", "", raw).strip(" _-()（）[]【】:/")
        if re.search(r"[\u4e00-\u9fff]", chinese_label):
            return chinese_label
        return "其他信息"
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", raw):
        return "其他信息"
    return _safe_text(raw)


def _column_key(value: Any) -> str:
    if not isinstance(value, Mapping):
        return str(value)
    label = str(value.get("label") or "").strip()
    if label and re.search(r"[\u4e00-\u9fff]", label):
        return label
    return str(value.get("key") or label)


def _display_value(value: Any, *, report: ReportDocument | None = None, citations: list[str] | None = None) -> str:
    if isinstance(value, (dict, list, tuple)):
        if isinstance(value, list):
            return html.escape("、".join(_safe_text(item) for item in value))
        return html.escape(_safe_text(value.get("text") or value.get("label") or "相关材料"))
    text = str(value or "").strip()
    if report is not None and text.lower() in {"相关材料", "相关材料、相关材料", "相关材料, 相关材料"}:
        titles = [
            _safe_text(source.get("title") or "").strip()
            for _index, source in _citation_sources(citations or [], report)
        ]
        resolved = list(dict.fromkeys(title for title in titles if title))
        if resolved:
            return html.escape("；".join(resolved))
    return html.escape(_safe_text(value))


def _chart_description(dataset: Any, y_field: str) -> str:
    return f'<figcaption class="chart-description">{html.escape(_chart_description_text(dataset, y_field))}</figcaption>'


def _chart_description_text(dataset: Any, y_field: str) -> str:
    measure = _display_key(y_field)
    if measure in {"数量", "数值"} and "信息量" in f"{dataset.purpose} {dataset.grain}":
        measure = "相关信息量（条）"
    details = [f"纵轴指标：{measure}", f"统计口径：{_safe_text(dataset.grain)}"]
    if dataset.purpose:
        details.append(f"数据内容：{_safe_text(dataset.purpose)}")
    if dataset.sample_scope:
        details.append(f"统计范围：{_safe_text(dataset.sample_scope)}")
    if dataset.time_scope:
        details.append(f"时间口径：{_safe_text(dataset.time_scope)}")
    return "；".join(details)


def _metric_card(item: Mapping[str, Any]) -> str:
    label = _safe_text(item.get("label") or item.get("name") or "指标")
    value = _safe_text(item.get("value") if item.get("value") is not None else "暂无")
    unit = _safe_text(item.get("unit") or "")
    scope = _safe_text(item.get("time_scope") or "")
    display = _metric_display_value(value, unit)
    long_value = len(value) > 20
    display_value = display if len(display) <= 32 else display[:30] + "…"
    value_class = " class=\"kpi-long\"" if long_value else ""
    title = f" title=\"{html.escape(display, quote=True)}\"" if long_value else ""
    details = f"<details class=\"kpi-details\"><summary>查看完整数值</summary><p>{html.escape(display)}</p></details>" if len(display) > 32 else ""
    return f"<div class=\"kpi\"><b{value_class}{title}>{html.escape(display_value)}</b><span>{html.escape(label)}</span>{f'<small class=\"muted\">{html.escape(scope)}</small>' if scope else ''}{details}</div>"


def _metric_display_value(value: Any, unit: Any) -> str:
    """Join a metric value and unit without producing duplicated suffixes."""

    value_text = _safe_text(value).strip()
    unit_text = _safe_text(unit).strip()
    if not unit_text or not value_text:
        return value_text
    if unit_text == "%" and value_text.endswith("%"):
        return value_text
    if value_text.endswith(unit_text):
        return value_text
    return f"{value_text}{unit_text}"


def _unique_metrics(metrics: list[Any]) -> list[Any]:
    """Remove repeated observations while retaining different time scopes."""

    result: list[Any] = []
    seen: set[tuple[str, str, str, str]] = set()

    def normalize_scope(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", value, flags=re.UNICODE)

    def normalize_value(value: str, unit: str) -> str:
        number = _as_number(value)
        if number is not None:
            return f"number:{number:g}:{normalize_scope(unit)}"
        return f"text:{normalize_scope(value)}:{normalize_scope(unit)}"

    for metric in metrics:
        name = _safe_text(getattr(metric, "name", ""))
        value = _safe_text(getattr(metric, "value", ""))
        unit = _safe_text(getattr(metric, "unit", ""))
        scope = _safe_text(getattr(metric, "time_scope", ""))
        key = (name, normalize_value(value, unit), normalize_scope(unit), normalize_scope(scope))
        name_key = re.sub(r"[（(][^）)]*[）)]|\d{2,4}年?|口径[一二三四五12345]|同一|官网|后续|报道时点", "", name)
        name_key = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", name_key)
        duplicate = key in seen or any(
            existing[1] == key[1]
            and existing[2] == key[2]
            and existing[3] == key[3]
            and SequenceMatcher(None, name_key, existing[0]).ratio() >= 0.58
            for existing in seen
        )
        if duplicate:
            continue
        seen.add((name_key, key[1], key[2], key[3]))
        result.append(metric)
    return result


def _render_dashboard(report: ReportDocument, source_map: dict[str, dict[str, Any]]) -> str:
    if not report.metrics:
        return ""
    cards = "".join(_metric_card(metric.to_dict()) for metric in _unique_metrics(report.metrics)[:12])
    return f"<section class=\"dashboard\"><h2>关键指标</h2><div class=\"kpi-grid\">{cards}</div><p class=\"footnote\">指标来自调查材料中的明确口径；时间范围和来源可在相关章节及来源目录中复核。</p></section>"


def _timeline(rows: list[dict[str, Any]]) -> str:
    items: list[str] = []
    ordered_rows = sorted(
        enumerate(rows),
        key=lambda indexed: _timeline_sort_key(indexed[1], indexed[0]),
    )
    for _, row in ordered_rows:
        time_value = row.get("time") or row.get("date") or row.get("time_scope") or row.get("wave") or ""
        event = row.get("event") or row.get("trigger") or row.get("observable_action") or row.get("action") or row.get("observable_node") or ""
        actor = row.get("subject") or row.get("actor") or ""
        text = _safe_text(event)
        if actor:
            text = f"{_safe_text(actor)}：{text}"
        items.append(f"<div class=\"timeline-item\"><div class=\"timeline-time\">{html.escape(_safe_text(time_value))}</div><div class=\"timeline-event\">{html.escape(text)}</div></div>")
    return f"<div class=\"timeline\">{''.join(items)}</div>" if items else ""


def _timeline_sort_key(row: Mapping[str, Any], original_index: int) -> tuple[int, int, int, int, int]:
    time_value = _safe_text(row.get("time") or row.get("date") or row.get("time_scope") or row.get("wave") or "")
    match = re.search(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})(?:\s*[-/.月]\s*(\d{1,2}))?", time_value)
    if not match:
        return (1, 0, 0, 0, original_index)
    year, month, day = (int(value or 0) for value in match.groups())
    return (0, year, month, day, original_index)


def _infer_chart_fields(rows: list[dict[str, Any]]) -> tuple[str, str]:
    if not rows:
        return "", ""
    columns = [column for column in rows[0] if not _is_internal_column(column)]
    numeric = [column for column in columns if any(_as_number(row.get(column)) is not None for row in rows)]
    categorical = [column for column in columns if column not in numeric]
    return (categorical[0] if categorical else (columns[0] if columns else ""), numeric[0] if numeric else "")


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).replace(",", "").replace("％", "%").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    match = re.fullmatch(r"(?:约|大约|超过|超|近)?\s*(\d+(?:\.\d+)?)\s*(万|亿|千|百)?\s*(?:余|多)?", text)
    if match:
        return float(match.group(1)) * {"万": 10_000, "亿": 100_000_000, "千": 1_000, "百": 100}.get(match.group(2) or "", 1)
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _render_svg_chart(
    rows: list[dict[str, Any]],
    *,
    chart_kind: str,
    x_field: str,
    y_field: str,
    title: str,
) -> str:
    """Render a self-contained SVG from an existing same-grain dataset."""

    points = [
        (_safe_text(row.get(x_field)), number)
        for row in rows[:24]
        if (number := _as_number(row.get(y_field))) is not None
    ]
    if not points:
        return ""
    kind = str(chart_kind or "bar").lower()
    if kind not in {"line", "area", "bar", "pie", "doughnut", "scatter", "radar"}:
        return ""
    label = html.escape(_safe_text(title) or "数据图表", quote=True)
    width, height = 760, 300
    if kind in {"pie", "doughnut"}:
        suffix = "%" if any(str(row.get(y_field) or "").strip().endswith(("%", "％")) for row in rows) else ""
        return _render_svg_pie(points, kind=kind, title=label, value_suffix=suffix)
    if kind == "radar":
        return _render_svg_radar(points, title=label)
    if kind == "scatter":
        return _render_svg_scatter(rows, x_field=x_field, y_field=y_field, title=label)
    left, right, top = 54, 22, 24
    plot_height = 218
    axis_bottom = top + plot_height
    label_indices = _chart_label_indices(len(points))
    wrapped_labels = {
        index: textwrap.wrap(points[index][0], width=10, break_long_words=True, break_on_hyphens=False) or [""]
        for index in label_indices
    }
    label_lines = max((len(lines) for lines in wrapped_labels.values()), default=1)
    height = axis_bottom + 16 + label_lines * 13 + 10
    widest_label = max(
        (_estimated_svg_text_width(line) for lines in wrapped_labels.values() for line in lines),
        default=0,
    )
    label_padding = widest_label / 2 + 12
    plot_left = left + label_padding
    plot_right = width - right - label_padding
    plot_width = plot_right - plot_left
    maximum = max(max(value for _, value in points), 0)
    minimum = min(min(value for _, value in points), 0)
    span = maximum - minimum or 1
    base_y = top + (maximum / span) * plot_height
    x_positions = [plot_left + (index * plot_width / max(len(points) - 1, 1)) for index in range(len(points))]
    y_positions = [top + ((maximum - value) / span) * plot_height for _, value in points]
    parts = [
        f'<div class="chart-shell bar-chart"><svg viewBox="0 0 {width} {height}" role="img" aria-label="{label}">',
        f"<title>{label}</title>",
        f'<line class="axis" stroke="#b8c9cd" stroke-width="1" x1="{left}" y1="{axis_bottom}" x2="{width - right}" y2="{axis_bottom}"/>',
        f'<line class="axis" stroke="#b8c9cd" stroke-width="1" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}"/>',
    ]
    for fraction in (0, 0.5, 1):
        y = top + fraction * plot_height
        value = maximum - fraction * span
        parts.append(f'<line class="grid" stroke="#e5eef0" stroke-width="1" x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}"/>')
        parts.append(f'<text fill="#667085" font-size="11" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{html.escape(_format_number(value))}</text>')
    if kind == "bar":
        bar_width = min(48, max(12, plot_width / max(len(points) * 1.6, 1)))
        for index, ((raw_label, value), x, y) in enumerate(zip(points, x_positions, y_positions)):
            bar_y = min(y, base_y)
            bar_height = max(abs(base_y - y), 2)
            parts.append(f'<rect class="bar-mark" fill="#3d8790" x="{x - bar_width / 2:.1f}" y="{bar_y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" rx="4"><title>{html.escape(raw_label)}：{html.escape(_format_number(value))}</title></rect>')
            parts.append(f'<text class="value-label" fill="#344054" font-size="10" x="{x:.1f}" y="{bar_y - 5:.1f}" text-anchor="middle">{html.escape(_format_number(value))}</text>')
    else:
        line_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(x_positions, y_positions))
        if kind == "area":
            parts.append(f'<polygon class="area" fill="#176b87" fill-opacity=".14" points="{x_positions[0]:.1f},{base_y:.1f} {line_points} {x_positions[-1]:.1f},{base_y:.1f}"/>')
        if kind in {"line", "area"}:
            parts.append(f'<polyline class="series" stroke="#176b87" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round" points="{line_points}"/>')
        for (raw_label, value), x, y in zip(points, x_positions, y_positions):
            parts.append(f'<circle class="point" fill="#e27856" stroke="#fff" stroke-width="2" cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{html.escape(raw_label)}：{html.escape(_format_number(value))}</title></circle>')
    for index, lines in wrapped_labels.items():
        x = x_positions[index]
        full_label = html.escape(points[index][0])
        parts.append(f'<text class="axis-label" fill="#667085" font-size="11" x="{x:.1f}" y="{axis_bottom + 14}" text-anchor="middle"><title>{full_label}</title>')
        parts.extend(
            f'<tspan x="{x:.1f}" dy="{0 if line_no == 0 else 13}">{html.escape(line)}</tspan>'
            for line_no, line in enumerate(lines)
        )
        parts.append("</text>")
    parts.append("</svg></div>")
    return "".join(parts)


def _estimated_svg_text_width(value: str, font_size: float = 11) -> float:
    return sum(
        font_size * (1.0 if unicodedata.east_asian_width(char) in {"F", "W"} else 0.55)
        for char in value
    )


def _chart_label_indices(count: int, max_labels: int = 6) -> list[int]:
    if count <= max_labels:
        return list(range(count))
    step = math.ceil((count - 1) / (max_labels - 1))
    return sorted(set(range(0, count, step)) | {count - 1})


def _render_svg_pie(points: list[tuple[str, float]], *, kind: str, title: str, value_suffix: str = "") -> str:
    import math

    width, height, cy, radius = 760, 300, 150, 105
    cx = width / 2
    total = sum(max(value, 0) for _, value in points)
    if total <= 0:
        return ""
    parts = [f'<div class="chart-shell bar-chart"><svg viewBox="0 0 {width} {height}" role="img" aria-label="{title}"><title>{title}</title>']
    angle = -math.pi / 2
    colors = ("#176b87", "#e27856", "#6d9f71", "#8b78a9", "#d3a13d", "#7e9aa2")
    legend: list[str] = []
    for index, (raw_label, raw_value) in enumerate(points):
        value = max(raw_value, 0)
        delta = value / total * math.tau
        end = angle + delta
        x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        x2, y2 = cx + radius * math.cos(end), cy + radius * math.sin(end)
        large = 1 if delta > math.pi else 0
        path = f"M {cx} {cy} L {x1:.2f} {y1:.2f} A {radius} {radius} 0 {large} 1 {x2:.2f} {y2:.2f} Z"
        color = colors[index % len(colors)]
        display_value = _format_number(raw_value) + value_suffix
        parts.append(f'<path d="{path}" fill="{color}"><title>{html.escape(raw_label)}：{html.escape(display_value)}</title></path>')
        legend.append(f'<span><i style="background:{color}"></i>{html.escape(raw_label)} {html.escape(display_value)}</span>')
        angle = end
    if kind == "doughnut":
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{radius * .52:.1f}" fill="#fff"/>')
    parts.append("</svg><div class=\"chart-legend\">" + "".join(legend) + "</div></div>")
    return "".join(parts)


def _render_svg_radar(points: list[tuple[str, float]], *, title: str) -> str:
    """Render several comparable dimensions for one object as a radar chart."""

    if len(points) < 3:
        return ""
    width, height = 760, 360
    center_x, center_y, radius = 300, 178, 118
    maximum = max(max(value for _, value in points), 0) or 1
    angles = [-math.pi / 2 + math.tau * index / len(points) for index in range(len(points))]
    outer = [(center_x + radius * math.cos(angle), center_y + radius * math.sin(angle)) for angle in angles]
    parts = [f'<div class="chart-shell"><svg viewBox="0 0 {width} {height}" role="img" aria-label="{title}"><title>{title}</title>']
    for level in (0.33, 0.66, 1.0):
        ring = [(center_x + radius * level * math.cos(angle), center_y + radius * level * math.sin(angle)) for angle in angles]
        parts.append(f'<polygon class="grid" stroke="#e5eef0" stroke-width="1" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in ring)}" fill="none"/>')
    for x, y in outer:
        parts.append(f'<line class="grid" stroke="#e5eef0" stroke-width="1" x1="{center_x}" y1="{center_y}" x2="{x:.1f}" y2="{y:.1f}"/>')
    inner = [(center_x + radius * max(0, value) / maximum * math.cos(angle), center_y + radius * max(0, value) / maximum * math.sin(angle)) for (_, value), angle in zip(points, angles)]
    parts.append(f'<polygon class="area" fill="#176b87" fill-opacity=".14" stroke="#176b87" stroke-width="2" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in inner)}"/>')
    for index, ((raw_label, value), angle) in enumerate(zip(points, angles)):
        x, y = inner[index]
        label_x, label_y = center_x + (radius + 18) * math.cos(angle), center_y + (radius + 18) * math.sin(angle)
        anchor = "start" if math.cos(angle) > 0.25 else ("end" if math.cos(angle) < -0.25 else "middle")
        parts.append(f'<circle class="point" fill="#e27856" stroke="#fff" stroke-width="2" cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{html.escape(raw_label)}：{html.escape(_format_number(value))}</title></circle>')
        parts.append(f'<text fill="#667085" font-size="11" x="{label_x:.1f}" y="{label_y:.1f}" text-anchor="{anchor}">{html.escape(raw_label[:16])}</text>')
    parts.append("</svg></div>")
    return "".join(parts)


def _render_svg_scatter(rows: list[dict[str, Any]], *, x_field: str, y_field: str, title: str) -> str:
    points = [
        (_as_number(row.get(x_field)), _as_number(row.get(y_field)))
        for row in rows[:48]
    ]
    points = [(x, y) for x, y in points if x is not None and y is not None]
    if len(points) < 2:
        return ""
    width, height = 760, 300
    left, right, top, bottom = 54, 22, 24, 48
    plot_width, plot_height = width - left - right, height - top - bottom
    x_min, x_max = min(x for x, _ in points), max(x for x, _ in points)
    y_min, y_max = min(y for _, y in points), max(y for _, y in points)
    x_span, y_span = x_max - x_min or 1, y_max - y_min or 1
    parts = [f'<div class="chart-shell"><svg viewBox="0 0 {width} {height}" role="img" aria-label="{title}"><title>{title}</title>', f'<line class="axis" stroke="#b8c9cd" stroke-width="1" x1="{left}" y1="{top + plot_height}" x2="{width - right}" y2="{top + plot_height}"/><line class="axis" stroke="#b8c9cd" stroke-width="1" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}"/>']
    for x, y in points:
        px = left + (x - x_min) / x_span * plot_width
        py = top + (y_max - y) / y_span * plot_height
        parts.append(f'<circle class="point" fill="#e27856" stroke="#fff" stroke-width="2" cx="{px:.1f}" cy="{py:.1f}" r="5"><title>{html.escape(_format_number(x))}，{html.escape(_format_number(y))}</title></circle>')
    parts.extend([f'<text fill="#667085" font-size="11" x="{left}" y="{height - 14}" text-anchor="start">{html.escape(_format_number(x_min))}</text>', f'<text fill="#667085" font-size="11" x="{width - right}" y="{height - 14}" text-anchor="end">{html.escape(_format_number(x_max))}</text>', "</svg></div>"])
    return "".join(parts)


def _citation_document_ids(citations: list[str], report: ReportDocument) -> list[str]:
    source_ids = {str(item.get("document_id")) for item in report.sources}
    result: list[str] = []
    for citation in citations:
        resolved = report.citation_index.get(str(citation), [])
        if not resolved and str(citation) in source_ids:
            resolved = [str(citation)]
        for document_id in resolved:
            if document_id in source_ids and document_id not in result:
                result.append(document_id)
    return result


def _chapter_source_ids(blocks: list[Any], report: ReportDocument) -> list[str]:
    citations: list[str] = []
    for block in blocks:
        citations.extend(str(item) for item in getattr(block, "citations", []) or [])
    return _citation_document_ids(citations, report)


def _citation_sources(citations: list[str], report: ReportDocument) -> list[tuple[int, dict[str, Any]]]:
    ordered = {str(item.get("document_id")): index for index, item in enumerate(report.sources, 1)}
    by_id = {str(item.get("document_id")): item for item in report.sources}
    return [(ordered[document_id], by_id[document_id]) for document_id in _citation_document_ids(citations, report) if document_id in ordered]


def _source_list_entries(
    raw_items: Any,
    citations: list[str],
    report: ReportDocument,
) -> list[tuple[str, str, str]]:
    source_map = {str(item.get("document_id")): item for item in report.sources}
    entries: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(label: Any, summary: Any = "", url: Any = "") -> None:
        clean_label = _safe_text(label).strip()
        clean_summary = _safe_text(summary).strip()
        clean_url = _safe_http_url(url)
        key = (clean_label, clean_summary, clean_url)
        if clean_label and key not in seen:
            seen.add(key)
            entries.append(key)

    for item in raw_items if isinstance(raw_items, (list, tuple)) else ():
        if isinstance(item, Mapping):
            add(
                item.get("label") or item.get("title") or item.get("name") or item.get("publisher"),
                item.get("summary") or item.get("description") or item.get("excerpt"),
                item.get("locator") or item.get("url"),
            )
            continue
        item_id = str(item)
        document_ids = [item_id] if item_id in source_map else report.citation_index.get(item_id, [])
        if document_ids:
            for document_id in document_ids:
                source = source_map.get(str(document_id), {})
                add(source.get("title"), "", source.get("url"))
        elif not _INTERNAL_ID_RE.search(item_id):
            add(item_id)

    if not entries:
        for document_id in _citation_document_ids(citations, report):
            source = source_map.get(document_id, {})
            add(source.get("title"), "", source.get("url"))
    return entries


def _render_chapter_citations(citations: list[str], report: ReportDocument, source_map: dict[str, dict[str, Any]]) -> str:
    sources = _citation_sources(citations, report)
    if not sources:
        return ""
    items: list[str] = []
    for index, source in sources:
        title = _safe_text(source.get("title") or "来源材料")
        url = _safe_http_url(source.get("url"))
        label = f"来源 {index}：{title}"
        if url:
            label = f'<a class="source-link" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">{html.escape(label)}</a>'
        else:
            label = html.escape(label)
        items.append(f"<div class=\"source-item\">{label}</div>")
    return f"<details class=\"chapter-sources\"><summary>本节依据（{len(sources)} 条来源）</summary><div class=\"source-list\">{''.join(items)}</div></details>"


def _markdown_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        if isinstance(value, list):
            return "、".join(_safe_text(item) for item in value)
        return _safe_text(value.get("text") or value.get("label") or "相关材料")
    return _safe_text(value)


def _list_item_text(item: Any) -> str:
    if isinstance(item, Mapping):
        text = _safe_text(item.get("text") or item.get("label") or item.get("title") or "")
        detail = _safe_text(item.get("detail") or item.get("description") or "")
        return f"{text}：{detail}" if text and detail else (text or detail)
    return _safe_text(item)


def _render_sources(sources: list[dict[str, Any]]) -> str:
    items = []
    seen: set[str] = set()
    for index, source in enumerate(sources, 1):
        document_id = str(source.get("document_id") or "")
        if document_id in seen:
            continue
        seen.add(document_id)
        title = _safe_text(source.get("title") or "来源材料")
        url = _safe_http_url(source.get("url"))
        title_html = f"<a class=\"source-link\" href=\"{html.escape(url, quote=True)}\" target=\"_blank\" rel=\"noopener\">{html.escape(title)}</a>" if url else html.escape(title)
        publisher = _safe_text(source.get("publisher") or "")
        items.append(f"<div class=\"source-item\"><strong>来源 {index}：{title_html}</strong>{f'<span class=\"muted\">{html.escape(publisher)}</span>' if publisher else ''}</div>")
    return f"<section id=\"sources\"><h2>附录：来源目录</h2><details class=\"source-directory\"><summary>全部来源（{len(items)}）</summary><div class=\"source-list\">{''.join(items)}</div></details></section>"


def render_report_pdf(report: ReportDocument) -> bytes:
    """Paginate the same reader-facing layout used by the HTML report."""
    import fitz

    output = BytesIO()
    story = fitz.Story(html=render_report_html(report))
    writer = fitz.DocumentWriter(output)
    page_rect = fitz.paper_rect("a4")
    content_rect = page_rect + (42, 42, -42, -42)
    while True:
        device = writer.begin_page(page_rect)
        more, _filled = story.place(content_rect)
        story.draw(device)
        writer.end_page()
        if not more:
            break
    writer.close()
    return output.getvalue()


__all__ = ["render_report_html", "render_report_json", "render_report_markdown", "render_report_pdf"]
