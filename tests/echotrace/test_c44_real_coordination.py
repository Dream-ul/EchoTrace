"""Opt-in C4.4 acceptance against the development MySQL and Redis services.

This test deliberately exercises the coordination plane with small synthetic
tasks.  It is not the C5 user investigation and does not call the LLM or
Bocha.  Enable it explicitly with ``ECHOTRACE_RUN_C44_REAL=1``.
"""

from __future__ import annotations

from datetime import datetime
import os
from threading import Event as ThreadEvent, Thread
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from echotrace.application import InvestigationTaskService
from echotrace.domain.models import Event, InvestigationRun
from echotrace.infrastructure.coordination.redis import (
    OutboxDispatcher,
    RedisKeys,
    RedisPermitManager,
    RedisRateLimiter,
    RedisResponseCache,
    RedisStreamBus,
    RunEventNotifier,
    StreamTaskWorker,
)
from echotrace.infrastructure.coordination.redis.config import RedisConfig
from echotrace.infrastructure.persistence.mysql import SqlAlchemyUnitOfWork, create_database
from echotrace.infrastructure.persistence.mysql.models import RunEvent
from echotrace.runtime.environment import load_local_environment


pytestmark = pytest.mark.integration


def _enabled() -> bool:
    return os.getenv("ECHOTRACE_RUN_C44_REAL") == "1"


