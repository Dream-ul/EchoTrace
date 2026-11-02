from __future__ import annotations

from datetime import datetime

import pytest

from echotrace.infrastructure.coordination.redis import (
    InMemoryRedis,
    InvestigationStartMessage,
    NodeTaskMessage,
    OutboxDispatcher,
    RedisResponseCache,
    RedisKeys,
    RedisPermitManager,
    RedisRateLimiter,
    RedisStreamBus,
    ReportComposerWorker,
    RunEventNotifier,
    StreamTaskWorker,
    OrchestratorRunWorker,
)
from echotrace.application.investigation_service import TaskDeferredError


def _message(**overrides):
    values = {
        "outbox_id": "outbox_1",
        "task_id": "task_1",
        "run_id": "run_1",
        "node_id": "node_1",
        "slot_id": "origin_early_signals",
        "task_type": "node.research.initial",
        "agent": "researcher",
        "revision": 0,
        "state_version": 3,
        "idempotency_key": "task_1:initial",
        "context_pack_hash": "sha256:abc",
    }
    values.update(overrides)
    return NodeTaskMessage(**values)


def test_v2_keys_are_role_scoped_and_old_single_stream_contract_is_absent():
    keys = RedisKeys()
    assert keys.task_stream("researcher") == "echotrace:v2:tasks:researcher"
    assert keys.task_group("researcher") == "echotrace:v2:workers:researcher"
    assert keys.dead_letter_stream("verifier") == "echotrace:v2:tasks:verifier:dlq"
    assert keys.rate("bocha", "credential", "123", "tokens").startswith("echotrace:v2:rate:tokens:")
    assert not hasattr(keys, "tasks_stream")
    with pytest.raises(ValueError):
        keys.task_stream("Researcher")


def test_node_task_message_is_small_and_uses_beijing_audit_time():
    message = _message()
    fields = message.to_fields()
    assert set(fields) == {
        "outbox_id", "task_id", "run_id", "node_id", "slot_id", "task_type",
        "agent", "revision", "state_version", "idempotency_key", "context_pack_hash", "enqueued_at",
    }
    assert "payload" not in fields
    parsed = datetime.fromisoformat(fields["enqueued_at"])
    assert parsed.utcoffset().total_seconds() == 8 * 60 * 60
    assert NodeTaskMessage.from_fields({key.encode(): value for key, value in fields.items()}) == message
    with pytest.raises(ValueError):
        _message(task_type="task.created")


def test_stream_id_stays_epoch_while_audit_fields_are_beijing():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    bus.ensure_group("researcher")
    message_id = bus.publish_task("researcher", _message().to_fields())
    epoch_ms = int(message_id.split("-", 1)[0])
    assert abs(datetime.now().timestamp() * 1000 - epoch_ms) < 5_000
    rows = bus.read(role="researcher", consumer="worker-1")
    assert rows[0][1]["enqueued_at"].endswith("+08:00")
    bus.dead_letter(role="researcher", message_id=message_id, fields=rows[0][1], reason="test", deliveries=3)
    dlq_entry = client._streams[bus.keys.dead_letter_stream("researcher")][-1][1]
    assert datetime.fromisoformat(dlq_entry["failed_at"]).utcoffset().total_seconds() == 8 * 60 * 60


def test_rate_limiter_fails_closed_when_redis_is_unavailable():
    class BrokenRedis:
        def eval(self, *args, **kwargs):
            raise ConnectionError("redis down")

    decision = RedisRateLimiter(BrokenRedis()).allow(provider="bocha", credential="secret", limit=2, window_seconds=60)
    assert decision.allowed is False
    assert decision.remaining == 0

    memory_decision = RedisRateLimiter(InMemoryRedis()).allow(provider="bocha", credential="secret", limit=1, window_seconds=60)
    assert memory_decision.allowed is True


class _Outbox:
    def __init__(self, rows):
        self.rows = rows
        self.published = []
        self.failed = []

    def claim(self, **kwargs):
        return self.rows

    def mark_published(self, outbox_id, **kwargs):
        self.published.append((outbox_id, kwargs["message_id"]))
        return True

    def mark_failed(self, outbox_id, **kwargs):
        self.failed.append((outbox_id, kwargs["error"], kwargs["dead"]))
        return True


