from datetime import datetime
from zoneinfo import ZoneInfo

from echotrace.domain.models import Event, InvestigationRun, beijing_now
from echotrace.infrastructure.persistence.mysql import SqlAlchemyUnitOfWork, create_database
from echotrace.infrastructure.persistence.mysql.models import InvestigationRun as InvestigationRunRow


def test_persistence_timestamps_use_beijing_wall_clock():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    try:
        run = InvestigationRun(objective="时间测试", event=Event(title="事件", objective="时间测试"))
        before = datetime.now(ZoneInfo("Asia/Shanghai"))
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.runs.save(run)
            stored = uow.session.get(InvestigationRunRow, run.run_id)
            assert stored is not None
            assert abs((stored.created_at - before.replace(tzinfo=None)).total_seconds()) < 5

        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            loaded = uow.runs.get(run.run_id)
            assert loaded is not None
            assert loaded.created_at.utcoffset().total_seconds() == 8 * 60 * 60
        assert beijing_now().utcoffset().total_seconds() == 8 * 60 * 60
    finally:
        database.close()
