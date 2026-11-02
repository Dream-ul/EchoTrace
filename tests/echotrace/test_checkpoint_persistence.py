import json

from echotrace.domain.models import Document, Event, InvestigationRun
from echotrace.infrastructure.persistence.mysql import SqlAlchemyUnitOfWork, create_database
from echotrace.infrastructure.persistence.mysql.models import RunCheckpoint


def test_checkpoint_keeps_document_refs_and_bounded_trace_without_source_bodies():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    try:
        run = InvestigationRun(
            objective="checkpoint size",
            event=Event(title="checkpoint", objective="checkpoint size"),
            state_node="node_tasks_pending",
            documents=[
                Document(
                    source="web_search",
                    title=f"source {index}",
                    content="full source body " * 1000,
                    url=f"https://example.test/source/{index}",
                    metadata={"external_id": str(index)},
                )
                for index in range(40)
            ],
        )
        for index in range(100):
            run.add_trace("provider_call", "succeeded", response="provider output " * 1000, step_no=index)

        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.runs.save(run)
            uow.session.flush()
            row = uow.session.query(RunCheckpoint).filter_by(run_id=run.run_id).one()
            raw = row.state_json
            state = json.loads(raw) if isinstance(raw, str) else raw

            assert len(json.dumps(state, ensure_ascii=False)) < 250_000
            assert state["document_count"] == 40
            assert len(state["documents"]) == 40
            assert all(set(item) >= {"document_id", "content_hash", "url", "title"} for item in state["documents"])
            assert all("content" not in item for item in state["documents"])
            assert state["trace_count"] == 100
            assert len(state["trace"]) == 64
            assert all(len(item["response"]) <= 2100 for item in state["trace"])
    finally:
        database.close()


def test_new_checkpoint_supersedes_stale_runtime_state_node():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    try:
        run = InvestigationRun(
            objective="checkpoint precedence",
            event=Event(title="checkpoint", objective="checkpoint precedence"),
            state_node="provider_call",
            checkpoint="report_completed",
        )
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.runs.save(run)
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            loaded = uow.runs.get(run.run_id)
            assert loaded is not None
            assert loaded.checkpoint == "report_completed"
            assert loaded.state_node == "report_completed"
    finally:
        database.close()