class _Uow:
    def __init__(self, outbox):
        self.outbox = outbox

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_dispatcher_routes_node_tasks_to_the_agent_role_stream():
    row = {
        "outbox_id": "outbox_1",
        "aggregate_id": "task_1",
        "event_type": "node.research.initial",
        "payload": _message().to_fields(),
        "dedup_key": "task_1:initial",
        "publish_attempts": 0,
    }
    outbox = _Outbox([row])
    bus = RedisStreamBus(InMemoryRedis())
    dispatcher = OutboxDispatcher(lambda: _Uow(outbox), bus)
    result = dispatcher.dispatch_once(owner="dispatcher-1")
    assert result.published == 1
    assert bus.client._streams[bus.keys.task_stream("researcher")]
    assert not bus.client._streams[bus.keys.task_stream("verifier")]
    assert outbox.failed == []


def test_dispatcher_expands_atomic_node_plan_and_orchestrator_start_command():
    node = _message().to_fields()
    row = {
        "outbox_id": "plan_1",
        "aggregate_id": "plan_1",
        "event_type": "node_plan.created",
        "payload": {"run_id": "run_1", "tasks": [node]},
        "dedup_key": "plan_1:created",
        "publish_attempts": 0,
    }
    start = {
        "outbox_id": "start_1",
        "aggregate_id": "run_1",
        "event_type": "investigation.start",
        "payload": {"run_id": "run_1", "idempotency_key": "run_1:start"},
        "dedup_key": "run_1:start",
        "publish_attempts": 0,
    }
    outbox = _Outbox([row, start])
    bus = RedisStreamBus(InMemoryRedis())
    result = OutboxDispatcher(lambda: _Uow(outbox), bus).dispatch_once(owner="dispatcher-1")
    assert result.published == 2
    bus.ensure_group("researcher")
    node_rows = bus.read(role="researcher", consumer="researcher-1")
    assert node_rows and node_rows[0][1]["context_pack_hash"] == "sha256:abc"
    seen = []
    worker = OrchestratorRunWorker(
        bus, lambda run_id, execution_task_id: seen.append((run_id, execution_task_id)),
        consumer="orchestrator-1",
    )
    assert worker.process_once().succeeded == 1
    assert seen == [("run_1", "start_1")]
    assert InvestigationStartMessage.from_fields(bus.client._streams[bus.keys.task_stream("orchestrator")][0][1]).run_id == "run_1"


def test_worker_ack_happens_only_after_successful_mysql_commit():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    message = _message()
    bus.ensure_group("researcher")
    message_id = bus.publish_task("researcher", message.to_fields())
    order = []

    class Service:
        def get_task(self, task_id):
            return {"status": "pending"}

        def start_task(self, **kwargs):
            order.append("start")
            return {"attempt_id": "attempt_1"}

        def finish_task(self, **kwargs):
            order.append("finish")
            return True

    original_ack = bus.ack

    def ack(role, item):
        order.append("ack")
        return original_ack(role, item)

    bus.ack = ack
    worker = StreamTaskWorker(bus, Service(), lambda item: order.append("handler"), role="researcher", consumer="worker-1")
    result = worker.process_once()
    assert result.succeeded == 1
    assert order == ["start", "handler", "finish", "ack"]
    assert client.delivery_count(bus.keys.task_stream("researcher"), bus.keys.task_group("researcher"), message_id) == 0


def test_orchestrator_reclaims_a_pending_start_message():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    message = InvestigationStartMessage(
        outbox_id="start_outbox_1",
        run_id="run_1",
        idempotency_key="run_1:start",
    )
    message_id = bus.publish_investigation_start(message.to_fields())
    calls = []

    def handler(run_id, execution_task_id):
        calls.append((run_id, execution_task_id))
        if len(calls) == 1:
            raise RuntimeError("temporary provider failure")

    worker = OrchestratorRunWorker(bus, handler, consumer="orchestrator-1")
    assert worker.process_once().retried == 1
    pending_key = (bus.keys.task_stream("orchestrator"), bus.keys.task_group("orchestrator"), message_id)
    client._pending[pending_key].delivered_at -= 120
    assert worker.reclaim_once(min_idle_ms=1).succeeded == 1
    assert calls == [("run_1", "start_outbox_1"), ("run_1", "start_outbox_1")]


