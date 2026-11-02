"""Redis Streams adapters and a deterministic in-memory implementation."""

from __future__ import annotations

import itertools
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable

from echotrace.infrastructure.time import beijing_iso, epoch_ms

from .contracts import RedisKeys


@dataclass(slots=True)
class _Pending:
    stream: str
    group: str
    consumer: str
    message_id: str
    fields: dict[str, str]
    delivered_at: float
    deliveries: int = 1


class InMemoryRedis:
    """Small Redis Streams-compatible fake for unit and fault-injection tests.

    It models at-least-once delivery, pending entries, acknowledgement and
    ``XAUTOCLAIM`` without requiring a Redis server.
    """

    def __init__(self) -> None:
        self._streams: dict[str, deque[tuple[str, dict[str, str]]]] = defaultdict(deque)
        self._groups: set[tuple[str, str]] = set()
        self._group_cursors: dict[tuple[str, str], int] = defaultdict(int)
        self._pending: dict[tuple[str, str, str], _Pending] = {}
        self._values: dict[str, tuple[Any, float | None]] = {}
        self._semaphores: dict[str, dict[str, float]] = defaultdict(dict)
        self._buckets: dict[str, tuple[float, float, float]] = {}
        self._pubsub: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._counter = itertools.count(1)
        self._lock = threading.RLock()
        self.fail_xadd = False
        self.fail_read = False
        self.fail_commands = False

    def xgroup_create(self, name: str, groupname: str, id: str = "0-0", mkstream: bool = True) -> bool:
        with self._lock:
            if (name, groupname) in self._groups:
                raise ValueError("BUSYGROUP Consumer Group name already exists")
            self._groups.add((name, groupname))
            if mkstream:
                self._streams[name]
            return True

    def xadd(self, name: str, fields: dict[str, str], id: str = "*") -> str:
        if self.fail_xadd:
            raise ConnectionError("in-memory Redis XADD fault")
        with self._lock:
            # Redis stream IDs are absolute epoch coordinates. They must not
            # be shifted to Beijing time; audit fields carry the display zone.
            message_id = id if id != "*" else f"{epoch_ms()}-{next(self._counter)}"
            self._streams[name].append((message_id, dict(fields)))
            return message_id

    def xreadgroup(self, groupname: str, consumername: str, streams: dict[str, str], count: int = 1, block: int = 0) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        if self.fail_read:
            raise ConnectionError("in-memory Redis XREADGROUP fault")
        result: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        with self._lock:
            for stream, marker in streams.items():
                if (stream, groupname) not in self._groups:
                    raise ValueError("NOGROUP No such consumer group")
                rows: list[tuple[str, dict[str, str]]] = []
                entries = list(self._streams[stream])
                cursor = self._group_cursors[(stream, groupname)]
                delivered_index = cursor
                for index, (message_id, fields) in enumerate(entries, start=1):
                    if index <= cursor:
                        continue
                    key = (stream, groupname, message_id)
                    if key in self._pending:
                        continue
                    pending = _Pending(stream, groupname, consumername, message_id, dict(fields), time.time())
                    self._pending[key] = pending
                    rows.append((message_id, dict(fields)))
                    delivered_index = index
                    if len(rows) >= count:
                        break
                if rows:
                    self._group_cursors[(stream, groupname)] = delivered_index
                if rows:
                    result.append((stream, rows))
        return result

    def xack(self, name: str, groupname: str, *ids: str) -> int:
        removed = 0
        with self._lock:
            for message_id in ids:
                key = (name, groupname, message_id)
                if key in self._pending:
                    self._pending.pop(key, None)
                    removed += 1
        return removed

    def xautoclaim(self, name: str, groupname: str, consumername: str, min_idle_time: int, start_id: str = "0-0", count: int = 100) -> tuple[str, list[tuple[str, dict[str, str]]], list[str]]:
        now = time.time()
        claimed: list[tuple[str, dict[str, str]]] = []
        with self._lock:
            for key, pending in list(self._pending.items()):
                if key[0] != name or key[1] != groupname or (now - pending.delivered_at) * 1000 < min_idle_time:
                    continue
                pending.consumer = consumername
                pending.delivered_at = now
                pending.deliveries += 1
                claimed.append((pending.message_id, dict(pending.fields)))
                if len(claimed) >= count:
                    break
        return "0-0", claimed, []

    def delivery_count(self, stream: str, group: str, message_id: str) -> int:
        pending = self._pending.get((stream, group, message_id))
        return pending.deliveries if pending else 0

    def pexpire(self, key: str, milliseconds: int) -> bool:
        with self._lock:
            current = self._values.get(key)
            if current is None:
                return False
            self._values[key] = (current[0], time.time() + milliseconds / 1000)
            return True

    def compare_and_delete(self, key: str, token: str) -> bool:
        with self._lock:
            current = self.get(key)
            if current != token:
                return False
            self._values.pop(key, None)
            return True

    def compare_and_expire(self, key: str, token: str, milliseconds: int) -> bool:
        with self._lock:
            current = self.get(key)
            if current != token:
                return False
            self._values[key] = (token, time.time() + milliseconds / 1000)
            return True

    def set(self, key: str, value: Any, ex: int | None = None, nx: bool = False) -> bool:
        with self._lock:
            now = time.time()
            current = self._values.get(key)
            if current and current[1] is not None and current[1] <= now:
                self._values.pop(key, None)
                current = None
            if nx and current is not None:
                return False
            self._values[key] = (value, now + ex if ex else None)
            return True

    def get(self, key: str) -> Any:
        with self._lock:
            current = self._values.get(key)
            if current is None:
                return None
            if current[1] is not None and current[1] <= time.time():
                self._values.pop(key, None)
                return None
            return current[0]

    def delete(self, key: str) -> int:
        with self._lock:
            return int(self._values.pop(key, None) is not None)

    def semaphore_acquire(self, key: str, owner: str, limit: int, ttl_seconds: int) -> bool:
        with self._lock:
            now = time.time()
            holders = self._semaphores[key]
            for token, expiry in list(holders.items()):
                if expiry <= now:
                    holders.pop(token, None)
            if owner in holders:
                holders[owner] = now + ttl_seconds
                return True
            if len(holders) >= limit:
                return False
            holders[owner] = now + ttl_seconds
            return True

    def semaphore_renew(self, key: str, owner: str, ttl_seconds: int) -> bool:
        with self._lock:
            holders = self._semaphores.get(key, {})
            if owner not in holders or holders[owner] <= time.time():
                holders.pop(owner, None)
                return False
            holders[owner] = time.time() + ttl_seconds
            return True

    def semaphore_release(self, key: str, owner: str) -> bool:
        with self._lock:
            return self._semaphores.get(key, {}).pop(owner, None) is not None

    def dual_bucket_allow(self, request_key: str, token_key: str, *, now_ms: int, window_ms: int, request_capacity: int, token_capacity: int, token_cost: int) -> tuple[int, int, int]:
        with self._lock:
            def balance(key: str, capacity: int) -> float:
                current = self._buckets.get(key)
                if current is None:
                    return float(capacity)
                available, updated, expires = current
                if expires <= now_ms / 1000:
                    return float(capacity)
                return min(float(capacity), available + max(0, now_ms - updated) * capacity / window_ms)

            request_balance = balance(request_key, request_capacity)
            token_balance = balance(token_key, token_capacity) if token_cost > 0 else float(token_capacity)
            request_retry = max(0, (1 - request_balance) * window_ms / request_capacity) if request_balance < 1 else 0
            token_retry = max(0, (token_cost - token_balance) * window_ms / token_capacity) if token_cost > 0 and token_balance < token_cost else 0
            if request_balance < 1 or (token_cost > 0 and token_balance < token_cost):
                return 0, int(max(0, min(request_balance, token_balance))), int(max(request_retry, token_retry) / 1000)
            expires = now_ms / 1000 + (window_ms * 2) / 1000
            self._buckets[request_key] = (request_balance - 1, now_ms, expires)
            if token_cost > 0:
                self._buckets[token_key] = (token_balance - token_cost, now_ms, expires)
            return 1, int(max(0, request_balance - 1)), 0

    def eval(self, script: str, numkeys: int, *args: Any) -> list[int]:
        """Implement the bounded counter script used by the test adapter."""

        if "INCR" not in script or numkeys != 1 or len(args) < 3:
            raise NotImplementedError("in-memory adapter only supports the rate script")
        key = str(args[0])
        ttl_ms = int(args[1])
        limit = int(args[2])
        with self._lock:
            current = self.get(key)
            count = int(current or 0) + 1
            if current is None:
                self._values[key] = (count, time.time() + ttl_ms / 1000)
            else:
                self._values[key] = (count, self._values[key][1])
            if count <= limit:
                return [1, limit - count, 0]
            return [0, 0, max(0, ttl_ms // 1000)]

    def publish(self, channel: str, message: str) -> int:
        self._pubsub[channel].append({"type": "message", "channel": channel, "data": message})
        return 1

    def pop_published(self, channel: str) -> dict[str, Any] | None:
        return self._pubsub[channel].popleft() if self._pubsub[channel] else None


class RedisStreamBus:
    """Thin facade shared by redis-py and :class:`InMemoryRedis`."""

    def __init__(self, client: Any, *, keys: RedisKeys | None = None) -> None:
        self.client = client
        self.keys = keys or RedisKeys()

    @classmethod
    def from_url(cls, url: str, *, keys: RedisKeys | None = None, decode_responses: bool = True) -> "RedisStreamBus":
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis package is required for Redis coordination") from exc
        return cls(redis.Redis.from_url(url, decode_responses=decode_responses), keys=keys)

    @classmethod
    def from_config(cls, config: Any, *, keys: RedisKeys | None = None, decode_responses: bool = True) -> "RedisStreamBus":
        """Build a real client from ``RedisConfig`` without connecting yet."""
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis package is required for Redis coordination") from exc
        client = redis.Redis.from_url(
            config.url,
            decode_responses=decode_responses,
            socket_timeout=float(getattr(config, "socket_timeout_seconds", 5.0)),
            socket_connect_timeout=float(getattr(config, "socket_timeout_seconds", 5.0)),
        )
        return cls(client, keys=keys)

    def ensure_group(self, role: str) -> None:
        try:
            self.client.xgroup_create(self.keys.task_stream(role), self.keys.task_group(role), id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def publish_task(self, role: str, fields: dict[str, str]) -> str:
        return str(self.client.xadd(self.keys.task_stream(role), fields))

    def publish_investigation_start(self, fields: dict[str, str]) -> str:
        return str(self.client.xadd(self.keys.task_stream("orchestrator"), fields))

    def read(self, *, role: str, consumer: str, count: int = 1, block_ms: int = 0) -> list[tuple[str, dict[str, str], int]]:
        stream = self.keys.task_stream(role)
        group = self.keys.task_group(role)
        rows = self.client.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms)
        result: list[tuple[str, dict[str, str], int]] = []
        for _stream, messages in rows or []:
            for message_id, fields in messages:
                deliveries = self._delivery_count(role, str(message_id))
                result.append((str(message_id), dict(fields), deliveries))
        return result

    def reclaim(self, *, role: str, consumer: str, min_idle_ms: int = 60_000, count: int = 100) -> list[tuple[str, dict[str, str], int]]:
        stream = self.keys.task_stream(role)
        group = self.keys.task_group(role)
        result = self.client.xautoclaim(stream, group, consumer, min_idle_ms, start_id="0-0", count=count)
        if isinstance(result, (tuple, list)):
            messages = result[1] if len(result) > 1 else []
        else:
            messages = result.get("messages", [])
        return [(str(message_id), dict(fields), self._delivery_count(role, str(message_id))) for message_id, fields in messages]

    def _delivery_count(self, role: str, message_id: str) -> int:
        """Return Redis' delivery counter, distinct from the domain attempt."""
        stream = self.keys.task_stream(role)
        group = self.keys.task_group(role)
        if hasattr(self.client, "delivery_count"):
            return int(self.client.delivery_count(stream, group, message_id))
        # redis-py exposes XPENDING RANGE rather than a direct per-message API.
        try:
            rows = self.client.xpending_range(
                stream, group,
                min=message_id, max=message_id, count=1,
            )
            if rows:
                row = rows[0]
                return int(row.get("times_delivered", row.get(b"times-delivered", 1)))
        except Exception:
            # Delivery count is observability/retry policy.  A conservative
            # default keeps processing available when an older client lacks
            # XPENDING RANGE support.
            pass
        return 1

    def ack(self, role: str, message_id: str) -> int:
        return int(self.client.xack(self.keys.task_stream(role), self.keys.task_group(role), message_id))

    def dead_letter(self, *, role: str, message_id: str, fields: dict[str, str], reason: str, deliveries: int) -> str:
        dlq_fields = dict(fields)
        dlq_fields.update({
            "source_message_id": message_id,
            "dead_letter_reason": reason,
            "delivery_count": str(deliveries),
            "failed_at": beijing_iso(),
        })
        return str(self.client.xadd(self.keys.dead_letter_stream(role), dlq_fields))

    def publish_run_event(self, run_id: str, payload: dict[str, Any]) -> str:
        import json

        channel = self.keys.run_events(run_id)
        payload = dict(payload)
        payload.setdefault("published_at", beijing_iso())
        try:
            self.client.publish(channel, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        except Exception:
            # MySQL run_events remain the durable event source; realtime fan-out is
            # deliberately best effort.
            pass
        return channel


__all__ = ["InMemoryRedis", "RedisStreamBus"]
