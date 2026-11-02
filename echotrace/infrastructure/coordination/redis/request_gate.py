"""Cross-process concurrency and provider-rate coordination for external calls."""

from __future__ import annotations

import math
import secrets
import time
from typing import Any, Callable

from .permit import RedisPermitManager
from .rate_limit import RedisRateLimiter


class RedisCallGateError(RuntimeError):
    retryable = False

    def __init__(self, message: str, *, error_code: str = "redis_coordination_timeout") -> None:
        super().__init__(message)
        self.error_code = error_code


class RedisCallCoordinator:
    """Hold Redis permits and consume request/token capacity around a call."""

    def __init__(self, permits: RedisPermitManager, limiter: RedisRateLimiter) -> None:
        self.permits = permits
        self.limiter = limiter

    def execute(
        self,
        operation: Callable[[], Any],
        *,
        provider: str,
        credential: str | None,
        timeout_seconds: float,
        request_cost: int = 0,
        request_limit: int = 60,
        token_limit: int = 200_000,
        window_seconds: int = 60,
        scopes: tuple[tuple[str, str, int], ...] = (),
    ) -> Any:
        if request_cost > token_limit and request_cost > 0:
            raise RedisCallGateError(
                f"external request costs {request_cost} tokens but the configured window holds {token_limit}",
                error_code="redis_request_cost_exceeds_limit",
            )
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        owner = secrets.token_urlsafe(18)
        lease_seconds = max(2, math.ceil(timeout_seconds) + 5)
        normalized_scopes = (("global", "external_calls", 16), *scopes)

        while True:
            acquired = []
            denied = False
            for scope, identifier, limit in normalized_scopes:
                permit = self.permits.acquire(
                    scope=scope,
                    identifier=identifier,
                    limit=limit,
                    ttl_seconds=lease_seconds,
                    owner=owner,
                )
                if permit is None:
                    denied = True
                    break
                acquired.append(permit)

            decision = None
            if not denied:
                decision = self.limiter.allow(
                    provider=provider,
                    credential=credential,
                    limit=request_limit,
                    window_seconds=window_seconds,
                    token_cost=max(0, int(request_cost)),
                    token_limit=token_limit,
                )
                denied = not decision.allowed
            if not denied:
                break

            for permit in reversed(acquired):
                self.permits.release(permit)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RedisCallGateError(
                    f"Redis coordination could not admit provider {provider} before the call deadline",
                )
            wait = decision.retry_after_seconds if decision is not None else 0.1
            time.sleep(min(max(0.05, wait), remaining, 1.0))

        try:
            return operation()
        finally:
            for permit in reversed(acquired):
                self.permits.release(permit)


__all__ = ["RedisCallCoordinator", "RedisCallGateError"]