def test_orchestrator_records_terminal_failure_only_after_retries_are_exhausted():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    message = InvestigationStartMessage(
        outbox_id="start_outbox_2", run_id="run_2", idempotency_key="run_2:start",
    )
    message_id = bus.publish_investigation_start(message.to_fields())
    failures = []

    def on_terminal_failure(run_id, exc):
        failures.append((run_id, str(exc)))

    worker = OrchestratorRunWorker(
        bus,
        lambda *_args: (_ for _ in ()).throw(RuntimeError("classification unavailable")),
        consumer="orchestrator-1",
        max_deliveries=2,
        on_terminal_failure=on_terminal_failure,
    )

    assert worker.process_once().retried == 1
    assert failures == []
    pending_key = (bus.keys.task_stream("orchestrator"), bus.keys.task_group("orchestrator"), message_id)
    client._pending[pending_key].delivered_at -= 120

    result = worker.reclaim_once(min_idle_ms=1)

    assert result.dead_lettered == result.failed == 1
    assert failures == [("run_2", "classification unavailable")]


def test_report_worker_routes_plan_and_section_to_separate_handlers():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    plan = _message(task_type="report.plan", agent="report_composer", node_id="report-1", slot_id="report_plan")
    section = _message(task_type="report.section", agent="report_composer", node_id="report-1", slot_id="summary")
    bus.ensure_group("report_composer")
    bus.publish_task("report_composer", plan.to_fields())
    bus.publish_task("report_composer", section.to_fields())
    calls = []

    class Service:
        def get_task(self, task_id):
            return {"status": "pending"}

        def start_task(self, **kwargs):
            return {"attempt_id": kwargs["task_id"] + ":attempt"}

        def finish_task(self, **kwargs):
            return True

    worker = ReportComposerWorker(
        bus, Service(), plan_handler=lambda item: calls.append(("plan", item.slot_id)),
        section_handler=lambda item: calls.append(("section", item.slot_id)), consumer="report-1",
    )
    assert worker.process_once(count=2).succeeded == 2
    assert calls == [("plan", "report_plan"), ("section", "summary")]


def test_worker_keeps_message_pending_when_mysql_result_is_not_committed():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    bus.ensure_group("researcher")
    message_id = bus.publish_task("researcher", _message().to_fields())

    class Service:
        def get_task(self, task_id):
            return {"status": "pending"}

        def start_task(self, **kwargs):
            return {"attempt_id": "attempt_1"}

        def finish_task(self, **kwargs):
            return False

    worker = StreamTaskWorker(bus, Service(), lambda item: {"output_refs": ["ref_1"]}, role="researcher", consumer="worker-1")
    result = worker.process_once()
    assert result.retried == 1
    assert client.delivery_count(bus.keys.task_stream("researcher"), bus.keys.task_group("researcher"), message_id) == 1


def test_worker_reclaims_failed_pending_and_dead_letters_after_limit():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    bus.ensure_group("researcher")
    message_id = bus.publish_task("researcher", _message().to_fields())
    calls = {"handler": 0}

    class Service:
        def get_task(self, task_id):
            return {"status": "pending"}

        def start_task(self, **kwargs):
            return {"attempt_id": f"attempt_{calls['handler']}"}

        def finish_task(self, **kwargs):
            return True

    def handler(item):
        calls["handler"] += 1
        raise RuntimeError("provider failed")

    worker = StreamTaskWorker(bus, Service(), handler, role="researcher", consumer="worker-1", max_deliveries=2)
    assert worker.process_once().retried == 1
    pending_key = (bus.keys.task_stream("researcher"), bus.keys.task_group("researcher"), message_id)
    client._pending[pending_key].delivered_at -= 120
    assert worker.reclaim_once(min_idle_ms=1).dead_lettered == 1
    assert calls["handler"] == 2
    assert client._streams[bus.keys.dead_letter_stream("researcher")][-1][1]["delivery_count"] == "2"


