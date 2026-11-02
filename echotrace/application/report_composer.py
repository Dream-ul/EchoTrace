"""Report composition at the frozen Trace boundary.

The composer is deliberately small: the model writes the report prose and
block layout, while this module supplies a bounded material pack and enforces
identity, citation, dataset, and template contracts before persistence.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from echotrace.agent.contracts import AgentResult
from echotrace.domain.reporting import REPORT_BLOCK_TYPES, AnalysisDataset, ReportBlock, ReportDocument, VisualizationSpec
from echotrace.domain.trace_nodes import TraceContractError

from .node_trace import NodeTrace
from .report_data import materialize_report_datasets
from .report_templates import InvestigationReportTemplate


ReportRoleRunner = Callable[[str, str, dict[str, Any]], Any]


def build_report_material_pack(
    trace: NodeTrace,
    template: InvestigationReportTemplate | None = None,
) -> dict[str, Any]:
    """Build the bounded, source-grounded input projection for Report Composer."""

    trace = _scope_node_material_ids(trace)
    nodes: list[dict[str, Any]] = []
    document_ids: set[str] = set()
    for node in trace.nodes:
        draft = dict(node.get("draft") or {})
        claims = [dict(item) for item in draft.get("claims") or () if isinstance(item, Mapping)]
        evidence = [dict(item) for item in draft.get("evidence") or () if isinstance(item, Mapping)]
        media = [dict(item) for item in draft.get("media") or () if isinstance(item, Mapping)]
        metrics = [dict(item) for item in draft.get("metrics") or () if isinstance(item, Mapping)]
        datasets = [dict(item) for item in draft.get("datasets") or () if isinstance(item, Mapping)]
        for evidence_item in evidence:
            document_id = str(evidence_item.get("document_id") or "").strip()
            if document_id:
                document_ids.add(document_id)
        for claim in claims:
            document_ids.update(str(item).strip() for item in claim.get("source_document_ids") or () if str(item).strip())
        for media_item in media:
            document_id = str(media_item.get("document_id") or "").strip()
            if document_id:
                document_ids.add(document_id)
        nodes.append({
            "node_id": str(node.get("node_id") or ""),
            "slot_id": str(node.get("slot_id") or ""),
            "node_order": node.get("node_order"),
            "title": str(node.get("title") or ""),
            "goal": str(node.get("goal") or ""),
            "conclusion": str(draft.get("conclusion") or ""),
            "claims": claims,
            "evidence": evidence,
            "media": media,
            "metrics": metrics,
            "datasets": datasets,
        })
    result = {
        "trace_id": trace.trace_id,
        "run_id": trace.run_id,
        "investigation_type": trace.investigation_type,
        "subject": trace.subject,
        "nodes": nodes,
        "edges": [dict(item) for item in trace.edges],
        "source_document_ids": sorted(document_ids),
        "sources": _report_sources(trace, []),
    }
    if template is not None:
        result["template_datasets"] = [
            item.to_dict()
            for item in materialize_report_datasets(nodes=trace.nodes, template=template)
        ]
    return result


def compose_report(
    *,
    trace: NodeTrace,
    template: InvestigationReportTemplate,
    report_id: str,
    runner: ReportRoleRunner,
    source_catalog: list[dict[str, Any]] | None = None,
) -> ReportDocument:
    """Plan, generate each chapter, then assemble one frozen-Trace report."""

    _assert_frozen_trace(trace)
    material_pack = build_report_material_pack(trace, template)
    base_context = _base_context(trace, template, report_id, material_pack)
    plan = plan_report(trace=trace, template=template, report_id=report_id, runner=runner, base_context=base_context)
    sections = [
        generate_report_section(trace=trace, template=template, report_id=report_id, runner=runner,
                                plan=plan, chapter=chapter, order=order, base_context=base_context)
        for order, chapter in enumerate(template.chapters, 1)
    ]
    return assemble_report(
        trace=trace, template=template, report_id=report_id, plan=plan,
        sections=sections, source_catalog=source_catalog,
    )


def _base_context(trace: NodeTrace, template: InvestigationReportTemplate, report_id: str, material_pack: dict[str, Any]) -> dict[str, Any]:
    trace = _scope_node_material_ids(trace)
    return {
        "run_id": trace.run_id,
        "report_id": report_id,
        "trace_id": trace.trace_id,
        "investigation_type": trace.investigation_type,
        "trace": trace.to_dict(),
        "report_template": _template_payload(template),
        "material_pack": material_pack,
    }


def _assert_frozen_trace(trace: NodeTrace) -> None:
    if not trace.nodes:
        raise TraceContractError("报告生成需要至少一个冻结节点")
    for node in trace.nodes:
        draft = dict(node.get("draft") or {})
        if str(node.get("status") or "") != "frozen" or int(node.get("frozen_revision") or -1) != 3 or int(draft.get("revision") or -1) != 3:
            raise TraceContractError(f"节点 {node.get('slot_id') or node.get('node_id')} 尚未达到 revision=3/frozen")


def plan_report(*, trace: NodeTrace, template: InvestigationReportTemplate, report_id: str, runner: ReportRoleRunner, base_context: dict[str, Any] | None = None) -> dict[str, Any]:
    _assert_frozen_trace(trace)
    context = base_context or _base_context(trace, template, report_id, build_report_material_pack(trace, template))
    answer = _call_report_role(
        runner,
        "依据中文 Markdown 模板规划面向普通读者的报告目录、章节重点和总摘要。章节重点必须各自承担明确且不同的问题，并按模板顺序形成递进叙事；相邻章节可以承接，但不得反复概述全部材料。标题与摘要不得出现数据库字段名、JSON/Python 对象、run/node/claim/evidence 等内部 ID。只返回 report_plan JSON。",
        {**context, "phase": "plan"},
    )
    return _parse_plan(answer, trace=trace, template=template, report_id=report_id)


def generate_report_section(*, trace: NodeTrace, template: InvestigationReportTemplate, report_id: str, runner: ReportRoleRunner, plan: dict[str, Any], chapter: Any, order: int, base_context: dict[str, Any] | None = None) -> dict[str, Any]:
    _assert_frozen_trace(trace)
    context = base_context or _base_context(trace, template, report_id, build_report_material_pack(trace, template))
    material_pack = context["material_pack"]
    chapter_nodes = [node for node in material_pack["nodes"] if node.get("slot_id") in set(chapter.node_slots)]
    chapter_data_ids = {
        f"{'report_analysis' if dataset.collection_mode == 'analysis_feature' else 'report_template'}_{dataset.dataset_id}"
        for dataset in template.datasets
        if set(dataset.source_nodes).intersection(chapter.node_slots)
    }
    chapter_template_datasets = [
        dict(item) for item in material_pack.get("template_datasets", [])
        if str(item.get("dataset_id") or "") in chapter_data_ids
    ]
    supported_block_types = sorted(REPORT_BLOCK_TYPES)
    supported_block_json = json.dumps(supported_block_types, ensure_ascii=False)
    base_runner = runner

    def section_runner(role: str, objective: str, payload: dict[str, Any]) -> Any:
        constrained_objective = (
            f"{objective}\n"
            f"本章可使用的 block.type 为 {supported_block_json}。模板 recommended_blocks 是内容提示，不是白名单；请依据本章事实与数据的语义，自主选择最合适的结构。"
            "并列理由、问题、行动或结果使用 bullet_list；有明确先后顺序的步骤和阶段使用 ordered_list；连续叙事使用 paragraph。只有确有对应材料时才使用表格、时间线、指标网格、图表或媒体块。"
            "对可核验的数据，先判断比较对象、指标口径和粒度是否一致，再选择表格、timeline 或 chart；图表类型应匹配数据关系（时间变化用折线/面积，对比用柱状，构成占比用饼/环，相关性用散点，同一对象的多维度用雷达），不得为了装饰绘图。"
            "同一数据集、同一指标与同一时间范围只展示一次图表；已有图表覆盖相同数据时不要再输出重复的柱状图或折线图。渠道或类别的数量/占比表属于数据表，不属于主张表；完整互斥构成优先用饼图/环图表达并保留数值表。占比未合计到约100%时，只有在同口径冻结总量能够逐行核验数量与比例、且能计算其它部分时，才将其它部分作为单独扇区展示，否则保留表格，不得把已列类别重新归一化。"
            "视觉块必须引用当前章节可访问的冻结 Trace 数据集或媒体；图表同时提供 VisualizationSpec 和对应 dataset_ids。不得编造数值、样本、来源或媒体。"
            "证据主张归纳表使用 claim_table 并在 citations 引用来源；table 仅用于展示已存在的数据集，必须提供 dataset_ids。"
            "payload 中的结构化内容只能放入 items/rows 等结构化字段，不能把字典、数组或字段名拼接进 paragraph.text。"
            "面向读者的文字必须像正式调查报告，不能出现 JSON/Python 字典、metric_id、dataset_id、claim_id、evidence_id、node_id、run_id 等内部字段或 ID。"
            "来源 ID 只放入 citations 数组，随后由系统映射为来源目录中的标题和链接。"
            "应用提供的 template_datasets 是依据模板从冻结节点确定性整理的只读数据，可以引用但不得改写数值、样本范围、限制或来源；来源文本情感、立场和主题信号不能写成总体民意。"
        )
        return base_runner(role, constrained_objective, payload)

    runner = section_runner
    outline = [
        {
            "order": index,
            "chapter_id": item.chapter_id,
            "title": item.title,
            "purpose": item.purpose,
            "focus": plan["chapters"][item.chapter_id]["focus"],
            "subheadings": plan["chapters"][item.chapter_id]["subheadings"],
        }
        for index, item in enumerate(template.chapters, 1)
    ]
    current_outline = outline[order - 1]
    report_context = {
        "title": plan["title"],
        "summary": plan["summary"],
        "chapter_outline": outline,
        "previous_chapter": outline[order - 2] if order > 1 else None,
        "next_chapter": outline[order] if order < len(outline) else None,
    }
    chapter_context = {
        **plan["chapters"][chapter.chapter_id],
        "report_context": report_context,
        "current_outline": current_outline,
    }
    answer = _call_report_role(
        runner,
        f"根据完整报告主线生成“{chapter.title}”章节，只返回该章节 JSON。严格使用模板给出的子标题及顺序，不增加、删除或改写标题；每个子标题后放入对应的事实分析内容。承接前章而不复述，为后续章节提供自然衔接。",
        {**context, "phase": "section", "chapter": chapter_context,
         "trace": {"trace_id": trace.trace_id, "run_id": trace.run_id,
                   "investigation_type": trace.investigation_type, "subject": trace.subject,
                   "nodes": chapter_nodes, "edges": [dict(item) for item in trace.edges]},
         "material_pack": {"trace_id": trace.trace_id, "run_id": trace.run_id,
                           "investigation_type": trace.investigation_type, "subject": trace.subject,
                           "nodes": chapter_nodes, "edges": [dict(item) for item in trace.edges],
                           "source_document_ids": material_pack["source_document_ids"],
                           "template_datasets": chapter_template_datasets},
         "chapter_material": {"nodes": chapter_nodes,
                              "source_document_ids": material_pack["source_document_ids"],
                              "template_datasets": chapter_template_datasets}},
    )
    section = _parse_section(answer, chapter_id=chapter.chapter_id, chapter=chapter, order=order)
    return _normalize_section_citations(
        section, _trace_citation_ids(_scope_node_material_ids(trace)),
    )


def build_source_limits_fallback_section(*, trace: NodeTrace, chapter: Any, order: int, reason: str = "") -> dict[str, Any]:
    """Build a contract-safe sources/limits chapter from the frozen Trace.

    This chapter is intentionally text-only.  When the model cannot satisfy
    the section contract, inventing a metric, dataset, or chart would make the
    report less trustworthy.  The fallback keeps stable source references and
    states the evidence boundary so the report can still be assembled.
    """

    node_slots = set(getattr(chapter, "node_slots", ()) or ())
    selected = [node for node in trace.nodes if str(node.get("slot_id") or "") in node_slots]
    document_ids: set[str] = set()
    citations: list[str] = []
    for node in selected:
        node_id = str(node.get("node_id") or "").strip()
        if node_id:
            citations.append(node_id)
        draft = dict(node.get("draft") or {})
        for claim in draft.get("claims") or ():
            if not isinstance(claim, Mapping):
                continue
            claim_id = str(claim.get("claim_id") or "").strip()
            if claim_id:
                citations.append(claim_id)
            document_ids.update(
                str(item).strip()
                for item in claim.get("source_document_ids") or ()
                if str(item).strip()
            )
        for evidence in draft.get("evidence") or ():
            if not isinstance(evidence, Mapping):
                continue
            evidence_id = str(evidence.get("evidence_id") or "").strip()
            if evidence_id:
                citations.append(evidence_id)
            document_id = str(evidence.get("document_id") or "").strip()
            if document_id:
                document_ids.add(document_id)
        for media in draft.get("media") or ():
            if isinstance(media, Mapping):
                document_id = str(media.get("document_id") or "").strip()
                if document_id:
                    document_ids.add(document_id)

    # Keep the persisted fallback small and deterministic while retaining
    # enough IDs for a reader to audit the chapter against the frozen Trace.
    citations = list(dict.fromkeys(citations))[:96]
    source_items = sorted(document_ids)[:64]
    boundary = (
        f"本章依据本次调查中 {len(selected)} 个分析部分的主张与证据，共识别 {len(document_ids)} 个来源文档。"
        "来源数量只说明材料覆盖规模，不代表独立印证强度。报告中的结论限于有来源支持的主张；材料未提供原始记录或独立核验的事项，均按前文标注为待确认，不作补充推断。"
    )
    headings = list(getattr(chapter, "subheadings", ()) or ())
    source_heading = headings[0] if headings else None
    boundary_heading = headings[1] if len(headings) > 1 else None
    blocks = []
    if source_heading:
        blocks.append({
            "type": "heading",
            "payload": {"level": source_heading.level, "text": source_heading.title},
            "citations": [],
            "dataset_ids": [],
        })
    blocks.append({
            "type": "source_list",
            "payload": {"title": "支撑本章的主要来源", "source_ids": source_items},
            "citations": citations,
            "dataset_ids": [],
        })
    if boundary_heading:
        blocks.append({
            "type": "heading",
            "payload": {"level": boundary_heading.level, "text": boundary_heading.title},
            "citations": [],
            "dataset_ids": [],
        })
    blocks.append({
            "type": "paragraph",
            "payload": {"text": boundary},
            "citations": citations,
            "dataset_ids": [],
        })
    return {
        "chapter": {
            "chapter_id": chapter.chapter_id,
            "title": chapter.title,
            "order": order,
            "purpose": chapter.purpose,
            "summary": chapter.purpose,
            "blocks": blocks,
        },
        "metrics": [],
        "datasets": [],
        "visualizations": [],
        "media": [],
    }


def assemble_report(
    *,
    trace: NodeTrace,
    template: InvestigationReportTemplate,
    report_id: str,
    plan: dict[str, Any],
    sections: list[dict[str, Any]],
    source_catalog: list[dict[str, Any]] | None = None,
) -> ReportDocument:
    trace = _scope_node_material_ids(trace)
    _assert_frozen_trace(trace)
    material_pack = build_report_material_pack(trace, template)
    canonical = _trace_material_index(trace)
    for dataset in materialize_report_datasets(nodes=trace.nodes, template=template):
        canonical["datasets"][dataset.dataset_id] = dataset.to_dict()
    chapters: list[dict[str, Any]] = []
    report_metrics: dict[str, dict[str, Any]] = dict(canonical["metrics"])
    report_datasets: dict[str, dict[str, Any]] = dict(canonical["datasets"])
    report_visualizations: dict[str, dict[str, Any]] = {}
    report_media: dict[str, dict[str, Any]] = {}
    known_citations = _trace_citation_ids(trace)
    sections = [_normalize_section_citations(section, known_citations) for section in sections]
    for section in sections:
        available_dataset_ids = set(report_datasets)
        available_dataset_ids.update(
            str(item.get("dataset_id") or "")
            for item in section.get("datasets", [])
            if isinstance(item, Mapping) and str(item.get("dataset_id") or "").strip()
        )
        section = _normalize_section_for_assembly(
            section, available_dataset_ids=available_dataset_ids,
        )
        chapters.append(section["chapter"])
        for key, target in (("metrics", report_metrics), ("datasets", report_datasets), ("visualizations", report_visualizations), ("media", report_media)):
            for item in section.get(key, []):
                if not isinstance(item, Mapping):
                    raise TraceContractError(f"报告章节 {section['chapter']['chapter_id']} 的 {key} 项必须是对象")
                id_key = {"metrics": "metric_id", "datasets": "dataset_id", "visualizations": "visualization_id", "media": "media_id"}[key]
                item_id = str(item.get(id_key) or "")
                if item_id:
                    if key in canonical:
                        source_item = canonical[key].get(item_id)
                        if source_item is None and key == "metrics":
                            # Trace metrics are already included in the report;
                            # ignore model-created copies with non-Trace IDs.
                            continue
                        if source_item is None:
                            raise TraceContractError(f"报告章节 {section['chapter']['chapter_id']} 引用了冻结 Trace 中不存在的 {id_key}：{item_id}")
                        # The model may echo stale or edited fields beside a
                        # frozen ID. The Trace is authoritative; retain only
                        # its canonical object instead of failing assembly.
                        target[item_id] = dict(source_item)
                    else:
                        target[item_id] = dict(item)

        # A chapter may reference a frozen dataset or visualization without
        # repeating its full definition.  Carry the immutable Trace material
        # into the document so the renderer has the referenced data available.
        for block in section["chapter"].get("blocks", []):
            for dataset_id in block.get("dataset_ids", []):
                source_item = canonical["datasets"].get(str(dataset_id))
                if source_item is not None:
                    report_datasets.setdefault(str(dataset_id), dict(source_item))

    for visualization in report_visualizations.values():
        dataset_id = str(visualization.get("dataset_id") or "")
        source_item = canonical["datasets"].get(dataset_id)
        if source_item is not None:
            report_datasets.setdefault(dataset_id, dict(source_item))
    for dataset in report_datasets.values():
        for metric_id in dataset.get("input_metric_ids") or ():
            source_item = canonical["metrics"].get(str(metric_id))
            if source_item is not None:
                report_metrics.setdefault(str(metric_id), dict(source_item))

    report = ReportDocument.from_dict({
        "report_id": report_id,
        "run_id": trace.run_id,
        "trace_id": trace.trace_id,
        "investigation_type": trace.investigation_type,
        "template_id": template.template_id,
        "template_version": template.version,
        "template_content_hash": template.content_hash,
        "title": plan["title"],
        "subtitle": plan["subtitle"],
        "summary": plan["summary"],
        "toc": plan["toc"],
        "language": "zh-CN",
        "chapters": chapters,
        "media": list(report_media.values()),
        "source_index": material_pack["source_document_ids"],
        "sources": _report_sources(trace, source_catalog or []),
        "citation_index": _trace_citation_index(trace),
        "metrics": list(report_metrics.values()),
        "datasets": list(report_datasets.values()),
        "visualizations": list(report_visualizations.values()),
    })
    _materialize_template_presentations(report, trace=trace, template=template)
    _materialize_channel_composition_charts(report)
    _deduplicate_equivalent_charts(report)
    _validate_report_identity(report, trace=trace, template=template, report_id=report_id)
    _validate_report_references(report, trace)
    return report


def _normalize_section_for_assembly(
    section: Mapping[str, Any], *, available_dataset_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Repair persisted visualization blocks that omitted their dataset link.

    Older report attempts could persist a text-rich ``timeline`` or
    ``metric_grid`` without ``dataset_ids``.  Such a block is not renderable
    under the current contract.  Preserve its information as a paragraph;
    never attach an unrelated dataset merely to satisfy validation.
    """

    result = dict(section)
    chapter = dict(result.get("chapter") or {})
    normalized: list[dict[str, Any]] = []
    for raw in chapter.get("blocks") or ():
        if not isinstance(raw, Mapping):
            raise TraceContractError("报告章节 block 必须是对象")
        try:
            block = ReportBlock.from_dict(raw).to_dict()
            block = _resolve_section_dataset_references(block, available_dataset_ids or set())
            if block["type"] in {"paragraph", "callout"} and _is_internal_report_note(block["payload"].get("text")):
                continue
            normalized.append(block)
        except TraceContractError as exc:
            if "requires dataset_ids" not in str(exc):
                raise
            payload = dict(raw.get("payload") or {})
            normalized.append({
                "type": "paragraph",
                "payload": {"title": str(payload.get("title") or ""), "text": _block_text_fallback(payload)},
                "citations": [str(item) for item in raw.get("citations") or () if str(item).strip()],
                "dataset_ids": [],
            })
    chapter["blocks"] = normalized
    result["chapter"] = chapter
    return result


