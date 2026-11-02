from datetime import datetime, timedelta
from hashlib import sha256

import pytest

from echotrace.application import InvestigationTaskService
from echotrace.application.investigation_service import TaskDeferredError
from echotrace.domain import ConclusionIR, NodeTaskPlan, TraceContractError, get_trace_template
from echotrace.domain.models import Document, Event, InvestigationRun, InvestigationStatus
from echotrace.infrastructure.persistence.mysql import SqlAlchemyUnitOfWork, create_database
from echotrace.infrastructure.persistence.mysql.models import InvestigationNode, InvestigationTask, NodeRevision as NodeRevisionRow, OutboxEvent, TaskAttempt, TaskDependency, TaskPlan
from echotrace.runtime import NodeTaskPlanner


@pytest.fixture
def context():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    template = get_trace_template("public_event")
    run = InvestigationRun(
        objective="研究公共事件", event=Event(title="事件", objective="研究公共事件"),
        investigation_type="public_event",
        documents=[Document(source="official", title="公告", content="公告内容", url="https://example.test/source")],
    )
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        uow.runs.save(run)
        nodes = uow.trace_nodes.initialize_nodes(run_id=run.run_id, template=template)
    identities = {node["slot_id"]: node["node_id"] for node in nodes}
    try:
        yield database, run, template, identities
    finally:
        database.close()


def _plan(run, template, identities):
    proposal = {
        "template_id": template.template_id, "template_version": template.template_version,
        "tasks": [{"slot_id": spec.slot_id, "question": spec.goal, "capability": "search_web", "priority": spec.order} for spec in template.node_specs],
    }
    return NodeTaskPlanner(lambda *args: proposal).plan(
        run_id=run.run_id, objective=run.objective, template=template,
        node_ids=identities, available_capabilities=["search_web"],
        input_refs={spec.slot_id: [run.documents[0].document_id] for spec in template.node_specs},
    )


def test_node_plan_submission_is_atomic_idempotent_and_restart_readable(context):
    database, run, template, identities = context
    plan = _plan(run, template, identities)
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    first = service.submit_node_plan(plan=plan, template=template, available_capabilities=["search_web"])
    repeated = service.submit_node_plan(plan=plan, template=template, available_capabilities=["search_web"])
    assert first == repeated
    assert service.redrive_pending_tasks() == ()
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        assert uow.session.query(TaskPlan).count() == 1
        ready_slots = {spec.slot_id for spec in template.node_specs if not spec.depends_on_slot_ids}
        assert uow.session.query(InvestigationTask).count() == len(ready_slots)
        assert uow.session.query(OutboxEvent).count() == len(ready_slots)
        row = uow.session.get(TaskPlan, first.plan_id)
        restored = NodeTaskPlan.from_dict(row.definition_json)
        assert restored.to_dict() == plan.to_dict()
        assert service.get_node_plan(run.run_id).to_dict() == plan.to_dict()
        for expected in (item for item in plan.tasks if item.slot_id in ready_slots):
            task = uow.tasks.get_task(expected.task_id)
            assert task["node_id"] == identities[expected.slot_id]
            assert task["slot_id"] == expected.slot_id and task["node_revision"] == 0
            assert task["input_refs"] == [run.documents[0].document_id]
            assert task["input"]["task_type"] == "node.research.initial"
            assert task["input"]["aspects"] == list(expected.aspects)
            assert task["budget"]["deadline_seconds"] >= 60
            assert task["budget"]["deadline_epoch"] > 0
            assert "max_search_calls" not in task["budget"]
        events = uow.runs.events(run.run_id)
        assert sum(event["step"] == "node_plan.created" for event in events) == 1
        notifications = uow.session.query(OutboxEvent).all()
        assert {item.event_type for item in notifications} == {"node.research.initial"}
        assert {item.aggregate_id for item in notifications} == set(first.task_ids)


