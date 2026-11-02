from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import investigations
from echotrace.domain.models import Document, Event, InvestigationRun, InvestigationStatus, beijing_now
from echotrace.domain.reporting import ReportBlock, ReportChapter, ReportDocument


def _report(run_id: str) -> dict:
    return ReportDocument(
        report_id=f"report-{run_id}",
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        investigation_type="public_event",
        template_id="public_event.report",
        template_version="1.2",
        template_content_hash="template-hash",
        title="调查报告",
        subtitle="真实材料归纳",
        summary="根据已核验材料形成的结论。",
        chapters=[ReportChapter(
            "summary", "调查结论", 1, "说明核验结果", "结论",
            [ReportBlock("paragraph", {"text": "报告章节内容。"})],
        )],
    ).to_dict()


def _run(run_id: str = "run-1") -> InvestigationRun:
    run = InvestigationRun(
        objective="核查公开事件",
        event=Event(title="公开事件", objective="核查公开事件"),
        investigation_type="public_event",
        subject="事件主体",
        documents=[Document(source="web_search", title="来源正文", content="不应出现在 run 摘要中。", url="https://example.test/a")],
        run_id=run_id,
        status=InvestigationStatus.COMPLETED,
    )
    run.report_document = _report(run_id)
    run.graph_state = {
        "node_trace": {
            "trace_id": f"trace-{run_id}", "run_id": run_id,
            "investigation_type": "public_event", "subject": "事件主体",
            "template_id": "public_event.trace", "template_version": "1.0",
            "nodes": [{"node_id": "node-1", "slot_id": "origin_early_signals", "node_order": 1,
                       "title": "起点与早期信号", "goal": "核实起点", "status": "frozen",
                       "frozen_revision": 3, "draft": {"claims": [], "evidence": [], "media": [], "conclusion": "起点已核实"}}],
            "edges": [],
        },
        "cross_node_review": {"findings": []},
    }
    run.trace = [
        {"seq": index, "step": "agent_turn", "status": "completed", "at": beijing_now().isoformat()}
        for index in range(1, 5)
    ]
    return run


class _RunStore:
    def __init__(self, runs: list[InvestigationRun]):
        self.runs = {run.run_id: run for run in runs}
        self.saved: list[str] = []
        self.event_rows = [
            {"seq": seq, "step": "task", "status": "completed", "at": beijing_now().isoformat()}
            for seq in range(1, 6)
        ]

    def get(self, run_id: str):
        return self.runs.get(run_id)

    def get_summary(self, run_id: str):
        run = self.runs.get(run_id)
        if run is None:
            return None
        return {
            **investigations._summary(run),
            "scope": dict(run.scope), "error": run.error,
            "interrupt_reason": run.interrupt_reason, "state_node": run.state_node,
            "template_id": run.scope.get("template_id"),
            "template_version": run.scope.get("template_version"),
        }

    def graph_state(self, run_id: str):
        run = self.runs.get(run_id)
        return dict(run.graph_state) if run is not None else None

    def report_document(self, run_id: str):
        run = self.runs.get(run_id)
        return dict(run.report_document or {}) if run is not None else None

    def save(self, run: InvestigationRun, **kwargs):
        self.runs[run.run_id] = run
        self.saved.append(run.run_id)
        return run

    def list_summaries(self, *, limit: int, offset: int = 0):
        values = sorted(self.runs.values(), key=lambda run: run.updated_at, reverse=True)
        return [investigations._summary(run) for run in values[offset:offset + limit]]

    def events(self, run_id: str, *, after_seq: int = 0, limit: int | None = None):
        values = [item for item in self.event_rows if item["seq"] > after_seq]
        return values[:limit] if limit is not None else values


class _NodeStore:
    def __init__(self):
        self.rows = [{
            "node_id": "node-1", "run_id": "run-1", "template_id": "public_event.trace",
            "template_version": "1.0", "slot_id": "origin_early_signals", "node_order": 1,
            "template_key": "internal-template-key",
            "instance_no": 1, "status": "frozen", "current_revision": 3,
            "frozen_revision": 3, "current_review_id": "review-3",
        }]
        self.revisions = {
            index: {
                "revision_id": f"revision-{index}", "node_id": "node-1", "revision": index,
                "conclusion": f"第 {index} 版结论", "draft": {"conclusion": f"第 {index} 版结论", "claims": [], "evidence": [], "media": []},
                "review": None if index == 0 else {"revision": index, "status": "finalize" if index == 3 else "revise", "rewrite_instructions": [f"第 {index} 轮意见"]},
                "created_at": beijing_now().isoformat(),
            }
            for index in range(4)
        }

    def list_nodes(self, run_id: str):
        return self.rows if run_id == "run-1" else []

    def get_revision(self, node_id: str, revision_no: int | None = None):
        if node_id != "node-1":
            return None
        return self.revisions.get(revision_no if revision_no is not None else 3)