def _resolve_section_dataset_references(
    block: dict[str, Any], available_dataset_ids: set[str],
) -> dict[str, Any]:
    """Resolve local template aliases and suppress visuals without backing data."""

    dataset_ids = list(block.get("dataset_ids") or ())
    resolved: list[str] = []
    for dataset_id in dataset_ids:
        value = str(dataset_id)
        if value in available_dataset_ids:
            resolved.append(value)
        elif f"report_template_{value}" in available_dataset_ids:
            resolved.append(f"report_template_{value}")
        else:
            resolved.append("")
    if dataset_ids and all(resolved):
        block["dataset_ids"] = list(dict.fromkeys(resolved))
        return block
    if block.get("type") in {"chart", "table", "metric_grid", "timeline"} and dataset_ids:
        return {
            "type": "paragraph",
            "payload": {
                "title": str(block.get("payload", {}).get("title") or ""),
                "text": _block_text_fallback(block.get("payload", {})),
            },
            "citations": list(block.get("citations") or ()),
            "dataset_ids": [],
        }
    if dataset_ids:
        block["dataset_ids"] = list(dict.fromkeys(item for item in resolved if item))
    return block


def _block_text_fallback(payload: Mapping[str, Any]) -> str:
    text = str(payload.get("text") or "").strip()
    if text:
        return text
    return "该内容未绑定可复核数据集，报告仅保留已经确认的文字结论。"


