"""Load and validate investigation-type Markdown report templates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from echotrace.domain.reporting import REPORT_BLOCK_TYPES
from echotrace.domain.trace_nodes import TraceContractError
from echotrace.domain.trace_nodes import get_trace_template


_DEFAULT_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "report_templates"
_DATA_NEEDS = frozenset({"metrics", "sentiment", "stance", "topic", "media", "sources"})
_VISUAL_KINDS = frozenset({"line", "bar", "pie", "doughnut", "scatter", "area", "radar", "table", "metric_grid", "timeline", "matrix"})


@dataclass(frozen=True, slots=True)
class ReportTemplateSubheading:
    title: str
    level: int


@dataclass(frozen=True, slots=True)
class ReportTemplateChapter:
    chapter_id: str
    title: str
    purpose: str
    node_slots: tuple[str, ...]
    recommended_blocks: tuple[str, ...]
    data_needs: tuple[str, ...]
    empty_data_behavior: str
    subheadings: tuple[ReportTemplateSubheading, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportTemplateDataset:
    dataset_id: str
    purpose: str
    source_nodes: tuple[str, ...]
    columns: tuple[str, ...]
    required: bool
    collection_mode: str
    collection_guidance: str
    sample_unit: str
    minimum_samples: int
    feature_type: str = ""
    aggregation: str = ""
    merge_strategy: str = "comparable_rows"


@dataclass(frozen=True, slots=True)
class ReportTemplateVisual:
    visualization_id: str
    type: str
    title: str
    dataset_id: str
    x_field: str
    y_field: str
    fallback: str


@dataclass(frozen=True, slots=True)
class InvestigationReportTemplate:
    template_id: str
    version: str
    investigation_type: str
    trace_template_id: str
    trace_template_version: str
    language: str
    chapters: tuple[ReportTemplateChapter, ...]
    datasets: tuple[ReportTemplateDataset, ...]
    visuals: tuple[ReportTemplateVisual, ...]
    instructions: str
    content_hash: str


def _front_matter(source: str, path: Path) -> tuple[dict[str, Any], str]:
    if not source.startswith("---\n"):
        raise TraceContractError(f"report template must start with YAML front matter: {path.name}")
    _, remainder = source.split("---\n", 1)
    header, separator, body = remainder.partition("\n---\n")
    if not separator:
        raise TraceContractError(f"report template front matter is not closed: {path.name}")
    try:
        metadata = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise TraceContractError(f"invalid report template YAML: {path.name}") from exc
    if not isinstance(metadata, dict):
        raise TraceContractError(f"report template front matter must be a mapping: {path.name}")
    return metadata, body.strip()


def _strings(value: Any, field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise TraceContractError(f"report template {field_name} must be a list of non-empty strings")
    result = tuple(item.strip() for item in value)
    if len(set(result)) != len(result):
        raise TraceContractError(f"report template {field_name} cannot contain duplicates")
    if not allow_empty and not result:
        raise TraceContractError(f"report template {field_name} cannot be empty")
    return result


def _chapters(metadata: Mapping[str, Any], path: Path) -> tuple[ReportTemplateChapter, ...]:
    raw_chapters = metadata.get("chapters")
    if not isinstance(raw_chapters, list) or not raw_chapters:
        raise TraceContractError(f"report template must define chapters: {path.name}")
    chapters: list[ReportTemplateChapter] = []
    for index, item in enumerate(raw_chapters):
        if not isinstance(item, dict):
            raise TraceContractError(f"report template chapter {index} must be a mapping")
        chapter = ReportTemplateChapter(
            chapter_id=str(item.get("id") or "").strip(),
            title=str(item.get("title") or "").strip(),
            purpose=str(item.get("purpose") or "").strip(),
            node_slots=_strings(item.get("node_slots"), f"chapters[{index}].node_slots", allow_empty=True),
            recommended_blocks=_strings(item.get("recommended_blocks"), f"chapters[{index}].recommended_blocks"),
            data_needs=_strings(item.get("data_needs", []), f"chapters[{index}].data_needs", allow_empty=True),
            empty_data_behavior=str(item.get("empty_data_behavior") or "").strip(),
        )
        if not all((chapter.chapter_id, chapter.title, chapter.purpose, chapter.empty_data_behavior)):
            raise TraceContractError(f"report template chapter {index} is missing required text")
        unknown_blocks = set(chapter.recommended_blocks) - REPORT_BLOCK_TYPES
        if unknown_blocks:
            raise TraceContractError(f"report template chapter {chapter.chapter_id} has unsupported blocks: {sorted(unknown_blocks)}")
        unknown_needs = set(chapter.data_needs) - _DATA_NEEDS
        if unknown_needs:
            raise TraceContractError(f"report template chapter {chapter.chapter_id} has unsupported data needs: {sorted(unknown_needs)}")
        chapters.append(chapter)
    ids = [item.chapter_id for item in chapters]
    titles = [item.title for item in chapters]
    if len(ids) != len(set(ids)) or len(titles) != len(set(titles)):
        raise TraceContractError(f"report template chapter ids and titles must be unique: {path.name}")
    return tuple(chapters)


def _chapter_subheadings(
    instructions: str,
    chapters: tuple[ReportTemplateChapter, ...],
    path: Path,
) -> tuple[ReportTemplateChapter, ...]:
    chapter_titles = {chapter.title: chapter.chapter_id for chapter in chapters}
    headings: dict[str, list[ReportTemplateSubheading]] = {chapter.chapter_id: [] for chapter in chapters}
    current_chapter_id = ""
    for line in instructions.splitlines():
        match = re.match(r"^(#{2,5})\s+(.+?)\s*$", line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        if level == 2:
            current_chapter_id = chapter_titles.get(title, "")
            continue
        if level > 4:
            raise TraceContractError(f"report template heading depth cannot exceed 4: {path.name}")
        if not current_chapter_id:
            raise TraceContractError(f"report template subheading must follow a chapter heading: {path.name}")
        previous_level = headings[current_chapter_id][-1].level if headings[current_chapter_id] else 2
        if level == 4 and previous_level < 3:
            raise TraceContractError(f"report template heading levels cannot skip a parent: {path.name}")
        headings[current_chapter_id].append(ReportTemplateSubheading(title=title, level=level))

    result = []
    for chapter in chapters:
        outline = tuple(headings[chapter.chapter_id])
        if not outline:
            raise TraceContractError(f"report template chapter {chapter.chapter_id} must define subheadings: {path.name}")
        if len({(item.level, item.title) for item in outline}) != len(outline):
            raise TraceContractError(f"report template chapter {chapter.chapter_id} contains duplicate subheadings")
        result.append(replace(chapter, subheadings=outline))
    return tuple(result)


def _datasets_and_visuals(
    metadata: Mapping[str, Any],
    *,
    known_slots: set[str],
    path: Path,
) -> tuple[tuple[ReportTemplateDataset, ...], tuple[ReportTemplateVisual, ...]]:
    raw_datasets = metadata.get("datasets", [])
    if not isinstance(raw_datasets, list):
        raise TraceContractError(f"report template datasets must be a list: {path.name}")
    datasets: list[ReportTemplateDataset] = []
    for index, item in enumerate(raw_datasets):
        if not isinstance(item, dict):
            raise TraceContractError(f"report template dataset {index} must be a mapping")
        source_nodes = _strings(item.get("source_nodes", []), f"datasets[{index}].source_nodes", allow_empty=True)
        unknown_slots = set(source_nodes) - known_slots
        if unknown_slots:
            raise TraceContractError(f"report template dataset {item.get('id')!r} references unknown node slots: {sorted(unknown_slots)}")
        columns = _strings(item.get("columns", []), f"datasets[{index}].columns")
        collection = item.get("collection")
        if not isinstance(collection, dict):
            raise TraceContractError(f"report template dataset {item.get('id')!r} must define collection guidance")
        collection_mode = str(collection.get("mode") or "").strip()
        if collection_mode not in {"research", "analysis_feature"}:
            raise TraceContractError(f"report template dataset {item.get('id')!r} has unsupported collection mode")
        if collection_mode == "research" and "citation_ids" not in columns:
            raise TraceContractError(f"report template dataset {item.get('id')!r} must retain row-level citation_ids")
        collection_guidance = str(collection.get("guidance") or "").strip()
        sample_unit = str(collection.get("sample_unit") or "").strip()
        minimum_samples = collection.get("minimum_samples")
        if not collection_guidance or not sample_unit:
            raise TraceContractError(f"report template dataset {item.get('id')!r} is missing collection guidance or sample_unit")
        if isinstance(minimum_samples, bool) or not isinstance(minimum_samples, int) or minimum_samples < 1:
            raise TraceContractError(f"report template dataset {item.get('id')!r} minimum_samples must be a positive integer")
        feature_type = str(collection.get("feature_type") or "").strip()
        aggregation = str(collection.get("aggregation") or "").strip()
        merge_strategy = str(collection.get("merge_strategy") or "comparable_rows").strip()
        if merge_strategy not in {"comparable_rows", "event_rows"}:
            raise TraceContractError(f"report template dataset {item.get('id')!r} has unsupported merge strategy")
        if collection_mode == "analysis_feature":
            if feature_type not in {"sentiment", "stance", "topic"} or aggregation != "count_by_label":
                raise TraceContractError(f"report template dataset {item.get('id')!r} has unsupported analysis feature aggregation")
            if not {"document_id", "label", "count", "citation_ids"}.issubset(columns):
                raise TraceContractError(f"report template dataset {item.get('id')!r} must retain document, label, count and citation columns")
        elif aggregation not in {"", "count_by_label"}:
            raise TraceContractError(f"report template dataset {item.get('id')!r} has unsupported research aggregation")
        if collection_mode == "research" and aggregation == "count_by_label" and not {"sample_id", "label", "count", "citation_ids"}.issubset(columns):
            raise TraceContractError(f"report template dataset {item.get('id')!r} must retain sample_id, label, count and citation columns")
        dataset = ReportTemplateDataset(
            dataset_id=str(item.get("id") or "").strip(),
            purpose=str(item.get("purpose") or "").strip(),
            source_nodes=source_nodes,
            columns=columns,
            required=bool(item.get("required", False)),
            collection_mode=collection_mode,
            collection_guidance=collection_guidance,
            sample_unit=sample_unit,
            minimum_samples=minimum_samples,
            feature_type=feature_type,
            aggregation=aggregation,
            merge_strategy=merge_strategy,
        )
        if not dataset.dataset_id or not dataset.purpose:
            raise TraceContractError(f"report template dataset {index} is missing id or purpose")
        datasets.append(dataset)
    dataset_ids = [item.dataset_id for item in datasets]
    if len(dataset_ids) != len(set(dataset_ids)):
        raise TraceContractError(f"report template dataset ids must be unique: {path.name}")

    raw_visuals = metadata.get("visuals", [])
    if not isinstance(raw_visuals, list):
        raise TraceContractError(f"report template visuals must be a list: {path.name}")
    visuals: list[ReportTemplateVisual] = []
    by_dataset = {item.dataset_id: item for item in datasets}
    for index, item in enumerate(raw_visuals):
        if not isinstance(item, dict):
            raise TraceContractError(f"report template visual {index} must be a mapping")
        visual = ReportTemplateVisual(
            visualization_id=str(item.get("id") or "").strip(),
            type=str(item.get("type") or "").strip(),
            title=str(item.get("title") or "").strip(),
            dataset_id=str(item.get("dataset") or "").strip(),
            x_field=str(item.get("x_field") or "").strip(),
            y_field=str(item.get("y_field") or "").strip(),
            fallback=str(item.get("fallback") or "").strip(),
        )
        if not visual.visualization_id or not visual.type or not visual.title or not visual.dataset_id or not visual.fallback:
            raise TraceContractError(f"report template visual {index} is missing required fields")
        if visual.type not in _VISUAL_KINDS:
            raise TraceContractError(f"report template visual {visual.visualization_id} has unsupported type: {visual.type}")
        dataset = by_dataset.get(visual.dataset_id)
        if dataset is None:
            raise TraceContractError(f"report template visual {visual.visualization_id} references unknown dataset: {visual.dataset_id}")
        for field in (visual.x_field, visual.y_field):
            if field and field not in dataset.columns:
                raise TraceContractError(f"report template visual {visual.visualization_id} references unknown dataset column: {field}")
        visuals.append(visual)
    visual_ids = [item.visualization_id for item in visuals]
    if len(visual_ids) != len(set(visual_ids)):
        raise TraceContractError(f"report template visual ids must be unique: {path.name}")
    return tuple(datasets), tuple(visuals)


def load_report_template(
    investigation_type: str,
    *,
    template_root: str | Path | None = None,
) -> InvestigationReportTemplate:
    """Load the only report template selected by an investigation type."""

    key = str(investigation_type).strip()
    if not key or not re.fullmatch(r"[a-z0-9_]+", key):
        raise TraceContractError("investigation_type must be a registered lowercase identifier")
    path = Path(template_root or _DEFAULT_TEMPLATE_ROOT) / f"{key}.md"
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TraceContractError(f"report template not found for investigation type: {key}") from exc
    metadata, instructions = _front_matter(source, path)
    trace_template = get_trace_template(key)
    expected = {
        "template_id": f"{key}.report",
        "investigation_type": key,
        "trace_template_id": trace_template.template_id,
        "trace_template_version": trace_template.template_version,
        "language": "zh-CN",
    }
    for field_name, expected_value in expected.items():
        if metadata.get(field_name) != expected_value:
            raise TraceContractError(
                f"report template {path.name} has mismatched {field_name}: "
                f"expected {expected_value!r}, got {metadata.get(field_name)!r}"
            )
    version = str(metadata.get("version") or "").strip()
    if not version:
        raise TraceContractError(f"report template must define a version: {path.name}")
    known_slots = {item.slot_id for item in trace_template.node_specs}
    chapters = _chapters(metadata, path)
    chapter_headings = re.findall(r"^##\s+(.+?)\s*$", instructions, flags=re.MULTILINE)
    if chapter_headings != [item.title for item in chapters]:
        raise TraceContractError(f"report template chapter headings do not match metadata order: {path.name}")
    chapters = _chapter_subheadings(instructions, chapters, path)
    datasets, visuals = _datasets_and_visuals(metadata, known_slots=known_slots, path=path)
    referenced_slots = {slot for chapter in chapters for slot in chapter.node_slots}
    unknown_slots = referenced_slots - known_slots
    if unknown_slots:
        raise TraceContractError(f"report template references unknown node slots: {sorted(unknown_slots)}")
    uncovered_slots = known_slots - referenced_slots
    if uncovered_slots:
        raise TraceContractError(f"report template does not cover trace node slots: {sorted(uncovered_slots)}")
    return InvestigationReportTemplate(
        template_id=expected["template_id"],
        version=version,
        investigation_type=key,
        trace_template_id=trace_template.template_id,
        trace_template_version=trace_template.template_version,
        language="zh-CN",
        chapters=chapters,
        datasets=datasets,
        visuals=visuals,
        instructions=instructions,
        content_hash=sha256(source.encode("utf-8")).hexdigest(),
    )


__all__ = [
    "InvestigationReportTemplate", "ReportTemplateChapter", "ReportTemplateSubheading", "ReportTemplateDataset",
    "ReportTemplateVisual", "load_report_template",
]