@pytest.fixture
def api_client():
    run = _run()
    store = _RunStore([run])
    node_store = _NodeStore()
    runtime = SimpleNamespace(
        run_store=store,
        controller=SimpleNamespace(node_store=node_store),
        enqueue_run=lambda *args, **kwargs: "task-1",
        close=lambda: None,
    )
    app = FastAPI()
    app.include_router(investigations.router)
    app.dependency_overrides[investigations.get_investigation_runtime] = lambda: runtime
    with TestClient(app) as client:
        yield client, runtime
    app.dependency_overrides.clear()


def test_investigation_list_is_bounded_and_returns_summary_page(api_client):
    client, _ = api_client
    second = _run("run-2")
    client.app.dependency_overrides[investigations.get_investigation_runtime]().run_store.runs[second.run_id] = second

    response = client.get("/api/investigations?limit=1&offset=0")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["runs"]) == 1
    assert payload["has_more"] is True
    assert payload["next_offset"] == 1
    assert "content" not in payload["runs"][0]
    assert payload["runs"][0]["event"]["title"] == "公开事件"


def test_investigation_detail_excludes_full_documents_trace_and_report(api_client):
    client, _ = api_client

    response = client.get("/api/investigations/run-1")

    assert response.status_code == 200
    payload = response.json()["run"]
    assert payload["template_id"] == "public_event.trace"
    assert "documents" not in payload
    assert "trace" not in payload
    assert "graph_state" not in payload
    assert "report_document" not in payload


def test_node_endpoint_exposes_current_progress_and_review_history(api_client):
    client, _ = api_client

    response = client.get("/api/investigations/run-1/nodes")

    assert response.status_code == 200
    node = response.json()["nodes"][0]
    assert node["title"] == "起点与早期信号"
    assert node["status"] == "frozen"
    assert "template_key" not in node
    assert [item["revision"] for item in node["revision_history"]] == [0, 1, 2, 3]
    assert [item["review"]["revision"] for item in node["revision_history"][1:]] == [1, 2, 3]