def _is_internal_report_note(value: Any) -> bool:
    text = str(value or "")
    if not text.strip():
        return False
    return any(marker in text for marker in (
        "为何本章不绘制", "为什么本章不绘制", "当前数据不具备可比较的数值轴",
        "本章实际使用的数据集", "本报告使用的数据集", "本章使用的数据集",
    ))


def _trace_material_index(trace: NodeTrace) -> dict[str, dict[str, dict[str, Any]]]:
    """Index structured node material so the model can reference, but not rewrite, it."""

    result = {"metrics": {}, "datasets": {}, "media": {}}
    for node in trace.nodes:
        draft = dict(node.get("draft") or {})
        for key, id_key in (("metrics", "metric_id"), ("datasets", "dataset_id"), ("media", "media_id")):
            for item in draft.get(key) or ():
                if not isinstance(item, Mapping):
                    continue
                item_id = str(item.get(id_key) or "").strip()
                if item_id:
                    existing = result[key].get(item_id)
                    if existing is not None and existing != dict(item):
                        raise TraceContractError(f"冻结 Trace 中存在重复且不一致的 {id_key}：{item_id}")
                    result[key][item_id] = dict(item)
    return result


def _scope_node_material_ids(trace: NodeTrace) -> NodeTrace:
    """Make node-local metric, dataset, and media IDs unique in report scope."""

    specifications = (("metrics", "metric_id"), ("datasets", "dataset_id"), ("media", "media_id"))
    counts: dict[str, dict[str, int]] = {key: {} for key, _ in specifications}
    for node in trace.nodes:
        draft = dict(node.get("draft") or {})
        for key, id_key in specifications:
            for item in draft.get(key) or ():
                if isinstance(item, Mapping):
                    item_id = str(item.get(id_key) or "").strip()
                    if item_id:
                        counts[key][item_id] = counts[key].get(item_id, 0) + 1

    used: dict[str, set[str]] = {key: set() for key, _ in specifications}
    scoped_nodes: list[dict[str, Any]] = []
    for node in trace.nodes:
        node_value = dict(node)
        draft = dict(node_value.get("draft") or {})
        maps: dict[str, dict[str, str]] = {key: {} for key, _ in specifications}
        for key, id_key in specifications:
            scoped_items: list[dict[str, Any]] = []
            for raw_item in draft.get(key) or ():
                if not isinstance(raw_item, Mapping):
                    continue
                item = dict(raw_item)
                item_id = str(item.get(id_key) or "").strip()
                if item_id:
                    scoped_id = item_id
                    if counts[key].get(item_id, 0) > 1:
                        scoped_id = f"{item_id}__{node_value.get('node_id') or node_value.get('slot_id')}"
                    base_id = scoped_id
                    suffix = 2
                    while scoped_id in used[key]:
                        scoped_id = f"{base_id}__{suffix}"
                        suffix += 1
                    used[key].add(scoped_id)
                    maps[key][item_id] = scoped_id
                    item[id_key] = scoped_id
                scoped_items.append(item)
            draft[key] = scoped_items
        for dataset in draft.get("datasets") or ():
            dataset["input_metric_ids"] = [
                maps["metrics"].get(str(item), str(item))
                for item in dataset.get("input_metric_ids") or ()
            ]
            dataset["source_dataset_ids"] = [
                maps["datasets"].get(str(item), str(item))
                for item in dataset.get("source_dataset_ids") or ()
            ]
        node_value["draft"] = draft
        scoped_nodes.append(node_value)
    return replace(trace, nodes=tuple(scoped_nodes))


def _trace_citation_index(trace: NodeTrace) -> dict[str, list[str]]:
    """Resolve internal Trace identifiers to source documents for presentation."""

    result: dict[str, set[str]] = {}
    for node in trace.nodes:
        node_id = str(node.get("node_id") or "").strip()
        draft = dict(node.get("draft") or {})
        evidence_by_id = {
            str(item.get("evidence_id") or ""): str(item.get("document_id") or "")
            for item in draft.get("evidence") or ()
            if isinstance(item, Mapping)
        }
        claims = [item for item in draft.get("claims") or () if isinstance(item, Mapping)]
        node_sources: set[str] = set()
        for claim in claims:
            claim_id = str(claim.get("claim_id") or "").strip()
            documents = {
                evidence_by_id.get(str(evidence_id), "")
                for evidence_id in claim.get("evidence_ids") or ()
            }
            documents.update(str(value) for value in claim.get("source_document_ids") or ())
            documents.discard("")
            node_sources.update(documents)
            if claim_id and documents:
                result.setdefault(claim_id, set()).update(documents)
        for evidence_id, document_id in evidence_by_id.items():
            if evidence_id and document_id:
                result.setdefault(evidence_id, set()).add(document_id)
                node_sources.add(document_id)
        if node_id and node_sources:
            result.setdefault(node_id, set()).update(node_sources)
        for document_id in node_sources:
            result.setdefault(document_id, set()).add(document_id)
    return {key: sorted(values) for key, values in result.items()}


def _trace_citation_ids(trace: NodeTrace) -> set[str]:
    known = {trace.trace_id, trace.run_id}
    for node in trace.nodes:
        for key in ("node_id", "slot_id"):
            value = str(node.get(key) or "").strip()
            if value:
                known.add(value)
        known.update(str(item).strip() for item in node.get("source_document_ids") or () if str(item).strip())
        draft = dict(node.get("draft") or {})
        for collection, id_key in (
            ("claims", "claim_id"),
            ("evidence", "evidence_id"),
            ("media", "media_id"),
            ("metrics", "metric_id"),
            ("datasets", "dataset_id"),
            ("analysis_features", "feature_id"),
        ):
            for item in draft.get(collection) or ():
                if not isinstance(item, Mapping):
                    continue
                value = str(item.get(id_key) or "").strip()
                if value:
                    known.add(value)
                if collection == "claims":
                    known.update(
                        str(document_id).strip()
                        for document_id in item.get("source_document_ids") or ()
                        if str(document_id).strip()
                    )
                elif collection in {"evidence", "media"}:
                    document_id = str(item.get("document_id") or "").strip()
                    if document_id:
                        known.add(document_id)
    return known