def test_submission_rejects_nodes_belonging_to_another_run_before_writing(context):
    database, run, template, identities = context
    identities["origin_early_signals"] = "another-runs-node"
    plan = _plan(run, template, identities)
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    with pytest.raises(TraceContractError, match="assigned node"):
        service.submit_node_plan(plan=plan, template=template, available_capabilities=["search_web"])
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        assert uow.session.query(TaskPlan).count() == 0
        assert uow.session.query(InvestigationTask).count() == 0


def test_frozen_revision_can_refresh_only_its_derived_conclusion_ir(context):
    database, run, _template, identities = context
    node_id = identities["origin_early_signals"]
    conclusion = "旧版展示结论，包含详见 nclaim_internal。"
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        node = uow.session.get(InvestigationNode, node_id)
        node.status = "frozen"
        node.current_revision = 0
        node.frozen_revision = 0
        uow.session.add(NodeRevisionRow(
            revision_id="nrev-render-refresh",
            node_id=node_id,
            revision_no=0,
            conclusion=conclusion,
            draft_json={"node_id": node_id, "conclusion": conclusion, "claims": [{"text": "原有主张"}], "conclusion_ir": {"old": True}},
        ))

    ir = ConclusionIR(
        schema_version="conclusion-ir.v1",
        blocks=[{"type": "heading", "level": 2, "text": "研究结论"}, {"type": "paragraph", "text": "润色后的展示结论。"}],
        source_hash=sha256(conclusion.encode("utf-8")).hexdigest(),
    )
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        uow.trace_nodes.update_frozen_conclusion_ir(node_id, 0, ir)

    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        stored = uow.trace_nodes.get_revision(node_id, 0)
        assert stored["conclusion"] == conclusion
        assert stored["draft"]["claims"] == [{"text": "原有主张"}]
        assert stored["draft"]["conclusion_ir"] == ir.to_dict()


def test_changed_definition_cannot_reuse_a_submitted_revision_and_rolls_back(context):
    database, run, template, identities = context
    plan = _plan(run, template, identities)
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    first = service.submit_node_plan(plan=plan, template=template, available_capabilities=["search_web"])
    payload = plan.to_dict()
    payload["tasks"][0]["question"] = "改变已经提交的研究任务。"
    with pytest.raises(ValueError, match="idempotency key"):
        service.submit_node_plan(plan=NodeTaskPlan.from_dict(payload), template=template, available_capabilities=["search_web"])
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        assert uow.session.query(TaskPlan).count() == 1
        assert uow.session.query(InvestigationTask).count() == len(
            [spec for spec in template.node_specs if not spec.depends_on_slot_ids]
        )
        assert uow.runs.get(run.run_id).plan_id == first.plan_id


def test_existing_node_plan_id_is_idempotent_when_runtime_budget_changes(context, monkeypatch):
    database, run, template, identities = context
    plan = _plan(run, template, identities)
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    first = service.submit_node_plan(plan=plan, template=template, available_capabilities=["search_web"])
    monkeypatch.setenv("ECHOTRACE_NODE_TASK_DEADLINE_SECONDS", "7200")
    second = service.ensure_node_tasks(plan=plan, template=template)
    assert second.plan_id == first.plan_id
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        assert uow.session.query(TaskPlan).count() == 1


def test_explicit_retry_of_failed_node_task_gets_fresh_deadline(context, monkeypatch):
    database, run, template, identities = context
    monkeypatch.setenv("ECHOTRACE_NODE_TASK_DEADLINE_SECONDS", "1200")
    plan = _plan(run, template, identities)
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    submission = service.submit_node_plan(plan=plan, template=template, available_capabilities=["search_web"])
    task_id = submission.task_ids[0]
    original = service.get_task(task_id)
    assert original is not None

    attempt = service.start_task(task_id=task_id, worker_id="researcher-test")
    assert service.finish_task(
        attempt_id=attempt["attempt_id"],
        status="failed",
        error_code="agent_task_timeout",
        error_summary="task deadline exceeded",
        worker_id="researcher-test",
    )

    monkeypatch.setenv("ECHOTRACE_NODE_TASK_DEADLINE_SECONDS", "3600")
    retry = service.ensure_node_tasks(plan=plan, template=template, retry_failed=True)
    recovered = service.get_task(task_id)
    assert recovered is not None
    assert task_id in retry.task_ids
    assert recovered["status"] == "failed"
    assert recovered["budget"]["deadline_seconds"] == 3600
    assert recovered["budget"]["deadline_epoch"] > original["budget"]["deadline_epoch"]
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        attempts = uow.tasks.get_attempts(task_id)
    assert [row["status"] for row in attempts] == ["failed"]

    next_attempt = service.start_task(task_id=task_id, worker_id="researcher-test")
    assert next_attempt["attempt_no"] == 2


