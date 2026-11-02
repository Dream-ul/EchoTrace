"""Read models for the node-driven investigation workbench."""

from __future__ import annotations

from echotrace.application.node_trace import NodeTrace, build_node_trace_blocks
from echotrace.domain.models import InvestigationRun


class InvestigationProjection:
    WORKFLOW_STAGES = (
        ("intake", "调查接收"),
        ("node_plan.created", "节点任务编排"),
        ("node_wave.completed", "节点研究与审稿"),
        ("node_trace", "冻结调查脉络"),
        ("report", "生成报告"),
    )
    LIFECYCLE_STEPS = {"run", "resume", "pause", "cancel", "intake"}
    _FAILURE_STATUSES = {"failed", "error", "blocked", "retry_exhausted"}

    @staticmethod
    def node_trace(run: InvestigationRun) -> dict | None:
        value = (run.graph_state or {}).get("node_trace")
        return dict(value) if isinstance(value, dict) else None

    @staticmethod
    def cross_node_review(run: InvestigationRun) -> dict | None:
        value = (run.graph_state or {}).get("cross_node_review")
        return dict(value) if isinstance(value, dict) else None

    @classmethod
    def node_trace_blocks(cls, run: InvestigationRun) -> list[dict]:
        value = cls.node_trace(run)
        if not value:
            return []
        trace = NodeTrace(
            trace_id=str(value.get("trace_id") or ""),
            run_id=str(value.get("run_id") or run.run_id),
            investigation_type=str(value.get("investigation_type") or run.investigation_type),
            subject=str(value.get("subject") or run.subject or run.event.title),
            template_id=str(value.get("template_id") or ""),
            template_version=str(value.get("template_version") or ""),
            nodes=tuple(value.get("nodes") or ()),
            edges=tuple(value.get("edges") or ()),
        )
        return build_node_trace_blocks(trace)

    @staticmethod
    def timeline(run: InvestigationRun) -> list[dict]:
        return sorted(run.trace, key=lambda item: int(item.get("seq", 0)))

    @classmethod
    def workflow_stages(cls, run: InvestigationRun) -> list[dict]:
        events = cls.timeline(run)
        latest = {str(event.get("step")): event for event in events}
        result: list[dict] = []
        for key, label in cls.WORKFLOW_STAGES:
            event = latest.get(key)
            if event is None:
                result.append({"key": key, "label": label, "status": "pending", "started_at": None, "completed_at": None, "error": None})
                continue
            status = str(event.get("status") or "running")
            normalized = "failed" if status in cls._FAILURE_STATUSES else ("completed" if status in {"completed", "succeeded"} else "running")
            result.append({
                "key": key, "label": label, "status": normalized,
                "started_at": event.get("at"),
                "completed_at": event.get("at") if normalized == "completed" else None,
                "error": event.get("error") if normalized == "failed" else None,
            })
        return result

    @classmethod
    def lifecycle_events(cls, run: InvestigationRun) -> list[dict]:
        return [event for event in cls.timeline(run) if str(event.get("step")) in cls.LIFECYCLE_STEPS]

    @classmethod
    def audit_events(cls, run: InvestigationRun) -> list[dict]:
        stage_keys = {key for key, _ in cls.WORKFLOW_STAGES}
        return [event for event in cls.timeline(run) if str(event.get("step")) not in stage_keys and str(event.get("step")) not in cls.LIFECYCLE_STEPS]

    @classmethod
    def workbench_timeline(cls, run: InvestigationRun) -> list[dict]:
        return cls.lifecycle_events(run) + [
            {"step": item["key"], "label": item["label"], "status": item["status"], "at": item["started_at"], "error": item["error"]}
            for item in cls.workflow_stages(run)
        ]

    @staticmethod
    def source_clusters(run: InvestigationRun) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for document in run.documents:
            key = str((document.metadata or {}).get("source_cluster") or document.source or "unknown")
            groups.setdefault(key, []).append({"document_id": document.document_id, "title": document.title, "source": document.source, "url": document.url})
        return [{"cluster_id": key, "documents": documents, "document_count": len(documents)} for key, documents in sorted(groups.items())]

    @staticmethod
    def skill_audit(run: InvestigationRun) -> list[dict]:
        return [{key: item[key] for key in ("seq", "step", "status", "skill", "skill_hash", "prompt_version") if key in item} for item in run.trace if item.get("skill") or item.get("skill_hash")]

    @staticmethod
    def tool_audit(run: InvestigationRun) -> list[dict]:
        fields = ("seq", "step", "status", "capability", "provider", "agent", "error_code", "duration_ms", "attempt", "error")
        return [{key: item[key] for key in fields if key in item} for item in run.trace if item.get("step") == "provider_call" or item.get("capability")]


__all__ = ["InvestigationProjection"]
