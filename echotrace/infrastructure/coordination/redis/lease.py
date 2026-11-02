"""Short-lived Redis leases used for cross-process coordination.

Leases are advisory: MySQL state and task CAS remain the source of truth.  A
random owner token prevents one worker from releasing or renewing another
worker's lease after expiry.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from .contracts import RedisKeys


@dataclass(frozen=True, slots=True)
class Lease:
    key: str
    owner: str
    ttl_seconds: int


class RedisLeaseManager:
    def __init__(self, client: Any, *, keys: RedisKeys | None = None) -> None:
        self.client = client
        self.keys = keys or RedisKeys()

    def acquire(self, *, resource: str, identifier: str, ttl_seconds: int = 60, owner: str | None = None) -> Lease | None:
        ttl_seconds = max(1, int(ttl_seconds))
        key = self.keys.lease(resource, identifier)
        token = owner or secrets.token_urlsafe(18)
        if bool(self.client.set(key, token, ex=ttl_seconds, nx=True)):
            return Lease(key, token, ttl_seconds)
        return None

    def renew(self, lease: Lease) -> bool:
        milliseconds = max(1000, lease.ttl_seconds * 1000)
        if hasattr(self.client, "compare_and_expire"):
            return bool(self.client.compare_and_expire(lease.key, lease.owner, milliseconds))
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
          return redis.call('PEXPIRE', KEYS[1], ARGV[2])
        end
        return 0
        """
        try:
            return bool(self.client.eval(script, 1, lease.key, lease.owner, str(milliseconds)))
        except Exception:
            return False

    def release(self, lease: Lease) -> bool:
        if hasattr(self.client, "compare_and_delete"):
            return bool(self.client.compare_and_delete(lease.key, lease.owner))
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
          return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        try:
            return bool(self.client.eval(script, 1, lease.key, lease.owner))
        except Exception:
            return False


__all__ = ["Lease", "RedisLeaseManager"]