def test_redelivered_failed_node_task_gets_fresh_deadline_on_start(context, monkeypatch):
    database, run, template, identities = context
    monkeypatch.setenv("ECHOTRACE_NODE_TASK_DEADLINE_SECONDS", "1200")
    plan = _plan(run, template, identities)
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    submission = service.submit_node_plan(plan=plan, template=template, available_capabilities=["search_web"])
    task_id = submission.task_ids[0]
    original = service.get_task(task_id)
    assert original is not None

    attempt = service.start_task(task_id=task_id, worker_id="researcher-test")
    assert service.finish_task(
        attempt_id=attempt["attempt_id"],
        status="failed",
        error_code="step_limit",
        error_summary="agent step budget exhausted",
        worker_id="researcher-test",
    )

    monkeypatch.setenv("ECHOTRACE_NODE_TASK_DEADLINE_SECONDS", "3600")
    next_attempt = service.start_task(task_id=task_id, worker_id="researcher-retry")
    retried = service.get_task(task_id)
    assert retried is not None
    assert retried["budget"]["deadline_seconds"] == 3600
    assert retried["budget"]["deadline_epoch"] > original["budget"]["deadline_epoch"]
    assert next_attempt["attempt_no"] == 2


def test_pause_defers_running_attempt_and_blocks_new_attempt_until_resume(context):
    database, run, template, identities = context
    run.status = InvestigationStatus.RUNNING
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        uow.runs.save(run)
    plan = _plan(run, template, identities)
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    submission = service.submit_node_plan(plan=plan, template=template, available_capabilities=["search_web"])
    task_id = submission.task_ids[0]
    attempt = service.start_task(task_id=task_id, worker_id="researcher-test")

    service.pause_run(run_id=run.run_id, reason="人工暂停")
    assert service.finish_task(
        attempt_id=attempt["attempt_id"], status="succeeded", worker_id="researcher-test",
    ) is False
    assert service.get_task(task_id)["status"] == "pending"
    assert service.get_task(task_id)["lease_owner"] is None
    with pytest.raises(TaskDeferredError):
        service.start_task(task_id=task_id, worker_id="researcher-test")

    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        assert [item["status"] for item in uow.tasks.get_attempts(task_id)] == ["cancelled"]
        resumed = uow.runs.get(run.run_id)
        resumed.status = InvestigationStatus.RUNNING
        resumed.error = None
        resumed.interrupt_reason = None
        uow.runs.save(resumed)

    resumed_attempt = service.start_task(task_id=task_id, worker_id="researcher-test")
    assert resumed_attempt["attempt_no"] == 2