def _normalize_section_citations(
    section: Mapping[str, Any], known_citations: set[str],
) -> dict[str, Any]:
    result = dict(section)
    chapter = dict(result.get("chapter") or {})
    blocks = []
    warnings = list(result.get("_citation_warnings") or ())
    for raw in chapter.get("blocks") or ():
        if not isinstance(raw, Mapping):
            blocks.append(raw)
            continue
        block = dict(raw)
        citations = [str(item).strip() for item in block.get("citations") or () if str(item).strip()]
        unknown = sorted(set(citations) - known_citations)
        if unknown:
            block["citations"] = [item for item in citations if item in known_citations]
            warnings.append({"chapter_id": chapter.get("chapter_id"), "unknown_ids": unknown})
        blocks.append(block)
    chapter["blocks"] = blocks
    result["chapter"] = chapter
    if warnings:
        result["_citation_warnings"] = warnings
    else:
        result.pop("_citation_warnings", None)
    return result


def section_citation_warnings(section: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return citation normalization diagnostics without persisting them as report content."""
    return [dict(item) for item in section.get("_citation_warnings") or () if isinstance(item, Mapping)]


def _report_sources(
    trace: NodeTrace,
    source_catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine stored source metadata with evidence locators from the frozen Trace."""

    sources: dict[str, dict[str, Any]] = {}
    for item in source_catalog:
        if not isinstance(item, Mapping):
            continue
        document_id = str(item.get("document_id") or "").strip()
        if not document_id:
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        publisher = str(item.get("publisher") or "").strip()
        sources[document_id] = {
            "document_id": document_id,
            "title": title or _source_title(url, document_id),
            "url": url,
            "publisher": publisher,
            "published_at": str(item.get("published_at") or "").strip(),
        }

    for node in trace.nodes:
        draft = dict(node.get("draft") or {})
        for item in draft.get("evidence") or ():
            if not isinstance(item, Mapping):
                continue
            document_id = str(item.get("document_id") or "").strip()
            if not document_id:
                continue
            locator = dict(item.get("locator") or {})
            url = str(locator.get("url") or locator.get("source_url") or "").strip()
            existing = sources.get(document_id, {})
            sources[document_id] = {
                "document_id": document_id,
                "title": existing.get("title") or str(locator.get("title") or locator.get("source_label") or locator.get("section") or _source_title(url, document_id)),
                "url": existing.get("url") or url,
                "publisher": existing.get("publisher") or str(locator.get("publisher") or "").strip(),
                "published_at": existing.get("published_at") or str(locator.get("published_at") or "").strip(),
            }
    return [sources[key] for key in sorted(sources)]


def _materialize_template_presentations(
    report: ReportDocument,
    *,
    trace: NodeTrace,
    template: InvestigationReportTemplate,
) -> None:
    """Turn template visuals and source-backed observations into renderable blocks."""

    datasets = {item.dataset_id: item for item in report.datasets}
    visualizations = {item.visualization_id: item for item in report.visualizations}
    chapters = {item.chapter_id: item for item in report.chapters}
    template_datasets = {item.dataset_id: item for item in template.datasets}

    if report.metrics:
        summary = next((item for item in report.chapters if "metric_grid" in _chapter_blocks(template, item.chapter_id)), None)
        metric_dataset_id = "report_metric_observations"
        if summary and metric_dataset_id not in datasets:
            metric_rows = [
                {
                    "指标": item.name,
                    "数值": item.value,
                    "单位": item.unit,
                    "时间范围": item.time_scope,
                }
                for item in report.metrics
            ]
            metric_dataset = AnalysisDataset(
                dataset_id=metric_dataset_id,
                purpose="报告中展示的来源指标",
                columns=["指标", "数值", "单位", "时间范围"],
                rows=metric_rows,
                grain="单项来源指标",
                sample_scope="调查节点中有证据定位的指标",
                time_scope="各指标分别注明",
                method="逐项引用节点中的来源指标，不合并不同统计口径",
                input_metric_ids=[item.metric_id for item in report.metrics],
                citation_ids=list(dict.fromkeys(item.evidence_id for item in report.metrics)),
                source_dataset_ids=[],
            )
            report.datasets.append(metric_dataset)
            datasets[metric_dataset_id] = metric_dataset
            if not any(block.type == "metric_grid" for block in summary.blocks):
                summary.blocks.insert(0, ReportBlock(
                    type="metric_grid",
                    payload={"title": "关键指标", "items": [
                        {
                            "label": item.name,
                            "value": item.value,
                            "unit": item.unit,
                            "time_scope": item.time_scope,
                        }
                        for item in report.metrics[:12]
                    ]},
                    citations=list(dict.fromkeys(item.evidence_id for item in report.metrics)),
                    dataset_ids=[metric_dataset_id],
                ))

        _materialize_comparable_metric_series(report, trace=trace, template=template)

    for visual in template.visuals:
        template_dataset = template_datasets.get(visual.dataset_id)
        if template_dataset is None:
            continue
        selected: tuple[dict[str, Any], dict[str, Any]] | None = None
        projected: dict[str, Any] | None = None
        if template_dataset.collection_mode == "analysis_feature":
            candidate = datasets.get(f"report_analysis_{template_dataset.dataset_id}")
            if candidate and _visual_rows_usable(
                visual.type, candidate.rows, visual.x_field, visual.y_field,
            ):
                selected = ({"slot_id": "application_analysis"}, candidate.to_dict())
        elif f"report_template_{template_dataset.dataset_id}" in datasets:
            candidate = datasets[f"report_template_{template_dataset.dataset_id}"]
            if visual.type == "timeline" and candidate.rows:
                selected = ({"slot_id": "template_projection"}, candidate.to_dict())
            elif _visual_rows_usable(visual.type, candidate.rows, visual.x_field, visual.y_field):
                selected = ({"slot_id": "template_projection"}, candidate.to_dict())
        else:
            candidates = [
                (node, dataset)
                for node in trace.nodes
                if str(node.get("slot_id") or "") in set(template_dataset.source_nodes)
                for dataset in (dict(node.get("draft") or {}).get("datasets") or ())
                if isinstance(dataset, Mapping)
                and str(dataset.get("purpose") or "") == template_dataset.purpose
                and set(template_dataset.columns).issubset(set(dataset.get("columns") or ()))
            ]
            for node, candidate in candidates if selected is None else ():
                if visual.type == "timeline":
                    projected = _project_timeline_dataset(candidate, visual.visualization_id)
                    if projected and len(projected.get("rows") or ()) >= template_dataset.minimum_samples:
                        selected = (node, candidate)
                        break
                elif visual.x_field in (candidate.get("columns") or ()) and visual.y_field in (candidate.get("columns") or ()):
                    rows = candidate.get("rows") or []
                    if len(rows) >= template_dataset.minimum_samples and _visual_rows_usable(visual.type, rows, visual.x_field, visual.y_field):
                        selected = (node, candidate)
                        break
        if not selected:
            continue
        node, candidate = selected
        dataset_payload = projected or dict(candidate)
        dataset_id = str(dataset_payload.get("dataset_id") or "")
        if not dataset_id:
            continue
        if dataset_id not in datasets:
            dataset = AnalysisDataset.from_dict(dataset_payload)
            report.datasets.append(dataset)
            datasets[dataset_id] = dataset
        visualization_id = f"report_{visual.visualization_id}"
        if visualization_id not in visualizations:
            visualization = VisualizationSpec(
                visualization_id=visualization_id,
                type="chart" if visual.type in {"line", "bar", "pie", "doughnut", "scatter", "area", "radar"} else visual.type,
                title=visual.title,
                dataset_id=dataset_id,
                fallback=visual.fallback,
                chart_kind=visual.type if visual.type in {"line", "bar", "pie", "doughnut", "scatter", "area", "radar"} else "",
                x_field=visual.x_field,
                y_field=visual.y_field,
                citations=list(dataset_payload.get("citation_ids") or ()),
            )
            report.visualizations.append(visualization)
            visualizations[visualization_id] = visualization
        block_type = "chart" if visual.type in {"line", "bar", "pie", "doughnut", "scatter", "area", "radar"} else visual.type
        candidates = [
            chapter for chapter in template.chapters
            if visual.dataset_id in {item.dataset_id for item in template.visuals}
            and set(chapter.node_slots) & set(template_dataset.source_nodes)
        ]
        target = next(
            (chapter for chapter in candidates if block_type in chapter.recommended_blocks),
            candidates[0] if candidates else None,
        )
        target_chapter = chapters[target.chapter_id] if target else None
        if target_chapter and not any(
            block.type == block_type and block.dataset_ids == [dataset_id]
            for block in target_chapter.blocks
        ):
            citations = list(dataset_payload.get("citation_ids") or ())
            target_chapter.blocks.insert(0, ReportBlock(
                type=block_type,
                payload={
                    "title": visual.title,
                    **({"visualization_id": visualization_id} if visual.type != "timeline" else {}),
                },
                citations=citations,
                dataset_ids=[dataset_id],
            ))
def _deduplicate_equivalent_charts(report: ReportDocument) -> None:
    datasets = {item.dataset_id: item for item in report.datasets}
    visuals = {item.visualization_id: item for item in report.visualizations}
    for chapter in report.chapters:
        # A raw comment-sample dataset has one row per comment (count=1). It
        # is useful as a detail table, but a chart over those rows renders one
        # bar per comment and duplicates the template's aggregated sentiment
        # chart. Drop only the raw chart when an equivalent label distribution
        # is already present in this chapter.
        aggregate_datasets = [
            dataset
            for block in chapter.blocks
            if block.type == "chart"
            for visual in [visuals.get(str(block.payload.get("visualization_id") or ""))]
            for dataset in [datasets.get(visual.dataset_id if visual else (block.dataset_ids[0] if block.dataset_ids else ""))]
            if dataset is not None and not _is_raw_sample_distribution(dataset)
        ]
        seen: set[tuple[Any, ...]] = set()
        retained: list[ReportBlock] = []
        for block in chapter.blocks:
            if block.type != "chart":
                retained.append(block)
                continue
            visual = visuals.get(str(block.payload.get("visualization_id") or ""))
            dataset_id = visual.dataset_id if visual else (block.dataset_ids[0] if block.dataset_ids else "")
            dataset = datasets.get(dataset_id)
            if dataset is None:
                retained.append(block)
                continue
            if _is_raw_sample_distribution(dataset) and any(
                _is_aggregated_sample_distribution(candidate, dataset)
                for candidate in aggregate_datasets
            ):
                continue
            x_field = visual.x_field if visual else ""
            y_field = visual.y_field if visual else ""
            if not x_field or not y_field:
                x_field, y_field = _infer_chart_fields_for_report(dataset.rows)
            if not x_field or not y_field:
                retained.append(block)
                continue
            points = tuple(
                (
                    _normalize_chart_signature_value(row.get(x_field)),
                    _normalize_chart_signature_value(row.get(y_field)),
                    tuple(sorted(
                        (str(key), _normalize_chart_signature_value(value))
                        for key, value in row.items()
                        if str(key).strip().lower() not in {
                            "citation", "citations", "citation_id", "citation_ids",
                            "source_id", "source_ids", "evidence_id", "evidence_ids",
                        }
                        and key not in {x_field, y_field}
                    )),
                )
                for row in dataset.rows
                if isinstance(row, Mapping)
            )
            if len(points) < 2:
                retained.append(block)
                continue
            signature = (
                _chart_axis_signature(x_field),
                _chart_axis_signature(y_field),
                points,
            )
            if signature not in seen:
                seen.add(signature)
                retained.append(block)
        chapter.blocks = retained


def _is_raw_sample_distribution(dataset: AnalysisDataset) -> bool:
    """Return true for one-row-per-sample label data, never a chart series."""

    columns = set(dataset.columns)
    rows = [row for row in dataset.rows if isinstance(row, Mapping)]
    if not {"sample_id", "label", "count"}.issubset(columns) or len(rows) < 2:
        return False
    return all(_metric_number(row.get("count")) == 1 for row in rows)


def _is_aggregated_sample_distribution(
    dataset: AnalysisDataset,
    raw_dataset: AnalysisDataset,
) -> bool:
    """Check whether a chart dataset is the label aggregation of raw samples."""

    if "sample_id" in dataset.columns or not {"label", "count"}.issubset(dataset.columns):
        return False

    expected: dict[str, int] = {}
    for row in raw_dataset.rows:
        label = str(row.get("label") or "").strip()
        if label:
            expected[label] = expected.get(label, 0) + 1
    actual: dict[str, int] = {}
    for row in dataset.rows:
        label = str(row.get("label") or "").strip()
        count = _metric_number(row.get("count"))
        if not label or count is None or not count.is_integer():
            return False
        actual[label] = actual.get(label, 0) + int(count)
    return bool(expected) and actual == expected


def _normalize_chart_signature_value(value: Any) -> str:
    text = str(value if value is not None else "").strip().replace(",", "")
    number = _metric_number(text)
    if number is not None:
        return f"number:{number:g}"
    return " ".join(text.split())


def _chart_axis_signature(field: str) -> str:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(field).strip().lower())
    if normalized in {"time", "date", "datetime", "eventtime", "时间", "日期", "事件时间"}:
        return "time"
    if normalized in {"count", "quantity", "amount", "informationcount", "infocount", "数量", "信息量", "相关信息量", "相关网络信息量"}:
        return "count"
    if normalized in {"share", "percentage", "percent", "ratio", "占比", "比例"}:
        return "share"
    return normalized


