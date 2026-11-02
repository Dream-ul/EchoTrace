"""Contracts shared by frozen node traces and report composition.

The objects in this module describe data that is already grounded in a node
draft.  They do not fetch sources or calculate values; those responsibilities
belong to the research and analysis layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import TraceContractError


REPORT_BLOCK_TYPES = frozenset({
    "heading",
    "paragraph",
    "bullet_list",
    "ordered_list",
    "callout",
    "evidence_card",
    "claim_table",
    "timeline",
    "metric_grid",
    "chart",
    "table",
    "image",
    "video",
    "source_list",
})
_VISUAL_TYPES = {"chart", "table", "metric_grid", "timeline", "matrix"}
_CHART_KINDS = {"line", "bar", "pie", "doughnut", "scatter", "area", "radar"}


def _text(value: Any, field_name: str, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise TraceContractError(f"{field_name} cannot be empty")
    return result


def _strings(value: Any, field_name: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise TraceContractError(f"{field_name} must be a list of strings")
    result = [str(item).strip() for item in value]
    if any(not item for item in result):
        raise TraceContractError(f"{field_name} cannot contain empty values")
    if len(result) != len(set(result)):
        raise TraceContractError(f"{field_name} cannot contain duplicates")
    if required and not result:
        raise TraceContractError(f"{field_name} cannot be empty")
    return result


def _payload(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TraceContractError(f"{field_name} must be an object")
    return dict(value)


@dataclass(slots=True)
class MetricObservation:
    """A source-grounded observation that may be used in a report."""

    metric_id: str
    name: str
    value: int | float | str
    unit: str
    time_scope: str
    source_document_id: str
    evidence_id: str
    locator: dict[str, Any]
    direct_or_derived: str = "direct"
    formula: str = ""
    geography: str = ""
    subject: str = ""
    population: str = ""
    denominator: str = ""
    limitation: str = ""
    confidence: float | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "metric_id",
            "name",
            "unit",
            "time_scope",
            "source_document_id",
            "evidence_id",
        ):
            setattr(self, field_name, _text(getattr(self, field_name), f"metric.{field_name}"))
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float, str)):
            raise TraceContractError("metric.value must be a number or string")
        if isinstance(self.value, str) and not self.value.strip():
            raise TraceContractError("metric.value cannot be empty")
        self.locator = _payload(self.locator, "metric.locator")
        if not self.locator:
            raise TraceContractError("metric.locator cannot be empty")
        self.direct_or_derived = _text(self.direct_or_derived, "metric.direct_or_derived")
        if self.direct_or_derived not in {"direct", "derived"}:
            raise TraceContractError("metric.direct_or_derived must be direct or derived")
        self.formula = _text(self.formula, "metric.formula", required=False)
        if self.direct_or_derived == "derived" and not self.formula:
            raise TraceContractError("derived metric requires formula")
        for field_name in ("geography", "subject", "population", "denominator", "limitation"):
            setattr(self, field_name, _text(getattr(self, field_name), f"metric.{field_name}", required=False))
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
                raise TraceContractError("metric.confidence must be a number or null")
            if not 0 <= self.confidence <= 1:
                raise TraceContractError("metric.confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "time_scope": self.time_scope,
            "source_document_id": self.source_document_id,
            "evidence_id": self.evidence_id,
            "locator": dict(self.locator),
            "direct_or_derived": self.direct_or_derived,
            "formula": self.formula,
            "geography": self.geography,
            "subject": self.subject,
            "population": self.population,
            "denominator": self.denominator,
            "limitation": self.limitation,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MetricObservation":
        return cls(**_payload(payload, "metric"))


@dataclass(slots=True)
class AnalysisDataset:
    """A same-grain dataset prepared for a table or visualization."""

    dataset_id: str
    purpose: str
    columns: list[str]
    rows: list[dict[str, Any]]
    grain: str
    sample_scope: str
    time_scope: str
    method: str
    input_metric_ids: list[str] = field(default_factory=list)
    citation_ids: list[str] = field(default_factory=list)
    source_dataset_ids: list[str] = field(default_factory=list)
    source_feature_ids: list[str] = field(default_factory=list)
    source_block_id: str = ""
    limitations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in ("dataset_id", "purpose", "grain", "sample_scope", "time_scope", "method"):
            setattr(self, field_name, _text(getattr(self, field_name), f"dataset.{field_name}"))
        self.columns = _strings(self.columns, "dataset.columns", required=True)
        if not isinstance(self.rows, list):
            raise TraceContractError("dataset.rows must be a list")
        normalized_rows: list[dict[str, Any]] = []
        for index, row in enumerate(self.rows):
            value = _payload(row, f"dataset.rows[{index}]")
            unknown = set(value) - set(self.columns)
            if unknown:
                raise TraceContractError(f"dataset.rows[{index}] contains unknown columns: {sorted(unknown)}")
            normalized_rows.append(value)
        self.rows = normalized_rows
        self.input_metric_ids = _strings(self.input_metric_ids, "dataset.input_metric_ids")
        self.citation_ids = _strings(self.citation_ids, "dataset.citation_ids")
        self.source_dataset_ids = _strings(self.source_dataset_ids, "dataset.source_dataset_ids")
        self.source_feature_ids = _strings(self.source_feature_ids, "dataset.source_feature_ids")
        self.source_block_id = _text(self.source_block_id, "dataset.source_block_id", required=False)
        self.limitations = _strings(self.limitations, "dataset.limitations")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "purpose": self.purpose,
            "columns": list(self.columns),
            "rows": [dict(row) for row in self.rows],
            "grain": self.grain,
            "sample_scope": self.sample_scope,
            "time_scope": self.time_scope,
            "method": self.method,
            "input_metric_ids": list(self.input_metric_ids),
            "citation_ids": list(self.citation_ids),
            "source_dataset_ids": list(self.source_dataset_ids),
            "source_feature_ids": list(self.source_feature_ids),
            "source_block_id": self.source_block_id,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AnalysisDataset":
        value = _payload(payload, "dataset")
        allowed = {
            "dataset_id", "purpose", "columns", "rows", "grain", "sample_scope",
            "time_scope", "method", "input_metric_ids", "citation_ids",
            "source_dataset_ids", "source_feature_ids", "source_block_id", "limitations",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise TraceContractError(f"dataset contains unsupported fields: {', '.join(unknown)}")
        return cls(**value)


@dataclass(slots=True)
class VisualizationSpec:
    """A renderer-safe request to display an existing dataset."""

    visualization_id: str
    type: str
    title: str
    dataset_id: str
    fallback: str
    chart_kind: str = ""
    x_field: str = ""
    y_field: str = ""
    citations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in ("visualization_id", "title", "dataset_id", "fallback"):
            setattr(self, field_name, _text(getattr(self, field_name), f"visualization.{field_name}"))
        self.type = _text(self.type, "visualization.type")
        # Models sometimes return the chart kind in `type` instead of using
        # the normalized `chart` + `chart_kind` pair.
        if self.type in _CHART_KINDS:
            self.chart_kind = self.chart_kind or self.type
            self.type = "chart"
        if self.type not in _VISUAL_TYPES:
            raise TraceContractError(f"unsupported visualization type: {self.type}")
        self.chart_kind = _text(self.chart_kind, "visualization.chart_kind", required=False)
        if self.type == "chart" and self.chart_kind not in _CHART_KINDS:
            raise TraceContractError("chart visualization requires a supported chart_kind")
        self.x_field = _text(self.x_field, "visualization.x_field", required=False)
        self.y_field = _text(self.y_field, "visualization.y_field", required=False)
        self.citations = _strings(self.citations, "visualization.citations")

    def to_dict(self) -> dict[str, Any]:
        return {
            "visualization_id": self.visualization_id,
            "type": self.type,
            "title": self.title,
            "dataset_id": self.dataset_id,
            "fallback": self.fallback,
            "chart_kind": self.chart_kind,
            "x_field": self.x_field,
            "y_field": self.y_field,
            "citations": list(self.citations),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VisualizationSpec":
        value = _payload(payload, "visualization")
        value.setdefault("fallback", "table")
        return cls(**value)


@dataclass(slots=True)
class ReportBlock:
    type: str
    payload: dict[str, Any]
    citations: list[str] = field(default_factory=list)
    dataset_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.type = _text(self.type, "report_block.type")
        if self.type not in REPORT_BLOCK_TYPES:
            raise TraceContractError(f"unsupported report block type: {self.type}")
        self.payload = _payload(self.payload, "report_block.payload")
        self.citations = _strings(self.citations, "report_block.citations")
        self.dataset_ids = _strings(self.dataset_ids, "report_block.dataset_ids")
        if self.type in {"chart", "table", "metric_grid", "timeline"} and not self.dataset_ids:
            raise TraceContractError(f"{self.type} block requires dataset_ids")
        if self.type in {"image", "video"}:
            media_id = str(self.payload.get("media_id") or "").strip()
            if not media_id:
                raise TraceContractError(f"{self.type} block requires payload.media_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "payload": dict(self.payload),
            "citations": list(self.citations),
            "dataset_ids": list(self.dataset_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReportBlock":
        return cls(**_payload(payload, "report_block"))


@dataclass(slots=True)
class ReportChapter:
    chapter_id: str
    title: str
    order: int
    purpose: str
    summary: str
    blocks: list[ReportBlock]

    def __post_init__(self) -> None:
        for field_name in ("chapter_id", "title", "purpose", "summary"):
            setattr(self, field_name, _text(getattr(self, field_name), f"report_chapter.{field_name}"))
        if not isinstance(self.order, int) or self.order < 1:
            raise TraceContractError("report_chapter.order must be a positive integer")
        self.blocks = [item if isinstance(item, ReportBlock) else ReportBlock.from_dict(item) for item in self.blocks]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "title": self.title,
            "order": self.order,
            "purpose": self.purpose,
            "summary": self.summary,
            "blocks": [item.to_dict() for item in self.blocks],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReportChapter":
        value = _payload(payload, "report_chapter")
        value["blocks"] = [ReportBlock.from_dict(item) for item in value.get("blocks", [])]
        return cls(**value)


@dataclass(slots=True)
class ReportDocument:
    """The only report output contract for the new node-driven workflow."""

    report_id: str
    run_id: str
    trace_id: str
    investigation_type: str
    template_id: str
    template_version: str
    title: str
    summary: str
    chapters: list[ReportChapter]
    subtitle: str = ""
    toc: list[dict[str, Any]] = field(default_factory=list)
    media: list[dict[str, Any]] = field(default_factory=list)
    template_content_hash: str = ""
    source_index: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    citation_index: dict[str, list[str]] = field(default_factory=dict)
    language: str = "zh-CN"
    metrics: list[MetricObservation] = field(default_factory=list)
    datasets: list[AnalysisDataset] = field(default_factory=list)
    visualizations: list[VisualizationSpec] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in (
            "report_id", "run_id", "trace_id", "investigation_type", "template_id",
            "template_version", "title", "summary", "language",
        ):
            setattr(self, field_name, _text(getattr(self, field_name), f"report.{field_name}"))
        if self.language != "zh-CN":
            raise TraceContractError("report.language must be zh-CN")
        self.chapters = [item if isinstance(item, ReportChapter) else ReportChapter.from_dict(item) for item in self.chapters]
        if not self.chapters:
            raise TraceContractError("report.chapters cannot be empty")
        if [item.order for item in self.chapters] != list(range(1, len(self.chapters) + 1)):
            raise TraceContractError("report chapter orders must be contiguous and start at 1")
        if len({item.chapter_id for item in self.chapters}) != len(self.chapters):
            raise TraceContractError("report chapter_id values must be unique")
        self.subtitle = _text(self.subtitle, "report.subtitle", required=False)
        if not isinstance(self.toc, list):
            raise TraceContractError("report.toc must be a list")
        normalized_toc: list[dict[str, Any]] = []
        for index, item in enumerate(self.toc):
            value = _payload(item, f"report.toc[{index}]")
            chapter_id = _text(value.get("chapter_id"), f"report.toc[{index}].chapter_id")
            title = _text(value.get("title"), f"report.toc[{index}].title")
            if chapter_id not in {chapter.chapter_id for chapter in self.chapters}:
                raise TraceContractError(f"report.toc references unknown chapter: {chapter_id}")
            normalized_toc.append({"chapter_id": chapter_id, "title": title})
        self.toc = normalized_toc
        if not self.toc:
            self.toc = [{"chapter_id": chapter.chapter_id, "title": chapter.title} for chapter in self.chapters]
        if [item["chapter_id"] for item in self.toc] != [chapter.chapter_id for chapter in self.chapters]:
            raise TraceContractError("report.toc must follow chapter order")
        if not isinstance(self.media, list):
            raise TraceContractError("report.media must be a list")
        normalized_media: list[dict[str, Any]] = []
        media_ids: set[str] = set()
        for index, item in enumerate(self.media):
            value = _payload(item, f"report.media[{index}]")
            media_id = _text(value.get("media_id"), f"report.media[{index}].media_id")
            media_type = _text(value.get("media_type"), f"report.media[{index}].media_type")
            if media_id in media_ids:
                raise TraceContractError(f"report.media contains duplicate media_id: {media_id}")
            if media_type not in {"image", "video"}:
                raise TraceContractError("report.media.media_type must be image or video")
            source_url = str(value.get("source_url") or "").strip()
            object_key = str(value.get("object_key") or "").strip()
            if bool(source_url) == bool(object_key):
                raise TraceContractError("report.media requires exactly one of source_url or object_key")
            media_ids.add(media_id)
            normalized_media.append({**value, "media_id": media_id, "media_type": media_type})
        self.media = normalized_media
        self.template_content_hash = _text(self.template_content_hash, "report.template_content_hash")
        self.source_index = _strings(self.source_index, "report.source_index")
        if not isinstance(self.sources, list):
            raise TraceContractError("report.sources must be a list")
        normalized_sources: list[dict[str, Any]] = []
        source_ids: set[str] = set()
        for index, item in enumerate(self.sources):
            value = _payload(item, f"report.sources[{index}]")
            document_id = _text(value.get("document_id"), f"report.sources[{index}].document_id")
            title = _text(value.get("title"), f"report.sources[{index}].title")
            url = str(value.get("url") or "").strip()
            if document_id in source_ids:
                raise TraceContractError(f"report.sources contains duplicate document_id: {document_id}")
            source_ids.add(document_id)
            normalized_sources.append({**value, "document_id": document_id, "title": title, "url": url})
        self.sources = normalized_sources
        if not isinstance(self.citation_index, Mapping):
            raise TraceContractError("report.citation_index must be an object")
        self.citation_index = {
            str(citation_id): _strings(document_ids, f"report.citation_index.{citation_id}")
            for citation_id, document_ids in self.citation_index.items()
        }
        if source_ids and any(
            not set(document_ids).issubset(source_ids)
            for document_ids in self.citation_index.values()
        ):
            raise TraceContractError("report.citation_index references unknown source documents")
        self.metrics = [item if isinstance(item, MetricObservation) else MetricObservation.from_dict(item) for item in self.metrics]
        self.datasets = [item if isinstance(item, AnalysisDataset) else AnalysisDataset.from_dict(item) for item in self.datasets]
        self.visualizations = [item if isinstance(item, VisualizationSpec) else VisualizationSpec.from_dict(item) for item in self.visualizations]
        metric_ids = {item.metric_id for item in self.metrics}
        dataset_ids = {item.dataset_id for item in self.datasets}
        if len(metric_ids) != len(self.metrics) or len(dataset_ids) != len(self.datasets):
            raise TraceContractError("report metric_id and dataset_id values must be unique")
        for dataset in self.datasets:
            if not set(dataset.input_metric_ids).issubset(metric_ids):
                raise TraceContractError(f"dataset references unknown metric: {dataset.dataset_id}")
        for visualization in self.visualizations:
            if visualization.dataset_id not in dataset_ids:
                raise TraceContractError(f"visualization references unknown dataset: {visualization.dataset_id}")
        known_dataset_ids = dataset_ids
        for chapter in self.chapters:
            for block in chapter.blocks:
                if not set(block.dataset_ids).issubset(known_dataset_ids):
                    raise TraceContractError(f"report block references unknown dataset in chapter: {chapter.chapter_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "investigation_type": self.investigation_type,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "title": self.title,
            "subtitle": self.subtitle,
            "summary": self.summary,
            "toc": [dict(item) for item in self.toc],
            "chapters": [item.to_dict() for item in self.chapters],
            "media": [dict(item) for item in self.media],
            "template_content_hash": self.template_content_hash,
            "source_index": list(self.source_index),
            "sources": [dict(item) for item in self.sources],
            "citation_index": {key: list(value) for key, value in self.citation_index.items()},
            "language": self.language,
            "metrics": [item.to_dict() for item in self.metrics],
            "datasets": [item.to_dict() for item in self.datasets],
            "visualizations": [item.to_dict() for item in self.visualizations],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReportDocument":
        value = _payload(payload, "report")
        value["chapters"] = [ReportChapter.from_dict(item) for item in value.get("chapters", [])]
        value["metrics"] = [MetricObservation.from_dict(item) for item in value.get("metrics", [])]
        value["datasets"] = [AnalysisDataset.from_dict(item) for item in value.get("datasets", [])]
        value["visualizations"] = [VisualizationSpec.from_dict(item) for item in value.get("visualizations", [])]
        return cls(**value)


__all__ = [
    "AnalysisDataset",
    "MetricObservation",
    "ReportBlock",
    "ReportChapter",
    "ReportDocument",
    "REPORT_BLOCK_TYPES",
    "VisualizationSpec",
]