def test_report_task_graph_is_idempotent_and_sections_depend_on_plan(context):
    database, run, template, identities = context
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        for node in uow.session.query(InvestigationNode).filter(InvestigationNode.run_id == run.run_id).all():
            node.status = "frozen"
        investigation_plan_id = uow.tasks.add_plan(
            run_id=run.run_id, objective="调查计划", allowed_agents=["orchestrator"],
            allowed_capabilities=["plan_investigation"], budget={}, stop_conditions=[],
        )
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    chapter_ids = ["summary", "sources"]
    first = service.submit_report_tasks(
        run_id=run.run_id, report_id="report-1", template_id="public_event.report",
        template_version="1.0", chapter_ids=chapter_ids, trace_id="trace-1",
    )
    assert service.submit_report_tasks(
        run_id=run.run_id, report_id="report-1", template_id="public_event.report",
        template_version="1.0", chapter_ids=chapter_ids, trace_id="trace-1",
    ) == first
    report_tasks = service.get_report_tasks(run.run_id)
    assert [(item["stage"], item["slot_id"], item["status"]) for item in report_tasks] == [
        ("report_plan", "report_plan", "pending"),
        ("report_section", "summary", "pending"),
        ("report_section", "sources", "pending"),
    ]
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        tasks = uow.session.query(InvestigationTask).filter(InvestigationTask.plan_id == first.plan_id).all()
        assert {task.stage for task in tasks} == {"report_plan", "report_section"}
        assert uow.session.query(TaskDependency).count() == 2
        assert uow.session.query(OutboxEvent).filter(OutboxEvent.event_type == "report.plan").count() == 1
        assert uow.session.get(TaskPlan, investigation_plan_id).status == "active"
        assert uow.session.get(TaskPlan, first.plan_id).status == "proposed"
        assert uow.runs.get(run.run_id).plan_id == investigation_plan_id
        plan_task = next(task for task in tasks if task.stage == "report_plan")
        section_task = next(task for task in tasks if task.stage == "report_section")
        assert plan_task.node_id is None
        assert section_task.node_id is None
        section_id = section_task.task_id
        plan_id = plan_task.task_id
    with pytest.raises(ValueError, match="dependencies are not complete") as error:
        service.start_task(task_id=section_id, worker_id="report-worker")
    assert getattr(error.value, "not_ready", False) is True
    attempt = service.start_task(task_id=plan_id, worker_id="report-worker")
    assert attempt["task_id"] == plan_id


def test_explicit_report_resume_redrives_failed_section_with_new_state_version(context):
    database, run, template, identities = context
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        for node in uow.session.query(InvestigationNode).filter(InvestigationNode.run_id == run.run_id).all():
            node.status = "frozen"
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    kwargs = {
        "run_id": run.run_id, "report_id": "report-retry", "template_id": "public_event.report",
        "template_version": "1.0", "chapter_ids": ["summary"], "trace_id": "trace-retry",
    }
    submission = service.submit_report_tasks(**kwargs)
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        tasks = uow.session.query(InvestigationTask).filter_by(plan_id=submission.plan_id).all()
        plan_task = next(task for task in tasks if task.stage == "report_plan")
        section_task = next(task for task in tasks if task.stage == "report_section")
        plan_task_id, section_task_id = plan_task.task_id, section_task.task_id

    plan_attempt = service.start_task(task_id=plan_task_id, worker_id="report-worker")
    assert service.finish_task(attempt_id=plan_attempt["attempt_id"], status="succeeded", worker_id="report-worker")
    section_attempt = service.start_task(task_id=section_task_id, worker_id="report-worker")
    assert service.finish_task(
        attempt_id=section_attempt["attempt_id"], status="failed", worker_id="report-worker",
        error_code="contract_error", error_summary="invalid report section",
    )

    retried = service.submit_report_tasks(**kwargs, retry_failed=True)
    assert retried.plan_id == submission.plan_id
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        task = uow.tasks.get_task(section_task_id)
        assert task is not None and task["status"] == "failed" and task["state_version"] == 3
        attempt_rows = uow.session.query(TaskAttempt).filter_by(task_id=section_task_id).all()
        assert [(item.attempt_no, item.status) for item in attempt_rows] == [(1, "failed")]
        retry_event = uow.session.query(OutboxEvent).filter_by(aggregate_id=section_task_id, event_type="node.task.redrive").one()
        assert retry_event.payload_json["state_version"] == task["state_version"]
        assert retry_event.payload_json["task_type"] == "report.section"