def _infer_chart_fields_for_report(rows: list[dict[str, Any]]) -> tuple[str, str]:
    if not rows:
        return "", ""
    columns = list(rows[0])
    numeric = [key for key in columns if key not in {"citation_ids"} and any(_numeric(row.get(key)) for row in rows)]
    categorical = [key for key in columns if key not in numeric and key not in {"citation_ids"}]
    return (categorical[0] if categorical else "", numeric[0] if numeric else "")


def _materialize_channel_composition_charts(report: ReportDocument) -> None:
    """Turn a complete, explicitly tabulated channel share into a cited pie chart."""

    datasets = {item.dataset_id: item for item in report.datasets}
    visuals = {item.visualization_id: item for item in report.visualizations}
    for chapter in report.chapters:
        additions: list[tuple[int, ReportBlock]] = []
        for index, block in enumerate(chapter.blocks):
            if block.type not in {"table", "claim_table"}:
                continue
            raw_columns = block.payload.get("columns") or ()
            column_keys = [
                str(item.get("key") or item.get("label") or "") if isinstance(item, Mapping) else str(item)
                for item in raw_columns
            ]
            columns = [str(item.get("label") or item.get("key") or "") if isinstance(item, Mapping) else str(item) for item in raw_columns]
            title = str(block.payload.get("title") or "")
            searchable_columns = [f"{key} {label}" for key, label in zip(column_keys, columns)]
            if "渠道" not in title and not any("渠道" in name or "平台" in name for name in searchable_columns):
                continue
            raw_rows = block.payload.get("rows") or ()
            if len(columns) != 3 or not raw_rows:
                continue
            channel_index = next((i for i, name in enumerate(searchable_columns) if "渠道" in name or "平台" in name), -1)
            share_index = next((i for i, name in enumerate(searchable_columns) if "占" in name or "比例" in name or name.strip().lower() in {"share", "percentage", "percent", "ratio"}), -1)
            if share_index < 0 and "占" in title:
                share_index = next((i for i, name in enumerate(searchable_columns) if "量" not in name and i != channel_index), -1)
            count_index = next((i for i, name in enumerate(searchable_columns) if i not in {channel_index, share_index} and any(token in name for token in ("数量", "信息量", "count", "amount"))), -1)
            if count_index < 0:
                count_index = next((i for i in range(3) if i not in {channel_index, share_index}), -1)
            if channel_index < 0:
                channel_index = next((i for i in range(3) if i not in {share_index, count_index}), -1)
            source_dataset_ids = list(dict.fromkeys(block.dataset_ids))
            if share_index < 0 or count_index < 0 or channel_index < 0 or len({channel_index, share_index, count_index}) != 3:
                continue
            rows: list[dict[str, Any]] = []
            for raw in raw_rows:
                if isinstance(raw, Mapping):
                    raw = [
                        raw.get(key, raw.get(label, ""))
                        for key, label in zip(column_keys, columns)
                    ]
                if not isinstance(raw, (list, tuple)) or len(raw) != 3:
                    rows = []
                    break
                share_text = str(raw[share_index]).replace("％", "%").strip()
                if not share_text.endswith("%"):
                    rows = []
                    break
                share = _numeric(share_text[:-1])
                count = _metric_number(raw[count_index])
                if share is None or share < 0 or count is None or count < 0 or not count.is_integer() or not str(raw[channel_index]).strip():
                    rows = []
                    break
                rows.append({
                    "channel": str(raw[channel_index]).strip(),
                    "count": int(count),
                    "share": share_text,
                })
            if len(rows) < 2 or not block.citations:
                continue
            total_share = sum(_metric_number(row["share"]) or 0 for row in rows)
            source_dataset = next((
                datasets[item_id]
                for item_id in source_dataset_ids
                if item_id in datasets and _dataset_matches_channel_composition(
                    datasets[item_id], rows,
                )
            ), None)
            input_metric_ids: list[str] = []
            metric_evidence_ids: list[str] = []
            limitations: list[str] = []
            if not 99.5 <= total_share <= 100.5:
                total_metric = _matching_channel_total_metric(
                    report, block, rows,
                )
                if total_metric is None:
                    continue
                total_count = _metric_number(total_metric.value, total_metric.unit)
                displayed_count = sum(int(row["count"]) for row in rows)
                if total_count is None or total_count < displayed_count:
                    continue
                residual_count = int(round(total_count - displayed_count))
                residual_share = residual_count / total_count * 100 if total_count else 0
                if residual_count:
                    rows.append({
                        "channel": "其它",
                        "count": residual_count,
                        "share": f"{residual_share:.1f}%",
                    })
                    metric_evidence_ids = [total_metric.evidence_id]
                    limitations.append("其它部分按同口径监测总量减去表中已列渠道数量计算。")
                input_metric_ids = [total_metric.metric_id]
            if not 99.5 <= sum(_metric_number(row["share"]) or 0 for row in rows) <= 100.5:
                continue
            source_ref = f"{chapter.chapter_id}::table::{str(block.payload.get('title') or '').strip()}"
            dataset_id = f"report_channel_composition_{chapter.chapter_id}"
            citation_ids = list(dict.fromkeys([*block.citations, *metric_evidence_ids]))
            for row in rows:
                row["citation_ids"] = list(dict.fromkeys([
                    *block.citations,
                    *(metric_evidence_ids if row["channel"] == "其它" else []),
                ]))
            chart_title = "渠道占比"
            chart_purpose = f"{chart_title}；{limitations[-1]}" if limitations else chart_title
            dataset = AnalysisDataset(
                dataset_id=dataset_id,
                purpose=chart_purpose,
                columns=["channel", "count", "share", "citation_ids"],
                rows=rows,
                grain=source_dataset.grain if source_dataset else "监测样本内的渠道构成",
                sample_scope=source_dataset.sample_scope if source_dataset else str(block.payload.get("caption") or title),
                time_scope=source_dataset.time_scope if source_dataset else (total_metric.time_scope if input_metric_ids else "表格所列范围"),
                method="按表中已有渠道数量和占比构建饼图；其它部分仅在同口径冻结总量可核验时计算",
                input_metric_ids=input_metric_ids,
                citation_ids=citation_ids,
                source_dataset_ids=[source_dataset.dataset_id] if source_dataset else [],
                source_block_id=source_ref,
                limitations=["图表仅表示表中监测样本的渠道构成，不代表全网流量结构", *limitations],
            )
            if dataset_id not in datasets:
                report.datasets.append(dataset)
                datasets[dataset_id] = dataset
            visual_id = f"report_channel_composition_{chapter.chapter_id}"
            if visual_id not in visuals:
                visual = VisualizationSpec(
                    visualization_id=visual_id,
                    type="chart",
                    title=chart_title,
                    dataset_id=dataset_id,
                    fallback="table",
                    chart_kind="pie",
                    x_field="channel",
                    y_field="share",
                    citations=citation_ids,
                )
                report.visualizations.append(visual)
                visuals[visual_id] = visual
            additions.append((index + 1, ReportBlock(
                type="chart",
                payload={"title": chart_title, "visualization_id": visual_id},
                citations=citation_ids,
                dataset_ids=[dataset_id],
            )))
        for index, chart in reversed(additions):
            chapter.blocks.insert(index, chart)