@pytest.mark.skipif(not _enabled(), reason="set ECHOTRACE_RUN_C44_REAL=1 for real C4.4 acceptance")
def test_real_mysql_redis_coordination_plane():
    load_local_environment()
    database_url = os.getenv("ECHOTRACE_DATABASE_URL", "").strip()
    if not database_url:
        pytest.fail("ECHOTRACE_DATABASE_URL is required")

    database = create_database(database_url)
    redis_config = RedisConfig.from_env()
    import redis

    client = redis.Redis.from_url(
        redis_config.url,
        decode_responses=True,
        socket_timeout=redis_config.socket_timeout_seconds,
        socket_connect_timeout=redis_config.socket_timeout_seconds,
    )
    namespace = f"echotrace:c44:{uuid4().hex}"
    keys = RedisKeys(namespace=namespace)
    bus = RedisStreamBus(client, keys=keys)
    task_service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    run = InvestigationRun(
        objective="C4.4 协调平面临时验收",
        event=Event(title="C4.4 临时事件", objective="C4.4 协调平面临时验收"),
        investigation_type="public_event",
        subject="C4.4 临时事件",
    )
    plan_id = f"c44p_{uuid4().hex[:20]}"
    task_ids: list[str] = []
    event_id = run.event.event_id

    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.runs.save(run)
            uow.tasks.add_plan(
                run_id=run.run_id,
                plan_id=plan_id,
                objective=run.objective,
                allowed_agents=["researcher"],
                allowed_capabilities=["search_web"],
                budget={"max_parallel_tasks": 2},
                stop_conditions=[],
                definition={"acceptance": "c4.4"},
            )
            for index in range(2):
                task_id = f"c44t_{uuid4().hex[:20]}"
                task_ids.append(task_id)
                uow.tasks.add_task(
                    run_id=run.run_id,
                    plan_id=plan_id,
                    task_id=task_id,
                    agent="researcher",
                    stage="node_research",
                    question=f"C4.4 临时任务 {index}",
                    capability="search_web",
                    idempotency_key=task_id,
                    node_id=None,
                    slot_id=f"c44s_{index}",
                    node_revision=0,
                    input_data={
                        "stage": "node_research", "revision": 0,
                        "node_id": f"c44n_{index}", "slot_id": f"c44s_{index}",
                    },
                )
            uow.outbox.enqueue(
                aggregate_type="node_plan",
                aggregate_id=plan_id,
                event_type="node_plan.created",
                payload={
                    "run_id": run.run_id,
                    "tasks": [
                        {
                            "task_id": task_id,
                            "run_id": run.run_id,
                            "node_id": f"c44n_{index}",
                            "slot_id": f"c44s_{index}",
                            "task_type": "node.research.initial",
                            "agent": "researcher",
                            "revision": 0,
                            "state_version": 0,
                            "idempotency_key": task_id,
                            "context_pack_hash": f"sha256:c44-{index}",
                            "enqueued_at": datetime.now().astimezone().isoformat(),
                        }
                        for index, task_id in enumerate(task_ids)
                    ],
                },
                dedup_key=f"{plan_id}:created",
            )

        bus.ensure_group("researcher")
        dispatcher = OutboxDispatcher(
            lambda: SqlAlchemyUnitOfWork(database.session_factory),
            bus,
            max_publish_attempts=3,
            lease_seconds=30,
        )
        first_dispatch = dispatcher.dispatch_once(owner="c44-dispatcher")
        assert first_dispatch.published == 1

        seen: list[str] = []

        def handler(message):
            seen.append(message.task_id)
            return {"output_refs": [f"artifact:{message.task_id}"]}

        workers = [
            StreamTaskWorker(
                bus,
                task_service,
                handler,
                role="researcher",
                consumer=f"c44-worker-{index}",
                lease_seconds=30,
            )
            for index in range(2)
        ]
        results = [worker.process_once(count=1, block_ms=5000) for worker in workers]
        assert sorted(item.succeeded for item in results) == [1, 1]
        assert sorted(seen) == sorted(task_ids)

        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            stored = [uow.tasks.get_task(task_id) for task_id in task_ids]
            assert all(item and item["status"] == "succeeded" for item in stored)
            assert all(len(uow.tasks.get_attempts(task_id)) == 1 for task_id in task_ids)

        # Duplicate delivery after a committed result is acknowledged without
        # invoking the handler again.
        assert workers[0].process_once(count=1, block_ms=1).received == 0

        permit_manager = RedisPermitManager(client, keys=keys)
        permit = permit_manager.acquire(scope="run", identifier=run.run_id, limit=1, owner="c44-owner", ttl_seconds=30)
        assert permit is not None
        assert permit_manager.renew(permit) is True
        assert permit_manager.release(permit) is True

        limiter = RedisRateLimiter(client, keys=keys)
        allowed = limiter.allow(provider="bocha", credential="c44", limit=2, window_seconds=60, token_limit=10, token_cost=2)
        blocked = limiter.allow(provider="bocha", credential="c44", limit=2, window_seconds=60, token_limit=10, token_cost=9)
        assert allowed.allowed is True
        assert blocked.allowed is False

        cache = RedisResponseCache(client, keys=keys, max_value_bytes=4096)
        cache_key = cache.set(
            capability="search", provider="bocha", provider_version="c44",
            schema_version="1", request={"query": "c44"}, response={"items": ["doc"]}, ttl_seconds=30,
        )
        assert cache.get(
            capability="search", provider="bocha", provider_version="c44",
            schema_version="1", request={"query": "c44"},
        ) == {"items": ["doc"]}

        notifier = RunEventNotifier(bus)
        channel = notifier.publish(run_id=run.run_id, seq=1, event_type="c44.started", committed=True)
        assert channel == keys.run_events(run.run_id)
        notifier.replay(run_id=run.run_id, after_seq=1, events=[{"seq": 1, "event_type": "c44.started"}, {"seq": 2, "event_type": "c44.finished"}])

        # Simulate Redis stream loss, then rebuild the pending task from the
        # MySQL task authority and dispatch it again.
        rebuild_task_id = f"c44t_{uuid4().hex[:20]}"
        task_ids.append(rebuild_task_id)
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.tasks.add_task(
                run_id=run.run_id,
                plan_id=plan_id,
                task_id=rebuild_task_id,
                agent="researcher",
                stage="node_research",
                question="C4.4 Redis 重建临时任务",
                capability="search_web",
                idempotency_key=rebuild_task_id,
                node_id=None,
                slot_id="c44s_rebuild",
                node_revision=0,
                input_data={
                    "stage": "node_research", "revision": 0,
                    "node_id": "c44n_rebuild", "slot_id": "c44s_rebuild",
                },
            )
            uow.outbox.enqueue(
                aggregate_type="investigation_task",
                aggregate_id=rebuild_task_id,
                    event_type="node.task.initial",
                payload={
                    "task_id": rebuild_task_id,
                    "run_id": run.run_id,
                    "node_id": "c44n_rebuild",
                    "slot_id": "c44s_rebuild",
                    "task_type": "node.research.initial",
                    "agent": "researcher",
                    "revision": 0,
                    "state_version": 0,
                    "idempotency_key": rebuild_task_id,
                    "context_pack_hash": "sha256:c44-rebuild",
                },
                dedup_key=f"{rebuild_task_id}:initial:0",
            )
        rebuild_dispatch = dispatcher.dispatch_once(owner="c44-dispatcher")
        assert rebuild_dispatch.published >= 1
        assert rebuild_dispatch.failed == 0
        client.delete(keys.task_stream("researcher"), keys.dead_letter_stream("researcher"))
        bus.ensure_group("researcher")
        assert rebuild_task_id in task_service.redrive_pending_tasks()
        rebuild_after_loss = dispatcher.dispatch_once(owner="c44-dispatcher")
        assert rebuild_after_loss.published >= 1
        assert rebuild_after_loss.failed == 0
        result = workers[0].process_once(count=1, block_ms=5000)
        assert result.succeeded == 1
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            assert uow.tasks.get_task(rebuild_task_id)["status"] == "succeeded"
            assert any(event["step"] == "node.task.redrive" for event in uow.runs.events(run.run_id))
    finally:
        redis_keys = list(client.scan_iter(match=f"{namespace}:*"))
        if redis_keys:
            client.delete(*redis_keys)
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE investigation_runs SET plan_id = NULL WHERE run_id = :run_id"), {"run_id": run.run_id})
            for task_id in task_ids:
                connection.execute(text("DELETE FROM task_attempts WHERE task_id = :task_id"), {"task_id": task_id})
                connection.execute(text("DELETE FROM task_dependencies WHERE task_id = :task_id OR depends_on_task_id = :task_id"), {"task_id": task_id})
                connection.execute(text("DELETE FROM outbox_events WHERE aggregate_id = :task_id"), {"task_id": task_id})
            connection.execute(text("DELETE FROM outbox_events WHERE aggregate_id = :plan_id"), {"plan_id": plan_id})
            connection.execute(text("DELETE FROM run_events WHERE run_id = :run_id"), {"run_id": run.run_id})
            connection.execute(text("DELETE FROM investigation_tasks WHERE run_id = :run_id"), {"run_id": run.run_id})
            connection.execute(text("DELETE FROM task_plans WHERE plan_id = :plan_id"), {"plan_id": plan_id})
            connection.execute(text("DELETE FROM investigation_runs WHERE run_id = :run_id"), {"run_id": run.run_id})
            connection.execute(text("DELETE FROM events WHERE event_id = :event_id"), {"event_id": event_id})
        database.close()