def test_explicit_report_resume_redrives_failed_plan_with_new_state_version(context):
    database, run, template, identities = context
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        for node in uow.session.query(InvestigationNode).filter(InvestigationNode.run_id == run.run_id).all():
            node.status = "frozen"
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    kwargs = {
        "run_id": run.run_id, "report_id": "report-plan-retry", "template_id": "public_event.report",
        "template_version": "1.0", "chapter_ids": ["summary"], "trace_id": "trace-plan-retry",
    }
    submission = service.submit_report_tasks(**kwargs)
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        plan_task_id = next(
            task.task_id for task in uow.session.query(InvestigationTask).filter_by(plan_id=submission.plan_id).all()
            if task.stage == "report_plan"
        )

    attempt = service.start_task(task_id=plan_task_id, worker_id="report-worker")
    assert service.finish_task(
        attempt_id=attempt["attempt_id"], status="failed", worker_id="report-worker",
        error_code="contract_error", error_summary="invalid report plan",
    )

    retried = service.submit_report_tasks(**kwargs, retry_failed=True)
    assert retried.plan_id == submission.plan_id
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        task = uow.tasks.get_task(plan_task_id)
        assert task is not None and task["status"] == "failed" and task["state_version"] == 3
        attempt_rows = uow.session.query(TaskAttempt).filter_by(task_id=plan_task_id).all()
        assert [(item.attempt_no, item.status) for item in attempt_rows] == [(1, "failed")]
        retry_event = uow.session.query(OutboxEvent).filter_by(
            aggregate_id=plan_task_id, event_type="node.task.redrive",
        ).one()
        assert retry_event.payload_json["state_version"] == task["state_version"]
        assert retry_event.payload_json["task_type"] == "report.plan"


def test_pending_report_tasks_rebuild_report_composer_envelopes(context):
    database, run, template, identities = context
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        for node in uow.session.query(InvestigationNode).filter(InvestigationNode.run_id == run.run_id).all():
            node.status = "frozen"
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    submission = service.submit_report_tasks(
        run_id=run.run_id, report_id="report-redrive", template_id="public_event.report",
        template_version="1.0", chapter_ids=["summary"], trace_id="trace-redrive",
    )
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        section = next(
            task for task in uow.session.query(InvestigationTask).filter_by(plan_id=submission.plan_id).all()
            if task.stage == "report_section"
        )
        envelope = service._task_envelope(uow.tasks.get_task(section.task_id))

    assert envelope["task_type"] == "report.section"
    assert envelope["agent"] == "report_composer"
    assert envelope["node_id"] == "report-redrive"
    assert envelope["slot_id"] == "summary"


def test_task_heartbeat_renews_authoritative_lease_and_fences_other_worker(context):
    database, run, template, identities = context
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        for node in uow.session.query(InvestigationNode).filter(InvestigationNode.run_id == run.run_id).all():
            node.status = "frozen"
    service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    plan = service.submit_report_tasks(
        run_id=run.run_id, report_id="report-heartbeat", template_id="public_event.report",
        template_version="1.0", chapter_ids=["summary"], trace_id="trace-heartbeat",
    )
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        task_id = next(
            task.task_id for task in uow.session.query(InvestigationTask).filter_by(plan_id=plan.plan_id).all()
            if task.stage == "report_plan"
        )
    attempt = service.start_task(task_id=task_id, worker_id="worker-a", lease_seconds=1)
    original_lease = datetime.fromisoformat(service.get_task(task_id)["lease_until"])

    assert service.heartbeat_attempt(
        attempt_id=attempt["attempt_id"], worker_id="worker-b", lease_seconds=60,
    ) is False
    assert service.heartbeat_attempt(
        attempt_id=attempt["attempt_id"], worker_id="worker-a", lease_seconds=60,
    ) is True
    with SqlAlchemyUnitOfWork(database.session_factory) as uow:
        renewed_lease = datetime.fromisoformat(uow.tasks.get_task(task_id)["lease_until"])
        assert renewed_lease > original_lease + timedelta(seconds=30)
        assert uow.tasks.recover_expired(now=original_lease + timedelta(seconds=2)) == []

    assert service.finish_task(
        attempt_id=attempt["attempt_id"], status="succeeded", worker_id="worker-b",
    ) is False
    assert service.finish_task(
        attempt_id=attempt["attempt_id"], status="succeeded", worker_id="worker-a",
    ) is True