def _matching_channel_total_metric(
    report: ReportDocument,
    block: ReportBlock,
    rows: list[dict[str, Any]],
) -> Any | None:
    caption = " ".join(str(block.payload.get(key) or "") for key in ("title", "caption"))
    caption_dates = _date_scope_signature(caption)
    if len(caption_dates) < 2:
        return None
    for metric in report.metrics:
        name = metric.name.lower()
        if "监测" not in name or not any(term in name for term in ("累计", "总量", "累积")):
            continue
        if "峰值" in name or "单日" in name or metric.unit.strip() != "条":
            continue
        if _date_scope_signature(metric.time_scope) != caption_dates:
            continue
        total = _metric_number(metric.value, metric.unit)
        if total is None or total <= 0:
            continue
        matched = True
        for row in rows:
            count = float(row["count"])
            share_text = str(row["share"]).rstrip("%").strip()
            share = _metric_number(share_text)
            decimals = len(share_text.partition(".")[2]) if "." in share_text else 0
            tolerance = 0.5 * 10 ** (-decimals) + 1e-6
            if share is None or abs(count / total * 100 - share) > tolerance:
                matched = False
                break
        if matched and sum(int(row["count"]) for row in rows) <= total:
            return metric
    return None


def _date_scope_signature(value: str) -> tuple[tuple[int | None, int, int], ...]:
    dates = []
    pattern = re.compile(
        r"(?:(?P<cn_year>20\d{2})年)?(?P<cn_month>\d{1,2})月(?P<cn_day>\d{1,2})日?"
        r"|(?P<iso_year>20\d{2})[-/.](?P<iso_month>\d{1,2})[-/.](?P<iso_day>\d{1,2})"
    )
    previous_year: int | None = None
    previous_month: int | None = None
    for match in pattern.finditer(str(value)):
        month = match.group("cn_month") or match.group("iso_month")
        day = match.group("cn_day") or match.group("iso_day")
        year_text = match.group("cn_year") or match.group("iso_year")
        year = int(year_text) if year_text else previous_year
        if year is not None and previous_month is not None and int(month) < previous_month:
            year += 1
        dates.append((year, int(month), int(day)))
        previous_year, previous_month = year, int(month)
    return tuple(dates)


def _dataset_matches_channel_composition(dataset: AnalysisDataset, table_rows: list[dict[str, Any]]) -> bool:
    """Require the displayed composition to match a cited source dataset."""

    aliases = {
        "channel": {"channel", "platform", "platform_name", "渠道", "平台"},
        "count": {"count", "information_count", "info_count", "value_count", "数量", "信息量（条）"},
        "share": {"share", "percentage", "percent", "ratio", "占比", "比例"},
    }
    fields: dict[str, str] = {}
    for meaning, candidates in aliases.items():
        match = next((column for column in dataset.columns if str(column).strip().lower() in candidates), None)
        if match is None:
            return False
        fields[meaning] = match
    if not dataset.rows or len(dataset.rows) != len(table_rows):
        return False

    def normalize_count(value: Any) -> str:
        text = str(value if value is not None else "").strip().replace(",", "")
        number = _metric_number(text)
        return f"{number:g}" if number is not None else " ".join(text.split())

    def normalize_share(value: Any) -> str:
        text = str(value if value is not None else "").strip().replace("％", "%")
        number = _numeric(text[:-1]) if text.endswith("%") else _numeric(text)
        if number is None:
            return ""
        if not text.endswith("%") and number <= 1:
            number *= 100
        return f"{number:g}"

    source_rows = {
        (
            str(row.get(fields["channel"]) or "").strip(),
            normalize_count(row.get(fields["count"])),
            normalize_share(row.get(fields["share"])),
        )
        for row in dataset.rows
    }
    displayed_rows = {
        (str(row["channel"]).strip(), normalize_count(row["count"]), normalize_share(row["share"]))
        for row in table_rows
    }
    return len(source_rows) == len(dataset.rows) and source_rows == displayed_rows


def _report_dataset_source_block_exists(report: ReportDocument, dataset: AnalysisDataset) -> bool:
    if "::table::" not in dataset.source_block_id:
        return False
    chapter_id, title = dataset.source_block_id.split("::table::", 1)
    if not chapter_id or not title:
        return False
    chapter = next((item for item in report.chapters if item.chapter_id == chapter_id), None)
    if chapter is None:
        return False
    return any(
        block.type in {"table", "claim_table"}
        and str(block.payload.get("title") or "").strip() == title
        and set(block.citations).issubset(set(dataset.citation_ids))
        for block in chapter.blocks
    )


def _chapter_blocks(template: InvestigationReportTemplate, chapter_id: str) -> set[str]:
    chapter = next((item for item in template.chapters if item.chapter_id == chapter_id), None)
    return set(chapter.recommended_blocks) if chapter else set()


def _materialize_comparable_metric_series(
    report: ReportDocument,
    *,
    trace: NodeTrace,
    template: InvestigationReportTemplate,
) -> None:
    """Project only genuinely comparable source metrics into a chart dataset.

    Metrics are often heterogeneous (views, people, months, percentages).  A
    chart is created only when at least two numeric observations share a unit
    and a sufficiently similar metric family.  Approximate ranges and mixed
    units remain cards/tables instead of being silently combined.
    """

    if any(item.visualization_id == "report_metric-series" for item in report.visualizations):
        return
    candidates = [item for item in report.metrics if _metric_number(item.value, item.unit) is not None]
    groups: list[list[Any]] = []
    for metric in candidates:
        placed = False
        for group in groups:
            if metric.unit == group[0].unit and _metric_family_compatible(metric.name, group[0].name):
                group.append(metric)
                placed = True
                break
        if not placed:
            groups.append([metric])
    groups = [group for group in groups if len({_metric_number(item.value, item.unit) for item in group}) >= 2]
    if not groups:
        return
    group = max(groups, key=lambda value: (len(value), -report.metrics.index(value[0])))
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for metric in group:
        number = _metric_number(metric.value, metric.unit)
        if number is None:
            continue
        key = (metric.time_scope, number)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "时间": metric.time_scope,
            "指标": metric.name,
            "数值": number,
            "单位": metric.unit,
            "citation_ids": [metric.evidence_id, metric.source_document_id],
        })
    if len(rows) < 2:
        return
    dataset_id = "report_metric_series"
    dataset = AnalysisDataset(
        dataset_id=dataset_id,
        purpose="同一指标口径在不同时间点的可比观测",
        columns=["时间", "指标", "数值", "单位", "citation_ids"],
        rows=rows,
        grain="同一指标口径的一次观测",
        sample_scope="冻结节点中明确给出时间范围、数值和单位的指标",
        time_scope="各观测分别注明",
        method="仅保留同单位、同指标族且可解析为单一数值的原始指标；不合并区间值",
        input_metric_ids=[item.metric_id for item in group],
        citation_ids=list(dict.fromkeys(identifier for row in rows for identifier in row["citation_ids"])),
        source_dataset_ids=[],
    )
    report.datasets.append(dataset)
    visualization = VisualizationSpec(
        visualization_id="report_metric-series",
        type="chart",
        title="可比指标变化",
        dataset_id=dataset_id,
        fallback="table",
        chart_kind="line",
        x_field="时间",
        y_field="数值",
        citations=list(dataset.citation_ids),
    )
    report.visualizations.append(visualization)
    metric_nodes = {
        str(node.get("slot_id") or "")
        for node in trace.nodes
        if any(str(item.get("metric_id") or "") in set(dataset.input_metric_ids) for item in (dict(node.get("draft") or {}).get("metrics") or ()) if isinstance(item, Mapping))
    }
    def _chapter_can_host_metric_chart(chapter: Any) -> bool:
        template_chapter = next((item for item in template.chapters if item.chapter_id == chapter.chapter_id), None)
        return bool(
            template_chapter
            and "chart" in template_chapter.recommended_blocks
            and metric_nodes.intersection(template_chapter.node_slots)
        )

    target = next((chapter for chapter in report.chapters if _chapter_can_host_metric_chart(chapter)), None)
    if target is None:
        target = next((chapter for chapter in report.chapters if "chart" in _chapter_blocks(template, chapter.chapter_id)), None)
    if target is None:
        return
    target.blocks.insert(0, ReportBlock(
        type="chart",
        payload={"title": "可比指标变化", "visualization_id": visualization.visualization_id},
        citations=list(dataset.citation_ids),
        dataset_ids=[dataset_id],
    ))