def test_worker_keeps_running_claim_pending_and_retries_after_owner_failure():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    bus.ensure_group("researcher")
    message_id = bus.publish_task("researcher", _message().to_fields())
    state = {"status": "pending", "attempt_no": 0}

    class Service:
        def get_task(self, task_id):
            return dict(state)

        def start_task(self, **kwargs):
            state["attempt_no"] += 1
            state["status"] = "running"
            return {"attempt_id": f"attempt_{state['attempt_no']}", "attempt_no": state["attempt_no"]}

        def finish_task(self, **kwargs):
            state["status"] = kwargs["status"]
            return True

    service = Service()
    worker = StreamTaskWorker(
        bus,
        service,
        lambda _item: (_ for _ in ()).throw(RuntimeError("step budget exhausted")),
        role="researcher",
        consumer="researcher-1",
        max_deliveries=2,
    )
    assert worker.process_once().retried == 1
    assert state["status"] == "failed"

    pending_key = (bus.keys.task_stream("researcher"), bus.keys.task_group("researcher"), message_id)
    state["status"] = "running"
    client._pending[pending_key].delivered_at -= 120
    assert worker.reclaim_once(min_idle_ms=1).received == 1
    assert client.delivery_count(*pending_key) == 2

    # Reclaims while the original worker owns a live MySQL attempt must not
    # spend retry attempts or ACK away the only durable redelivery trigger.
    for _ in range(3):
        client._pending[pending_key].delivered_at -= 120
        assert worker.reclaim_once(min_idle_ms=1).received == 1
    assert state["attempt_no"] == 1
    assert client.delivery_count(*pending_key) == 5

    state["status"] = "failed"
    client._pending[pending_key].delivered_at -= 120
    retry_worker = StreamTaskWorker(
        bus,
        service,
        lambda _item: {"output_refs": ["recovered"]},
        role="researcher",
        consumer="researcher-2",
        max_deliveries=2,
    )
    assert retry_worker.reclaim_once(min_idle_ms=1).succeeded == 1
    assert state["attempt_no"] == 2
    assert state["status"] == "succeeded"
    assert client.delivery_count(*pending_key) == 0
    assert not client._streams[bus.keys.dead_letter_stream("researcher")]


def test_worker_keeps_duplicate_pending_when_task_becomes_running_during_start():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    bus.ensure_group("researcher")
    message_id = bus.publish_task("researcher", _message().to_fields())
    state = {"status": "failed"}
    calls = {"handler": 0, "paused": 0}

    class Service:
        def get_task(self, task_id):
            return dict(state)

        def start_task(self, **kwargs):
            # A different consumer claimed the task after the worker's first
            # read, so failed-task recovery now observes a running task.
            state["status"] = "running"
            raise ValueError("cannot recover task that is not failed: running")

        def pause_run_for_task_failure(self, **kwargs):
            calls["paused"] += 1

    def handler(_item):
        calls["handler"] += 1

    worker = StreamTaskWorker(
        bus, Service(), handler, role="researcher", consumer="worker-2", max_deliveries=1,
    )
    result = worker.process_once()

    pending_key = (bus.keys.task_stream("researcher"), bus.keys.task_group("researcher"), message_id)
    assert result.received == 1
    assert result.dead_lettered == result.failed == result.retried == 0
    assert calls == {"handler": 0, "paused": 0}
    assert client.delivery_count(*pending_key) == 1
    assert not client._streams[bus.keys.dead_letter_stream("researcher")]


def test_worker_does_not_retry_non_retryable_provider_rejection():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    bus.ensure_group("researcher")
    message_id = bus.publish_task("researcher", _message().to_fields())
    calls = {"handler": 0, "paused": 0}

    class ProviderRejected(RuntimeError):
        retryable = False
        error_code = "provider_policy_rejected"

    class Service:
        def get_task(self, task_id):
            return {"status": "pending"}

        def start_task(self, **kwargs):
            return {"attempt_id": "attempt_rejected"}

        def finish_task(self, **kwargs):
            return True

        def pause_run_for_task_failure(self, **kwargs):
            calls["paused"] += 1

    def handler(item):
        calls["handler"] += 1
        raise ProviderRejected("provider rejected request content")

    worker = StreamTaskWorker(
        bus, Service(), handler, role="researcher", consumer="worker-1", max_deliveries=3,
    )
    result = worker.process_once()

    assert result.dead_lettered == 1
    assert result.retried == 0
    assert calls == {"handler": 1, "paused": 1}
    assert client.delivery_count(bus.keys.task_stream("researcher"), bus.keys.task_group("researcher"), message_id) == 0
    dlq = client._streams[bus.keys.dead_letter_stream("researcher")][-1][1]
    assert dlq["dead_letter_reason"] == "provider rejected request content"


def test_worker_keeps_paused_task_pending_without_retry_or_dead_letter():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    bus.ensure_group("researcher")
    message_id = bus.publish_task("researcher", _message().to_fields())
    calls = {"handler": 0, "deferred": 0}

    class Service:
        def get_task(self, task_id):
            return {"status": "pending"}

        def start_task(self, **kwargs):
            raise TaskDeferredError("investigation is paused")

    worker = StreamTaskWorker(
        bus, Service(), lambda _message: calls.__setitem__("handler", calls["handler"] + 1),
        role="researcher", consumer="worker-paused", max_deliveries=1,
    )
    result = worker.process_once()

    pending_key = (bus.keys.task_stream("researcher"), bus.keys.task_group("researcher"), message_id)
    assert result.received == 1
    assert result.dead_lettered == result.failed == result.retried == 0
    assert calls == {"handler": 0, "deferred": 0}
    assert client.delivery_count(*pending_key) == 1
    assert not client._streams[bus.keys.dead_letter_stream("researcher")]