def test_trace_endpoint_returns_one_trace_projection_without_duplicate_blocks(api_client):
    client, runtime = api_client
    runtime.controller.node_store.revisions[3]["draft"]["conclusion"] = "编辑后的节点结论"
    runtime.controller.node_store.revisions[3]["draft"]["conclusion_ir"] = {
        "schema_version": "conclusion-ir.v1",
        "source_hash": "test-hash",
        "blocks": [{"type": "list", "ordered": True, "items": ["第一项", "第二项"]}],
    }

    response = client.get("/api/investigations/run-1/trace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["trace"]["trace_id"] == "trace-run-1"
    assert payload["review"] == {"findings": []}
    node = payload["trace"]["nodes"][0]
    assert node["draft"]["conclusion"] == "编辑后的节点结论"
    assert node["draft"]["conclusion_ir"]["blocks"][0]["type"] == "list"
    assert "blocks" not in payload
    assert "node_trace_blocks" not in payload


def test_event_endpoint_uses_bounded_sequence_pages(api_client):
    client, _ = api_client

    response = client.get("/api/investigations/run-1/events?after_seq=1&limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert [event["seq"] for event in payload["events"]] == [2, 3]
    assert payload["has_more"] is True
    assert payload["next_after_seq"] == 3


def test_report_progress_endpoint_exposes_planning_and_chapter_states(api_client):
    client, runtime = api_client
    run = runtime.run_store.runs["run-1"]
    run.report_document = {}
    run.status = InvestigationStatus.RUNNING
    runtime.task_service = SimpleNamespace(get_report_tasks=lambda run_id: [
        {"task_id": "plan", "stage": "report_plan", "slot_id": "report_plan", "status": "succeeded"},
        {"task_id": "section-1", "stage": "report_section", "slot_id": "summary", "status": "succeeded"},
        {"task_id": "section-2", "stage": "report_section", "slot_id": "origin_scope", "status": "running"},
    ])

    response = client.get("/api/investigations/run-1/report/progress")

    assert response.status_code == 200
    payload = response.json()
    assert payload["exists"] is True
    assert payload["status"] == "writing"
    assert payload["completed_sections"] == 1
    assert payload["total_sections"] == 5
    assert payload["sections"][0] == {"chapter_id": "summary", "title": "执行摘要", "status": "completed"}
    assert payload["sections"][1]["status"] == "generating"
    assert payload["sections"][2]["status"] == "queued"


def test_report_progress_endpoint_marks_every_section_complete_with_persisted_report(api_client):
    client, _ = api_client

    response = client.get("/api/investigations/run-1/report/progress")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["completed_sections"] == payload["total_sections"] == 5
    assert {item["status"] for item in payload["sections"]} == {"completed"}


def test_workbench_counts_persisted_active_node_content_without_trace_aliases(api_client):
    client, runtime = api_client
    node_store = runtime.controller.node_store
    node_store.revisions[3]["draft"]["claims"] = [{"claim_id": "claim-1"}]
    node_store.revisions[3]["draft"]["evidence"] = [{"evidence_id": "evidence-1"}]

    response = client.get("/api/investigations/run-1/workbench")

    assert response.status_code == 200
    payload = response.json()["workbench"]
    assert payload["counts"] == {"documents": 1, "claims": 1, "evidence": 1, "nodes": 1, "frozen_nodes": 1}
    assert "trace" not in payload
    assert "node_trace" not in payload
    assert "node_trace_blocks" not in payload
    assert "cross_node_review" not in payload


@pytest.mark.parametrize(
    ("format_name", "content_type"),
    [("json", "application/json"), ("markdown", "text/markdown"), ("html", "text/html")],
)
def test_report_endpoint_uses_the_persisted_report_document(api_client, format_name, content_type):
    client, _ = api_client

    response = client.get(f"/api/investigations/run-1/report?format={format_name}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    if format_name == "json":
        assert response.json()["report"]["report_id"] == "report-run-1"
    else:
        assert "调查报告" in response.text


def test_create_and_run_endpoints_return_compact_run_contract(api_client):
    client, runtime = api_client

    created = client.post("/api/investigations", json={"objective": "新建调查", "investigation_type": "public_event"})
    assert created.status_code == 201
    run_id = created.json()["run"]["run_id"]
    assert "documents" not in created.json()["run"]

    started = client.post(f"/api/investigations/{run_id}/run")
    assert started.status_code == 200
    assert started.json()["run"]["status"] == "running"
    assert runtime.run_store.saved[-1] == run_id


def test_create_defaults_to_ai_classification_when_type_is_omitted(api_client):
    client, _runtime = api_client

    created = client.post("/api/investigations", json={"objective": "调查未指定类别的事件"})

    assert created.status_code == 201
    assert created.json()["run"]["investigation_type"] == "auto"


def test_pause_resume_and_cancel_actions_enforce_run_state(api_client):
    client, runtime = api_client
    run = _run("run-control")
    run.status = InvestigationStatus.RUNNING
    runtime.run_store.runs[run.run_id] = run

    paused = client.post(f"/api/investigations/{run.run_id}/pause", params={"reason": "人工复核"})
    assert paused.status_code == 200
    assert paused.json()["run"]["status"] == "paused"

    resumed = client.post(f"/api/investigations/{run.run_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["run"]["status"] == "running"

    cancelled = client.post(f"/api/investigations/{run.run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["run"]["status"] == "cancelled"
    assert client.post(f"/api/investigations/{run.run_id}/resume").status_code == 409


def test_openapi_describes_report_export_content_types(api_client):
    client, _ = api_client

    schema = client.get("/openapi.json").json()
    response = schema["paths"]["/api/investigations/{run_id}/report"]["get"]["responses"]["200"]

    assert {"application/json", "text/markdown", "text/html", "application/pdf"}.issubset(response["content"])


def test_page_sizes_are_validated_at_the_api_boundary(api_client):
    client, _ = api_client

    assert client.get("/api/investigations?limit=101").status_code == 422
    assert client.get("/api/investigations/run-1/events?limit=501").status_code == 422
    assert client.post("/api/investigations", json={"objective": "调查", "unknown": True}).status_code == 422
