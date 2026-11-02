from __future__ import annotations

from types import SimpleNamespace

import pytest

from echotrace.application import InvestigationTaskService, load_report_template
from echotrace.domain.models import Document, Event, InvestigationRun, InvestigationStatus
from echotrace.domain.trace_nodes import TraceContractError, get_trace_template, list_trace_templates
from echotrace.infrastructure.coordination.redis import (
    InMemoryRedis,
    OutboxDispatcher,
    OrchestratorRunWorker,
    RedisStreamBus,
    ReportComposerWorker,
    StreamTaskWorker,
)
from echotrace.infrastructure.persistence.mysql import (
    SqlAlchemyRunStore,
    SqlAlchemyTraceNodeStore,
    SqlAlchemyUnitOfWork,
    create_database,
)
from echotrace.infrastructure.persistence.mysql.models import InvestigationTask
from echotrace.runtime.node_controller import NodeController, NodeControllerPolicy
from echotrace.runtime.node_planner import NodeTaskPlanner
from echotrace.runtime.node_workflow import NodeInvestigationController
from echotrace.runtime.node_worker import NodeRoleTaskHandler


def test_auto_type_orchestrator_failure_is_retryable():
    run = InvestigationRun(
        objective="调查事件",
        event=Event(title="事件", objective="调查事件"),
        investigation_type="auto",
        status=InvestigationStatus.RUNNING,
        documents=[Document(source="news", title="来源", content="正文")],
    )

    class ProviderFailure(RuntimeError):
        error_code = "provider_timeout"
        retryable = False

    class RunStore:
        def get(self, run_id):
            return run if run_id == run.run_id else None

        def save(self, value, **_kwargs):
            return value

    controller = SimpleNamespace(
        bind_run=lambda _run: None,
        planner=SimpleNamespace(
            classify=lambda **_kwargs: (_ for _ in ()).throw(ProviderFailure("timeout")),
        ),
    )
    workflow = NodeInvestigationController(
        controller=controller,
        run_store=RunStore(),
        node_store=SimpleNamespace(list_nodes=lambda _run_id: []),
    )

    with pytest.raises(TraceContractError, match="automatic classification failed") as caught:
        workflow.run(run)

    assert caught.value.retryable is True
    assert caught.value.error_code == "provider_timeout"
    assert any(event.get("step") == "classification" and event.get("status") == "failed" for event in run.trace)