def _metric_number(value: Any, unit: str = "") -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    unit_text = str(unit or "").strip()
    if unit_text and text.endswith(unit_text):
        text = text[: -len(unit_text)].strip()
    text = text.replace("％", "%").rstrip("%").strip()
    match = re.fullmatch(r"(?:约|大约|超过|超|近)?\s*(\d+(?:\.\d+)?)\s*(万|亿|千|百)?\s*(?:余|多)?", text)
    if not match:
        return None
    number = float(match.group(1))
    return number * {"万": 10_000, "亿": 100_000_000, "千": 1_000, "百": 100}.get(match.group(2) or "", 1)


def _metric_family_compatible(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        value = re.sub(r"（[^）]*）|\([^)]*\)", "", str(value))
        value = re.sub(r"\d{2,4}年?", "", value)
        value = re.sub(r"(?:同一|后续时点|口径[一二三四五12345]|原|官网|截至|晚间报道时点|报道时点)", "", value)
        return re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", value)

    left_text, right_text = normalize(left), normalize(right)
    return bool(left_text and left_text == right_text)


def _numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    text = str(value).strip().replace(",", "").replace("％", "%")
    if text.endswith("%"):
        text = text[:-1].strip()
    if re.fullmatch(r"(?:约|大约|超过|超|近)?\s*\d+(?:\.\d+)?\s*(?:万|亿|千|百)?\s*(?:余|多)?", text):
        return True
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _visual_rows_usable(chart_kind: str, rows: Any, x_field: str, y_field: str) -> bool:
    """Apply the minimum shape required by each visual without inventing data."""

    valid = [row for row in rows if isinstance(row, Mapping)]
    if not valid or not x_field or not y_field:
        return False
    for comparison_field in ("unit", "denominator"):
        if any(comparison_field in row for row in valid):
            values = {str(row.get(comparison_field) or "").strip() for row in valid}
            if len(values) != 1 or "" in values:
                return False
    if chart_kind in {"scatter"}:
        return len(valid) >= 2 and all(_numeric(row.get(x_field)) and _numeric(row.get(y_field)) for row in valid)
    if chart_kind in {"pie", "doughnut", "radar"}:
        return len(valid) >= (3 if chart_kind == "radar" else 2) and all(_numeric(row.get(y_field)) for row in valid)
    return len(valid) >= 2 and all(_numeric(row.get(y_field)) for row in valid)


def _project_timeline_dataset(dataset: Mapping[str, Any], visual_id: str) -> dict[str, Any] | None:
    columns = set(str(item) for item in dataset.get("columns") or ())
    time_key = next((key for key in ("time", "date", "time_scope", "wave") if key in columns), None)
    event_key = next((key for key in ("event", "trigger", "observable_action", "action", "observable_node") if key in columns), None)
    subject_key = next((key for key in ("subject", "actor", "platform") if key in columns), None)
    if not time_key or not event_key:
        return None
    rows: list[dict[str, Any]] = []
    citation_ids = list(str(item) for item in dataset.get("citation_ids") or ())
    for row in dataset.get("rows") or ():
        if not isinstance(row, Mapping):
            continue
        time_value = row.get(time_key)
        event_value = row.get(event_key)
        if not time_value or not event_value:
            continue
        projected_row: dict[str, Any] = {"time": time_value, "event": event_value}
        if subject_key and row.get(subject_key):
            projected_row["subject"] = row[subject_key]
        for key in ("source_document_id", "claim_id"):
            if row.get(key):
                projected_row[key] = row[key]
                citation_ids.append(str(row[key]))
        rows.append(projected_row)
    if not rows:
        return None
    source_dataset_id = str(dataset.get("dataset_id") or "data")
    return {
        "dataset_id": f"report_{visual_id.replace('-', '_')}_{source_dataset_id}",
        "purpose": "按来源材料字段投影的事件时间线",
        "columns": list(rows[0]),
        "rows": rows,
        "grain": str(dataset.get("grain") or "每条来源观察"),
        "sample_scope": str(dataset.get("sample_scope") or "冻结节点中的现有数据集"),
        "time_scope": str(dataset.get("time_scope") or "以各记录中的时间字段为准"),
        "method": "仅映射来源数据集中已有的时间、事件和主体字段，不补造时间或事件",
        "input_metric_ids": list(dataset.get("input_metric_ids") or ()),
        "citation_ids": list(dict.fromkeys(citation_ids)),
        "source_dataset_ids": [source_dataset_id],
        "limitations": list(dataset.get("limitations") or ()),
    }


def _source_title(url: str, document_id: str) -> str:
    host = urlparse(url).hostname if url else ""
    return host.removeprefix("www.") if host else "来源材料"


def _template_payload(template: InvestigationReportTemplate) -> dict[str, Any]:
    return {
        "template_id": template.template_id,
        "version": template.version,
        "investigation_type": template.investigation_type,
        "trace_template_id": template.trace_template_id,
        "trace_template_version": template.trace_template_version,
        "language": template.language,
        "chapters": [asdict(chapter) for chapter in template.chapters],
        "datasets": [asdict(dataset) for dataset in template.datasets],
        "visuals": [asdict(visual) for visual in template.visuals],
        "instructions": template.instructions,
        "content_hash": template.content_hash,
    }


def _call_report_role(runner: ReportRoleRunner, objective: str, context: dict[str, Any]) -> Any:
    raw = runner("report_composer", objective, context)
    answer = raw.answer if isinstance(raw, AgentResult) else raw
    if isinstance(answer, str):
        try:
            # Report JSON is also nested in agent_control.answer; prose may
            # contain literal newlines from the model.  Structural and
            # citation validation still runs after this tolerant boundary.
            answer = json.loads(answer, strict=False)
        except json.JSONDecodeError as exc:
            raise TraceContractError("Report Composer 输出不是有效 JSON") from exc
    if not isinstance(answer, Mapping):
        raise TraceContractError("Report Composer 输出必须是 JSON 对象")
    return dict(answer)


def _parse_plan(value: Mapping[str, Any], *, trace: NodeTrace, template: InvestigationReportTemplate, report_id: str) -> dict[str, Any]:
    plan = dict(value.get("report_plan") or value)
    if str(plan.get("template_id") or template.template_id) != template.template_id:
        raise TraceContractError("报告规划 template_id 不匹配")
    title = str(plan.get("title") or "").strip()
    summary = str(plan.get("summary") or "").strip()
    if not title or not summary:
        raise TraceContractError("报告规划必须包含中文 title 和 summary")
    rows = plan.get("chapters")
    if not isinstance(rows, list) or len(rows) != len(template.chapters):
        raise TraceContractError("报告规划必须覆盖模板全部章节")
    expected = [chapter.chapter_id for chapter in template.chapters]
    actual = [str(item.get("chapter_id") or item.get("id") or "") for item in rows if isinstance(item, Mapping)]
    if actual != expected:
        raise TraceContractError("报告规划章节顺序必须与 Markdown 模板一致")
    chapters = {}
    for chapter, item in zip(template.chapters, rows):
        if not isinstance(item, Mapping):
            raise TraceContractError("报告规划章节必须是对象")
        chapters[chapter.chapter_id] = {
            "chapter_id": chapter.chapter_id,
            "title": chapter.title,
            "purpose": chapter.purpose,
            "focus": str(item.get("focus") or chapter.purpose).strip(),
            "node_slots": list(chapter.node_slots),
            "recommended_blocks": list(chapter.recommended_blocks),
            "data_needs": list(chapter.data_needs),
            "subheadings": [
                {"level": heading.level, "title": heading.title}
                for heading in chapter.subheadings
            ],
        }
    return {
        "title": title,
        "subtitle": str(plan.get("subtitle") or "").strip(),
        "summary": summary,
        "chapters": chapters,
        "toc": [{"chapter_id": chapter.chapter_id, "title": chapter.title} for chapter in template.chapters],
    }


def _parse_section(value: Mapping[str, Any], *, chapter_id: str, chapter: Any, order: int) -> dict[str, Any]:
    payload = dict(value.get("section") or value)
    if str(payload.get("chapter_id") or "") != chapter_id:
        raise TraceContractError(f"报告章节 {chapter_id} 的 chapter_id 不匹配")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        raise TraceContractError(f"报告章节 {chapter_id} 必须包含 blocks 数组")
    allowed = set(REPORT_BLOCK_TYPES)
    for item in blocks:
        if not isinstance(item, Mapping) or str(item.get("type") or "") not in allowed:
            raise TraceContractError(f"报告章节 {chapter_id} 使用了不支持的 block")
    expected_headings = [
        (heading.level, heading.title)
        for heading in chapter.subheadings
    ]
    actual_headings = []
    heading_waiting_for_content = False
    for item in blocks:
        block_type = str(item.get("type") or "")
        if block_type != "heading":
            if heading_waiting_for_content:
                heading_waiting_for_content = False
            continue
        if heading_waiting_for_content:
            raise TraceContractError(f"报告章节 {chapter_id} 的每个子标题后都必须有正文内容")
        raw_heading_payload = item.get("payload")
        if not isinstance(raw_heading_payload, Mapping):
            raise TraceContractError(f"报告章节 {chapter_id} 的 heading payload 必须是对象")
        heading_payload = dict(raw_heading_payload)
        title = str(heading_payload.get("text") or heading_payload.get("title") or "").strip()
        try:
            level = int(heading_payload.get("level") or 3)
        except (TypeError, ValueError):
            level = 3
        actual_headings.append((level, title))
        heading_waiting_for_content = True
    if actual_headings != expected_headings:
        raise TraceContractError(
            f"报告章节 {chapter_id} 的标题必须完整匹配 Markdown 模板的子章节结构"
        )
    if heading_waiting_for_content:
        raise TraceContractError(f"报告章节 {chapter_id} 的每个子标题后都必须有正文内容")
    return {
        "chapter": {
            "chapter_id": chapter_id,
            "title": chapter.title,
            "order": order,
            "purpose": chapter.purpose,
            "summary": str(payload.get("summary") or "").strip() or chapter.purpose,
            "blocks": blocks,
        },
        "metrics": [dict(item) for item in payload.get("metrics") or () if isinstance(item, Mapping)],
        "datasets": [dict(item) for item in payload.get("datasets") or () if isinstance(item, Mapping)],
        "visualizations": [dict(item) for item in payload.get("visualizations") or () if isinstance(item, Mapping)],
        "media": [dict(item) for item in payload.get("media") or () if isinstance(item, Mapping)],
    }


def _validate_report_identity(
    report: ReportDocument,
    *,
    trace: NodeTrace,
    template: InvestigationReportTemplate,
    report_id: str,
) -> None:
    expected = {
        "report_id": report_id,
        "run_id": trace.run_id,
        "trace_id": trace.trace_id,
        "investigation_type": trace.investigation_type,
        "template_id": template.template_id,
        "template_version": template.version,
        "template_content_hash": template.content_hash,
    }
    actual = {
        "report_id": report.report_id,
        "run_id": report.run_id,
        "trace_id": report.trace_id,
        "investigation_type": report.investigation_type,
        "template_id": report.template_id,
        "template_version": report.template_version,
        "template_content_hash": report.template_content_hash,
    }
    mismatches = [f"{key}={actual[key]!r} expected {value!r}" for key, value in expected.items() if actual[key] != value]
    if mismatches:
        raise TraceContractError("ReportDocument 身份字段不匹配：" + "; ".join(mismatches))
    expected_chapters = [chapter.chapter_id for chapter in template.chapters]
    actual_chapters = [chapter.chapter_id for chapter in report.chapters]
    if actual_chapters != expected_chapters:
        raise TraceContractError("ReportDocument 章节顺序必须与 Markdown 模板一致")


def _validate_report_references(report: ReportDocument, trace: NodeTrace) -> None:
    known: set[str] = {trace.trace_id, trace.run_id}
    claims: dict[str, Mapping[str, Any]] = {}
    evidence: dict[str, Mapping[str, Any]] = {}
    media: dict[str, Mapping[str, Any]] = {}
    metrics: dict[str, Mapping[str, Any]] = {}
    datasets: dict[str, Mapping[str, Any]] = {}
    analysis_features: dict[str, Mapping[str, Any]] = {}
    for node in trace.nodes:
        known.update(str(node.get(key) or "") for key in ("node_id", "slot_id") if str(node.get(key) or ""))
        draft = dict(node.get("draft") or {})
        for item in draft.get("analysis_features") or ():
            if isinstance(item, Mapping) and str(item.get("feature_id") or "").strip():
                analysis_features[str(item["feature_id"])] = item
        for item in draft.get("claims") or ():
            if isinstance(item, Mapping) and str(item.get("claim_id") or "").strip():
                claims[str(item["claim_id"])] = item
        for item in draft.get("evidence") or ():
            if isinstance(item, Mapping) and str(item.get("evidence_id") or "").strip():
                evidence[str(item["evidence_id"])] = item
        for item in draft.get("media") or ():
            if isinstance(item, Mapping) and str(item.get("media_id") or "").strip():
                media[str(item["media_id"])] = item
        for item in draft.get("metrics") or ():
            if isinstance(item, Mapping) and str(item.get("metric_id") or "").strip():
                metrics[str(item["metric_id"])] = item
        for item in draft.get("datasets") or ():
            if isinstance(item, Mapping) and str(item.get("dataset_id") or "").strip():
                datasets[str(item["dataset_id"])] = item
    for collection in (claims, evidence, media, metrics, datasets, analysis_features):
        known.update(collection)
    document_ids = {
        str(item.get("document_id") or "").strip()
        for item in [*evidence.values(), *media.values()]
        if str(item.get("document_id") or "").strip()
    }
    known.update(report.source_index)
    known.update(item.metric_id for item in report.metrics)
    known.update(item.dataset_id for item in report.datasets)
    report_media = {str(item.get("media_id")): item for item in report.media}
    if not set(report_media).issubset(media):
        unknown = sorted(set(report_media) - set(media))
        raise TraceContractError(f"报告引用了冻结 Trace 中不存在的媒体：{', '.join(unknown[:8])}")
    for metric in report.metrics:
        if metric.metric_id not in metrics:
            raise TraceContractError(f"报告指标不属于冻结 Trace：{metric.metric_id}")
        if metric.source_document_id not in document_ids or metric.evidence_id not in evidence:
            raise TraceContractError(f"报告指标缺少可追溯来源：{metric.metric_id}")
    report_datasets = {item.dataset_id: item for item in report.datasets}
    for dataset in report.datasets:
        frozen_source = datasets.get(dataset.dataset_id)
        dataset_payload = dataset.to_dict()
        dataset_payload.pop("source_dataset_ids", None)
        normalized_frozen = AnalysisDataset.from_dict(frozen_source).to_dict() if frozen_source is not None else None
        if normalized_frozen is not None:
            normalized_frozen.pop("source_dataset_ids", None)
        is_frozen_dataset = normalized_frozen is not None and dataset_payload == normalized_frozen
        if not is_frozen_dataset:
            source_dataset_ids = set(dataset.source_dataset_ids)
            source_feature_ids = set(dataset.source_feature_ids)
            metric_ids = set(dataset.input_metric_ids)
            source_block_valid = _report_dataset_source_block_exists(report, dataset)
            if not source_dataset_ids.issubset(datasets) or not metric_ids.issubset(metrics) or not source_feature_ids.issubset(analysis_features):
                raise TraceContractError(f"报告投影数据集缺少冻结 Trace 来源：{dataset.dataset_id}")
            if not source_dataset_ids and not metric_ids and not source_feature_ids and not source_block_valid:
                raise TraceContractError(f"报告投影数据集必须明确引用源数据集、指标或分析特征：{dataset.dataset_id}")
            if dataset.source_block_id and not source_block_valid:
                raise TraceContractError(f"报告投影数据集引用了不存在的来源表格：{dataset.dataset_id}")
            if not dataset.method.strip():
                raise TraceContractError(f"报告投影数据集必须说明整理方法：{dataset.dataset_id}")
        if not set(dataset.input_metric_ids).issubset(metrics):
            raise TraceContractError(f"报告数据集引用了不存在的冻结指标：{dataset.dataset_id}")
        if not set(dataset.source_dataset_ids).issubset(datasets):
            raise TraceContractError(f"报告数据集引用了不存在的冻结源数据集：{dataset.dataset_id}")
        if not set(dataset.source_feature_ids).issubset(analysis_features):
            raise TraceContractError(f"报告数据集引用了不存在的冻结分析特征：{dataset.dataset_id}")
        if not set(dataset.citation_ids).issubset(known):
            raise TraceContractError(f"报告数据集引用了不存在的事实 ID：{dataset.dataset_id}")
        if "citation_ids" in dataset.columns:
            for index, row in enumerate(dataset.rows):
                row_citations = row.get("citation_ids")
                if not isinstance(row_citations, list) or not row_citations:
                    raise TraceContractError(f"报告数据集每行必须保留来源引用：{dataset.dataset_id}[{index}]")
                if not set(row_citations).issubset(known):
                    raise TraceContractError(f"报告数据集行引用了不存在的事实 ID：{dataset.dataset_id}[{index}]")
    for visual in report.visualizations:
        dataset = report_datasets.get(visual.dataset_id)
        if dataset is None:
            raise TraceContractError(f"报告图表引用了不存在的报告数据集：{visual.visualization_id}")
        columns = set(dataset.columns)
        for field_name, field in (("x_field", visual.x_field), ("y_field", visual.y_field)):
            if field and field not in columns:
                raise TraceContractError(
                    f"报告图表 {visual.visualization_id} 的 {field_name} 不属于数据集字段：{field}"
                )
        if not set(visual.citations).issubset(known):
            raise TraceContractError(f"报告图表引用了不存在的事实 ID：{visual.visualization_id}")
    for chapter in report.chapters:
        for block in chapter.blocks:
            unknown = sorted(set(block.citations) - known)
            if unknown:
                raise TraceContractError(
                    f"报告章节 {chapter.chapter_id} 引用了不存在的事实 ID：{', '.join(unknown[:8])}"
                )
            if block.type in {"chart", "table", "metric_grid", "timeline"}:
                if not set(block.dataset_ids).issubset(report_datasets):
                    raise TraceContractError(f"报告章节 {chapter.chapter_id} 引用了不存在的报告数据集")
            if block.type in {"image", "video"}:
                media_id = str(block.payload.get("media_id") or "")
                if media_id not in media:
                    raise TraceContractError(f"报告章节 {chapter.chapter_id} 引用了不存在的冻结媒体：{media_id}")


__all__ = [
    "ReportRoleRunner", "build_report_material_pack", "compose_report",
    "plan_report", "generate_report_section", "build_source_limits_fallback_section", "assemble_report",
    "section_citation_warnings",
]
