"""Redis Lua semaphores for bounded cross-process concurrency."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from .contracts import RedisKeys


@dataclass(frozen=True, slots=True)
class Permit:
    key: str
    owner: str
    ttl_seconds: int


class RedisPermitManager:
    """Acquire, renew and release owner-bound concurrency permits.

    MySQL remains the task authority. A permit only controls whether an
    external/model call may start; losing Redis therefore fails closed.
    """

    _ACQUIRE_SCRIPT = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
local ttl = tonumber(ARGV[3]) * 1000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
local existing = redis.call('ZSCORE', KEYS[1], ARGV[1])
if existing then
  redis.call('ZADD', KEYS[1], now_ms + ttl, ARGV[1])
  redis.call('PEXPIRE', KEYS[1], ttl)
  return 1
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], now_ms + ttl, ARGV[1])
redis.call('PEXPIRE', KEYS[1], ttl)
return 1
"""
    _RENEW_SCRIPT = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
local ttl = tonumber(ARGV[2])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  redis.call('ZADD', KEYS[1], now_ms + ttl, ARGV[1])
  redis.call('PEXPIRE', KEYS[1], ttl)
  return 1
end
return 0
"""
    _RELEASE_SCRIPT = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""

    def __init__(self, client: Any, *, keys: RedisKeys | None = None) -> None:
        self.client = client
        self.keys = keys or RedisKeys()

    def acquire(self, *, scope: str, identifier: str, limit: int, ttl_seconds: int = 60, owner: str | None = None) -> Permit | None:
        if limit <= 0 or ttl_seconds <= 0:
            raise ValueError("permit limit and ttl_seconds must be positive")
        key = self.keys.permit(scope, identifier)
        token = owner or secrets.token_urlsafe(18)
        try:
            if hasattr(self.client, "semaphore_acquire"):
                allowed = self.client.semaphore_acquire(key, token, int(limit), int(ttl_seconds))
            elif hasattr(self.client, "eval"):
                allowed = self.client.eval(self._ACQUIRE_SCRIPT, 1, key, token, str(limit), str(ttl_seconds))
            else:
                return None
        except Exception:
            return None
        return Permit(key, token, int(ttl_seconds)) if bool(allowed) else None

    def renew(self, permit: Permit) -> bool:
        try:
            if hasattr(self.client, "semaphore_renew"):
                return bool(self.client.semaphore_renew(permit.key, permit.owner, permit.ttl_seconds))
            if hasattr(self.client, "eval"):
                return bool(self.client.eval(self._RENEW_SCRIPT, 1, permit.key, permit.owner, str(permit.ttl_seconds * 1000)))
        except Exception:
            return False
        return False

    def release(self, permit: Permit) -> bool:
        try:
            if hasattr(self.client, "semaphore_release"):
                return bool(self.client.semaphore_release(permit.key, permit.owner))
            if hasattr(self.client, "eval"):
                return bool(self.client.eval(self._RELEASE_SCRIPT, 1, permit.key, permit.owner))
        except Exception:
            return False
        return False


__all__ = ["Permit", "RedisPermitManager"]
