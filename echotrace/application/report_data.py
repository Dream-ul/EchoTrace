"""Template-driven data collection and source-analysis projections for reports."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
import json
from typing import Any, Mapping

from echotrace.domain.reporting import AnalysisDataset

from .report_templates import InvestigationReportTemplate, ReportTemplateDataset


def research_data_needs(template: InvestigationReportTemplate, slot_id: str) -> list[dict[str, Any]]:
    """Return only the report datasets assigned to this node slot."""

    result: list[dict[str, Any]] = []
    for dataset in template.datasets:
        if slot_id not in dataset.source_nodes or dataset.collection_mode != "research":
            continue
        item = asdict(dataset)
        item["visuals"] = [
            asdict(visual) for visual in template.visuals
            if visual.dataset_id == dataset.dataset_id
        ]
        result.append(item)
    return result


def analysis_feature_datasets_for_node(
    *,
    slot_id: str,
    requirements: list[dict[str, Any]],
    analysis_features: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[AnalysisDataset]:
    """Persist one source-located analysis observation per document and label."""

    evidence_by_document: dict[str, list[str]] = defaultdict(list)
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        document_id = str(item.get("document_id") or "").strip()
        evidence_id = str(item.get("evidence_id") or "").strip()
        if document_id and evidence_id:
            evidence_by_document[document_id].append(evidence_id)

    result: list[AnalysisDataset] = []
    for requirement in requirements:
        if requirement.get("collection_mode") != "analysis_feature":
            continue
        feature_type = str(requirement.get("feature_type") or "")
        rows: list[dict[str, Any]] = []
        feature_ids: list[str] = []
        citations: list[str] = []
        seen: set[tuple[str, str]] = set()
        for feature in analysis_features:
            if not isinstance(feature, Mapping) or feature.get("feature_type") != feature_type:
                continue
            if feature.get("factuality") != "analysis_feature":
                continue
            document_id = str(feature.get("document_id") or "").strip()
            row_citations = list(dict.fromkeys(evidence_by_document.get(document_id, [])))
            if not document_id or not row_citations:
                continue
            value = feature.get("value") if isinstance(feature.get("value"), Mapping) else {}
            labels = _feature_labels(feature_type, value)
            for label in labels:
                key = (document_id, label)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "document_id": document_id,
                    "label": label,
                    "count": 1,
                    "citation_ids": row_citations,
                })
                citations.extend(row_citations)
                feature_id = str(feature.get("feature_id") or "").strip()
                if feature_id:
                    feature_ids.append(feature_id)
        if not rows:
            continue
        doc_count = len({row["document_id"] for row in rows})
        methods = list(dict.fromkeys(
            "/".join(str(feature.get(key) or "").strip() for key in ("backend", "model_name", "model_version") if str(feature.get(key) or "").strip())
            for feature in analysis_features
            if isinstance(feature, Mapping)
            and feature.get("feature_type") == feature_type
            and str(feature.get("feature_id") or "").strip() in feature_ids
        ))
        method_detail = "; analyzer=" + ", ".join(methods) if methods else ""
        result.append(AnalysisDataset(
            dataset_id=f"analysis_{requirement['dataset_id']}_{slot_id}",
            purpose=str(requirement["purpose"]),
            columns=list(requirement["columns"]),
            rows=rows,
            grain="每篇唯一来源文档的一个分析标签",
            sample_scope=f"本节点证据引用的 {doc_count} 篇公开来源文档全文；不代表社交评论样本或公众总体",
            time_scope="材料发布时间和事件时间均按原来源分别识别，不构成连续时间序列",
            method=f"从应用生成的 {feature_type} analysis_feature 提取单篇文本标签；每行保留来源文档、特征 ID 和对应证据，不把分析标签当作事实主张{method_detail}",
            citation_ids=list(dict.fromkeys(citations)),
            source_feature_ids=list(dict.fromkeys(feature_ids)),
            limitations=[
                "样本单位为公开来源文档，不是社交平台评论或随机抽取的公众样本",
                "分析标签用于描述所采集文本信号，不代表事实真伪或总体态度",
            ],
        ))
    return result


def materialize_report_analysis_datasets(
    *,
    nodes: list[dict[str, Any]],
    template: InvestigationReportTemplate,
) -> list[AnalysisDataset]:
    """Aggregate frozen per-document features only when the template sample floor is met."""

    output: list[AnalysisDataset] = []
    for requirement in template.datasets:
        if requirement.collection_mode != "analysis_feature":
            continue
        candidates: list[AnalysisDataset] = []
        for node in nodes:
            if str(node.get("slot_id") or "") not in requirement.source_nodes:
                continue
            draft = dict(node.get("draft") or {})
            for raw in draft.get("datasets") or ():
                if not isinstance(raw, Mapping):
                    continue
                dataset = AnalysisDataset.from_dict(raw)
                if dataset.purpose == requirement.purpose and set(requirement.columns).issubset(dataset.columns):
                    candidates.append(dataset)
        if not candidates:
            continue

        observations: dict[tuple[str, str], dict[str, Any]] = {}
        for dataset in candidates:
            for row in dataset.rows:
                document_id = str(row.get("document_id") or "").strip()
                label = str(row.get("label") or "").strip()
                if not document_id or not label:
                    continue
                key = (document_id, label)
                entry = observations.setdefault(key, {"citations": set()})
                entry["citations"].update(str(item) for item in row.get("citation_ids", ()) if str(item).strip())
        sample_documents = {document_id for document_id, _ in observations}
        if len(sample_documents) < requirement.minimum_samples:
            continue

        labels: dict[str, set[str]] = defaultdict(set)
        label_citations: dict[str, set[str]] = defaultdict(set)
        for (document_id, label), entry in observations.items():
            labels[label].add(document_id)
            label_citations[label].update(entry["citations"])
        if len(labels) < 2:
            continue
        source_ids = list(dict.fromkeys(dataset.dataset_id for dataset in candidates))
        feature_ids = list(dict.fromkeys(
            feature_id for dataset in candidates for feature_id in dataset.source_feature_ids
        ))
        citations = list(dict.fromkeys(
            citation for dataset in candidates for citation in dataset.citation_ids
        ))
        rows = [
            {
                "label": label,
                "count": len(documents),
                "share": round(len(documents) / len(sample_documents), 4),
                "citation_ids": sorted(label_citations[label]),
            }
            for label, documents in sorted(labels.items(), key=lambda item: (-len(item[1]), item[0]))
        ]
        limitations = list(dict.fromkeys(
            limitation for dataset in candidates for limitation in dataset.limitations
        ))
        if requirement.feature_type == "topic":
            limitations.append("一篇来源文档可匹配多个主题，因此主题比例不互斥，合计可能超过 100%")
        output.append(AnalysisDataset(
            dataset_id=f"report_analysis_{requirement.dataset_id}",
            purpose=requirement.purpose,
            columns=["label", "count", "share", "citation_ids"],
            rows=rows,
            grain=f"每个{requirement.sample_unit}中出现的一类{requirement.feature_type}标签",
            sample_scope=f"{len(sample_documents)} 篇去重后的公开来源文档全文；不代表社交评论样本或公众总体",
            time_scope="材料发布时间和事件时间均按原来源分别识别，不构成连续时间序列",
            method=f"按唯一来源文档聚合 {requirement.feature_type} 标签；一篇文档仅在同一标签内计数一次",
            citation_ids=citations,
            source_dataset_ids=source_ids,
            source_feature_ids=feature_ids,
            limitations=limitations,
        ))
    return output


def materialize_report_research_datasets(
    *,
    nodes: list[dict[str, Any]],
    template: InvestigationReportTemplate,
) -> list[AnalysisDataset]:
    """Join node-owned datasets only under the comparison rules in the template."""

    output: list[AnalysisDataset] = []
    for requirement in template.datasets:
        if requirement.collection_mode != "research":
            continue
        candidates: list[AnalysisDataset] = []
        for node in nodes:
            if str(node.get("slot_id") or "") not in requirement.source_nodes:
                continue
            draft = dict(node.get("draft") or {})
            for raw in draft.get("datasets") or ():
                if not isinstance(raw, Mapping):
                    continue
                dataset = AnalysisDataset.from_dict(raw)
                if dataset.purpose == requirement.purpose and set(requirement.columns).issubset(dataset.columns):
                    candidates.append(dataset)
        if not candidates:
            continue

        if requirement.merge_strategy == "event_rows":
            selected = candidates
        else:
            compatible: dict[tuple[str, str, str], list[AnalysisDataset]] = defaultdict(list)
            for dataset in candidates:
                compatible[(dataset.grain, dataset.sample_scope, dataset.method)].append(dataset)
            selected = max(
                compatible.values(),
                key=lambda group: (sum(len(item.rows) for item in group), -candidates.index(group[0])),
            )

        rows_by_key: dict[str, dict[str, Any]] = {}
        for dataset in selected:
            for raw_row in dataset.rows:
                row = {column: raw_row[column] for column in requirement.columns if column in raw_row}
                if not any(value not in (None, "", []) for key, value in row.items() if key != "citation_ids"):
                    continue
                row_citations = list(dict.fromkeys(str(item) for item in row.get("citation_ids", ()) if str(item).strip()))
                row["citation_ids"] = row_citations
                identity = {key: value for key, value in row.items() if key != "citation_ids"}
                key = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
                existing = rows_by_key.setdefault(key, row)
                existing["citation_ids"] = list(dict.fromkeys([*existing.get("citation_ids", ()), *row_citations]))
        source_rows = list(rows_by_key.values())
        output_columns = list(requirement.columns)
        if requirement.aggregation == "count_by_label":
            samples: dict[str, dict[str, Any]] = {}
            conflicts: set[str] = set()
            for row in source_rows:
                sample_id = str(row.get("sample_id") or "").strip()
                label = str(row.get("label") or "").strip()
                count = row.get("count")
                if not sample_id or not label or isinstance(count, bool) or count != 1:
                    continue
                current = samples.get(sample_id)
                if current is not None and current["label"] != label:
                    conflicts.add(sample_id)
                    continue
                sample = samples.setdefault(sample_id, {"label": label, "citations": set()})
                sample["citations"].update(str(item) for item in row.get("citation_ids", ()) if str(item).strip())
            for sample_id in conflicts:
                samples.pop(sample_id, None)
            if len(samples) < requirement.minimum_samples:
                continue
            label_samples: dict[str, set[str]] = defaultdict(set)
            label_citations: dict[str, set[str]] = defaultdict(set)
            for sample_id, sample in samples.items():
                label_samples[sample["label"]].add(sample_id)
                label_citations[sample["label"]].update(sample["citations"])
            if len(label_samples) < 2:
                continue
            rows = [
                {
                    "label": label,
                    "count": len(sample_ids),
                    "share": round(len(sample_ids) / len(samples), 4),
                    "citation_ids": sorted(label_citations[label]),
                }
                for label, sample_ids in sorted(label_samples.items(), key=lambda item: (-len(item[1]), item[0]))
            ]
            output_columns = ["label", "count", "share", "citation_ids"]
        else:
            rows = source_rows
            if len(rows) < requirement.minimum_samples:
                continue

        source_ids = list(dict.fromkeys(item.dataset_id for item in selected))
        citations = list(dict.fromkeys(
            citation for dataset in selected for citation in dataset.citation_ids
        ))
        citations.extend(citation for row in rows for citation in row["citation_ids"])
        limitations = list(dict.fromkeys(
            limitation for dataset in selected for limitation in dataset.limitations
        ))
        if requirement.merge_strategy == "event_rows":
            grain = "每条来源明确记录的事件/阶段观察"
            sample_scope = f"按模板汇集 {len(source_ids)} 个节点中的来源定位记录"
            time_scope = "按每条记录中的来源时间字段分别判断"
            method = "只拼接模板指定节点中的来源数据行，按可见字段去重并合并对应证据引用；不补造时间或事件"
        else:
            first = selected[0]
            grain = first.grain
            sample_scope = first.sample_scope
            time_scope = "各记录时间范围见对应行和来源"
            method = "仅合并粒度、样本范围和整理方法完全一致的数据集；重复行去重并合并来源引用"
            if len(selected) < len(candidates):
                limitations.append("另有节点数据因粒度、样本范围或方法不同，未与本组数值合并")
        first_selected = selected[0]
        output.append(AnalysisDataset(
            dataset_id=f"report_template_{requirement.dataset_id}",
            purpose=requirement.purpose,
            columns=output_columns,
            rows=rows,
            grain=(f"每个{requirement.sample_unit}的一项情感标签" if requirement.aggregation == "count_by_label" else grain),
            sample_scope=(f"{len(samples)} 条去重且可定位的用户评论，平台和时间范围：{first_selected.sample_scope}" if requirement.aggregation == "count_by_label" else sample_scope),
            time_scope=(first_selected.time_scope if requirement.aggregation == "count_by_label" else time_scope),
            method=("按 sample_id 去重用户评论样本后按情感标签计数；同一评论标签冲突时排除该样本，比例仅适用于已收集样本" if requirement.aggregation == "count_by_label" else method),
            input_metric_ids=list(dict.fromkeys(metric_id for item in selected for metric_id in item.input_metric_ids)),
            citation_ids=list(dict.fromkeys(citations)),
            source_dataset_ids=source_ids,
            source_feature_ids=list(dict.fromkeys(feature_id for item in selected for feature_id in item.source_feature_ids)),
            limitations=(list(dict.fromkeys([*limitations, "该评论集为可访问来源中的便利样本，不是随机抽样，不能代表平台或公众总体"])) if requirement.aggregation == "count_by_label" else limitations),
        ))
    return output


def materialize_report_datasets(
    *,
    nodes: list[dict[str, Any]],
    template: InvestigationReportTemplate,
) -> list[AnalysisDataset]:
    return [
        *materialize_report_research_datasets(nodes=nodes, template=template),
        *materialize_report_analysis_datasets(nodes=nodes, template=template),
    ]


def _feature_labels(feature_type: str, value: Mapping[str, Any]) -> list[str]:
    if feature_type == "topic":
        raw = value.get("topics")
        if not isinstance(raw, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
    label = str(value.get("label_zh") or value.get("label") or "").strip()
    if not label or label in {"不明确", "unclear"}:
        return []
    return [label]


__all__ = [
    "analysis_feature_datasets_for_node",
    "materialize_report_analysis_datasets",
    "materialize_report_datasets",
    "materialize_report_research_datasets",
    "research_data_needs",
]
