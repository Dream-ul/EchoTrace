"""Application time boundaries.

Business and audit timestamps are rendered in Beijing time. Redis internals
still use Unix epoch values for stream ids, TTLs and lease idle calculations.
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_iso(value: datetime | None = None, *, timespec: str = "milliseconds") -> str:
    current = value or now_beijing()
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    return current.astimezone(BEIJING_TZ).isoformat(timespec=timespec)


def epoch_seconds() -> float:
    """Return an absolute epoch coordinate for Redis TTL/lease arithmetic."""

    return time.time()


def epoch_ms() -> int:
    """Return an absolute epoch millisecond coordinate for Redis Stream IDs."""

    return int(epoch_seconds() * 1000)


__all__ = ["BEIJING_TZ", "beijing_iso", "epoch_ms", "epoch_seconds", "now_beijing"]
