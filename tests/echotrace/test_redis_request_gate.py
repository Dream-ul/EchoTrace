from __future__ import annotations

from threading import Event, Thread

import pytest

from echotrace.infrastructure.coordination.redis import (
    InMemoryRedis,
    RedisCallCoordinator,
    RedisCallGateError,
    RedisPermitManager,
    RedisRateLimiter,
)


def _coordinator():
    client = InMemoryRedis()
    return RedisCallCoordinator(RedisPermitManager(client), RedisRateLimiter(client))


def test_call_gate_holds_and_releases_provider_permit_around_external_work():
    coordinator = _coordinator()
    entered = Event()
    release = Event()
    first_result = []

    def first_call():
        first_result.append(coordinator.execute(
            lambda: (entered.set(), release.wait(2), "first")[-1],
            provider="llm:test",
            credential="secret",
            timeout_seconds=3,
            request_limit=20,
            scopes=(("provider", "llm:test", 1),),
        ))

    thread = Thread(target=first_call)
    thread.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RedisCallGateError, match="could not admit"):
            coordinator.execute(
                lambda: "should-not-run",
                provider="llm:test",
                credential="secret",
                timeout_seconds=0.1,
                request_limit=20,
                scopes=(("provider", "llm:test", 1),),
            )
    finally:
        release.set()
        thread.join(timeout=2)

    assert first_result == ["first"]
    assert coordinator.execute(
        lambda: "after-release",
        provider="llm:test",
        credential="secret",
        timeout_seconds=1,
        request_limit=20,
        scopes=(("provider", "llm:test", 1),),
    ) == "after-release"


def test_call_gate_enforces_request_capacity_and_releases_permits_after_errors():
    coordinator = _coordinator()
    with pytest.raises(ValueError, match="provider failure"):
        coordinator.execute(
            lambda: (_ for _ in ()).throw(ValueError("provider failure")),
            provider="bocha",
            credential="key",
            timeout_seconds=1,
            request_limit=2,
            scopes=(("provider", "bocha", 1),),
        )
    assert coordinator.execute(
        lambda: "admitted",
        provider="bocha",
        credential="key",
        timeout_seconds=1,
        request_limit=2,
        scopes=(("provider", "bocha", 1),),
    ) == "admitted"

    limited = _coordinator()
    limited.execute(
        lambda: None,
        provider="bocha",
        credential="key",
        timeout_seconds=1,
        request_limit=1,
    )
    with pytest.raises(RedisCallGateError):
        limited.execute(
            lambda: None,
            provider="bocha",
            credential="key",
            timeout_seconds=0.05,
            request_limit=1,
        )