def test_worker_defers_active_attempt_when_handler_observes_pause():
    client = InMemoryRedis()
    bus = RedisStreamBus(client)
    bus.ensure_group("researcher")
    message_id = bus.publish_task("researcher", _message().to_fields())
    calls = {"deferred": 0, "finished": 0}

    class Service:
        def get_task(self, task_id):
            return {"status": "pending"}

        def start_task(self, **kwargs):
            return {"attempt_id": "attempt-paused"}

        def defer_task(self, **kwargs):
            calls["deferred"] += 1
            return True

        def finish_task(self, **kwargs):
            calls["finished"] += 1
            return True

    def handler(_message):
        raise TaskDeferredError("investigation was paused while running")

    worker = StreamTaskWorker(
        bus, Service(), handler, role="researcher", consumer="worker-paused", max_deliveries=1,
    )
    result = worker.process_once()

    pending_key = (bus.keys.task_stream("researcher"), bus.keys.task_group("researcher"), message_id)
    assert result.received == 1
    assert result.dead_lettered == result.failed == result.retried == 0
    assert calls == {"deferred": 1, "finished": 0}
    assert client.delivery_count(*pending_key) == 1
    assert not client._streams[bus.keys.dead_letter_stream("researcher")]


def test_permit_is_bounded_owner_bound_and_renewable():
    client = InMemoryRedis()
    manager = RedisPermitManager(client)
    first = manager.acquire(scope="provider", identifier="bocha", limit=1, owner="worker-a", ttl_seconds=30)
    assert first is not None
    assert manager.acquire(scope="provider", identifier="bocha", limit=1, owner="worker-b", ttl_seconds=30) is None
    assert manager.renew(first) is True
    assert manager.release(PermitLike(first.key, "wrong-owner", first.ttl_seconds)) is False
    assert manager.release(first) is True
    assert manager.acquire(scope="provider", identifier="bocha", limit=1, owner="worker-b", ttl_seconds=30) is not None


class PermitLike:
    def __init__(self, key, owner, ttl_seconds):
        self.key, self.owner, self.ttl_seconds = key, owner, ttl_seconds


def test_dual_bucket_consumes_request_and_tokens_atomically():
    client = InMemoryRedis()
    limiter = RedisRateLimiter(client)
    first = limiter.allow(provider="llm", credential="secret", limit=2, window_seconds=60, token_limit=5, token_cost=4, now=1_700_000_000)
    assert first.allowed is True
    second = limiter.allow(provider="llm", credential="secret", limit=2, window_seconds=60, token_limit=5, token_cost=2, now=1_700_000_001)
    assert second.allowed is False
    assert second.token_key != second.request_key
    third = limiter.allow(provider="llm", credential="secret", limit=2, window_seconds=60, token_limit=5, token_cost=1, now=1_700_000_061)
    assert third.allowed is True


def test_cache_is_bounded_and_notifier_replays_only_new_sequences():
    client = InMemoryRedis()
    cache = RedisResponseCache(client, max_value_bytes=20)
    with pytest.raises(ValueError, match="max_value_bytes"):
        cache.set(capability="search", provider="bocha", provider_version="1", schema_version="1", request={"q": "x"}, response={"value": "012345678901234567890"})
    cache = RedisResponseCache(client, max_value_bytes=1_000)
    key = cache.set(capability="search", provider="bocha", provider_version="1", schema_version="1", request={"q": "x"}, response={"items": ["doc-1"]})
    assert cache.get(capability="search", provider="bocha", provider_version="1", schema_version="1", request={"q": "x"}) == {"items": ["doc-1"]}
    assert key.startswith("echotrace:v2:cache:")
    notifier = RunEventNotifier(RedisStreamBus(client))
    with pytest.raises(ValueError, match="committed"):
        notifier.publish(run_id="run-1", seq=1, event_type="started")
    notifier.publish(run_id="run-1", seq=1, event_type="started", committed=True)
    notifier.replay(run_id="run-1", after_seq=1, events=[{"seq": 1, "event_type": "started"}, {"seq": 2, "event_type": "finished", "payload": {"ok": True}}])
    published = []
    while True:
        item = client.pop_published("echotrace:v2:run-events:run-1")
        if item is None:
            break
        published.append(item["data"])
    assert len(published) == 2
    assert '"seq": 2' in published[-1]
