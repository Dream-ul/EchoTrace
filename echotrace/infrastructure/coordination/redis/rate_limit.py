"""Shared provider request/token rate limiting."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from .contracts import RedisKeys


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float = 0.0
    key: str = ""
    request_key: str = ""
    token_key: str = ""


class RedisRateLimiter:
    """Atomic request and token buckets; Redis failure always fails closed."""

    _SCRIPT = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
local window = tonumber(ARGV[1])
local request_capacity = tonumber(ARGV[2])
local token_capacity = tonumber(ARGV[3])
local token_cost = tonumber(ARGV[4])
local function read_bucket(key, capacity)
  local value = redis.call('HMGET', key, 'available', 'updated_ms')
  local available = tonumber(value[1])
  local updated = tonumber(value[2])
  if not available or not updated then return capacity, now_ms end
  available = math.min(capacity, available + math.max(0, now_ms - updated) * capacity / window)
  return available, now_ms
end
local requests, updated = read_bucket(KEYS[1], request_capacity)
local tokens = token_capacity
if token_cost > 0 then tokens, updated = read_bucket(KEYS[2], token_capacity) end
if requests < 1 or (token_cost > 0 and tokens < token_cost) then
  local retry = 0
  if requests < 1 then retry = math.max(retry, (1 - requests) * window / request_capacity) end
  if token_cost > 0 and tokens < token_cost then retry = math.max(retry, (token_cost - tokens) * window / token_capacity) end
  return {0, math.floor(math.max(0, math.min(requests, tokens))), math.ceil(retry / 1000)}
end
redis.call('HSET', KEYS[1], 'available', requests - 1, 'updated_ms', updated)
redis.call('PEXPIRE', KEYS[1], window * 2)
if token_cost > 0 then
  redis.call('HSET', KEYS[2], 'available', tokens - token_cost, 'updated_ms', updated)
  redis.call('PEXPIRE', KEYS[2], window * 2)
end
return {1, math.floor(requests - 1), 0}
"""

    def __init__(self, client: Any, *, keys: RedisKeys | None = None) -> None:
        self.client = client
        self.keys = keys or RedisKeys()

    def allow(self, *, provider: str, credential: str | None, limit: int, window_seconds: int, token_cost: int = 0, token_limit: int | None = None, now: float | None = None) -> RateLimitDecision:
        if limit <= 0 or window_seconds <= 0 or token_cost < 0:
            raise ValueError("rate limits must be positive and token_cost non-negative")
        token_capacity = max(1, int(token_limit if token_limit is not None else limit))
        credential_hash = hashlib.sha256((credential or "anonymous").encode("utf-8")).hexdigest()[:16]
        current = now if now is not None else time.time()
        bucket = int(current // window_seconds)
        request_key = self.keys.rate(provider, credential_hash, str(bucket), "requests")
        token_key = self.keys.rate(provider, credential_hash, str(bucket), "tokens")
        try:
            if hasattr(self.client, "dual_bucket_allow"):
                result = self.client.dual_bucket_allow(request_key, token_key, now_ms=int(current * 1000), window_ms=window_seconds * 1000, request_capacity=limit, token_capacity=token_capacity, token_cost=token_cost)
            elif hasattr(self.client, "eval"):
                result = self.client.eval(self._SCRIPT, 2, request_key, token_key, str(window_seconds * 1000), str(limit), str(token_capacity), str(token_cost))
            else:
                return RateLimitDecision(False, 0, float(window_seconds), request_key, request_key, token_key)
            allowed, remaining, retry = [int(item) for item in result]
            return RateLimitDecision(bool(allowed), remaining, float(retry), request_key, request_key, token_key)
        except Exception:
            return RateLimitDecision(False, 0, float(window_seconds), request_key, request_key, token_key)


__all__ = ["RateLimitDecision", "RedisRateLimiter"]
