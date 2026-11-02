"""Application use-cases and transaction boundaries for EchoTrace."""

from .investigation_service import InvestigationTaskService, PlanSubmission, TaskNotReadyError
from .node_trace import NodeTrace, assemble_frozen_trace, assemble_node_trace, build_node_trace_blocks, refresh_frozen_trace
from .node_review import CrossNodeFinding, CrossNodeReview, review_node_trace
from .report_templates import (
    InvestigationReportTemplate,
    ReportTemplateChapter,
    ReportTemplateDataset,
    ReportTemplateVisual,
    load_report_template,
)
from .report_composer import (
    ReportRoleRunner, assemble_report, build_report_material_pack, compose_report,
    generate_report_section, plan_report,
)
from .report_renderers import render_report_html, render_report_json, render_report_markdown, render_report_pdf

__all__ = [
    "InvestigationTaskService",
    "PlanSubmission",
    "TaskNotReadyError",
    "NodeTrace",
    "assemble_frozen_trace",
    "assemble_node_trace",
    "build_node_trace_blocks",
    "refresh_frozen_trace",
    "CrossNodeFinding",
    "CrossNodeReview",
    "review_node_trace",
    "InvestigationReportTemplate",
    "ReportTemplateChapter",
    "ReportTemplateDataset",
    "ReportTemplateVisual",
    "load_report_template",
    "ReportRoleRunner",
    "build_report_material_pack",
    "compose_report",
    "plan_report",
    "generate_report_section",
    "assemble_report",
    "render_report_html",
    "render_report_json",
    "render_report_markdown",
    "render_report_pdf",
]