@pytest.mark.skipif(not _enabled(), reason="set ECHOTRACE_RUN_C44_REAL=1 for real MySQL concurrency acceptance")
def test_mysql_run_event_sequence_uses_current_read_after_stale_snapshot():
    """A waiter must allocate after the latest committed event under REPEATABLE READ."""
    load_local_environment()
    database_url = os.getenv("ECHOTRACE_DATABASE_URL", "").strip()
    if not database_url:
        pytest.fail("ECHOTRACE_DATABASE_URL is required")
    database = create_database(database_url)
    run = InvestigationRun(
        objective="C4.4 event sequence concurrency acceptance",
        event=Event(title="C4.4 event sequence concurrency", objective="event sequence concurrency"),
    )
    snapshot_ready = ThreadEvent()
    writer_committed = ThreadEvent()
    writer_result: dict[str, int] = {}
    writer_error: list[BaseException] = []

    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.runs.save(run)

        def stale_snapshot_writer() -> None:
            try:
                with SqlAlchemyUnitOfWork(database.session_factory) as uow:
                    writer_result["snapshot_seq"] = int(
                        uow.session.scalar(
                            select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.run_id)
                        ) or 0
                    )
                    snapshot_ready.set()
                    if not writer_committed.wait(timeout=15):
                        raise TimeoutError("concurrent event writer did not commit")
                    writer_result["seq"] = uow.runs.append_event(
                        run_id=run.run_id,
                        event_type="stale_snapshot_writer",
                        actor_type="test",
                    )
            except BaseException as exc:
                writer_error.append(exc)
                snapshot_ready.set()

        thread = Thread(target=stale_snapshot_writer, name="mysql-event-sequence-test")
        thread.start()
        assert snapshot_ready.wait(timeout=10), "stale snapshot transaction did not start"
        assert not writer_error

        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            committed_seq = uow.runs.append_event(
                run_id=run.run_id,
                event_type="concurrent_writer",
                actor_type="test",
            )
        writer_committed.set()
        thread.join(timeout=15)

        assert not thread.is_alive(), "stale snapshot writer did not finish"
        assert not writer_error
        assert writer_result["snapshot_seq"] < committed_seq
        assert writer_result["seq"] == committed_seq + 1
    finally:
        writer_committed.set()
        if "thread" in locals():
            thread.join(timeout=15)
        with database.engine.begin() as connection:
            connection.execute(text("DELETE FROM run_events WHERE run_id = :run_id"), {"run_id": run.run_id})
            connection.execute(text("DELETE FROM investigation_runs WHERE run_id = :run_id"), {"run_id": run.run_id})
            connection.execute(text("DELETE FROM events WHERE event_id = :event_id"), {"event_id": run.event.event_id})
        database.close()