def test_role_streams_complete_node_rounds_unlock_dependencies_and_enqueue_report():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    template = get_trace_template("public_event")
    run = InvestigationRun(
        objective="调查一项公开事件",
        event=Event(title="公开事件", objective="调查一项公开事件"),
        investigation_type="auto",
        documents=[Document(
            source="official", title="情况说明", content="有关部门发布情况说明。",
            url="https://example.test/notice",
        )],
    )
    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.runs.save(run)
        run_store = SqlAlchemyRunStore(database.session_factory)
        node_store = SqlAlchemyTraceNodeStore(database.session_factory)
        task_service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
        calls = {"researcher": 0, "verifier": 0, "report_plan": 0, "report_sections": 0}
        orchestrator_task_ids = []
        classification_contexts = []

        def orchestrator(_role, _objective, context):
            if context.get("classification") is True:
                classification_contexts.append(context)
                return {
                    "investigation_type": "public_event",
                    "rationale": "调查目标聚焦单一公共事件及其发展过程。",
                }
            orchestrator_task_ids.append(context["execution_task_id"])
            return {
                "template_id": template.template_id,
                "template_version": template.template_version,
                "tasks": [
                    {
                        "slot_id": spec.slot_id,
                        "question": spec.goal,
                        "capability": "search_web",
                        "priority": spec.order,
                    }
                    for spec in template.node_specs
                ],
            }

        def researcher(_role, _objective, context):
            calls["researcher"] += 1
            task = context["task"]
            document_id = context["source_documents"][0]["document_id"]
            claim_id = f"claim-{task['revision']}"
            evidence_id = f"evidence-{task['revision']}"
            return {
                "node_id": task["node_id"],
                "slot_id": task["slot_id"],
                "revision": task["revision"],
                "claims": [{
                    "claim_id": claim_id,
                    "claim_type": "fact",
                    "text": f"第 {task['revision']} 轮归纳的事实主张。",
                    "source_document_ids": [document_id],
                    "evidence_ids": [evidence_id],
                }],
                "evidence": [{
                    "evidence_id": evidence_id,
                    "claim_id": claim_id,
                    "document_id": document_id,
                    "excerpt": "有关部门发布情况说明。",
                    "locator": {"url": "https://example.test/notice"},
                }],
                "conclusion": "材料显示有关部门发布了情况说明。",
                "media": [],
            }

        def verifier(_role, _objective, context):
            calls["verifier"] += 1
            task = context["node_task"]
            review_round = context["review_round"]
            final = review_round == 3
            return {
                "review_id": "model-review-id",
                "node_id": task["node_id"],
                "slot_id": task["slot_id"],
                "revision": review_round,
                "status": "finalize" if final else "revise",
                "covered_aspects": ["主体与行动"],
                "missing_aspects": [] if final else ["处置结果"],
                "redundant_claims": [],
                "unclear_claims": [],
                "evidence_followups": [] if final else ["补充独立来源"],
                "rewrite_instructions": ["按时间顺序组织主张"],
                "next_research_tasks": [] if final else ["检索处置结果"],
                "final_editor_note": "节点内容已完成终审。",
            }

        available = ["search_web"]
        controller = NodeController(
            repository=node_store,
            repository_factory=lambda: SqlAlchemyTraceNodeStore(database.session_factory),
            planner=NodeTaskPlanner(orchestrator, max_parallel_tasks=3),
            researcher=researcher,
            verifier=verifier,
            available_capabilities=available,
            plan_loader=task_service.get_node_plan,
            plan_saver=lambda plan, selected: task_service.submit_node_plan(
                plan=plan, template=selected, available_capabilities=available,
            ),
            policy=NodeControllerPolicy(max_role_attempts=1),
        )
        workflow = NodeInvestigationController(
            controller=controller,
            run_store=run_store,
            node_store=node_store,
            report_task_submitter=lambda selected_run, trace, report_template: task_service.submit_report_tasks(
                run_id=selected_run.run_id,
                report_id=f"report_{selected_run.run_id}",
                template_id=report_template.template_id,
                template_version=report_template.version,
                chapter_ids=[chapter.chapter_id for chapter in report_template.chapters],
                trace_id=trace.trace_id,
            ),
        )
        workflow.node_task_submitter = lambda plan, selected: task_service.ensure_node_tasks(
            plan=plan, template=selected,
        )
        redis_bus = RedisStreamBus(InMemoryRedis())
        dispatcher = OutboxDispatcher(
            lambda: SqlAlchemyUnitOfWork(database.session_factory), redis_bus,
        )
        node_handler = NodeRoleTaskHandler(
            controller=controller,
            run_store=run_store,
            node_store=node_store,
            task_service=task_service,
            workflow=workflow,
        )
        researcher_worker = StreamTaskWorker(
            redis_bus, task_service, node_handler, role="researcher", consumer="researcher-1", lease_seconds=3,
        )
        verifier_worker = StreamTaskWorker(
            redis_bus, task_service, node_handler, role="verifier", consumer="verifier-1", lease_seconds=3,
        )

        def report_plan_handler(message):
            calls["report_plan"] += 1
            assert message.task_type == "report.plan"
            return {"output_refs": [f"report-plan:{message.node_id}"]}

        def report_section_handler(message):
            calls["report_sections"] += 1
            assert message.task_type == "report.section"
            return {"output_refs": [f"report-section:{message.slot_id}"]}

        report_worker = ReportComposerWorker(
            redis_bus,
            task_service,
            plan_handler=report_plan_handler,
            section_handler=report_section_handler,
            consumer="report-1",
            lease_seconds=3,
        )

        orchestrator_worker = OrchestratorRunWorker(
            redis_bus,
            lambda run_id, execution_task_id: workflow.run(
                run_store.get(run_id), execution_task_id=execution_task_id,
            ),
            consumer="orchestrator-1",
        )
        task_service.enqueue_run_start(run_id=run.run_id)

        for _ in range(200):
            dispatcher.dispatch_once(owner="test-dispatcher", limit=100)
            orchestrator_worker.process_once(count=10)
            researcher_worker.process_once(count=100)
            verifier_worker.process_once(count=100)
            report_worker.process_once(count=100)
            if calls["report_sections"] and calls["report_sections"] == len(load_report_template("public_event").chapters):
                break

        nodes = node_store.list_nodes(run.run_id)
        assert len(nodes) == len(template.node_specs)
        assert all(node["status"] == "frozen" for node in nodes)
        assert calls["researcher"] == len(template.node_specs) * 4
        assert calls["verifier"] == len(template.node_specs) * 3
        assert calls["report_plan"] == 1
        assert calls["report_sections"] == len(load_report_template("public_event").chapters)
        assert len(orchestrator_task_ids) == 1 and orchestrator_task_ids[0].startswith("outbox_")
        assert len(classification_contexts) == 1
        assert [item["investigation_type"] for item in classification_contexts[0]["investigation_type_candidates"]] == [
            item.investigation_type for item in list_trace_templates()
        ]
        assert classification_contexts[0]["source_documents"][0]["document_id"] == run.documents[0].document_id

        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            tasks = uow.session.query(InvestigationTask).filter_by(run_id=run.run_id).all()
            assert all(task.status == "succeeded" for task in tasks)
            assert sum(task.agent == "researcher" for task in tasks) == len(template.node_specs) * 4
            assert sum(task.agent == "verifier" for task in tasks) == len(template.node_specs) * 3
            assert sum(task.stage == "report_plan" for task in tasks) == 1
            assert sum(task.stage == "report_section" for task in tasks) == len(load_report_template("public_event").chapters)
            for node in nodes:
                assert [node_store.get_revision(node["node_id"], revision)["revision"] for revision in range(4)] == list(range(4))
                assert all(node_store.get_review(node["node_id"], revision) for revision in range(1, 4))
    finally:
        database.close()
