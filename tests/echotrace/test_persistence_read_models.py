from datetime import timedelta

from echotrace.domain.models import Event, InvestigationRun, beijing_now
from echotrace.infrastructure.persistence.mysql import SqlAlchemyRunStore, SqlAlchemyUnitOfWork, create_database
from echotrace.infrastructure.persistence.mysql.models import RunEvent


def test_run_summary_pages_read_only_summary_columns_and_order_by_update_time():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    store = SqlAlchemyRunStore(database.session_factory)
    now = beijing_now()
    runs = [
        InvestigationRun(
            objective=f"调查目标 {index}",
            event=Event(title=f"事件 {index}", objective=f"调查目标 {index}"),
            run_id=f"run-{index}",
            updated_at=now + timedelta(minutes=index),
            created_at=now,
        )
        for index in range(3)
    ]
    try:
        for run in runs:
            run.documents = []
            store.save(run)

        page = store.list_summaries(limit=2, offset=1)

        assert [item["run_id"] for item in page] == ["run-1", "run-0"]
        assert page[0]["event"]["title"] == "事件 1"
        assert "documents" not in page[0]
        assert "report_document" not in page[0]
        assert page[0]["updated_at"].utcoffset().total_seconds() == 8 * 60 * 60
    finally:
        database.close()


def test_event_sequence_page_is_limited_in_the_repository_query():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    store = SqlAlchemyRunStore(database.session_factory)
    run = InvestigationRun(objective="分页事件", event=Event(title="分页事件", objective="分页事件"))
    try:
        store.save(run)
        for index in range(1, 5):
            store.append_event(run.run_id, {"seq": index, "step": "progress", "status": "completed"})

        events = store.events(run.run_id, after_seq=1, limit=2)

        assert [item["seq"] for item in events] == [2, 3]
    finally:
        database.close()


def test_append_event_accounts_for_pending_trace_rows_in_same_transaction():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    store = SqlAlchemyRunStore(database.session_factory)
    run = InvestigationRun(objective="event sequencing", event=Event(title="event sequencing", objective="event sequencing"))
    try:
        store.save(run)

        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            assert uow.runs.get(run.run_id) is not None  # Exercise an already-loaded identity-map row.
            uow.session.add(RunEvent(
                run_event_id="re_pending",
                run_id=run.run_id,
                seq=1,
                event_type="role_task",
                actor_type="runtime",
                payload_json={"status": "paused"},
            ))
            seq = uow.runs.append_event(
                run_id=run.run_id,
                event_type="role_task.dead_lettered",
                actor_type="worker",
                payload={"task_id": "task-1"},
            )

            assert seq == 2

        with database.session_factory() as session:
            seqs = session.query(RunEvent.seq).filter_by(run_id=run.run_id).order_by(RunEvent.seq).all()
            assert [item[0] for item in seqs] == [1, 2]
    finally:
        database.close()
